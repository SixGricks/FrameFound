"""`framefound install`: from a checkout and a venv to a running FrameFound.

Safe to run again — after a failed first attempt, or any time something
looks off. Each step makes what is missing and leaves what is there: an
existing framefound.env keeps every value, an existing database cluster is
never re-initialised, agents already running are left running.

The bootstrap script (infrastructure/mac/install.sh) only does what has to
happen before Python can: Homebrew's uv, a venv, `pip install`. Everything
else is here, where it can be tested.
"""

import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from framefound.native import database, envfile, proc, services
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout

Say = Callable[[str], None]

# program -> the Homebrew formula that provides it
TOOLS = {
    "valkey-server": "valkey",
    "caddy": "caddy",
    "node": "node",
    "npm": "node",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffmpeg",
    "exiftool": "exiftool",
    # macOS ships openrsync (or rsync 2.6.9), which lacks the filters and
    # progress output the proxy copy and the migration rely on.
    "rsync": "rsync",
}
BREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))
# macOS privacy controls keep background processes out of these, so neither
# the checkout nor the install folder may live in one.
PROTECTED_FOLDERS = ("Desktop", "Documents", "Downloads")


class InstallError(RuntimeError):
    """Something the operator has to fix; the message says what."""


def check_platform(layout: Layout, platform: str = sys.platform) -> None:
    if platform != "darwin":
        raise InstallError(
            "The native install is for macOS. On Linux use the containers "
            "(infrastructure/scripts/install.sh)."
        )
    home = Path.home()
    for folder in PROTECTED_FOLDERS:
        protected = home / folder
        for path, what in ((layout.source, "source checkout"), (layout.home, "install folder")):
            if path == protected or protected in path.parents:
                raise InstallError(
                    f"The {what} is in {protected}, which macOS keeps background "
                    f"services out of. Move it (for example to {home / 'FrameFound'})."
                )


def brew_prefix() -> Path:
    for prefix in BREW_PREFIXES:
        if (prefix / "bin" / "brew").exists():
            return prefix
    raise InstallError("Homebrew is not installed. Install it from https://brew.sh and run again.")


def rsync() -> str:
    """Homebrew's rsync by full path where there is one: the system's
    (openrsync) is the one a bare `rsync` may find first."""
    for prefix in BREW_PREFIXES:
        candidate = prefix / "bin" / "rsync"
        if candidate.exists():
            return str(candidate)
    return "rsync"


def with_homebrew_on_path(path: str) -> str:
    """`path` with Homebrew's bin folders in front, for a shell that was
    opened before Homebrew was set up."""
    parts = path.split(os.pathsep) if path else []
    for prefix in reversed(BREW_PREFIXES):
        folder = str(prefix / "bin")
        if folder not in parts and Path(folder).is_dir():
            parts.insert(0, folder)
    return os.pathsep.join(parts)


