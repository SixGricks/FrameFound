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
