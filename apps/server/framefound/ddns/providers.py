"""DNS provider interface + Cloudflare adapter.

Only scoped API tokens are accepted. Cloudflare's *global* key authorises
every action on every zone in the account; a Zone:DNS:Edit token cannot do
anything but change the records we point it at. The UI refuses global keys
(they are 37-char hex) rather than silently accepting a dangerous credential.
"""

from dataclasses import dataclass
from typing import Protocol

import httpx
import structlog

log = structlog.get_logger()

API_TIMEOUT_S = 20
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"

# Queried in order; the first that answers wins. Cloudflare's endpoint is
# plain text and needs no JSON parsing, so it leads.
IPV4_SOURCES = (
    "https://1.1.1.1/cdn-cgi/trace",
    "https://api.ipify.org",
)
IPV6_SOURCES = ("https://api6.ipify.org",)


class DnsError(RuntimeError):
    """Provider call failed. Message is safe to show to an administrator."""


@dataclass(frozen=True)
class DnsRecord:
    name: str  # media.example.com
    ip: str
    record_type: str  # A | AAAA
    proxied: bool = False
    ttl: int = 300


class DnsProvider(Protocol):
    async def verify(self) -> str: ...
    async def upsert(self, record: DnsRecord) -> None: ...


def looks_like_global_key(token: str) -> bool:
    """Cloudflare global API keys are 37 lowercase hex characters."""
    candidate = token.strip()
    return len(candidate) == 37 and all(c in "0123456789abcdef" for c in candidate)


async def detect_public_ip(*, ipv6: bool = False) -> str | None:
    """Ask the internet what address it sees us as."""
    sources = IPV6_SOURCES if ipv6 else IPV4_SOURCES
    async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
        for url in sources:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                text = resp.text.strip()
                if "cdn-cgi/trace" in url:  # key=value lines
                    for line in text.splitlines():
                        if line.startswith("ip="):
                            return line[3:].strip()
                    continue
                return text
            except (httpx.HTTPError, ValueError):
                continue
    return None


class CloudflareProvider:
    """Cloudflare DNS via a scoped Zone:DNS:Edit token."""

    def __init__(self, token: str, zone: str) -> None:
        if looks_like_global_key(token):
            raise DnsError(
                "That looks like a Cloudflare global API key. Create a scoped "
                "API token with Zone:DNS:Edit permission instead."
            )
        self._token = token.strip()
        self._zone = zone.strip()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    async def _zone_id(self, client: httpx.AsyncClient) -> str:
        resp = await client.get(
            f"{CLOUDFLARE_API}/zones", params={"name": self._zone}, headers=self._headers()
        )
        if resp.status_code == 403:
            raise DnsError("The API token was rejected. Check its permissions and zone scope.")
        resp.raise_for_status()
        results = resp.json().get("result") or []
        if not results:
            raise DnsError(f"Zone '{self._zone}' was not found for this token")
        return str(results[0]["id"])

    async def verify(self) -> str:
        """Confirm the token can see the zone. Returns the zone id."""
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
                return await self._zone_id(client)
        except httpx.HTTPError as err:
            raise DnsError("Could not reach Cloudflare") from err

    async def upsert(self, record: DnsRecord) -> None:
        try:
            async with httpx.AsyncClient(timeout=API_TIMEOUT_S) as client:
                zone_id = await self._zone_id(client)
                existing = await client.get(
                    f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records",
                    params={"type": record.record_type, "name": record.name},
                    headers=self._headers(),
                )
                existing.raise_for_status()
                payload = {
                    "type": record.record_type,
                    "name": record.name,
                    "content": record.ip,
                    "ttl": record.ttl,
                    "proxied": record.proxied,
                }
                found = existing.json().get("result") or []
                if found:
                    resp = await client.put(
                        f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records/{found[0]['id']}",
                        json=payload,
                        headers=self._headers(),
                    )
                else:
                    resp = await client.post(
                        f"{CLOUDFLARE_API}/zones/{zone_id}/dns_records",
                        json=payload,
                        headers=self._headers(),
                    )
                resp.raise_for_status()
        except httpx.HTTPError as err:
            raise DnsError("Could not update the DNS record") from err
        log.info("ddns.record_updated", name=record.name, type=record.record_type)


def build_provider(kind: str, token: str, zone: str) -> DnsProvider:
    if kind == "cloudflare":
        return CloudflareProvider(token, zone)
    raise DnsError(f"Unsupported DNS provider: {kind}")
