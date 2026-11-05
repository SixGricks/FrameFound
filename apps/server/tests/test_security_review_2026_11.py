"""The 2026-11 security review: each test fails against the code before it.

The findings, and why each fix is shaped the way it is, are in
docs/roadmap.md under 2026-11-05.
"""

import io
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers
from starlette.requests import Request

from framefound.api.v1.libraries import PathMappingIn
from framefound.api.v1.remote_access import classify_client
from framefound.auth.client_address import TUNNEL_UNKNOWN, resolve_client_ip
from framefound.auth.ratelimit import PRUNE_AT, LoginRateLimiter

DOCKER_BRIDGE = "172.16.0.0/12"
ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}


def _repo_file(*parts: str) -> Path | None:
    """A file elsewhere in the repository, or None where only apps/server was
    copied (the VM gate runs from /src, three levels deep)."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent.joinpath(*parts)
        if candidate.is_file():
            return candidate
    return None


def _request(peer: str, headers: dict[str, str]) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": Headers(headers).raw,
        "client": (peer, 1234),
    }
    return Request(scope)


# --- Cloudflare Tunnel visitors are on the internet --------------------------


def test_a_tunnel_visitor_is_classified_as_internet() -> None:
    """Through the tunnel, Caddy rewrote X-Forwarded-For to cloudflared's
    172.x address and every visitor was "lan": the kill switch did nothing."""
    request = _request(
        "172.18.0.4",
        {
            "x-forwarded-for": "172.18.0.9",
            "x-framefound-ingress": "tunnel",
            "x-framefound-client": "81.2.69.142",
        },
    )
    ip = resolve_client_ip(request, DOCKER_BRIDGE)
    assert ip == "81.2.69.142"
    assert classify_client(ip) == "internet"


@pytest.mark.parametrize("claimed", ["", "192.168.1.5", "127.0.0.1", "not-an-ip"])
def test_a_tunnel_visitor_cannot_claim_to_be_local(claimed: str) -> None:
    request = _request(
        "172.18.0.4", {"x-framefound-ingress": "tunnel", "x-framefound-client": claimed}
    )
    ip = resolve_client_ip(request, DOCKER_BRIDGE)
    assert ip == TUNNEL_UNKNOWN
    assert classify_client(ip) == "internet"


def test_the_tunnel_marker_from_an_untrusted_peer_is_ignored() -> None:
    request = _request(
        "203.0.113.9", {"x-framefound-ingress": "tunnel", "x-framefound-client": "198.51.100.7"}
    )
    assert resolve_client_ip(request, DOCKER_BRIDGE) == "203.0.113.9"


def test_caddy_marks_only_its_tunnel_entrance() -> None:
    path = _repo_file("infrastructure", "caddy", "Caddyfile")
    if path is None:
        pytest.skip("checked where the whole repository is; the VM gate copies apps/server alone")
    caddyfile = path.read_text(encoding="utf-8")
    assert ":8081 {" in caddyfile
    assert caddyfile.count("import not_tunnel") == 2  # the :80 site and the domain site
    assert "request_header -X-FrameFound-Ingress" in caddyfile


# --- sign-in -----------------------------------------------------------------


def test_rotating_the_email_is_throttled_per_address(client: TestClient) -> None:
    client.post(
        "/api/v1/auth/setup",
        json={"setup_token": "test-setup-token-123", **ADMIN},
    )
    statuses = [
        client.post(
            "/api/v1/auth/login", json={"email": f"user{i}@example.com", "password": "guess"}
        ).status_code
        for i in range(22)
    ]
    assert statuses[:20] == [401] * 20
    assert statuses[-1] == 429


def test_expired_limiter_buckets_are_forgotten(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = LoginRateLimiter(window_seconds=10)
    clock = [1000.0]
    monkeypatch.setattr("framefound.auth.ratelimit.time.monotonic", lambda: clock[0])
    for i in range(PRUNE_AT):
        limiter.record_failure(f"spray:{i}")
    clock[0] += 60
    limiter.record_failure("one more")
    assert len(limiter._buckets) == 1


# --- path profiles fit their platform ----------------------------------------


@pytest.mark.parametrize(
    ("platform", "prefix"),
    [("windows", "Z:\\Photos"), ("windows", "\\\\nas\\Photos"), ("macos", "/Volumes/Photos")],
)
def test_a_profile_that_fits_its_platform_is_accepted(platform: str, prefix: str) -> None:
    PathMappingIn(profile_name="x", platform=platform, mapped_prefix=prefix)


@pytest.mark.parametrize(
    ("platform", "prefix"),
    [("macos", "Z:\\Photos"), ("windows", "/Volumes/Photos"), ("linux", "photos")],
)
def test_a_profile_for_the_wrong_platform_is_refused(platform: str, prefix: str) -> None:
    with pytest.raises(ValueError):
        PathMappingIn(profile_name="x", platform=platform, mapped_prefix=prefix)


# --- uploads and decoding ----------------------------------------------------


def _signed_in(client: TestClient) -> None:
    client.post("/api/v1/auth/setup", json={"setup_token": "test-setup-token-123", **ADMIN})
    client.post("/api/v1/auth/login", json=ADMIN)


def _png(width: int, height: int) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("L", (width, height), 0).save(out, "PNG")
    return out.getvalue()


def test_a_sky_cannot_be_uploaded_as_html(client: TestClient) -> None:
    """The name rule allowed `x.html`; Pillow's verify() accepts a PNG with
    <script> in a text chunk; FileResponse served it as text/html."""
    _signed_in(client)
    response = client.put("/api/v1/develop/skies/x.html", content=_png(4, 4))
    assert response.status_code == 400
    assert client.get("/api/v1/develop/skies/x.html/image").status_code == 400


def test_a_sky_is_served_as_an_image_and_inert(client: TestClient) -> None:
    _signed_in(client)
    assert client.put("/api/v1/develop/skies/dusk.png", content=_png(4, 4)).status_code < 300
    served = client.get("/api/v1/develop/skies/dusk.png/image")
    assert served.headers["content-type"] == "image/png"
    assert "sandbox" in served.headers["content-security-policy"]


# --- tokens are never stronger than their account ----------------------------


async def test_a_disabled_accounts_tokens_stop_working(tmp_path: Path) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from framefound.auth import panel_tokens
    from framefound.db.base import Base
    from framefound.db.models import User

    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        user = User(id=uuid.uuid4(), email="ed@example.com", password_hash="x", role="user")
        db.add(user)
        await db.flush()
        minted = await panel_tokens.mint(db, user=user, name="Premiere", scopes=["read"])
        await db.commit()
        assert await panel_tokens.resolve(db, minted.plaintext) is not None
        user.disabled = True
        await db.commit()
        assert await panel_tokens.resolve(db, minted.plaintext) is None
    await engine.dispose()


def test_the_geocoder_never_logs_its_request_url() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "framefound" / "media" / "geocoding.py"
    ).read_text(encoding="utf-8")
    assert "error=str(exc)" not in source


# --- the fixes' own review (2026-11-05) --------------------------------------


@pytest.mark.parametrize("shared", ["172.18.0.4", TUNNEL_UNKNOWN, None])
def test_a_shared_address_cannot_lock_everyone_out(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, shared: str | None
) -> None:
    """A tunnel still aimed at caddy:80 puts every visitor on cloudflared's
    172.x: twenty made-up emails locked out the right password as well."""
    client.post("/api/v1/auth/setup", json={"setup_token": "test-setup-token-123", **ADMIN})
    monkeypatch.setattr("framefound.auth.router.client_ip", lambda _request: shared)
    sprayed = {
        client.post(
            "/api/v1/auth/login", json={"email": f"user{i}@example.com", "password": "guess"}
        ).status_code
        for i in range(25)
    }
    assert sprayed == {401}
    assert client.post("/api/v1/auth/login", json=ADMIN).status_code == 200
    # The per address-and-email limit still holds there.
    repeated = [
        client.post(
            "/api/v1/auth/login", json={"email": ADMIN["email"], "password": "guess"}
        ).status_code
        for _ in range(6)
    ]
    assert repeated[-1] == 429


def _in_app(client: TestClient, app: FastAPI, work: Callable[[Any], Awaitable[Any]]) -> Any:
    """Run `work(db)` on the app's own loop and database."""
    from framefound.db.engine import get_session

    async def run() -> Any:
        result = None
        async for db in app.dependency_overrides[get_session]():
            result = await work(db)
        return result

    assert client.portal is not None
    return client.portal.call(run)


