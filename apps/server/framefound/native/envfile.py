"""The install's one settings file, `~/FrameFound/framefound.env`.

Plain `KEY=value` lines, read literally: nothing is expanded or executed,
so a password with a `$` or a path with spaces is exactly what was written.
The same format the containers' `.env` uses, which is what lets a migration
carry values across.

Updates keep the operator's file: comments, order and any line they added
stay put; only the named keys change. Written through a temporary file and
renamed, mode 600, because it holds the database password and the key that
unseals every stored secret.
"""

import os
import secrets
from pathlib import Path

from framefound.native.layout import DEFAULT_PORTS, Layout


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        key, value = _split(line)
        if key is not None:
            values[key] = value
    return values


def read(path: Path) -> dict[str, str]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _split(line: str) -> tuple[str | None, str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None, ""
    key, _, value = stripped.partition("=")
    key = key.strip()
    if not key.replace("_", "").isalnum():
        return None, ""
    return key, value.strip()


def merged(text: str, values: dict[str, str], *, only_missing: bool = False) -> str:
    """`text` with `values` applied: existing keys rewritten in place (or left
    alone with `only_missing`), new keys appended."""
    remaining = dict(values)
    lines: list[str] = []
    for line in text.splitlines():
        key, _current = _split(line)
        if key is not None and key in remaining:
            value = remaining.pop(key)
            lines.append(line if only_missing else f"{key}={value}")
        else:
            lines.append(line)
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(lines) + "\n"


def added(text: str, template: str) -> str:
    """`text` with every setting of `template` it lacks appended, each with
    the comment written above it in the template.

    What `install` does to an existing file when a release introduces a
    setting. The comments come too because a bare line tells the operator
    nothing: `FRAMEFOUND_RUNTIMES_DIR=…` alone does not say that it is what
    turns the AI models page on. Nothing already in `text` changes, and a
    key that is there blank stays blank."""
    present = parse(text)
    blocks: list[list[str]] = []
    comments: list[str] = []
    # Whether the template's previous line was a key just taken, so that
    # neighbouring new keys (two ports) stay one block.
    joined = False
    for line in template.splitlines():
        key, _value = _split(line)
        if key is None:
            if line.strip().startswith("#"):
                comments.append(line)
            else:
                comments, joined = [], False
            continue
        if key in present:
            comments, joined = [], False
            continue
        if joined:
            blocks[-1].extend([*comments, line])
        else:
            blocks.append([*comments, line])
        comments, joined = [], True
    if not blocks:
        return text
    kept = text.rstrip("\n")
    return "\n\n".join(([kept] if kept else []) + ["\n".join(block) for block in blocks]) + "\n"


def update(path: Path, values: dict[str, str], *, only_missing: bool = False) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    write(path, merged(text, values, only_missing=only_missing))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    # Created 600 from the first byte: never world-readable, even briefly.
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(partial, path)


def initial(layout: Layout, pg_bin: str) -> str:
    """A fresh install's file, with generated secrets. `install` merges this
    into an existing file with `only_missing`, so re-running it never
    replaces a secret or a choice already made."""
    ports = DEFAULT_PORTS
    return f"""# FrameFound on this Mac — every setting in one place (docs/mac.md).
# Plain KEY=value lines, taken literally: no quotes, no $VARIABLES.
# After changing anything here:  framefound restart

# ---- Who and where ----
FRAMEFOUND_SERVER_NAME=FrameFound
# The address people, the Premiere/Lightroom panels and Claude use:
#   http://<this Mac>:{ports["FRAMEFOUND_HTTP_PORT"]}
FRAMEFOUND_HTTP_PORT={ports["FRAMEFOUND_HTTP_PORT"]}

# ---- Files ----
# What every page reads: thumbnails, frames, transcripts, backups.
FRAMEFOUND_DATA_DIR={layout.data}
# One folder for everything bulky (a folder on a NAS share): video proxies,
# slideshow videos, listing exports, basemaps and backup copies, each in a
# subfolder of its own. Set by:  framefound install --store <folder>
# Do not change it by editing this line. The folder is also recorded in
# <data>/.framefound-stores.json, and that record is what FrameFound
# follows: an edit here changes nothing, restart or not. Change it with
# `framefound install --store <folder>` or on the Storage page. The same
# record holds any kind moved somewhere of its own (the Storage page, or
# `framefound storage move`), and wins over these lines for that too.
FRAMEFOUND_BULK_DIR=
# Video proxies alone, when they are to live somewhere other than the bulk
# folder. Set and changed by:  framefound proxies move <folder>
FRAMEFOUND_PROXY_DIR=
FRAMEFOUND_MODELS_DIR={layout.models}
# The software that runs AI models on this Mac, one folder per version,
# installed and updated from the AI models page. This line is what turns
# that page on.
FRAMEFOUND_RUNTIMES_DIR={layout.runtimes}
# Libraries must be folders under this. /Volumes covers every mounted share
# and external drive.
FRAMEFOUND_MEDIA_ROOT=/Volumes
# Generation pauses below this much free space on a store.
FRAMEFOUND_MIN_FREE_GB=20

# ---- Backups ----
# Nightly, into <data>/backups; the newest this many are kept.
FRAMEFOUND_BACKUP_KEEP=14
# A second copy off this Mac. Empty: the bulk folder's `backups` when there
# is a bulk folder, otherwise none.
FRAMEFOUND_BACKUP_COPY_DIR=

# ---- How hard it works ----
# Worker processes per lane. The workers run below Adobe in priority; lower
# these if editing still stutters during a big backfill.
FRAMEFOUND_FAST_WORKERS=3
FRAMEFOUND_MEDIA_WORKERS=2
FRAMEFOUND_VISION_WORKERS=2
# 0 lets FFmpeg use every core.
FRAMEFOUND_FFMPEG_THREADS=0
FRAMEFOUND_WHISPER_THREADS=6
FRAMEFOUND_WHISPER_MODEL=small
# cpu | coreml. Turn coreml on only after `framefound doctor --parity` passes.
FRAMEFOUND_ONNX_PROVIDER=cpu

# ---- Secrets (generated; a migration replaces the first with the old server's) ----
FRAMEFOUND_SECRET_KEY={secrets.token_hex(32)}
# One-time: creates the first admin on a fresh install. Unused after a migration.
FRAMEFOUND_SETUP_TOKEN={secrets.token_urlsafe(18)}
POSTGRES_PASSWORD={secrets.token_hex(24)}
REDIS_PASSWORD={secrets.token_hex(24)}

# ---- Plumbing (loopback only; change a port only if something else uses it) ----
FRAMEFOUND_API_PORT={ports["FRAMEFOUND_API_PORT"]}
FRAMEFOUND_WEB_PORT={ports["FRAMEFOUND_WEB_PORT"]}
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT={ports["POSTGRES_PORT"]}
POSTGRES_USER=framefound
POSTGRES_DB=framefound
REDIS_HOST=127.0.0.1
REDIS_PORT={ports["REDIS_PORT"]}
# The local AI model server, and the model it runs (only the server talks to that).
FRAMEFOUND_MODEL_SERVER_PORT={ports["FRAMEFOUND_MODEL_SERVER_PORT"]}
FRAMEFOUND_MODEL_RUNTIME_PORT={ports["FRAMEFOUND_MODEL_RUNTIME_PORT"]}
# Caddy is the only proxy, and it is on this machine.
FRAMEFOUND_TRUSTED_PROXIES=127.0.0.1/32
# Homebrew's Postgres with pgvector, found at install time.
FRAMEFOUND_PG_BIN={pg_bin}
"""