def missing_formulae(which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """Formulae to install, in a stable order, for the programs not found."""
    needed: list[str] = []
    for program, formula in TOOLS.items():
        if which(program) is None and formula not in needed:
            needed.append(formula)
    return needed


def postgres_majors_with_vector(prefix: Path) -> list[int]:
    """Homebrew Postgres major versions that pgvector's bottle was built for."""
    majors = []
    for control in prefix.glob("share/postgresql@*/extension/vector.control"):
        found = re.search(r"postgresql@(\d+)", str(control))
        if found:
            majors.append(int(found.group(1)))
    return sorted(set(majors))


def find_pg_bin(prefix: Path) -> Path | None:
    """The bin folder of a Homebrew Postgres that has pgvector, or None.

    The oldest such version: FrameFound was built on 16, and a restore from
    the old server goes up a version or two either way, so there is nothing
    to gain from the newest and a little to lose."""
    for major in postgres_majors_with_vector(prefix):
        bin_ = prefix / "opt" / f"postgresql@{major}" / "bin"
        if (bin_ / "postgres").exists():
            return bin_
    return None


def ensure_tools(say: Say) -> Path:
    """Install what is missing with Homebrew. Returns the Postgres bin folder."""
    prefix = brew_prefix()
    brew = str(prefix / "bin" / "brew")
    formulae = missing_formulae()
    if not postgres_majors_with_vector(prefix):
        formulae.append("pgvector")
    if formulae:
        say(f"Installing with Homebrew: {' '.join(formulae)}")
        proc.run([brew, "install", *formulae])
    pg_bin = find_pg_bin(prefix)
    if pg_bin is None:
        majors = postgres_majors_with_vector(prefix)
        if not majors:
            raise InstallError(
                "Homebrew's pgvector did not install an extension for any Postgres "
                "version. Run `brew reinstall pgvector` and try again."
            )
        say(f"Installing with Homebrew: postgresql@{majors[0]}")
        proc.run([brew, "install", f"postgresql@{majors[0]}"])
        pg_bin = find_pg_bin(prefix)
    if pg_bin is None:
        raise InstallError("Could not find a Homebrew Postgres with pgvector after installing one.")
    still_missing = missing_formulae()
    if still_missing:
        raise InstallError(f"Still not on PATH after installing: {' '.join(still_missing)}")
    return pg_bin


def ensure_env(layout: Layout, pg_bin: Path) -> dict[str, str]:
    """Create framefound.env, or add any setting a newer release introduced.
    Nothing already in the file is changed."""
    template = envfile.initial(layout, str(pg_bin))
    if layout.env_file.exists():
        envfile.update(layout.env_file, envfile.parse(template), only_missing=True)
    else:
        envfile.write(layout.env_file, template)
    os.chmod(layout.env_file, 0o600)
    return envfile.read(layout.env_file)


def valkey_conf(layout: Layout, env: dict[str, str]) -> str:
    return (
        "# Generated by `framefound install`; edit framefound.env instead.\n"
        "bind 127.0.0.1\n"
        "protected-mode yes\n"
        f"port {services.port(env, 'REDIS_PORT')}\n"
        f'dir "{layout.valkey}"\n'
        "appendonly yes\n"
        "maxmemory 1gb\n"
        # The queue must refuse new jobs rather than silently drop old ones.
        "maxmemory-policy noeviction\n"
        f"requirepass {env['REDIS_PASSWORD']}\n"
    )


# Nothing the API serves is a page to run; should a stored file ever come
# back as HTML, it renders inert (the same header the containers' Caddy sets).
API_CSP = "default-src 'none'; frame-ancestors 'none'; sandbox"


def caddyfile(env: dict[str, str]) -> str:
    """The front door. The same rules as infrastructure/caddy/Caddyfile's
    local mode: only /api/* reaches the API, nothing is served from disk,
    and nobody outside can claim to have come through a tunnel."""
    api = services.port(env, "FRAMEFOUND_API_PORT")
    web = services.port(env, "FRAMEFOUND_WEB_PORT")
    http = services.port(env, "FRAMEFOUND_HTTP_PORT")
    return f"""# Generated by `framefound install`; edit framefound.env instead.
{{
	admin off
	auto_https off
}}

:{http} {{
	header {{
		X-Content-Type-Options "nosniff"
		X-Frame-Options "DENY"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}}
	encode gzip zstd
	request_body {{
		max_size 100MB
	}}
	request_header -X-FrameFound-Ingress
	request_header -X-FrameFound-Client

	handle /api/* {{
		@not_docs not path /api/docs* /api/redoc* /api/openapi.json
		header @not_docs Content-Security-Policy "{API_CSP}"
		reverse_proxy 127.0.0.1:{api}
	}}
	handle {{
		reverse_proxy 127.0.0.1:{web}
	}}
}}
"""


def write_configs(layout: Layout, env: dict[str, str]) -> None:
    envfile.write(layout.conf / "valkey.conf", valkey_conf(layout, env))  # 600: has the password
    (layout.conf / "Caddyfile").write_text(caddyfile(env), encoding="utf-8")


def build_web(layout: Layout, say: Say) -> None:
    """Build the web app and put the standalone server in `web/`.

    Assembled beside the live copy and swapped in, so a failed build leaves
    the running app as it was."""
    source = layout.web_source
    say("Building the web app (a minute or two)")
    env = {**os.environ, "NEXT_TELEMETRY_DISABLED": "1"}
    proc.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=source, env=env)
    proc.run(["npm", "run", "build"], cwd=source, env=env)
    built = source / ".next" / "standalone"
    if not (built / "server.js").is_file():
        raise InstallError(f"The web build produced no server at {built / 'server.js'}")
    fresh = layout.home / "web.new"
    shutil.rmtree(fresh, ignore_errors=True)
    shutil.copytree(built, fresh, symlinks=True)
    shutil.copytree(source / ".next" / "static", fresh / ".next" / "static", dirs_exist_ok=True)
    if (source / "public").is_dir():
        shutil.copytree(source / "public", fresh / "public", dirs_exist_ok=True)
    old = layout.home / "web.old"
    shutil.rmtree(old, ignore_errors=True)
    if layout.web.exists():
        layout.web.rename(old)
    fresh.rename(layout.web)
    shutil.rmtree(old, ignore_errors=True)


