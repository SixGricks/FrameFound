"""Remote-access configuration, persisted in app_settings.

The DNS token is sealed before storage and never returned to a client — the
API reports only whether one is configured.
"""

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.crypto import SecretUnavailable, seal, unseal
from framefound.db.models import AppSetting

REMOTE_ACCESS_KEY = "remote_access"
DDNS_STATE_KEY = "ddns_state"


@dataclass
class RemoteAccessConfig:
    """Mode drives what the wizard shows and what the app enforces."""

    mode: str = "local"  # local | tailscale | domain | tunnel
    public_access_enabled: bool = False
    domain: str = ""
    ddns_provider: str = ""  # "" | cloudflare
    ddns_zone: str = ""
    ddns_record: str = ""
    ddns_token_sealed: str = ""
    ddns_ipv4: bool = True
    ddns_ipv6: bool = False
    ddns_proxied: bool = False
    ddns_interval_minutes: int = 5

    @property
    def ddns_configured(self) -> bool:
        return bool(self.ddns_provider and self.ddns_zone and self.ddns_token_sealed)

    def token(self) -> str:
        if not self.ddns_token_sealed:
            raise SecretUnavailable("No DNS token is configured")
        return unseal(self.ddns_token_sealed)

    def with_token(self, plaintext: str) -> None:
        self.ddns_token_sealed = seal(plaintext) if plaintext else ""


@dataclass
class DdnsState:
    """Last-known result, surfaced on the remote-access page."""

    last_ipv4: str = ""
    last_ipv6: str = ""
    last_checked_at: str = ""
    last_updated_at: str = ""
    last_error: str = ""
    consecutive_failures: int = 0
    history: list[str] = field(default_factory=list)


async def load_config(db: AsyncSession) -> RemoteAccessConfig:
    row = await db.get(AppSetting, REMOTE_ACCESS_KEY)
    if row is None:
        return RemoteAccessConfig()
    known = {f for f in RemoteAccessConfig().__dict__}
    return RemoteAccessConfig(**{k: v for k, v in row.value.items() if k in known})


async def save_config(db: AsyncSession, config: RemoteAccessConfig) -> None:
    await _put(db, REMOTE_ACCESS_KEY, asdict(config))


async def load_state(db: AsyncSession) -> DdnsState:
    row = await db.get(AppSetting, DDNS_STATE_KEY)
    if row is None:
        return DdnsState()
    known = {f for f in DdnsState().__dict__}
    return DdnsState(**{k: v for k, v in row.value.items() if k in known})


async def save_state(db: AsyncSession, state: DdnsState) -> None:
    state.history = state.history[-20:]  # bounded: this is a status panel
    await _put(db, DDNS_STATE_KEY, asdict(state))


async def _put(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()
