"""The Premiere panel as a `.ccx`, the file Creative Cloud installs.

    framefound panel   ->   ~/FrameFound/premiere-panel/FrameFound-0.4.1.ccx

A `.ccx` is a zip of the plugin's own files with `manifest.json` at its
root. Double-clicked on the Mac, Creative Cloud installs it and the panel
appears in Premiere under Window > UXP Plugins: no UXP Developer Tool and no
developer mode. `framefound install` (which every `framefound update` ends
in) makes it when the panel's version has none yet, and says so.

The panel's files are the three in apps/panel-premiere and any icon its
manifest names; never the dev install scripts beside them, which UXP would
read looking for a manifest entry.
"""

import json
import os
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from framefound.native.layout import Layout

Say = Callable[[str], None]

FILES = ("manifest.json", "index.html", "main.js")
# Every entry gets this time, so the same files make the same .ccx byte for
# byte and a rebuild of an unchanged panel changes nothing.
STAMP = (2026, 1, 1, 0, 0, 0)


class PanelError(RuntimeError):
    """The panel cannot be packaged. Message safe to show."""


def source(layout: Layout) -> Path:
    return layout.source / "apps" / "panel-premiere"


def folder(layout: Layout) -> Path:
    return layout.home / "premiere-panel"


def read_manifest(src: Path) -> dict[str, Any]:
    """The panel's manifest, checked for what made Creative Cloud refuse it.

    "host" written as a list, which the UXP Developer Tool takes, is refused
    by Creative Cloud with "Couldn't install plugin… Error code -4" (it cannot
    read the manifest). One host is an object."""
    try:
        manifest = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise PanelError(f"The Premiere panel's manifest cannot be read: {err}") from err
    if not isinstance(manifest, dict):
        raise PanelError("The Premiere panel's manifest is not a JSON object.")
    host = manifest.get("host")
    if not isinstance(host, dict) or host.get("app") != "premierepro":
        raise PanelError(
            'The Premiere panel\'s manifest must name one host as an object, {"app": '
            '"premierepro", ...}; Creative Cloud cannot install it otherwise (error -4).'
        )
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise PanelError("The Premiere panel's manifest has no version.")
    return manifest


def _icons(manifest: dict[str, Any]) -> list[str]:
    """Every icon path the manifest names, the plugin's own and its panels'."""
    found: list[str] = []
    for owner in [manifest, *(manifest.get("entrypoints") or [])]:
        if not isinstance(owner, dict):
            continue
        for icon in owner.get("icons") or []:
            path = icon.get("path") if isinstance(icon, dict) else None
            if isinstance(path, str) and path:
                found.append(path)
    return found


def contents(src: Path, manifest: dict[str, Any]) -> list[str]:
    """The files that go in the .ccx, as paths inside it. A file the
    manifest names that is not there stops the build: UXP reports a missing
    asset as an unrelated-looking error (docs/panels.md)."""
    wanted = [*FILES, *(path for path in _icons(manifest) if path not in FILES)]
    main = manifest.get("main")
    if isinstance(main, str) and main not in wanted:
        wanted.append(main)
    missing = [path for path in wanted if not (src / path).is_file()]
    if missing:
        raise PanelError(f"The Premiere panel names files that are not there: {', '.join(missing)}")
    for path in wanted:
        if Path(path).is_absolute() or ".." in Path(path).parts:
            raise PanelError(f"The Premiere panel names a file outside its folder: {path}")
    return wanted


def ccx_path(layout: Layout, version: str) -> Path:
    return folder(layout) / f"FrameFound-{version}.ccx"


def build(layout: Layout) -> Path:
    """Write the .ccx for the panel in the checkout. Returns its path."""
    src = source(layout)
    manifest = read_manifest(src)
    names = contents(src, manifest)
    target = ccx_path(layout, manifest["version"].strip())
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".partial")
    with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as ccx:
        for name in names:
            entry = zipfile.ZipInfo(name, date_time=STAMP)
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            ccx.writestr(entry, (src / name).read_bytes())
    os.replace(partial, target)
    return target


def ensure(layout: Layout, say: Say) -> Path | None:
    """On install and update: make the .ccx when this panel version has none,
    and say what to do with it. A version already built is left alone and
    nothing is said. A panel that cannot be packaged is said and the install
    goes on: FrameFound itself does not need it."""
    try:
        version = read_manifest(source(layout))["version"].strip()
        target = ccx_path(layout, version)
        if target.is_file():
            return None
        earlier = [found for found in folder(layout).glob("FrameFound-*.ccx") if found != target]
        built = build(layout)
    except (PanelError, OSError) as err:
        say(f"The Premiere panel was not packaged: {err}")
        return None
    if earlier:
        say(f"Premiere panel {version} is ready: {built}. Double-click it to update the panel.")
    else:
        say(
            f"Premiere panel {version}: {built}. Double-click it to install it, then find it "
            "in Premiere under Window > UXP Plugins > FrameFound."
        )
    return built
