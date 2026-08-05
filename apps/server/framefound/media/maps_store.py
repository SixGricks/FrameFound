"""Google Maps configuration, persisted in app_settings.

Two keys, because they cannot carry the same restrictions:

- **browser key** — embedded in the page so the Maps JS API can load. Public
  by necessity; restrict it by HTTP referrer in the Google console. It is
  still sealed at rest and served only to an authenticated session, so it is
  not sitting in the built bundle for anyone who fetches the site.
- **geocoding key** — used only from the server for address lookups. Never
  leaves the machine; restrict it by IP.

Using one key for both would force a choice between a referrer restriction
that breaks server-side geocoding and no restriction at all.

Turning either on sends data to Google: tile requests reveal roughly where
the operator is looking, and a geocode reveals an exact coordinate. That is
a deliberate trade the operator opts into, and both stay empty by default.
"""

from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from framefound.auth.crypto import SecretUnavailable, seal, unseal
from framefound.db.models import AppSetting

MAPS_KEY = "google_maps"


# Basemap providers, in order of how much they tell an outsider.
#   none      — the local scatter. Nothing leaves the machine.
#   maplibre  — vector tiles from a style URL you choose. Point it at your own
#               OpenMapTiles/Protomaps server and nothing leaves your network.
#   google    — Google's tiles. Every pan reveals where you are looking.
PROVIDERS = ("none", "maplibre", "google")

# A MapLibre style is the single piece of configuration that decides where
# tiles come from, so it is the whole story for self-hosting. Left blank
# deliberately: guessing a public demo endpoint would quietly reintroduce the
# outbound dependency the operator chose MapLibre to avoid.
DEFAULT_STYLE_URL = ""

# maplibre-gl is loaded at runtime rather than bundled, so this can be pointed
# at a copy served from the same host for an install with no internet at all.
DEFAULT_LIBRARY_URL = "https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.js"
DEFAULT_STYLESHEET_URL = "https://unpkg.com/maplibre-gl@4/dist/maplibre-gl.css"


@dataclass
class MapsConfig:
    browser_key_sealed: str = ""
    geocoding_key_sealed: str = ""
    # Off unless asked for: a basemap is an outbound dependency, and the
    # scatter view works with no third party involved.
    basemap_enabled: bool = False
    provider: str = "none"
    # Self-hosted vector tiles. A style URL is all MapLibre needs; if it points
    # at your own server, no third party sees anything.
    style_url: str = DEFAULT_STYLE_URL
    library_url: str = DEFAULT_LIBRARY_URL
    stylesheet_url: str = DEFAULT_STYLESHEET_URL
    # Reverse geocoding only fills gaps. Folder names are more specific than
    # anything a gazetteer returns, so they win where they exist.
    geocode_unnamed_places: bool = True

    @property
    def basemap_ready(self) -> bool:
        """Enabled *and* actually configured.

        Reporting ready without the piece each provider needs would load a map
        that errors instead of falling back to the scatter, which is worse than
        no basemap at all.
        """
        if not self.basemap_enabled:
            return False
        if self.provider == "google":
            return bool(self.browser_key_sealed)
        if self.provider == "maplibre":
            return bool(self.style_url.strip())
        return False

    @property
    def geocoding_ready(self) -> bool:
        return bool(self.geocoding_key_sealed)

    def browser_key(self) -> str:
        if not self.browser_key_sealed:
            raise SecretUnavailable("No Google Maps browser key is configured")
        return unseal(self.browser_key_sealed)

    def geocoding_key(self) -> str:
        if not self.geocoding_key_sealed:
            raise SecretUnavailable("No Google geocoding key is configured")
        return unseal(self.geocoding_key_sealed)

    def with_browser_key(self, plaintext: str) -> None:
        self.browser_key_sealed = seal(plaintext) if plaintext else ""

    def with_geocoding_key(self, plaintext: str) -> None:
        self.geocoding_key_sealed = seal(plaintext) if plaintext else ""


async def load_maps_config(db: AsyncSession) -> MapsConfig:
    row = await db.get(AppSetting, MAPS_KEY)
    if row is None:
        return MapsConfig()
    known = set(MapsConfig().__dict__)
    return MapsConfig(**{k: v for k, v in row.value.items() if k in known})


async def save_maps_config(db: AsyncSession, config: MapsConfig) -> None:
    await _put(db, MAPS_KEY, asdict(config))


async def _put(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.commit()


FACES_KEY = "face_recognition"


@dataclass
class FaceConfig:
    """Face recognition settings.

    **On by default.** This install is single-operator and the feature is the
    point of the request. The off switch is real and immediate, and it exists
    because the calculus changes the moment someone else's footage is involved:
    face templates are biometric identifiers, and Illinois (BIPA), Texas and
    Washington attach statutory damages per person to collecting them without
    written consent, with a private right of action. GDPR Art. 9 treats them as
    special-category data. Self-hosting does not help — it makes the operator
    the controller.

    Turning it off stops detection and suggestion. It deliberately does *not*
    delete the people already named: losing that work on a toggle would be its
    own bug. `purge_on_disable` is offered separately for someone who means it.
    """

    enabled: bool = True
    # Grouping only. There is no pre-trained identity set and no external
    # lookup: every name in the system was typed by the operator.
    suggest_across_libraries: bool = True
    purge_on_disable: bool = False

    @property
    def active(self) -> bool:
        return self.enabled


async def load_face_config(db: AsyncSession) -> FaceConfig:
    row = await db.get(AppSetting, FACES_KEY)
    if row is None:
        return FaceConfig()
    known = set(FaceConfig().__dict__)
    return FaceConfig(**{k: v for k, v in row.value.items() if k in known})


async def save_face_config(db: AsyncSession, config: FaceConfig) -> None:
    await _put(db, FACES_KEY, asdict(config))


AI_EDIT_KEY = "ai_edit"


@dataclass
class AiEditConfig:
    """The AI recipe-picker: Claude looks at a small preview and returns
    slider values; the develop engine renders them locally at full size.

    Off until a key is configured, and even then photographs leave the
    machine only when the operator presses the button - a 768px preview per
    photo, to Anthropic, nothing else and nowhere else. The key is sealed
    like the maps keys: a database dump alone cannot spend it.
    """

    api_key_sealed: str = ""
    model: str = "claude-sonnet-5"
    enabled: bool = True

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.api_key_sealed)

    def api_key(self) -> str:
        if not self.api_key_sealed:
            raise SecretUnavailable("No Anthropic API key is configured")
        return unseal(self.api_key_sealed)

    def with_api_key(self, plaintext: str) -> None:
        self.api_key_sealed = seal(plaintext) if plaintext else ""


async def load_ai_edit_config(db: AsyncSession) -> AiEditConfig:
    row = await db.get(AppSetting, AI_EDIT_KEY)
    if row is None:
        return AiEditConfig()
    known = set(AiEditConfig().__dict__)
    return AiEditConfig(**{k: v for k, v in row.value.items() if k in known})


async def save_ai_edit_config(db: AsyncSession, config: AiEditConfig) -> None:
    await _put(db, AI_EDIT_KEY, asdict(config))
