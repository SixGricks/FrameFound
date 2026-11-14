"""The services of a native install: what each one is and how it starts.

The same roles as docker-compose.yml, one launchd agent each. The lanes keep
the split the queues were given the hard way (processing/tasks.py, top):
fast work never waits behind slow work.

    postgres       the catalogue
    valkey         the job queue
    api            FastAPI (runs the migrations, then serves)
    web            the Next.js app
    caddy          the one address: /api/* to the API, the rest to the web app
    scanner        watches and reconciles the libraries
    scheduler      Celery beat
    worker-fast    thumbnails, posters, metadata
    worker-media   proxies, frame sampling, motion, exports and renders
    worker-vision  CLIP embeddings, faces
    worker-ai      transcription (one at a time)
    model-server   local AI: loads a model when asked, unloads it when idle;
                   the runtime that answers is its child, never a service
    worker-local-ai  local AI's work: downloads, copies, installs, tests and
                   later every question to a model (one at a time, last in
                   line for the CPU)
    backup        hourly check; a new archive when the newest is a day old

launchd runs `python -m framefound.native run <service>`, never the program
itself: the launcher reads framefound.env first, so no password is ever
written into a plist, and one interpreter is the "responsible process" for
every service — a single entry if macOS asks about network volumes.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from framefound.native.layout import DEFAULT_PORTS, HOME_ENV, Layout

LABEL_PREFIX = "com.framefound."

# Homebrew is not on launchd's PATH; Apple silicon first, Intel second.
BASE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# The locale every service is started with, and the one the database cluster
# is created with (database.initialise), so the two cannot drift apart.
# launchd gives an agent no LANG or LC_ALL at all, and without one Postgres
# will not start in the background: on the first real install it logged
# "postmaster became multithreaded during startup" every 15 seconds, while
# the same command worked from a Terminal, which sets LANG itself. macOS
# always has this locale.
LOCALE = "en_US.UTF-8"


@dataclass(frozen=True)
class Service:
    name: str
    summary: str
    # Lower priority for the heavy lanes, so Premiere and Photoshop win any
    # contention for the CPU and the disk.
    nice: int = 0
    # Seconds launchd waits after SIGTERM before SIGKILL. Celery's warm
    # shutdown finishes the task in hand; Postgres waits for its clients.
    exit_timeout_s: int = 20
    # Loopback services that must be answering before this one starts,
    # by the env key holding their port.
    waits_for: tuple[str, ...] = ()
    # Run every N seconds and exit, rather than stay up.
    every_s: int | None = None


DB = ("POSTGRES_PORT",)
DB_AND_QUEUE = ("POSTGRES_PORT", "REDIS_PORT")

SERVICES: tuple[Service, ...] = (
    Service("postgres", "catalogue database", exit_timeout_s=60),
    Service("valkey", "job queue"),
    Service("api", "API", waits_for=DB_AND_QUEUE),
    Service("web", "web app"),
    Service("caddy", "front door"),
    Service("scanner", "library scanner", nice=5, waits_for=DB_AND_QUEUE),
    Service("scheduler", "nightly schedules", nice=5, waits_for=("REDIS_PORT",)),
    Service("worker-fast", "thumbnails and metadata", nice=5, waits_for=DB_AND_QUEUE),
    Service(
        "worker-media",
        "proxies, frames, exports",
        nice=10,
        exit_timeout_s=600,
        waits_for=DB_AND_QUEUE,
    ),
    Service(
        "worker-vision", "embeddings and faces", nice=10, exit_timeout_s=120, waits_for=DB_AND_QUEUE
    ),
    Service("worker-ai", "transcription", nice=10, exit_timeout_s=600, waits_for=DB_AND_QUEUE),
    # Needs no database and is healthy with nothing installed, so it waits
    # for nothing. 40 s: uvicorn gives a request in hand 5 s, then the
    # supervisor gives the runtime 20 s to stop and 5 s more to die; at 30
    # launchd's SIGKILL would land on the last of those.
    Service("model-server", "local AI model server", nice=10, exit_timeout_s=40),
    # The highest nice of any service, so it is the first to give way to
    # Adobe. 120 s: a restart must not wait an hour for a download to end;
    # the job then reads "stopped — start it again", and Resume carries on.
    Service(
        "worker-local-ai", "local AI models", nice=15, exit_timeout_s=120, waits_for=DB_AND_QUEUE
    ),
    Service("backup", "nightly backup", nice=10, waits_for=DB, every_s=3600),
)

# Start order is dependency order; stop order is its reverse, so the
# database's clients are gone before it is asked to shut down.
BY_NAME = {service.name: service for service in SERVICES}
# What reads framefound.env's application settings, and so needs a restart
# when one changes (a new proxy store, say). The model server is one of
# them: a restart unloads its model, which is loaded again on the next
# request.
APP_SERVICES = tuple(
    s.name for s in SERVICES if s.name not in ("postgres", "valkey", "web", "caddy", "backup")
)

# (queues, env key for concurrency, default concurrency)
LANES: dict[str, tuple[str, str | None, int]] = {
    "worker-fast": ("visuals,metadata,default", "FRAMEFOUND_FAST_WORKERS", 3),
    "worker-media": ("media,frames,exports", "FRAMEFOUND_MEDIA_WORKERS", 2),
    "worker-vision": ("vision", "FRAMEFOUND_VISION_WORKERS", 2),
    # Two Whisper models resident is the profile that got this lane killed
    # on the old server; one at a time is a rule, not a setting.
    "worker-ai": ("transcribe", None, 1),
    # One local-AI job at a time is a rule too: one model is loaded at a
    # time, and a second job would swap a model in under a running batch.
    # Nothing else consumes this queue, here or on a remote worker.
    "worker-local-ai": ("local_ai", None, 1),
}

# Sized for a 32 GB Mac that is also running Adobe: enough that pgvector
# search is served from memory, not so much that it competes for it.
POSTGRES_TUNING = (
    "shared_buffers=1GB",
    "effective_cache_size=4GB",
    "work_mem=64MB",
    "maintenance_work_mem=512MB",
)


def label(name: str) -> str:
    return LABEL_PREFIX + name


def port(env: dict[str, str], key: str) -> int:
    return int(env.get(key) or DEFAULT_PORTS[key])


@dataclass
class Launch:
    """One service's start: commands to finish first, then the program that
    replaces the launcher (so launchd supervises the real process)."""

    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    before: list[list[str]] = field(default_factory=list)


def _concurrency(env: dict[str, str], key: str | None, default: int) -> int:
    if key is None:
        return default
    try:
        return max(1, int(env.get(key, "") or default))
    except ValueError:
        return default


def launch(name: str, layout: Layout, env: dict[str, str]) -> Launch:
    """How `name` starts, given the install's settings."""
    bin_ = layout.venv_bin
    server = layout.server_source
    if name == "postgres":
        argv = [
            str(Path(env["FRAMEFOUND_PG_BIN"]) / "postgres"),
            "-D",
            str(layout.pgdata),
            "-p",
            str(port(env, "POSTGRES_PORT")),
            "-c",
            "listen_addresses=127.0.0.1",
            "-c",
            f"unix_socket_directories={layout.run}",
        ]
        for setting in POSTGRES_TUNING:
            argv += ["-c", setting]
        return Launch(argv, layout.home)
    if name == "valkey":
        return Launch(["valkey-server", str(layout.conf / "valkey.conf")], layout.valkey)
    if name == "api":
        return Launch(
            [
                str(bin_ / "uvicorn"),
                "framefound.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port(env, "FRAMEFOUND_API_PORT")),
                # The app decides whose X-Forwarded-For to believe
                # (auth/client_address.py); uvicorn must not pre-empt it.
                "--no-proxy-headers",
            ],
            server,
            # Migrations before serving, as in the api container: a schema
            # mismatch fails the start loudly instead of half-working.
            before=[[str(bin_ / "alembic"), "upgrade", "head"]],
        )
    if name == "web":
        return Launch(
            ["node", str(layout.web / "server.js")],
            layout.web,
            env={
                "PORT": str(port(env, "FRAMEFOUND_WEB_PORT")),
                "HOSTNAME": "127.0.0.1",
                "API_INTERNAL_URL": f"http://127.0.0.1:{port(env, 'FRAMEFOUND_API_PORT')}",
                "NODE_ENV": "production",
            },
        )
    if name == "caddy":
        return Launch(
            ["caddy", "run", "--config", str(layout.conf / "Caddyfile"), "--adapter", "caddyfile"],
            layout.home,
            # Caddy's own state (none to speak of in HTTP mode) stays inside
            # the install folder instead of ~/Library.
            env={"XDG_DATA_HOME": str(layout.run), "XDG_CONFIG_HOME": str(layout.run)},
        )
    if name == "scanner":
        return Launch([str(layout.python), "-m", "framefound.scanner"], server)
    if name == "scheduler":
        return Launch(
            [
                str(bin_ / "celery"),
                "-A",
                "framefound.worker",
                "beat",
                "-s",
                str(layout.run / "celerybeat-schedule"),
            ],
            server,
        )
    if name in LANES:
        queues, key, default = LANES[name]
        return Launch(
            [
                str(bin_ / "celery"),
                "-A",
                "framefound.worker",
                "worker",
                "-Q",
                queues,
                f"--concurrency={_concurrency(env, key, default)}",
                "-n",
                f"{name.removeprefix('worker-')}@%h",
                "--loglevel=INFO",
            ],
            server,
        )
    if name == "model-server":
        return Launch(
            [
                str(bin_ / "uvicorn"),
                "framefound.local_models.server:create_app",
                "--factory",
                # Never beyond this machine: a model answers anyone who can
                # reach it, and its port is not behind Caddy's rules.
                "--host",
                "127.0.0.1",
                "--port",
                str(port(env, "FRAMEFOUND_MODEL_SERVER_PORT")),
                # The server refuses anything not from loopback by the
                # connection's own address; a forwarded header must not
                # stand in for it.
                "--no-proxy-headers",
                # A generation in hand can take minutes. Stopping waits 5 s
                # for it, then the supervisor stops the runtime.
                "--timeout-graceful-shutdown",
                "5",
            ],
            server,
        )
    if name == "backup":
        return Launch([str(layout.python), "-m", "framefound.native", "backup", "--if-due"], server)
    raise KeyError(f"unknown service {name!r}")


