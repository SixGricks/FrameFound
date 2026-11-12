"""Can this machine run FrameFound work? Checked before any worker starts.

    python -m framefound.ops.worker_check            # everything but the models
    python -m framefound.ops.worker_check --parity   # also CoreML vs CPU vectors

Run on a worker that is not the server (docs/remote-workers.md), before
starting its workers. The native Mac install's `framefound doctor`
(native/doctor.py) is built from the same checks. Each line is one thing the
workers will need, said in terms of what to fix:

- the database and queue answer (the SSH tunnel is up), and the database is
  at the schema this checkout expects — a worker on older code would write
  rows the server no longer understands, or miss columns it now needs;
- the shared data store is mounted (its marker is there) and writable;
- every enabled library has a `root_map` entry and its folder is reachable;
- ffmpeg, ffprobe and exiftool are on PATH, and whether proxies will use
  the media engine;
- which ONNX provider the vision models will get. `--parity` embeds one
  image on the CPU and on CoreML and compares: every existing vector came
  from the CPU, and search compares new vectors against them, so a provider
  that drifts would quietly degrade search and every learned tag.

Nothing is written except a probe file in the data store, deleted at once.
"""

import argparse
import asyncio
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from framefound.config import Settings, get_settings
from framefound.storage.reachability import root_state
from framefound.storage.worker_roots import (
    UnmappedRoot,
    data_store_problem,
    local_root,
    parse_root_map,
)

# Cosine similarity between CPU and CoreML vectors of the same image below
# which CoreML is refused. Re-running the CPU on the same image gives 1.0;
# fp16 inference typically lands above 0.999. 0.995 still ranks the same
# neighbours first while catching a provider that changed the model.
PARITY_FLOOR = 0.995

TOOLS = ("ffmpeg", "ffprobe", "exiftool")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    # A failed fatal check stops the workers starting; the rest are advice.
    fatal: bool = True


def check_settings(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    try:
        mappings = parse_root_map(settings.root_map)
    except ValueError as err:
        return [Check("root map", False, str(err))]
    if not mappings:
        checks.append(
            Check(
                "root map",
                False,
                "FRAMEFOUND_ROOT_MAP is empty, so this process would act as the server: "
                "it could flag files missing and write to a local data folder.",
            )
        )
    else:
        pairs = ", ".join(f"{m.server} → {m.local}" for m in mappings)
        checks.append(Check("root map", True, pairs))
    checks.append(
        Check(
            "worker name",
            bool(settings.worker_name.strip()),
            settings.worker_name or "FRAMEFOUND_WORKER_NAME is empty; jobs will read as the server",
            fatal=False,
        )
    )
    return checks


def check_data_store(settings: Settings) -> list[Check]:
    problem = data_store_problem(settings.data_dir, settings)
    if problem is not None:
        return [Check("data store", False, problem)]
    try:
        with tempfile.NamedTemporaryFile(dir=settings.data_dir, prefix=".worker-check-"):
            pass
    except OSError as err:
        return [Check("data store", False, f"{settings.data_dir} is not writable here ({err})")]
    return [Check("data store", True, f"{settings.data_dir} is the server's store and writable")]


def check_tools() -> list[Check]:
    from framefound.processing import ffmpeg

    checks = [
        Check(tool, shutil.which(tool) is not None, shutil.which(tool) or "not on PATH")
        for tool in TOOLS
    ]
    if ffmpeg.videotoolbox_available():
        chosen = ffmpeg.videotoolbox_proxy_args(1080)
        detail = (
            f"media engine: {' '.join(chosen)}"
            if chosen
            else "media engine available, but videotoolbox_proxy_args() is not set: libx264"
        )
    else:
        detail = "libx264 on the CPU (no VideoToolbox here)"
    checks.append(Check("proxy encoder", True, detail, fatal=False))
    return checks


async def check_services(settings: Settings, *, libraries_fatal: bool = True) -> list[Check]:
    """Database, schema version, queue and every library's folder.

    A library this machine cannot see stops a remote worker (it would only
    hand every job back); on the server itself it is a share that is away,
    which the rest of FrameFound carries on without."""
    from alembic.script import ScriptDirectory
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import create_async_engine

    from framefound.db.models import Library

    checks: list[Check] = []
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5)
        client.ping()
        checks.append(Check("queue", True, f"{settings.redis_host}:{settings.redis_port}"))
    except Exception as err:  # noqa: BLE001 - report whatever stopped it
        where = f"{settings.redis_host}:{settings.redis_port}"  # never the URL: it has the password
        checks.append(Check("queue", False, f"no answer at {where} ({err})"))

    engine = create_async_engine(settings.db_url)
    try:
        async with engine.connect() as conn:
            current = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
            # A source checkout (an editable install) keeps the
            # migrations beside the package: apps/server/migrations.
            migrations = Path(__file__).resolve().parents[2] / "migrations"
            head = (
                ScriptDirectory(str(migrations)).get_current_head()
                if migrations.is_dir()
                else current
            )
            checks.append(
                Check(
                    "schema",
                    current == head,
                    f"database {current}, this checkout {head}"
                    + ("" if current == head else " — update one side so they match"),
                )
            )
            libraries = (
                await conn.execute(select(Library.name, Library.root_path, Library.enabled))
            ).all()
            for name, root_path, enabled in libraries:
                if not enabled:
                    continue
                check = _library_check(name, root_path, settings)
                check.fatal = libraries_fatal
                checks.append(check)
    except Exception as err:  # noqa: BLE001
        checks.append(
            Check("database", False, f"{settings.postgres_host}:{settings.postgres_port} ({err})")
        )
    finally:
        await engine.dispose()
    return checks