def install_command(layout: Layout, prefix: Path) -> Path:
    """Put `framefound` on PATH (Homebrew's bin is the user's to write)."""
    shim = prefix / "bin" / "framefound"
    shim.write_text(
        f'#!/bin/sh\n# FrameFound (docs/mac.md)\nexec "{layout.python}" -m framefound.native "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def wait_for_api(env: dict[str, str], seconds: float = 180) -> bool:
    """True once the API answers its health check (the first start runs
    every migration first, so this can take a while)."""
    url = f"http://127.0.0.1:{services.port(env, 'FRAMEFOUND_API_PORT')}/healthz"
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310 - loopback
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def start_all(layout: Layout, env: dict[str, str], launchd: Launchd, say: Say) -> None:
    """Write every agent and start it, the database first."""
    for folder in layout.folders():
        folder.mkdir(parents=True, exist_ok=True)
    for service in services.SERVICES:
        launchd.write(service)
    launchd.start("postgres")
    if not proc.wait_for_port(services.port(env, "POSTGRES_PORT"), 60):
        raise InstallError(f"Postgres did not start — see {layout.logs / 'postgres.log'}")
    if not database.exists(env):
        say("Creating the database")
        database.create(env)
    for service in services.SERVICES:
        launchd.start(service.name)


def stop_all(launchd: Launchd, names: tuple[str, ...] | None = None) -> None:
    """Stop in reverse start order, so the database outlives its clients."""
    wanted = names if names is not None else tuple(s.name for s in services.SERVICES)
    for service in reversed(services.SERVICES):
        if service.name in wanted:
            launchd.stop(service.name)


def install(layout: Layout, launchd: Launchd, say: Say) -> dict[str, str]:
    check_platform(layout)
    for folder in layout.folders():
        folder.mkdir(parents=True, exist_ok=True)
    os.chmod(layout.home, 0o700)
    pg_bin = ensure_tools(say)
    env = ensure_env(layout, pg_bin)
    if database.initialise(layout, env):
        say("Created the database cluster")
    write_configs(layout, env)
    build_web(layout, say)
    # A second run replaces the web build and may change an agent: stop what
    # is running (nothing, the first time) so everything starts from what is
    # on disk now.
    stop_all(launchd)
    say("Starting the services")
    start_all(layout, env, launchd, say)
    if not wait_for_api(env):
        raise InstallError(
            f"The API did not come up — see {layout.logs / 'api.log'}, then: framefound doctor"
        )
    install_command(layout, brew_prefix())
    return env
