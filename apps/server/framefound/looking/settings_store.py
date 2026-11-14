"""What the operator sets for looking, persisted in app_settings.

One row, `looking`, edited by an administrator on the AI models page. The
budgets, the overnight window and the pause switch are L1's
(local_models/settings_store.py) and are read from there, never copied:
two copies of "when is night" would one day disagree.

Read at the start of every piece of work rather than once per process, so a
change on the page takes effect at the next item, not the next restart.
"""

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from framefound.db.models import AppSetting

CONFIG_KEY = "looking"

# Pictures a model is shown for one shot: its start, middle and end, or the
# middle alone. Whether a given model reads three pictures in one question
# well cannot be known off the Mac, so it is a setting, not a release.
STILLS_CHOICES = (3, 1)


@dataclass
class LookingConfig:
    # May the Looker propose tags that are not yet in the catalogue's vocabulary.
    new_tags: bool = True
    # 3 = start, middle and end; 1 = the middle only.
    stills_per_shot: int = 3


async def load_config(db: AsyncSession) -> LookingConfig:
    """The stored settings; anything never saved, or unreadable, is its default.

    Keys this version does not know are dropped, so a row written by a newer
    version still loads after a rollback. A value of the wrong kind falls
    back too: this is read before every review, and a row edited by hand
    must not stop them all.

    Read from the database every time, not from the session: sessions here
    do not expire on commit, so a review that keeps one session for its
    whole run would otherwise go on with the first copy it read and never
    see a change saved on the page.
    """
    row = await db.get(AppSetting, CONFIG_KEY, populate_existing=True)
    if row is None or not isinstance(row.value, dict):
        return LookingConfig()
    return _read(row.value)


async def save_config(db: AsyncSession, config: LookingConfig) -> None:
    """Store the settings and commit. ValueError for a value the page must refuse."""
    if not isinstance(config.new_tags, bool):
        raise ValueError("Say yes or no to new tags.")
    if isinstance(config.stills_per_shot, bool) or config.stills_per_shot not in STILLS_CHOICES:
        raise ValueError("A shot is shown as 3 pictures (start, middle, end) or 1 (the middle).")
    value = asdict(config)
    row = await db.get(AppSetting, CONFIG_KEY)
    if row is None:
        db.add(AppSetting(key=CONFIG_KEY, value=value))
    else:
        row.value = value
    await db.commit()


def _read(stored: dict[str, Any]) -> LookingConfig:
    default = LookingConfig()
    new_tags = stored.get("new_tags", default.new_tags)
    stills = stored.get("stills_per_shot", default.stills_per_shot)
    return LookingConfig(
        new_tags=new_tags if isinstance(new_tags, bool) else default.new_tags,
        stills_per_shot=(
            stills
            if isinstance(stills, int) and not isinstance(stills, bool) and stills in STILLS_CHOICES
            else default.stills_per_shot
        ),
    )