def _library_check(name: str, root_path: str, settings: Settings) -> Check:
    try:
        root = local_root(root_path, settings)
    except UnmappedRoot as err:
        return Check(f"library {name}", False, str(err))
    state = root_state(root)
    return Check(
        f"library {name}",
        state == "ok",
        f"{root_path} → {root}" + ("" if state == "ok" else f" is {state} here — mount the share"),
    )


def check_onnx(parity: bool) -> list[Check]:
    from framefound.ai.runtime import onnx_providers

    providers = onnx_providers()
    checks = [Check("onnx provider", True, " → ".join(providers), fatal=False)]
    if parity and providers[0] != "CPUExecutionProvider":
        checks.append(_parity_check())
    return checks


def _parity_check() -> Check:
    import numpy as np
    from PIL import Image

    from framefound.ai.embeddings import ClipOnnxProvider
    from framefound.ai.runtime import onnx_providers

    with tempfile.TemporaryDirectory() as work:
        sample = Path(work) / "parity.jpg"
        rng = np.random.default_rng(7)
        gradient = np.linspace(0, 255, 224 * 224 * 3).reshape(224, 224, 3)
        noise = rng.integers(0, 40, (224, 224, 3))
        Image.fromarray(np.clip(gradient + noise, 0, 255).astype("uint8")).save(sample)
        accelerated = ClipOnnxProvider(providers=onnx_providers()).embed_image(sample)
        reference = ClipOnnxProvider(providers=["CPUExecutionProvider"]).embed_image(sample)
    similarity = float(np.dot(accelerated.vector, reference.vector))
    return Check(
        "coreml parity",
        similarity >= PARITY_FLOOR,
        f"cosine {similarity:.5f} against the CPU (floor {PARITY_FLOOR})"
        + ("" if similarity >= PARITY_FLOOR else " — set FRAMEFOUND_ONNX_PROVIDER=cpu"),
    )


def run(parity: bool = False) -> list[Check]:
    settings = get_settings()
    checks = check_settings(settings)
    checks += check_data_store(settings)
    checks += asyncio.run(check_services(settings))
    checks += check_tools()
    checks += check_onnx(parity)
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--parity", action="store_true", help="compare CoreML and CPU vectors (downloads CLIP)"
    )
    args = parser.parse_args(argv)
    return report(run(parity=args.parity), "the workers will not start until they are fixed")


def report(checks: list[Check], consequence: str) -> int:
    """Print one line per check; 1 if any fatal check failed, else 0.
    Shared with the native install's `framefound doctor`."""
    width = max(len(c.name) for c in checks)
    for check in checks:
        mark = "ok  " if check.ok else ("FAIL" if check.fatal else "warn")
        print(f"{mark}  {check.name:<{width}}  {check.detail}")
    failed = [c for c in checks if c.fatal and not c.ok]
    if failed:
        print(f"\n{len(failed)} problem(s); {consequence}.")
        return 1
    print("\nReady.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
