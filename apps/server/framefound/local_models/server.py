"""The model server: the `model-server` service, which runs one model at a time.

FrameFound never talks to the program that runs a model (the runtime)
directly. It asks this server, on 127.0.0.1, with the OpenAI chat shape and a
token only FrameFound's own code can make; the server does four things:

1. checks the model's folder against its manifest — every time, because a
   runtime runs whatever code a model's folder hands it (`files.folder_problem`);
2. starts the right runtime on that folder if it is not the one already
   loaded: a child process on loopback, with an environment of its own that
   holds none of FrameFound's secrets and cannot reach Hugging Face;
3. forwards one chat at a time and hands back the answer, with what was
   asked of whom and how long it took;
4. stops the runtime when it has been idle long enough, and says in a
   sentence when it died.

It holds no database and no policy (design L1, D2): whether a model may run
now — paused, the memory budget for the hour, still approved — is decided by
`ask()` before the request is sent, and the idle time travels with each
request. So it is healthy with the database down and with nothing installed,
and can be tested with a stand-in child, a stand-in clock and nothing else.
It imports no database and no queue (a test checks).

Everything that blocks (starting, waiting, forwarding) is plain `def`, run in
FastAPI's thread pool; one lock keeps it to one request, and one model, at a
time.
"""

import contextlib
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import subprocess
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Literal, Protocol

import httpx
import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from framefound.config import Settings, get_settings
from framefound.local_models import files, library, runtimes
from framefound.local_models.kinds import RuntimeKind, Serve
from framefound.local_models.kinds import kinds as kinds_here
from framefound.native import proc
from framefound.native.layout import Layout

log = structlog.get_logger()

TOKEN_HEADER = "X-FrameFound-Token"  # noqa: S105 - a header name, not a secret
IDLE_HEADER = "X-FrameFound-Idle-Seconds"
TIMEOUT_HEADER = "X-FrameFound-Timeout-Seconds"
# How long a request waits for the one before it before it gives up.
QUEUE_WAIT_S = 120.0
MAX_BODY_BYTES = 32 * 1024 * 1024
# The one form a picture may take: data FrameFound built. A runtime fetches
# an http(s) address it is given and opens anything else as a path.
PICTURE_PREFIX = "data:image/jpeg;base64,"

# How often the idle clock and the child are looked at.
TICK_S = 5.0
# A runtime is asked to stop, given this long, then killed.
STOP_WAIT_S = 20.0
KILL_WAIT_S = 5.0
# While loading, "are you ready" is asked this often.
POLL_S = 0.5
# On top of the load wait, for each GB of weights: a nine-gigabyte model read
# from the NAS for a Test does not load in five minutes.
LOAD_S_PER_GB = 15.0
# How much of the runtime's log a failure carries for the page.
TAIL_CHARS = 2000
# How much of a runtime's error answer is kept.
ERROR_CHARS = 500
# The runtime's log starts again once it is this large, so it cannot grow for ever.
MAX_LOG_BYTES = 10 * 1024 * 1024
LOG_NAME = "model-runtime.log"
RECORD_NAME = "runtime.json"

# The keys of a chat body FrameFound sends. Anything else is refused rather
# than passed on: a runtime may accept keys that load files or fetch things
# (an adapter, a draft model), and it is given only what FrameFound chose.
ALLOWED_KEYS = frozenset(
    {
        "model",
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "temperature",
        "top_p",
        "seed",
        "stop",
        "response_format",
        "stream",
    }
)
_ROLES = frozenset({"system", "user", "assistant"})
# Addresses that mean "every network interface". Looked for in a runtime's
# command line, to refuse it; never bound.
_EVERY_INTERFACE = frozenset({"0.0.0.0", "::", "[::]"})  # noqa: S104 - refused, never bound
_BASE64 = re.compile(r"[A-Za-z0-9+/=\s]*")
_GB = 1024**3

NOT_OUR_FOLDER = (
    "Models are only run from FrameFound's own model folders: the copies on this machine "
    "and the model library."
)
NOT_A_PICTURE = "Pictures are passed as data, never as an address."
ONLY_TEXT_AND_PICTURES = "Only text and pictures are ever sent to a model."
NO_MANIFEST = "This copy of the model has no list of its files, so it is not complete."
WOULD_NOT_LOAD = "It would not load."
DIED = "The model stopped unexpectedly while answering (it may have run out of memory)."
STILL_RUNNING = (
    "The model's program did not stop when it was told to. It is ended before the next "
    "model is loaded."
)
NOT_LOOPBACK = "The model server only answers this machine."
NO_KEY_SET = (
    "The model server has no secret key to check requests against, so it answers none. "
    "Set FRAMEFOUND_SECRET_KEY and restart it."
)
NOT_FROM_FRAMEFOUND = "This request did not carry the model server's token."
TOO_LARGE = "The request is too large for the model server."
NOT_JSON = "The request is not a JSON object."
DEMO_NEVER_STARTS = "The demo runtime is never started."


