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
import shlex
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
# Finding a tool proves something only when it is found where it will be run
# from. Two ways that went wrong, or could:
#
# - Every tool is looked for on the PATH the background services are given
#   (services.BASE_PATH), never this Terminal's. A Terminal often has a node
#   of its own (nvm, fnm, volta) or an ffmpeg from conda or MacPorts that
#   launchd's agents cannot see: counted as installed, Homebrew's would never
#   arrive, the web service could not start, and the install would still end
#   on "FrameFound is running" because only the API is asked.
# - A program macOS has a namesake of in /usr/bin that will not do is on that
#   PATH too, so for these only Homebrew's own copy counts. The first real
#   install never installed Homebrew's rsync, because `which rsync` always
#   answers on a Mac. rsync is the only one: macOS ships no valkey-server,
#   caddy, node, npm, ffmpeg, ffprobe or exiftool.
SHADOWED_BY_MACOS = frozenset({"rsync"})
BREW_PREFIXES = (Path("/opt/homebrew"), Path("/usr/local"))
# The PATH of the shell the operator is typing in, as the bootstrap script
# or the command itself (cli.main) found it, before Homebrew was put in
# front of it. It decides whether `framefound` typed on its own will be
# found (typed_command).
SHELL_PATH_ENV = "FRAMEFOUND_SHELL_PATH"
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


def homebrew_program(program: str) -> str | None:
    """Homebrew's own copy of `program` by full path, or None. Never the
    system's: nothing under a Homebrew prefix came with macOS."""
    for prefix in BREW_PREFIXES:
        candidate = prefix / "bin" / program
        if candidate.exists():
            return str(candidate)
    return None


def rsync() -> str:
    """Homebrew's rsync by full path where there is one: the system's
    (openrsync) is the one a bare `rsync` may find first."""
    return homebrew_program("rsync") or "rsync"


def with_homebrew_on_path(path: str) -> str:
    """`path` with Homebrew's bin folders in front, for a shell that was
    opened before Homebrew was set up."""
    parts = path.split(os.pathsep) if path else []
    for prefix in reversed(BREW_PREFIXES):
        folder = str(prefix / "bin")
        if folder not in parts and Path(folder).is_dir():
            parts.insert(0, folder)
    return os.pathsep.join(parts)


def service_program(program: str) -> str | None:
    """`program` as a background service would find it, or None: looked for
    on the PATH launchd's agents are given, not the one this was typed in."""
    return shutil.which(program, path=services.BASE_PATH)


def missing_formulae(
    which: Callable[[str], str | None] = service_program,
    homebrew: Callable[[str], str | None] = homebrew_program,
) -> list[str]:
    """Formulae to install, in a stable order, for the programs not found.

    A program macOS has its own version of is looked for under Homebrew
    only (`homebrew`); the rest where the services will look (`which`)."""
    needed: list[str] = []
    for program, formula in TOOLS.items():
        found = homebrew(program) if program in SHADOWED_BY_MACOS else which(program)
        if found is None and formula not in needed:
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
        raise InstallError(
            f"Still not found after Homebrew installed them: {' '.join(still_missing)}"
        )
    return pg_bin


def ensure_env(layout: Layout, pg_bin: Path) -> dict[str, str]:
    """Create framefound.env, or add any setting a newer release introduced.
    Nothing already in the file is changed."""
    template = envfile.initial(layout, str(pg_bin))
    if layout.env_file.exists():
        text = layout.env_file.read_text(encoding="utf-8")
        envfile.write(layout.env_file, envfile.added(text, template))
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
    """Put `framefound` on PATH (Homebrew's bin is the user's to write).
    Written whole each time, so a second run leaves the same file."""
    shim = prefix / "bin" / "framefound"
    shim.write_text(
        f'#!/bin/sh\n# FrameFound (docs/mac.md)\nexec "{layout.python}" -m framefound.native "$@"\n'
    )
    shim.chmod(0o755)
    return shim


def typed_command(layout: Layout) -> str:
    """What to type to run FrameFound's command, as things are right now.

    `framefound` once the command is in Homebrew's bin and the operator's
    shell looks there; its full path when the shell does not (a Terminal
    opened before Homebrew was put on PATH); and the interpreter itself
    while there is no command at all. A message that tells someone what to
    run next uses this, so it never names a command they do not have.

    The shell's PATH is the one recorded before Homebrew was put in front
    of it: by the bootstrap script, or by `cli.main` for every command typed
    afterwards. This process's own PATH always has Homebrew's bin on it by
    then, so it is only what is left to go on when neither has run."""
    try:
        shim = brew_prefix() / "bin" / "framefound"
    except InstallError:
        shim = None
    if shim is None or not shim.is_file():
        return f"{shlex.quote(str(layout.python))} -m framefound.native"
    shell_path = os.environ.get(SHELL_PATH_ENV) or os.environ.get("PATH", "")
    if str(shim.parent) in (folder.rstrip("/") for folder in shell_path.split(os.pathsep)):
        return "framefound"
    return shlex.quote(str(shim))


def place_command(layout: Layout, say: Say) -> None:
    """Put `framefound` on PATH now, before any step that can fail for a
    reason the command helps with. Says something only when typing the bare
    name will not work.

    Not being able to write it is no reason to stop: everything else
    installs without it, and every later message names what does work."""
    try:
        shim = install_command(layout, brew_prefix())
    except OSError as err:
        say(
            f"The framefound command could not be put in Homebrew's bin folder ({err}). "
            f"Until that is put right, type this in its place: {typed_command(layout)}"
        )
        return
    typed = typed_command(layout)
    if typed != "framefound":
        say(
            f"The framefound command is in {shim.parent}, where this Terminal does not "
            f"look. Type {typed}, or open a new Terminal window once Homebrew is on "
            'your PATH (https://brew.sh, "Next steps").'
        )


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
        raise InstallError(
            f"Postgres did not start — see {layout.logs / 'postgres.log'}, "
            f"then: {typed_command(layout)} doctor"
        )
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
    # As soon as it can work: the venv, the tools and the settings file are
    # all there. On the first real install this was the last step, Postgres
    # failed before it, and the message said to run a command that did not
    # exist yet.
    place_command(layout, say)
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
            f"The API did not come up — see {layout.logs / 'api.log'}, "
            f"then: {typed_command(layout)} doctor"
        )
    return env
