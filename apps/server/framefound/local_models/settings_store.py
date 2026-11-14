"""What the operator sets on the AI models page, persisted in app_settings.

Two rows:

- `local_models` — the publisher lists, the two memory budgets, the overnight
  window, how long an idle model stays loaded, the pause switch, and the
  Hugging Face token for models whose maker asks for an account. Written only
  by an administrator's request.
- `local_models_latest` — the newest version of each runtime that was found
  the last time somebody looked, so the page can say "an update is
  available" without calling the network to draw itself.

These are read on every question put to a model rather than once per process
(`get_settings()` is cached for the life of a process): a budget lowered or a
publisher blocked on the page has to take effect at the next question, not at
the next restart.

The token is sealed like every other stored secret (auth/crypto.py): a copy
of the database alone cannot use it. It is sent to Hugging Face and nowhere
else, and is never handed to a program that runs a model.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.crypto import seal, unseal
from framefound.db.models import AppSetting
from framefound.local_models import policy, schedule

CONFIG_KEY = "local_models"
# {"<runtime kind>": {"version", "checked_at", "problem"}}
LATEST_KEY = "local_models_latest"


@dataclass
class LocalModelsConfig:
    # Makers and converters are the operator's and are stored whole. Blocked
    # is stored as additions only: the built-in names are code
    # (policy.DEFAULT_BLOCKED), always in force, and grow with a release.
    makers: list[str] = field(default_factory=lambda: list(policy.DEFAULT_MAKERS))
    converters: list[str] = field(default_factory=lambda: list(policy.DEFAULT_CONVERTERS))
    blocked_added: list[str] = field(default_factory=list)
    day_budget_gb: float = 8.0
    night_budget_gb: float = 20.0
    night_start: str = "21:30"
    night_end: str = "05:30"
    idle_unload_minutes: int = 5
    paused: bool = False
    # When and by whom, for "Paused since Tue 14:02 by …": a pause somebody
    # forgot looks exactly like a model lane that is broken.
    paused_at: str = ""
    paused_by: str = ""
    hf_token_sealed: str = ""

    def lists(self) -> policy.PublisherLists:
        return policy.lists_of(self.makers, self.converters, self.blocked_added)

    def budgets(self) -> schedule.Budgets:
        """The budgets and window as the scheduling rules take them.

        A value that cannot be read falls back to its default rather than
        raising: this runs before every question to a model, and the page
        that saves these refuses anything unreadable in the first place.
        """
        default = schedule.Budgets()
        return schedule.Budgets(
            day_gb=_gb(self.day_budget_gb, default.day_gb),
            night_gb=_gb(self.night_budget_gb, default.night_gb),
            night_start=_clock(self.night_start, default.night_start),
            night_end=_clock(self.night_end, default.night_end),
        )

    def hf_token(self) -> str | None:
        """The token, or None when none is stored.

        Raises `SecretUnavailable` when one is stored but cannot be read
        (the server's secret key changed since): the page then asks for it
        to be pasted again instead of behaving as if there were none.
        """
        if not self.hf_token_sealed:
            return None
        return unseal(self.hf_token_sealed)

    def with_hf_token(self, plaintext: str) -> None:
        """Seal and keep a token; an empty string removes it."""
        self.hf_token_sealed = seal(plaintext) if plaintext else ""


async def load_config(db: AsyncSession) -> LocalModelsConfig:
    """The stored settings; anything never saved is its default.

    Keys this version does not know are dropped, so a row written by a newer
    version still loads after a rollback.
    """
    row = await db.get(AppSetting, CONFIG_KEY)
    if row is None:
        return LocalModelsConfig()
    known = set(LocalModelsConfig().__dict__)
    # Lists are copied. Handed over as they are, a caller's `makers.append`
    # changes the row's own value in place; if the session still holds that
    # row, the save that follows compares equal to what is "already there"
    # and nothing is written.
    kept: dict[str, Any] = {
        key: list(value) if isinstance(value, list) else value
        for key, value in row.value.items()
        if key in known
    }
    return LocalModelsConfig(**kept)


async def save_config(db: AsyncSession, config: LocalModelsConfig) -> None:
    """Store the settings and commit."""
    await _put(db, CONFIG_KEY, asdict(config))


async def load_latest(db: AsyncSession) -> dict[str, dict[str, Any]]:
    """Per runtime kind: the newest version found, when it was looked up, and what went wrong."""
    row = await db.get(AppSetting, LATEST_KEY)
    if row is None:
        return {}
    return {kind: dict(entry) for kind, entry in row.value.items() if isinstance(entry, dict)}


async def save_latest(
    db: AsyncSession,
    kind: str,
    version: str | None,
    problem: str | None,
    *,
    now: datetime | None = None,
) -> None:
    """Record one look for a runtime's newest version, and commit.

    A look that failed (`version` None, `problem` the sentence) keeps the
    version found last time: "0.7.12, could not check just now" is more use
    to the page than forgetting what was known.
    """
    latest = await load_latest(db)
    known = latest.get(kind, {}).get("version")
    latest[kind] = {
        "version": version if version is not None else known,
        "checked_at": (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "problem": problem,
    }
    await _put(db, LATEST_KEY, latest)


async def _put(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


def _gb(value: object, default: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return default
    return float(value)


def _clock(value: object, default: time) -> time:
    try:
        return schedule.parse_clock(value) if isinstance(value, str) else default
    except ValueError:
        return default