def port_in_use(port: int) -> str:
    return f"Port {port} is in use by another program."


def token_for(secret_key: str) -> str:
    """The token FrameFound's own code sends with every request: made from the secret key.

    Anyone who can read framefound.env can make it; nobody else on the
    machine can, so a request that carries it was sent by FrameFound, after
    `ask()` decided the model may run.
    """
    return hmac.new(secret_key.encode(), b"framefound model-server", hashlib.sha256).hexdigest()


class Refused(RuntimeError):
    """A request the server will not, or could not, carry out.

    `status` and `code` are what the HTTP answer carries; the message is a
    sentence fit to show; `detail` holds the runtime's own last words, when
    there are any (with its key replaced by ***).
    """

    def __init__(self, status: int, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.detail = detail


class Child(Protocol):
    """The part of `subprocess.Popen` the server uses: enough to stop it and see it stop."""

    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


# `proc.spawn`'s shape.
Spawn = Callable[..., Child]
State = Literal["idle", "loading", "ready", "busy"]


@dataclass
class _Loaded:
    """The runtime that is running, and what it was started on."""

    child: Child
    kind: RuntimeKind
    # The model's folder, resolved: exactly what `--model` and every
    # request's `model` say.
    folder: Path
    # The runtime version's own folder, and its version.
    runtime_dir: Path
    version: str
    key: str
    repo_id: str
    revision: str
    started_at: float
    last_used: float
    idle_s: float
    # Where in the log this start's lines begin.
    log_from: int
    load_seconds: float = 0.0
    memory_gb: float | None = None


class Supervisor:
    """Starts, feeds and stops the one runtime. Blocking; one request at a time.

    Every outside effect comes in through the constructor — the program that
    starts the child, the HTTP transport to it, the program runner for `ps`
    and `kill`, the port probe, the clock and the sleep — so a test drives it
    with stand-ins and starts nothing.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        kinds: Mapping[str, RuntimeKind] | None = None,
        spawn: Spawn = proc.spawn,
        http: httpx.BaseTransport | None = None,
        run: runtimes.Runner = proc.run,
        port_open: Callable[[int], bool] = proc.port_open,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        load_timeout_s: float = 300.0,
        layout: Layout | None = None,
    ) -> None:
        self.settings = settings
        self.kinds: dict[str, RuntimeKind] = (
            dict(kinds) if kinds is not None else kinds_here(settings)
        )
        self.load_timeout_s = load_timeout_s
        self.queue_wait_s = QUEUE_WAIT_S
        layout = layout or Layout.default()
        self.log_path = layout.logs / LOG_NAME
        # What was started, so a supervisor that was killed can find its
        # leftover child on the next start and end it.
        self.record_path = layout.run / RECORD_NAME
        self._spawn = spawn
        self._http = http
        self._run = run
        self._port_open = port_open
        self._clock = clock
        self._sleep = sleep
        # One request (and so one model) at a time.
        self._lock = threading.Lock()
        # Held only for a moment: what `status()` reads while a request runs.
        self._facts = threading.Lock()
        self._loaded: _Loaded | None = None
        self._state: State = "idle"
        self._problem: str | None = None

    @property
    def port(self) -> int:
        return self.settings.model_runtime_port

    # --- what the routes call -------------------------------------------------

    def start(self) -> None:
        """At start-up: end a runtime a killed supervisor left on the port."""
        with self._lock:
            try:
                self._clear_port()
            except Refused as refusal:
                self._set(problem=refusal.message)

    def status(self) -> dict[str, Any]:
        """What is loaded, since when, and when it will be stopped for being idle."""
        with self._facts:
            loaded, state, problem = self._loaded, self._state, self._problem
        now = self._clock()
        if loaded is None:
            return {
                "state": "idle",
                "folder": None,
                "repo_id": None,
                "revision": None,
                "runtime": None,
                "runtime_version": None,
                "memory_gb": None,
                "loaded_for_s": None,
                "idle_for_s": None,
                "unloads_in_s": None,
                "pid": None,
                "problem": problem,
            }
        resting = state == "ready"
        idle_for = max(0.0, now - loaded.last_used) if resting else 0.0
        return {
            "state": state,
            "folder": str(loaded.folder),
            "repo_id": loaded.repo_id,
            "revision": loaded.revision,
            "runtime": loaded.kind.name,
            "runtime_version": loaded.version,
            "memory_gb": loaded.memory_gb,
            "loaded_for_s": round(max(0.0, now - loaded.started_at), 1),
            "idle_for_s": round(idle_for, 1),
            "unloads_in_s": round(max(0.0, loaded.idle_s - idle_for), 1) if resting else None,
            "pid": loaded.child.pid,
            "problem": problem,
        }

    def chat(
        self, body: dict[str, Any], *, idle_s: float = 300.0, timeout_s: float = 300.0
    ) -> dict[str, Any]:
        """Answer one chat: check the folder, load the model if needed, forward, add the facts.

        `Refused` for everything that goes wrong, with the status and code
        the HTTP answer carries.
        """
        if not isinstance(body, dict):
            raise Refused(400, "bad_request", NOT_JSON)
        _check_body(body)
        # Before the turn is taken: naming a folder in a library on the NAS
        # looks at the share, and a share that has hung must hold up this
        # request alone — never a model that runs from this machine's disk,
        # nor the idle unload that frees its memory.
        folder = self._allowed_folder(body.get("model"))
        if not self._lock.acquire(timeout=self.queue_wait_s):
            raise Refused(
                503,
                "busy",
                f"The model was busy with another request for more than "
                f"{_duration(self.queue_wait_s)}. It will be asked again later.",
            )
        try:
            return self._chat(body, folder, idle_s=idle_s, timeout_s=timeout_s)
        finally:
            self._settle()
            self._lock.release()

    def unload(self, reason: str = "asked") -> bool:
        """Stop the runtime once the answer in hand is given. False when nothing was loaded."""
        if not self._lock.acquire(timeout=self.queue_wait_s):
            raise Refused(503, "busy", "The model is answering a request; try again in a moment.")
        try:
            loaded = self._loaded
            if loaded is None:
                return False
            self._end(loaded, problem=None)
            return True
        finally:
            self._settle()
            self._lock.release()

    def tick(self) -> None:
        """Every few seconds: stop a model idle for too long; notice a runtime that died.

        Skipped while a request holds the model: it is not idle, and a child
        that dies under it is that request's to report.
        """
        if not self._lock.acquire(blocking=False):
            return
        try:
            loaded = self._loaded
            if loaded is None:
                return
            code = loaded.child.poll()
            if code is not None:
                self._end(
                    loaded,
                    problem=(
                        f"The model stopped by itself (exit {code}) after it had loaded. It "
                        "is loaded again the next time it is asked."
                    ),
                )
            elif self._clock() - loaded.last_used >= loaded.idle_s:
                self._end(loaded, problem=None)
        finally:
            self._settle()
            self._lock.release()

    def close(self) -> None:
        """Stop the runtime now, without waiting for an answer in hand (the service is stopping)."""
        with self._facts:
            loaded = self._loaded
        if loaded is not None:
            self._end(loaded, problem=None)

    # --- one chat --------------------------------------------------------------

    def _chat(
        self, body: dict[str, Any], folder: Path, *, idle_s: float, timeout_s: float
    ) -> dict[str, Any]:
        manifest = self._inspect(folder)
        kind, root, runtime_dir, version = self._runtime_for(manifest)
        loaded, load_seconds = self._ready(kind, root, runtime_dir, version, folder, manifest)
        loaded.idle_s = idle_s
        self._set(state="busy")
        sent = kind.request(body, folder)
        began = self._clock()
        with self._client() as client:
            try:
                reply = client.post(
                    kind.chat_path,
                    json=sent,
                    headers=kind.auth_headers(loaded.key),
                    timeout=httpx.Timeout(timeout_s, connect=10.0),
                )
            except httpx.TimeoutException:
                # A generation that is stuck holds the lock and the memory:
                # it is ended, and the next request starts afresh.
                stuck = f"It took longer than {_duration(timeout_s)} to answer, so it was stopped."
                self._end(loaded, problem=stuck)
                raise Refused(504, "timeout", stuck) from None
            except httpx.TransportError:
                detail = self._tail(loaded)
                self._end(loaded, problem=DIED)
                raise Refused(502, "died", DIED, detail) from None
        answer_seconds = self._clock() - began
        loaded.last_used = self._clock()
        if reply.status_code != 200:
            raise Refused(
                502,
                "runtime_error",
                f"{kind.label} could not answer (it said {reply.status_code}).",
                _masked(reply.text, loaded.key)[:ERROR_CHARS],
            )
        try:
            answer = reply.json()
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            raise Refused(
                502,
                "runtime_error",
                f"{kind.label} answered in a form FrameFound could not read.",
                _masked(reply.text, loaded.key)[:ERROR_CHARS],
            )
        memory_gb, source = self._memory(kind, answer, loaded)
        loaded.memory_gb = memory_gb if memory_gb is not None else loaded.memory_gb
        answer = dict(answer)
        answer["framefound"] = {
            "repo_id": loaded.repo_id,
            "revision": loaded.revision,
            "runtime": kind.name,
            "runtime_version": loaded.version,
            "load_seconds": round(load_seconds, 2),
            "answer_seconds": round(answer_seconds, 2),
            "memory_gb": memory_gb,
            "memory_source": source,
            "demo": False,
        }
        return answer

    def _allowed_folder(self, model: object) -> Path:
        """The model's folder, resolved, when it is inside one of FrameFound's model folders.

        A model is only ever named by an absolute folder FrameFound put it
        in: a name that is not a folder makes a runtime go to Hugging Face
        for it, and a folder anywhere else is not one whose files were checked.
        """
        if not isinstance(model, str) or not model or "\0" in model:
            raise Refused(400, "not_allowed", NOT_OUR_FOLDER)
        try:
            path = Path(model)
            if not path.is_absolute():
                raise Refused(400, "not_allowed", NOT_OUR_FOLDER)
            folder = path.resolve()
        except (OSError, RuntimeError, ValueError):
            raise Refused(400, "not_allowed", NOT_OUR_FOLDER) from None
        # This machine's copies first, and alone when the folder is one of
        # them: the library may be on a share, and a share is not looked at
        # for a model that does not need it.
        if not self._in_library(folder):
            return folder
        # The stores file is read on each request, so a move of the library
        # is followed without a restart; a record that cannot be read leaves
        # only this machine's copies allowed. The library's folder as it is
        # spelt first (no look at the disk), then as the disk spells it (a
        # link on the way to it).
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            shelf = library.library_root(self.settings)
            if _inside(folder, Path(os.path.abspath(shelf))) or _inside(folder, shelf.resolve()):
                return folder
        raise Refused(400, "not_allowed", NOT_OUR_FOLDER)

    def _in_library(self, folder: Path) -> bool:
        """False for a folder among this machine's copies (`<models_dir>/local-ai`).

        Only this machine's own disk is looked at to say so.
        """
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            if _inside(folder, (self.settings.models_dir / library.LOCAL).resolve()):
                return False
        return True

    def _inspect(self, folder: Path) -> files.Manifest:
        """The folder's manifest, once the folder has passed the check every load must pass."""
        try:
            problem = files.folder_problem(folder)
            manifest = files.read_manifest(folder) if problem is None else None
        except OSError as err:
            # Nobody could look: the folder may be perfectly good. Not a bad
            # model — the drive is away or stalled, and asking later can work.
            raise Refused(503, "library_away", self._unreadable(folder), str(err)) from None
        if problem is not None or manifest is None:
            # A share that has dropped often reads as an empty folder (the
            # mount point is left behind): no manifest, which is what an
            # unfinished model looks like too. The library's marker tells
            # the two apart, so a library that is only away is never
            # reported as a damaged model.
            gone = self._library_gone(folder)
            if gone is not None:
                raise Refused(503, "library_away", gone)
        if problem is not None:
            raise Refused(409, "bad_files", problem)
        if manifest is None:
            # Gone between the check and the read.
            raise Refused(409, "bad_files", NO_MANIFEST)
        return manifest

    def _library_gone(self, folder: Path) -> str | None:
        """Why the folder cannot be read when it is in a library that is away, else None."""
        if not self._in_library(folder):
            return None
        away = library.unreachable(self.settings)
        if away is None:
            return None
        return f"{away} The model cannot be read from it until it is back."

    def _unreadable(self, folder: Path) -> str:
        return (
            self._library_gone(folder)
            or "The model's folder could not be read just now. Try again in a moment."
        )

    def _runtime_for(self, manifest: files.Manifest) -> tuple[RuntimeKind, Path, Path, str]:
        """The kind the manifest names, and the folder and version of it in use."""
        kind = self.kinds.get(manifest.runtime)
        if kind is None:
            raise Refused(
                409,
                "no_runtime",
                f"This model needs software this install does not have ({manifest.runtime[:60]}).",
            )
        if kind.demo:
            # The demo's model server is a stand-in that starts nothing.
            raise Refused(409, "no_runtime", DEMO_NEVER_STARTS)
        try:
            root = runtimes.root(self.settings)
            found = runtimes.current_dir(root, kind.name)
        except runtimes.RuntimeProblem as err:
            # A damaged record of which version is in use is not "not
            # installed": the page's way out is different.
            raise Refused(409, "no_runtime", str(err), err.detail) from None
        except (OSError, ValueError) as err:
            raise Refused(
                409,
                "no_runtime",
                f"Which version of {kind.label} is in use could not be read.",
                str(err),
            ) from None
        if found is None:
            raise Refused(409, "no_runtime", f"{kind.label} is not installed.")
        runtime_dir, version = found
        return kind, root, runtime_dir, version

    def _ready(
        self,
        kind: RuntimeKind,
        root: Path,
        runtime_dir: Path,
        version: str,
        folder: Path,
        manifest: files.Manifest,
    ) -> tuple[_Loaded, float]:
        """The runtime for this model and version, running: the one loaded, or a new one.

        Returns it and how long loading took (0.0 when it was already loaded).
        """
        current = self._loaded
        if current is not None:
            if (
                current.child.poll() is None
                and current.folder == folder
                and current.runtime_dir == runtime_dir
                and current.kind.name == kind.name
            ):
                return current, 0.0
            # Another model, another version of the software, or a runtime
            # that died: one model at a time, so whatever is there goes first.
            code = current.child.poll()
            self._end(
                current,
                problem=None
                if code is None
                else f"The model stopped by itself (exit {code}) after it had loaded.",
            )
        loaded = self._load(kind, root, runtime_dir, version, folder, manifest)
        return loaded, loaded.load_seconds

    def _load(
        self,
        kind: RuntimeKind,
        root: Path,
        runtime_dir: Path,
        version: str,
        folder: Path,
        manifest: files.Manifest,
    ) -> _Loaded:
        """Start `kind` on `folder`, wait until it answers, warm it up. `Refused` otherwise."""
        self._set(state="loading")
        try:
            self._clear_port()
        except Refused as refusal:
            self._set(problem=refusal.message)
            raise
        key = secrets.token_urlsafe(24)
        scratch = runtimes.scratch_dir(root)
        try:
            # The runtime's HF_HOME: empty, and its own.
            (scratch / "hf-home").mkdir(parents=True, exist_ok=True)
        except OSError as err:
            raise self._failed(
                WOULD_NOT_LOAD, f"Its working folder could not be made: {err}"
            ) from None
        try:
            started_as = kind.serve(runtime_dir, folder, manifest.variant, self.port, key, scratch)
        except runtimes.RuntimeProblem as err:
            # A version whose program was deleted by hand (llama.cpp's
            # unpacked archive) is still recorded as in use: the kind says
            # so in a sentence, which is the refusal, not a 500. Nothing has
            # been started or recorded yet, so there is nothing to undo.
            raise self._failed(str(err), err.detail) from None
        except (OSError, ValueError) as err:
            raise self._failed(WOULD_NOT_LOAD, str(err)) from None
        serve = _safe(kind, started_as, key)
        out, log_from = self._open_log()
        started = self._clock()
        try:
            with out:
                # The child has its own copy of the file once started.
                child = self._spawn(
                    serve.argv, env=serve.env, cwd=serve.cwd, stdout=out, stderr=subprocess.STDOUT
                )
        except (proc.CommandFailed, OSError) as err:
            raise self._failed(WOULD_NOT_LOAD, str(err)) from None
        loaded = _Loaded(
            child=child,
            kind=kind,
            folder=folder,
            runtime_dir=runtime_dir,
            version=version,
            key=key,
            repo_id=manifest.repo_id,
            revision=manifest.revision,
            started_at=started,
            last_used=started,
            idle_s=0.0,
            log_from=log_from,
        )
        self._write_record(loaded)
        self._set(loaded=loaded)
        wait_s = self.load_timeout_s + LOAD_S_PER_GB * _weights_gb(kind, manifest)
        deadline = started + wait_s
        try:
            with self._client() as client:
                self._wait_until_ready(client, loaded, deadline, wait_s)
                self._warm_up(client, loaded, deadline, wait_s)
        except Refused as refusal:
            self._end(loaded, problem=refusal.message)
            raise
        loaded.load_seconds = self._clock() - started
        loaded.last_used = self._clock()
        self._set(problem=None)
        return loaded

    def _wait_until_ready(
        self, client: httpx.Client, loaded: _Loaded, deadline: float, wait_s: float
    ) -> None:
        kind = loaded.kind
        while True:
            if loaded.child.poll() is not None:
                raise Refused(409, "load_failed", WOULD_NOT_LOAD, self._tail(loaded))
            try:
                reply = client.get(
                    kind.health_path, headers=kind.auth_headers(loaded.key), timeout=2.0
                )
                body = _json_object(reply)
                if kind.is_ready(reply.status_code, body):
                    return
            except httpx.HTTPError:
                # Not listening yet.
                pass
            if self._clock() >= deadline:
                raise self._too_slow(loaded, wait_s)
            self._sleep(POLL_S)

    def _warm_up(
        self, client: httpx.Client, loaded: _Loaded, deadline: float, wait_s: float
    ) -> None:
        """One token, so "loaded" means the model is in memory whichever way the runtime
        loads (its health check may answer before the weights are read)."""
        kind = loaded.kind
        body = kind.request(
            {"messages": [{"role": "user", "content": "ok"}], "max_tokens": 1, "temperature": 0},
            loaded.folder,
        )
        left = max(deadline - self._clock(), 1.0)
        try:
            reply = client.post(
                kind.chat_path,
                json=body,
                headers=kind.auth_headers(loaded.key),
                timeout=httpx.Timeout(left, connect=10.0),
            )
        except httpx.TimeoutException:
            raise self._too_slow(loaded, wait_s) from None
        except httpx.TransportError:
            raise Refused(409, "load_failed", WOULD_NOT_LOAD, self._tail(loaded)) from None
        if reply.status_code != 200:
            said = _masked(reply.text, loaded.key)[:ERROR_CHARS]
            raise Refused(409, "load_failed", WOULD_NOT_LOAD, self._tail(loaded) or said)

    def _too_slow(self, loaded: _Loaded, wait_s: float) -> Refused:
        return Refused(
            504,
            "load_timeout",
            f"It did not finish loading within {_duration(wait_s)}, so it was stopped.",
            self._tail(loaded),
        )

    def _failed(self, message: str, detail: str) -> Refused:
        self._set(problem=message)
        return Refused(409, "load_failed", message, detail)

    def _memory(
        self, kind: RuntimeKind, answer: Mapping[str, Any], loaded: _Loaded
    ) -> tuple[float | None, Literal["runtime", "process"] | None]:
        """The memory used: as the runtime reports it, else the size of its process.

        Two different measurements, so which one it is travels with it.
        """
        reported = kind.memory_gb(answer)
        if reported is not None:
            return round(reported, 2), "runtime"
        try:
            done = self._run(
                ["ps", "-o", "rss=", "-p", str(loaded.child.pid)], capture=True, check=False
            )
            kilobytes = int((done.stdout or "").strip()) if done.returncode == 0 else 0
        except (proc.CommandFailed, OSError, ValueError):
            return None, None
        if kilobytes <= 0:
            return None, None
        return round(kilobytes * 1024 / _GB, 2), "process"

    # --- the port and the leftover ----------------------------------------------

    def _clear_port(self) -> None:
        """Make sure nothing holds the runtime's port: end our own leftover, refuse a stranger's.

        A runtime is ours when the start recorded in `run/runtime.json` is
        still running and its command line is the one a kind here would
        have started (`kind.owns`). Anything else on the port is another
        program, and is never touched.
        """
        if not self._port_open(self.port):
            return
        record = self._read_record()
        pid = record.get("pid") if record else None
        runtime_dir = record.get("runtime_dir") if record else None
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 1
            or not isinstance(runtime_dir, str)
        ):
            raise Refused(409, "port_in_use", port_in_use(self.port))
        command = self._command_of(pid)
        if not command or not any(
            kind.owns(command, Path(runtime_dir)) for kind in self.kinds.values()
        ):
            raise Refused(409, "port_in_use", port_in_use(self.port))
        for signal in ("-TERM", "-KILL"):
            with contextlib.suppress(proc.CommandFailed, OSError):
                self._run(["kill", signal, str(pid)], capture=True, check=False)
            if self._port_closes(STOP_WAIT_S if signal == "-TERM" else KILL_WAIT_S):
                self._forget_record(pid)
                return
        raise Refused(409, "port_in_use", port_in_use(self.port))

    def _command_of(self, pid: int) -> str:
        try:
            done = self._run(["ps", "-o", "command=", "-p", str(pid)], capture=True, check=False)
        except (proc.CommandFailed, OSError):
            return ""
        return (done.stdout or "").strip() if done.returncode == 0 else ""

    def _port_closes(self, seconds: float) -> bool:
        deadline = self._clock() + seconds
        while self._port_open(self.port):
            if self._clock() >= deadline:
                return False
            self._sleep(POLL_S)
        return True

    # --- stopping ------------------------------------------------------------------

    def _end(self, loaded: _Loaded, *, problem: str | None) -> None:
        """Stop this runtime (through its handle), forget it, and say why when it went wrong.

        A runtime that outlives even the kill (stuck reading a share that
        has hung) keeps its record: it still holds the port, and the record
        is how the next load knows it as FrameFound's own and ends it,
        rather than refusing it as another program's.
        """
        if _stop(loaded.child):
            self._forget_record(loaded.child.pid)
        elif problem is None:
            problem = STILL_RUNNING
        with self._facts:
            if self._loaded is loaded:
                self._loaded = None
                self._state = "idle"
            self._problem = problem

    def _settle(self) -> None:
        with self._facts:
            self._state = "idle" if self._loaded is None else "ready"

    def _set(
        self,
        *,
        state: State | None = None,
        loaded: _Loaded | None = None,
        problem: str | None | Literal[False] = False,
    ) -> None:
        with self._facts:
            if state is not None:
                self._state = state
            if loaded is not None:
                self._loaded = loaded
            if problem is not False:
                self._problem = problem

    # --- the log and the record --------------------------------------------------

    def _open_log(self) -> tuple[IO[bytes], int]:
        """The runtime's log, opened for the child to write to, and where this start begins."""
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = self.log_path.stat().st_size
            except FileNotFoundError:
                size = 0
            if size > MAX_LOG_BYTES:
                return self.log_path.open("wb"), 0
            return self.log_path.open("ab"), size
        except OSError as err:
            raise self._failed(WOULD_NOT_LOAD, f"Its log could not be opened: {err}") from None

    def _tail(self, loaded: _Loaded) -> str:
        """What this start of the runtime wrote last, with its key replaced by ***."""
        try:
            with self.log_path.open("rb") as source:
                source.seek(loaded.log_from)
                raw = source.read()
        except OSError:
            return ""
        return _masked(raw.decode("utf-8", errors="replace"), loaded.key)[-TAIL_CHARS:].strip()

    def _write_record(self, loaded: _Loaded) -> None:
        record = {
            "pid": loaded.child.pid,
            "runtime": loaded.kind.name,
            "runtime_dir": str(loaded.runtime_dir),
            "model": str(loaded.folder),
            "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # Best effort: it is the second net under launchd's own clean-up.
        with contextlib.suppress(OSError):
            self.record_path.parent.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(
                prefix=f".{RECORD_NAME}.", suffix=".tmp", dir=self.record_path.parent
            )
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as out:
                    json.dump(record, out)
                os.replace(temporary, self.record_path)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(temporary)
                raise

    def _read_record(self) -> dict[str, Any] | None:
        try:
            body = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def _forget_record(self, pid: int) -> None:
        record = self._read_record()
        if record is not None and record.get("pid") == pid:
            with contextlib.suppress(OSError):
                self.record_path.unlink()

    def _client(self) -> httpx.Client:
        # Loopback only, and no proxy from the environment: a proxy setting
        # meant for the internet must not carry the key somewhere else.
        return httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}", transport=self._http, trust_env=False
        )