def test_a_readonly_accounts_export_token_grabs_as_its_browser_does(
    client: TestClient, app: FastAPI
) -> None:
    """The read-only refusal ignored the method, so the GET frame grab the
    same account makes in a browser failed with its token: every video still
    in a Lightroom listing import. A POSTed bin is still refused."""
    from framefound.auth import panel_tokens, service

    async def mint(db: Any) -> str:
        user = await service.create_user(
            db, "ro@example.com", "another-strong-password", "readonly"
        )
        await db.flush()
        minted = await panel_tokens.mint(db, user=user, name="LR", scopes=["read", "export"])
        await db.commit()
        return minted.plaintext

    headers = {"Authorization": f"Bearer {_in_app(client, app, mint)}"}
    grab = client.get(f"/api/v1/panel/grabs/{uuid.uuid4()}/0", headers=headers)
    assert grab.status_code == 404, grab.text  # past the scope check: no such video
    bin_ = client.post("/api/v1/panel/export/fcp7", headers=headers, json={})
    assert bin_.status_code == 403
    assert "read-only" in bin_.text


def test_concurrent_grabs_cannot_pass_the_cap_together(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Every request of a burst counted the stills before any ffmpeg had
    written one, so all of them passed the cap and the free-space floor."""
    import asyncio
    import time
    from datetime import UTC, datetime

    from httpx import ASGITransport, AsyncClient

    from framefound.api.v1 import panel
    from framefound.auth.deps import get_panel_principal
    from framefound.db.models import Asset, Library

    monkeypatch.setattr(panel, "MAX_GRABS_PER_VIDEO", 3)

    def slow_extract(_src: Path, dst: Path, _at: float) -> None:
        time.sleep(0.05)  # ffmpeg's partial appears only once it writes
        dst.write_bytes(b"II*\x00")

    monkeypatch.setattr("framefound.processing.ffmpeg.extract_still", slow_extract)
    app.dependency_overrides[get_panel_principal] = lambda: None

    async def video(db: Any) -> str:
        library = Library(name="L", root_path=str(tmp_path))
        db.add(library)
        await db.flush()
        asset = Asset(
            library_id=library.id,
            relative_path="walk.mp4",
            filename="walk.mp4",
            extension="mp4",
            media_type="video",
            size_bytes=1000,
            mtime=datetime(2026, 1, 1, tzinfo=UTC),
            availability="online",
        )
        db.add(asset)
        await db.commit()
        return str(asset.id)

    asset_id = _in_app(client, app, video)

    async def burst() -> list[int]:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
            responses = await asyncio.gather(
                *(http.get(f"/api/v1/panel/grabs/{asset_id}/{ms}") for ms in range(10))
            )
        return [r.status_code for r in responses]

    assert client.portal is not None
    statuses = client.portal.call(burst)
    assert sorted(statuses) == [200] * 3 + [429] * 7
    grabs = tmp_path / "data" / "grabs" / asset_id
    assert len(list(grabs.glob("*.tif"))) == 3


_PANEL_HARNESS = """
const fs = require("fs");
const vm = require("vm");
const [, , mainPath, stored] = process.argv;
const store = new Map();
if (stored !== "<absent>") store.set("ff.profile", stored);
const element = () => ({
  value: "", style: {}, innerHTML: "", textContent: "",
  appendChild() {}, addEventListener() {},
});
const elements = new Map();
const context = {
  document: {
    getElementById: (id) => elements.get(id) || elements.set(id, element()).get(id),
    createElement: element,
  },
  localStorage: {
    getItem: (key) => (store.has(key) ? store.get(key) : null),
    setItem: (key, value) => store.set(key, String(value)),
    removeItem: (key) => store.delete(key),
  },
  require: (name) => {
    if (name === "os") return { platform: () => "win32" };
    throw new Error(name);
  },
  console, setTimeout, clearTimeout, URL, AbortController,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(mainPath, "utf8"), context);
context.api = async () => [
  { profile_name: "DJ's PC", platform: "windows", mapped_prefix: "Y:" },
  { profile_name: "Mac Mini", platform: "macos", mapped_prefix: "/Volumes/X" },
];
vm.runInContext("loadProfiles()", context).then(() => {
  const profile = vm.runInContext("state.profile", context);
  const kept = store.has("ff.profile") ? store.get("ff.profile") : null;
  console.log(JSON.stringify({ profile, kept }));
});
"""


@pytest.mark.parametrize(
    ("stored", "profile", "kept"),
    [
        ("", "", ""),  # "No path profile", chosen on purpose
        ("<absent>", "DJ's PC", "DJ's PC"),  # never chosen: this OS's own
        ("Mac Mini", "Mac Mini", "Mac Mini"),  # another OS's, chosen on purpose
        ("Gone", "DJ's PC", "DJ's PC"),  # renamed or removed: as never chosen
    ],
)
def test_the_premiere_panel_keeps_the_profile_the_operator_chose(
    tmp_path: Path, stored: str, profile: str, kept: str
) -> None:
    """An explicit "No path profile" was stored as "", read back as "never
    chosen", and replaced by this OS's profile on every connect."""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.js"
    harness.write_text(_PANEL_HARNESS, encoding="utf-8")
    main_js = Path(__file__).resolve().parents[3] / "apps" / "panel-premiere" / "main.js"
    run = subprocess.run(  # noqa: S603 - fixed binary, argv form, test-made paths
        [node, str(harness), str(main_js), stored],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(run.stdout.strip().splitlines()[-1]) == {"profile": profile, "kept": kept}
