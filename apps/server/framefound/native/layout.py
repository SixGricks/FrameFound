"""Where a native install keeps things.

One folder, `~/FrameFound` (or `$FRAMEFOUND_HOME`), so backing the install
up, moving it or removing it is a matter of one path:

    framefound.env   every setting and secret (mode 600)
    conf/            generated Caddyfile and valkey.conf
    pgdata/          the Postgres cluster — the catalogue
    valkey/          the queue's append-only file
    data/            derived files: thumbnails, frames, transcripts, backups
    models/          downloaded model weights
    runtimes/        the software that runs local AI models, one folder per
                     version (FRAMEFOUND_RUNTIMES_DIR; local_models/runtimes.py)
    web/             the built web app
    run/             sockets and scheduler state
    logs/            one log per service

The bulky kinds are what an operator keeps elsewhere (a NAS share, an
external drive): video proxies, slideshow videos, listing exports, basemaps
and backup copies. `framefound install --store <folder>` gives them one
folder (`FRAMEFOUND_BULK_DIR`), and each can be moved on its own afterwards
(storage/stores.py; `framefound storage`, or the Storage page).
The source checkout is wherever this package was installed from.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import framefound

HOME_ENV = "FRAMEFOUND_HOME"

# Loopback ports for the pieces nobody talks to directly. Deliberately not
# the defaults (5432, 6379, 8000, 3000): a Mac that is also a workstation
# may already have a Postgres, a Redis or a dev server on those.
DEFAULT_PORTS = {
    "FRAMEFOUND_HTTP_PORT": "8080",  # the one address people and panels use
    "FRAMEFOUND_API_PORT": "54801",
    "FRAMEFOUND_WEB_PORT": "54802",
    "POSTGRES_PORT": "54803",
    "REDIS_PORT": "54804",
    # The local AI model server, and the runtime it starts. Both answer on
    # 127.0.0.1 only, and only FrameFound's own services talk to them.
    "FRAMEFOUND_MODEL_SERVER_PORT": "54805",
    "FRAMEFOUND_MODEL_RUNTIME_PORT": "54806",
}


@dataclass(frozen=True)
class Layout:
    home: Path
    source: Path  # the repository checkout

    @classmethod
    def default(cls) -> "Layout":
        home = Path(os.environ.get(HOME_ENV) or Path.home() / "FrameFound")
        # An editable install: apps/server/framefound/__init__.py -> repo root.
        source = Path(framefound.__file__).resolve().parents[3]
        return cls(home=home, source=source)

    @property
    def env_file(self) -> Path:
        return self.home / "framefound.env"

    @property
    def conf(self) -> Path:
        return self.home / "conf"

    @property
    def pgdata(self) -> Path:
        return self.home / "pgdata"

    @property
    def valkey(self) -> Path:
        return self.home / "valkey"

    @property
    def data(self) -> Path:
        return self.home / "data"

    @property
    def models(self) -> Path:
        return self.home / "models"

    @property
    def runtimes(self) -> Path:
        return self.home / "runtimes"

    @property
    def web(self) -> Path:
        return self.home / "web"

    @property
    def run(self) -> Path:
        return self.home / "run"

    @property
    def logs(self) -> Path:
        return self.home / "logs"

    @property
    def venv_bin(self) -> Path:
        return self.home / "venv" / "bin"

    @property
    def python(self) -> Path:
        return self.venv_bin / "python"

    @property
    def server_source(self) -> Path:
        return self.source / "apps" / "server"

    @property
    def web_source(self) -> Path:
        return self.source / "apps" / "web"

    def folders(self) -> tuple[Path, ...]:
        """Everything `install` creates (the venv is the bootstrap's)."""
        return (
            self.home,
            self.conf,
            self.valkey,
            self.data,
            self.models,
            self.runtimes,
            self.run,
            self.logs,
        )