# --- checks on what is sent and what is started ----------------------------------------


def _check_body(body: Mapping[str, Any]) -> None:
    """Refuse anything in a chat FrameFound would not have sent: an unknown key, a part
    that is neither text nor a picture, a picture that is an address."""
    for key in body:
        if key not in ALLOWED_KEYS:
            raise Refused(
                400,
                "not_allowed",
                f"{_shown(str(key))} is not something FrameFound sends to a model.",
            )
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise Refused(400, "bad_request", "A request needs at least one message.")
    for message in messages:
        if (
            not isinstance(message, dict)
            or message.get("role") not in _ROLES
            or not set(message) <= {"role", "content"}
        ):
            raise Refused(
                400,
                "bad_request",
                "Each message is a role (system, user or assistant) and content.",
            )
        content = message.get("content")
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise Refused(400, "bad_request", "A message's content is text or a list of parts.")
        for part in content:
            _check_part(part)


def _check_part(part: object) -> None:
    if not isinstance(part, dict):
        raise Refused(400, "bad_request", "A message's content is text or a list of parts.")
    kind = part.get("type")
    if kind == "text" and isinstance(part.get("text"), str) and set(part) <= {"type", "text"}:
        return
    if kind != "image_url":
        raise Refused(400, "not_allowed", ONLY_TEXT_AND_PICTURES)
    picture = part.get("image_url")
    url = picture if isinstance(picture, str) else None
    if isinstance(picture, dict) and set(picture) <= {"url", "detail"}:
        url = picture.get("url") if isinstance(picture.get("url"), str) else None
    if (
        # Anything beside the picture (a path, another address) is a way
        # to point the runtime somewhere else.
        not set(part) <= {"type", "image_url"}
        or url is None
        or not url.startswith(PICTURE_PREFIX)
        or not _BASE64.fullmatch(url, len(PICTURE_PREFIX))
    ):
        raise Refused(400, "not_allowed", NOT_A_PICTURE)