def process_environment(layout: Layout, env: dict[str, str]) -> dict[str, str]:
    """What every service's process gets on top of framefound.env."""
    models = env.get("FRAMEFOUND_MODELS_DIR") or str(layout.models)
    return {
        "PATH": f"{layout.venv_bin}:{BASE_PATH}",
        HOME_ENV: str(layout.home),
        # Where HuggingFace and faster-whisper keep downloads, as in the image.
        "HF_HOME": f"{models}/huggingface",
        "XDG_CACHE_HOME": f"{models}/cache",
        # Celery forks its pool. macOS kills a child that touches an
        # Objective-C class its parent was initialising at fork — which ONNX
        # Runtime and CoreML both trigger — unless this is set.
        "OBJC_DISABLE_INITIALIZE_FORK_SAFETY": "YES",
        "NEXT_TELEMETRY_DISABLED": "1",
        # Both: LC_ALL is the one that settles it whatever else is set, LANG
        # is the one programs that read the environment themselves look at.
        "LC_ALL": LOCALE,
        "LANG": LOCALE,
    }


def agent(service: Service, layout: Layout) -> dict[str, Any]:
    """The launchd agent for `service`, as a property list. No secrets: the
    launcher reads them from framefound.env when it starts."""
    plist: dict[str, Any] = {
        "Label": label(service.name),
        "ProgramArguments": [str(layout.python), "-m", "framefound.native", "run", service.name],
        "WorkingDirectory": str(layout.home),
        "EnvironmentVariables": {HOME_ENV: str(layout.home), "PATH": BASE_PATH},
        "RunAtLoad": True,
        "ProcessType": "Standard",
        "Nice": service.nice,
        "ExitTimeOut": service.exit_timeout_s,
        "StandardOutPath": str(layout.logs / f"{service.name}.log"),
        "StandardErrorPath": str(layout.logs / f"{service.name}.log"),
    }
    if service.every_s is not None:
        plist["StartInterval"] = service.every_s
    else:
        plist["KeepAlive"] = True
        # A service that cannot start (the database still coming up, a share
        # not mounted yet) retries on this beat rather than spinning.
        plist["ThrottleInterval"] = 15
    if service.nice:
        plist["LowPriorityBackgroundIO"] = True
    return plist
