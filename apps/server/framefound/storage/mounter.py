"""The mount helper: the one component allowed to mount a filesystem.

Mounting needs CAP_SYS_ADMIN, which is very close to root. Granting it to the
API — a process that terminates requests from the internet — would mean an
application bug becomes a host compromise. So the capability lives here, in a
service that:

- is on the internal compose network only, with no published port;
- accepts one shared secret, compared in constant time;
- accepts nothing but a `MountSpec`, re-validated on arrival even though the
  caller validated it, because this side is the one holding the capability;
- speaks no shell and interpolates nothing into a command line;
- writes credentials to a 0600 file that is removed before it returns.

It is deliberately tiny. Everything it does not do is the point.
"""

import asyncio
import hmac
import os
import tempfile
from pathlib import Path

import structlog
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from framefound.logging import configure_logging
from framefound.storage.spec import MountSpecError, assert_within_allowed_root, parse_mount_spec

log = structlog.get_logger()

MOUNT_TIMEOUT_S = 45
UMOUNT_TIMEOUT_S = 20


class MountRequest(BaseModel):
    protocol: str
    server: str
    share: str
    name: str
    purpose: str
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=400)


class UnmountRequest(BaseModel):
    target: str


class MountResult(BaseModel):
    ok: bool
    target: str
    detail: str = ""


def _shared_secret() -> str:
    secret = os.environ.get("FRAMEFOUND_MOUNTER_TOKEN", "")
    if not secret:
        # Failing closed: a helper with no token would accept anything that
        # reached it, and "nothing should reach it" is not an access control.
        raise RuntimeError("FRAMEFOUND_MOUNTER_TOKEN is not set")
    return secret


def _authorise(token: str | None) -> None:
    if not token or not hmac.compare_digest(token, _shared_secret()):
        raise HTTPException(status_code=401, detail="Unauthorised")


async def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return 124, "Timed out. The server may be unreachable."
    return process.returncode or 0, stdout.decode("utf-8", "replace").strip()


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(title="FrameFound mount helper", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/mount", response_model=MountResult)
    async def mount(
        body: MountRequest, x_framefound_token: str | None = Header(default=None)
    ) -> MountResult:
        _authorise(x_framefound_token)
        try:
            spec = parse_mount_spec(
                protocol=body.protocol,
                server=body.server,
                share=body.share,
                name=body.name,
                purpose=body.purpose,
            )
        except MountSpecError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

        target = Path(str(spec.target))
        target.mkdir(parents=True, exist_ok=True)
        if target.is_mount():
            return MountResult(ok=True, target=str(target), detail="Already mounted")

        credentials_path: str | None = None
        try:
            if spec.protocol == "cifs" and body.username:
                # A 0600 file rather than argv: /proc/*/cmdline is readable, so
                # a password on a command line is a password in `ps`. mkstemp
                # creates it 0600 from the outset — writing then chmod'ing
                # leaves a window where it is not.
                fd, credentials_path = tempfile.mkstemp(prefix="ffcred-", suffix=".cred")
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(f"username={body.username}\npassword={body.password}\n")

            code, output = await _run(spec.argv(credentials_path), MOUNT_TIMEOUT_S)
        finally:
            if credentials_path:
                Path(credentials_path).unlink(missing_ok=True)

        if code != 0:
            log.warning("mounter.failed", target=str(target), code=code, output=output[:300])
            return MountResult(ok=False, target=str(target), detail=output[:300] or "mount failed")
        log.info(
            "mounter.mounted",
            target=str(target),
            protocol=spec.protocol,
            read_only=spec.read_only,
        )
        return MountResult(ok=True, target=str(target))

    @app.post("/unmount", response_model=MountResult)
    async def unmount(
        body: UnmountRequest, x_framefound_token: str | None = Header(default=None)
    ) -> MountResult:
        _authorise(x_framefound_token)
        try:
            # The same confinement as mounting. Unmounting an arbitrary path is
            # its own denial of service.
            assert_within_allowed_root(Path(body.target).as_posix())  # type: ignore[arg-type]
        except MountSpecError as err:
            raise HTTPException(status_code=400, detail=str(err)) from err

        target = Path(body.target)
        if not target.is_mount():
            return MountResult(ok=True, target=str(target), detail="Not mounted")
        # Lazy: an in-flight read on a network share should not wedge the call.
        code, output = await _run(["umount", "-l", str(target)], UMOUNT_TIMEOUT_S)
        if code != 0:
            return MountResult(ok=False, target=str(target), detail=output[:300])
        log.info("mounter.unmounted", target=str(target))
        return MountResult(ok=True, target=str(target))

    return app


app = create_app()