def _safe(kind: RuntimeKind, serve: Serve, key: str) -> Serve:
    """The start a kind asks for, refused unless it is on loopback and cannot fetch or run
    code a model brought; the environment is made offline whatever the kind wrote."""
    argv = [str(word) for word in serve.argv]
    why = None
    if "127.0.0.1" not in argv or any(word in _EVERY_INTERFACE for word in argv):
        why = "it would listen beyond this machine"
    elif any("trust-remote-code" in word or "trust_remote_code" in word for word in argv):
        why = "it would run code that came with the model"
    elif any(key in word for word in argv):
        why = "its key would be readable by anyone on this Mac"
    if why is not None:
        raise Refused(
            409, "load_failed", f"FrameFound will not start {kind.label} that way: {why}."
        )
    env = {
        name: value
        for name, value in serve.env.items()
        if name != "MLX_TRUST_REMOTE_CODE" and not name.startswith("FRAMEFOUND_")
    }
    # Whatever the kind wrote: a model name where a folder belongs fails
    # instead of downloading.
    env["HF_HUB_OFFLINE"] = "1"
    return Serve(argv=argv, env=env, cwd=serve.cwd)


def _weights_gb(kind: RuntimeKind, manifest: files.Manifest) -> float:
    suffixes = tuple(suffix.lower() for suffix in kind.weights)
    size = sum(file.size for file in manifest.files if file.path.lower().endswith(suffixes))
    return size / _GB


