"""Google Drive, via a service account the operator shares folders with.

Auth is deliberately the simple kind: no OAuth browser dance on a headless
server. The operator creates a service account, pastes its JSON key into
Security (sealed like every other key), and shares Drive folders with the
account's email address exactly as they would with a person. Revocation is
un-sharing.

The JWT is hand-rolled with `cryptography` (already a dependency for the
Fernet seals) rather than pulling in google-auth: the grant is one RS256
signature and one token POST, and a dependency that size for two calls is
how images grow legs.

Everything network goes through one httpx client with an injectable
transport, which is what keeps the tests honest without talking to Google.
"""

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

log = structlog.get_logger()

TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — an endpoint, not a secret
API = "https://www.googleapis.com/drive/v3"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
# Full Drive scope: the account only sees what is explicitly shared with it,
# and `drive.file` (the narrow scope) cannot see shared folders at all.
SCOPE = "https://www.googleapis.com/auth/drive"
TIMEOUT_S = 60.0

MANIFEST_NAME = "FrameFound organize manifest.json"


class GdriveError(RuntimeError):
    """A Drive call failed in a way the operator needs to hear about."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def parse_folder_id(url_or_id: str) -> str:
    """Accept a full Drive folder URL or a bare folder id."""
    text = url_or_id.strip()
    for marker in ("/folders/", "id="):
        if marker in text:
            text = text.split(marker, 1)[1]
    for stop in ("?", "&", "/", "#"):
        text = text.split(stop, 1)[0]
    if not text or any(c in text for c in " '\""):
        raise GdriveError("That does not look like a Drive folder link")
    return text


def sign_jwt(sa_info: dict[str, Any], now: float | None = None) -> str:
    """The service-account grant assertion, RS256-signed."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT", "kid": sa_info.get("private_key_id", "")}
    claims = {
        "iss": sa_info["client_email"],
        "scope": SCOPE,
        "aud": TOKEN_URL,
        "iat": issued,
        "exp": issued + 3600,
    }
    signing_input = (
        _b64url(json.dumps(header, separators=(",", ":")).encode())
        + "."
        + _b64url(json.dumps(claims, separators=(",", ":")).encode())
    )
    key = serialization.load_pem_private_key(sa_info["private_key"].encode(), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise GdriveError("Google service-account keys are RSA; this one is not")
    signature = key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


class GdriveClient:
    """A thin, test-injectable Drive v3 client."""

    def __init__(self, sa_info: dict[str, Any], transport: httpx.BaseTransport | None = None):
        self._sa = sa_info
        self._client = httpx.Client(transport=transport, timeout=TIMEOUT_S)
        self._token: str | None = None
        self._token_expiry = 0.0

    def close(self) -> None:
        self._client.close()

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        response = self._client.post(
            TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": sign_jwt(self._sa),
            },
        )
        if response.status_code != 200:
            raise GdriveError(f"Google token exchange failed ({response.status_code})")
        payload = response.json()
        self._token = str(payload["access_token"])
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._access_token()}"
        response = self._client.request(method, url, headers=headers, **kwargs)
        if response.status_code == 404:
            raise GdriveError(
                "Drive says that does not exist — is the folder shared with the service account?"
            )
        if response.status_code >= 400:
            raise GdriveError(f"Drive returned {response.status_code}")
        return response

    def folder_name(self, folder_id: str) -> str:
        data = self._request(
            "GET", f"{API}/files/{folder_id}", params={"fields": "id,name,mimeType"}
        ).json()
        if data.get("mimeType") != "application/vnd.google-apps.folder":
            raise GdriveError("That link is a file, not a folder")
        return str(data["name"])

    def _list(self, query: str, fields: str) -> list[dict[str, Any]]:
        """Every file matching `query`, all pages, shared drives included —
        the operator's shoots live in a shared drive ("Intel Auctions"),
        which Drive hides from a query unless asked twice."""
        files: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "q": query,
                "fields": f"nextPageToken,files({fields})",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/files", params=params).json()
            files.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return files

    def list_folders(self, folder_id: str) -> list[dict[str, Any]]:
        """The folders directly inside a folder: id, name."""
        return self._list(
            f"'{folder_id}' in parents and trashed=false "
            "and mimeType = 'application/vnd.google-apps.folder'",
            "id,name",
        )

    def list_image_files(self, folder_id: str) -> list[dict[str, Any]]:
        """Every image directly in a folder, with size and checksum so a
        download can be skipped when the file is already here."""
        return self._list(
            f"'{folder_id}' in parents and trashed=false and mimeType contains 'image/'",
            "id,name,size,md5Checksum,modifiedTime",
        )

    def download(self, file_id: str, destination: Path) -> None:
        """The file's bytes, streamed to `destination` (written beside it
        first and moved in, so a half-finished download never looks done)."""
        partial = destination.with_suffix(destination.suffix + ".part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        with self._client.stream(
            "GET",
            f"{API}/files/{file_id}",
            params={"alt": "media", "supportsAllDrives": "true"},
            headers=headers,
        ) as response:
            if response.status_code >= 400:
                raise GdriveError(f"Drive returned {response.status_code} for a download")
            with partial.open("wb") as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        partial.replace(destination)

    def list_images(self, folder_id: str) -> list[dict[str, Any]]:
        """Every image directly in the folder: id, name, thumbnailLink."""
        files: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false and mimeType contains 'image/'",
                "fields": "nextPageToken,files(id,name,thumbnailLink)",
                "pageSize": 1000,
            }
            if token:
                params["pageToken"] = token
            data = self._request("GET", f"{API}/files", params=params).json()
            files.extend(data.get("files", []))
            token = data.get("nextPageToken")
            if not token:
                return files

    def find_file(self, folder_id: str, name: str) -> dict[str, Any] | None:
        """The newest file of that name in the folder: Drive allows duplicate
        names, and the manifest briefly has one while it is being replaced."""
        safe = name.replace("\\", "\\\\").replace("'", "\\'")
        data = self._request(
            "GET",
            f"{API}/files",
            params={
                "q": f"'{folder_id}' in parents and trashed=false and name = '{safe}'",
                "fields": "files(id,name)",
                "orderBy": "createdTime desc",
                "pageSize": 1,
            },
        ).json()
        found = data.get("files", [])
        return found[0] if found else None

    def rename(self, file_id: str, new_name: str) -> None:
        self._request(
            "PATCH", f"{API}/files/{file_id}", params={"fields": "id"}, json={"name": new_name}
        )

    def thumbnail(self, thumbnail_link: str, edge: int = 768) -> bytes:
        """Drive's own preview render — a few hundred KB instead of the
        original megabytes, and plenty for CLIP."""
        url = thumbnail_link.replace("=s220", f"=s{edge}")
        return self._request("GET", url).content

    def upload_json(self, folder_id: str, name: str, payload: dict[str, Any]) -> str:
        body = json.dumps(payload, indent=1).encode()
        metadata = json.dumps({"name": name, "parents": [folder_id]})
        boundary = "framefound-manifest"
        multipart = (
            (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                f"{metadata}\r\n--{boundary}\r\nContent-Type: application/json\r\n\r\n"
            ).encode()
            + body
            + f"\r\n--{boundary}--".encode()
        )
        data = self._request(
            "POST",
            f"{UPLOAD_API}/files",
            params={"uploadType": "multipart", "fields": "id"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            content=multipart,
        ).json()
        return str(data["id"])

    def download_json(self, file_id: str) -> dict[str, Any]:
        data = self._request("GET", f"{API}/files/{file_id}", params={"alt": "media"}).json()
        return dict(data)

    def delete(self, file_id: str) -> None:
        self._request("DELETE", f"{API}/files/{file_id}")
