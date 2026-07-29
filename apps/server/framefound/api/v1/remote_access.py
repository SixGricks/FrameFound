"""Remote-access configuration, status, and the public-access kill switch."""

import ipaddress

import structlog
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from framefound.auth import service as auth_service
from framefound.auth.crypto import SecretUnavailable
from framefound.auth.deps import CurrentUser, DbDep, client_ip, require_admin
from framefound.ddns import settings_store as store
from framefound.ddns import tailnet
from framefound.ddns.providers import DnsError, build_provider, detect_public_ip

log = structlog.get_logger()

router = APIRouter(prefix="/remote-access", tags=["remote-access"])

# Tailscale/Headscale hand out addresses from the CGNAT range.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")


def classify_client(ip: str | None) -> str:
    """How did this request reach us? Drives the status panel."""
    if not ip:
        return "unknown"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    if address.is_loopback:
        return "local"
    if address in TAILNET_V4:
        return "tailnet"
    if address.is_private:
        return "lan"
    return "internet"


class RemoteAccessOut(BaseModel):
    mode: str
    public_access_enabled: bool
    domain: str
    ddns_provider: str
    ddns_zone: str
    ddns_record: str
    ddns_configured: bool  # never the token itself
    ddns_ipv4: bool
    ddns_ipv6: bool
    ddns_proxied: bool
    ddns_interval_minutes: int
    # Live status
    your_connection: str
    last_ipv4: str
    last_checked_at: str
    last_updated_at: str
    last_error: str
    # Tailnet: learned from a request that actually arrived over it, so an
    # address is only ever shown once it has been proven to work.
    tailnet_host: str
    tailnet_url: str
    tailnet_seen_at: str
    on_tailnet_now: bool


class RemoteAccessUpdate(BaseModel):
    mode: str | None = Field(default=None, pattern="^(local|tailscale|domain|tunnel)$")
    public_access_enabled: bool | None = None
    domain: str | None = Field(default=None, max_length=253)
    ddns_provider: str | None = Field(default=None, pattern="^(|cloudflare)$")
    ddns_zone: str | None = Field(default=None, max_length=253)
    ddns_record: str | None = Field(default=None, max_length=253)
    ddns_token: str | None = Field(default=None, max_length=500)  # write-only
    ddns_ipv4: bool | None = None
    ddns_ipv6: bool | None = None
    ddns_proxied: bool | None = None
    ddns_interval_minutes: int | None = Field(default=None, ge=1, le=1440)


async def _render(db: DbDep, request: Request) -> RemoteAccessOut:
    config = await store.load_config(db)
    state = await store.load_state(db)
    ip = client_ip(request)
    on_tailnet = tailnet.is_tailnet_address(ip)
    return RemoteAccessOut(
        mode=config.mode,
        public_access_enabled=config.public_access_enabled,
        domain=config.domain,
        ddns_provider=config.ddns_provider,
        ddns_zone=config.ddns_zone,
        ddns_record=config.ddns_record,
        ddns_configured=config.ddns_configured,
        ddns_ipv4=config.ddns_ipv4,
        ddns_ipv6=config.ddns_ipv6,
        ddns_proxied=config.ddns_proxied,
        ddns_interval_minutes=config.ddns_interval_minutes,
        your_connection=classify_client(ip),
        last_ipv4=state.last_ipv4,
        last_checked_at=state.last_checked_at,
        last_updated_at=state.last_updated_at,
        last_error=state.last_error,
        tailnet_host=config.tailnet_host,
        tailnet_url=(
            tailnet.tailnet_url(config.tailnet_host, https=request.url.scheme == "https")
            if config.tailnet_host
            else ""
        ),
        tailnet_seen_at=config.tailnet_seen_at,
        on_tailnet_now=on_tailnet,
    )


@router.get("", response_model=RemoteAccessOut)
async def get_remote_access(request: Request, db: DbDep, _user: CurrentUser) -> RemoteAccessOut:
    # Learn the tailnet address opportunistically. Only from a request whose
    # *source* is already inside the tailnet range, so a forged Host header on
    # a public request cannot plant one.
    sighting = tailnet.sighting_from_request(client_ip(request), request.headers.get("host"))
    if sighting is not None:
        config = await store.load_config(db)
        if config.tailnet_host != sighting.host:
            config.tailnet_host = sighting.host
            config.tailnet_seen_at = sighting.seen_at
            await store.save_config(db, config)
            log.info("tailnet.address_learned", host=sighting.host)
        elif not config.tailnet_seen_at:
            config.tailnet_seen_at = sighting.seen_at
            await store.save_config(db, config)
    return await _render(db, request)


@router.put("", response_model=RemoteAccessOut, dependencies=[require_admin])
async def update_remote_access(
    body: RemoteAccessUpdate, request: Request, db: DbDep, user: CurrentUser
) -> RemoteAccessOut:
    config = await store.load_config(db)
    changes = body.model_dump(exclude_unset=True)
    token = changes.pop("ddns_token", None)
    for field, value in changes.items():
        setattr(config, field, value)
    if token is not None:
        config.with_token(token)
    await store.save_config(db, config)
    await auth_service.audit(
        db,
        "remote_access.updated",
        actor_user_id=user.id,
        ip=client_ip(request),
        # The token is deliberately absent from the audit detail.
        detail={"mode": config.mode, "public": config.public_access_enabled},
    )
    await db.commit()
    return await _render(db, request)


@router.post("/disable-public", response_model=RemoteAccessOut, dependencies=[require_admin])
async def disable_public_access(request: Request, db: DbDep, user: CurrentUser) -> RemoteAccessOut:
    """Kill switch: immediately stop serving requests from the open internet.

    Enforced in-app, so it takes effect on the next request without waiting
    for a reverse-proxy reload or a DNS change to propagate.
    """
    config = await store.load_config(db)
    config.public_access_enabled = False
    config.mode = "local"
    await store.save_config(db, config)
    await auth_service.audit(
        db, "remote_access.disabled", actor_user_id=user.id, ip=client_ip(request)
    )
    await db.commit()
    return await _render(db, request)


class DnsTestResult(BaseModel):
    ok: bool
    message: str
    detected_ipv4: str | None = None


@router.post("/test-dns", response_model=DnsTestResult, dependencies=[require_admin])
async def test_dns(db: DbDep, _user: CurrentUser) -> DnsTestResult:
    """Check the saved token against the provider without changing records."""
    config = await store.load_config(db)
    if not config.ddns_configured:
        raise HTTPException(400, "Set a DNS provider, zone, and token first")
    detected = await detect_public_ip()
    try:
        provider = build_provider(config.ddns_provider, config.token(), config.ddns_zone)
        await provider.verify()
    except (DnsError, SecretUnavailable) as err:
        return DnsTestResult(ok=False, message=str(err), detected_ipv4=detected)
    return DnsTestResult(
        ok=True,
        message=f"Token works and can see {config.ddns_zone}",
        detected_ipv4=detected,
    )