def _stop(child: Child) -> bool:
    """Ask the child to stop, wait, then kill it. Through its handle, never by pid.

    True once it is known to have ended; False when it is still running
    after the kill (a process stuck in the kernel ends only when that
    returns).
    """
    if child.poll() is not None:
        return True
    with contextlib.suppress(OSError):
        child.terminate()
    try:
        child.wait(timeout=STOP_WAIT_S)
        return True
    except subprocess.TimeoutExpired:
        pass
    with contextlib.suppress(OSError):
        child.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        child.wait(timeout=KILL_WAIT_S)
    return child.poll() is not None


def _inside(folder: Path, root: Path) -> bool:
    return folder != root and folder.is_relative_to(root)


def _json_object(reply: httpx.Response) -> dict[str, Any]:
    try:
        body = reply.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _masked(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _shown(text: str) -> str:
    cleaned = "".join(char if char.isprintable() else "?" for char in text)
    return cleaned if len(cleaned) <= 60 else cleaned[:59] + "…"


def _duration(seconds: float) -> str:
    if seconds < 90:
        whole = max(1, round(seconds))
        return f"{whole} second" if whole == 1 else f"{whole} seconds"
    return f"{round(seconds / 60)} minutes"


def _is_loopback(address: object) -> bool:
    if not isinstance(address, tuple | list) or not address:
        return False
    host = address[0]
    if not isinstance(host, str):
        return False
    try:
        found = ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(found, ipaddress.IPv6Address) and found.ipv4_mapped is not None:
        return found.ipv4_mapped.is_loopback
    return found.is_loopback


# --- the HTTP side ----------------------------------------------------------------------


class _LoopbackOnly:
    """Answer nothing that did not come to, and from, this machine's loopback address.

    The service is started on 127.0.0.1. Should it ever be bound wider by
    mistake, a request that arrives on another address — or from another
    machine — is refused before any route sees it, /healthz included.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and not (
            _is_loopback(scope.get("server")) and _is_loopback(scope.get("client"))
        ):
            await _error(403, "not_loopback", NOT_LOOPBACK)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _error(status: int, code: str, message: str, detail: str = "") -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "detail": detail}}, status_code=status
    )


def _seconds(request: Request, header: str, *, low: float, high: float) -> float | None:
    raw = request.headers.get(header)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or not low <= value <= high:
        raise Refused(
            400,
            "bad_request",
            f"{header} must be a number of seconds from {low:.0f} to {high:.0f}.",
        )
    return value


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise Refused(413, "too_large", TOO_LARGE)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            raise Refused(413, "too_large", TOO_LARGE)
        chunks.append(chunk)
    return b"".join(chunks)


def _tick_until(supervisor: Supervisor, stop: threading.Event) -> None:
    while not stop.wait(TICK_S):
        # A tick that fails is tried again in five seconds; it must never
        # take the thread (and with it every idle unload) down. It is
        # logged, so a tick that fails every time is not silent.
        try:
            supervisor.tick()
        except Exception:
            log.warning("model_server.tick_failed", exc_info=True)


def create_app(settings: Settings | None = None, supervisor: Supervisor | None = None) -> FastAPI:
    """The `model-server` service (uvicorn `--factory`, on 127.0.0.1)."""
    settings = settings or get_settings()
    supervisor = supervisor or Supervisor(settings)
    expected = token_for(settings.secret_key).encode() if settings.secret_key else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await run_in_threadpool(supervisor.start)
        stop = threading.Event()
        ticker = threading.Thread(
            target=_tick_until, args=(supervisor, stop), name="model-server-tick", daemon=True
        )
        ticker.start()
        try:
            yield
        finally:
            stop.set()
            await run_in_threadpool(ticker.join, TICK_S * 2)
            await run_in_threadpool(supervisor.close)

    app = FastAPI(
        title="FrameFound model server",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(_LoopbackOnly)

    def refusal(request: Request) -> JSONResponse | None:
        if expected is None:
            return _error(503, "no_secret", NO_KEY_SET)
        given = request.headers.get(TOKEN_HEADER, "").encode()
        if not hmac.compare_digest(given, expected):
            return _error(401, "bad_token", NOT_FROM_FRAMEFOUND)
        return None

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/status", response_model=None)
    def status(request: Request) -> JSONResponse | dict[str, Any]:
        return refusal(request) or supervisor.status()

    @app.post("/unload", response_model=None)
    def unload(request: Request) -> JSONResponse | dict[str, bool]:
        refused = refusal(request)
        if refused is not None:
            return refused
        try:
            return {"unloaded": supervisor.unload("asked")}
        except Refused as err:
            return _error(err.status, err.code, err.message, err.detail)

    @app.post("/v1/chat/completions", response_model=None)
    async def chat(request: Request) -> JSONResponse | dict[str, Any]:
        refused = refusal(request)
        if refused is not None:
            return refused
        try:
            raw = await _read_body(request)
            try:
                body = json.loads(raw)
            except (ValueError, RecursionError):
                body = None
            if not isinstance(body, dict):
                raise Refused(400, "bad_request", NOT_JSON)
            idle_s = _seconds(request, IDLE_HEADER, low=1, high=86_400)
            timeout_s = _seconds(request, TIMEOUT_HEADER, low=1, high=3_600)
            answer: dict[str, Any] = await run_in_threadpool(
                supervisor.chat,
                body,
                idle_s=300.0 if idle_s is None else idle_s,
                timeout_s=300.0 if timeout_s is None else timeout_s,
            )
            return answer
        except Refused as err:
            return _error(err.status, err.code, err.message, err.detail)

    return app
