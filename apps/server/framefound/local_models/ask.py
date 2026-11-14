"""Asking a model: the one way any part of FrameFound puts a question to a local model.

Every caller — L1's Test, and later the Looker and the Editor — goes through
`ask()`. It decides whether the model may run now (paused, the memory budget
for the hour, still approved, its copy on this machine), builds the one
request the model server accepts, and turns what comes back into an `Answer`
or into one of two exceptions:

- `NotNow`: the model may not run at the moment. Nothing is wrong with the
  item; the job waits and is run again (or, for the reasons that cannot
  clear by themselves, says what the operator has to change).
- `ModelFailed`: the model was asked and what came back is unusable, or the
  program running it broke. This item is recorded as failed; the next one
  is tried.

The model server (`server.py`) is reached through the `ModelServer`
protocol, so the tests and the demo hand in `fake.FakeModelServer` and no
test ever reaches a real model, a network or a child process. Section 13 of
the design (L1.md) is this module's contract with the later phases: the
signature of `ask`, the fields of `Answer`, the reasons, and the five
provenance names.
"""

import asyncio
import math
import re
import threading
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePath
from typing import Any, Literal, Protocol, cast, get_args

import httpx
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from framefound.config import Settings, get_settings
from framefound.db.models import LocalModel
from framefound.local_models import (
    ROLE_TITLES,
    ROLES,
    UNAVAILABLE,
    availability,
    library,
    registry,
    runtimes,
    schedule,
)
from framefound.local_models.kinds import RuntimeKind, kinds
from framefound.local_models.server import (
    IDLE_HEADER,
    QUEUE_WAIT_S,
    TIMEOUT_HEADER,
    TOKEN_HEADER,
    token_for,
)
from framefound.local_models.settings_store import load_config

MAX_PICTURES = 8
MAX_PICTURE_BYTES = 1_500_000

# What the model is told when a schema is given, after the prompt.
JSON_ONLY = "Answer with one JSON object and nothing else."
# The one repair turn, when the first answer was not the object asked for.
REPAIR = "That was not the JSON object asked for. Reply with only the JSON object."

# How long a model server that does not answer is left before it is asked again.
SERVER_DOWN_WAIT = timedelta(seconds=60)
# The model server bounds every wait of its own: the queue (QUEUE_WAIT_S),
# the load (300 s plus 15 s per GB of weights) and the answer (timeout_s).
# This client's own limit is only a backstop above those, for a server that
# has hung; it covers loading a model of up to about 100 GB of weights.
LOAD_ALLOWANCE_S = 300.0 + 15.0 * 100
CONNECT_S = 5.0

Later = Literal["paused", "window", "not_here", "library_away", "server_down", "busy"]
Never = Literal[
    "unavailable", "no_model", "not_installed", "not_approved", "no_runtime", "never_fits"
]
Failure = Literal["load_failed", "bad_files", "died", "timeout", "bad_answer", "runtime_error"]
_LATER: frozenset[str] = frozenset(get_args(Later))
_REASONS: frozenset[str] = _LATER | frozenset(get_args(Never))
_FAILURES: frozenset[str] = frozenset(get_args(Failure))

SERVER_DOWN = "The model server is not answering. It will be asked again in a minute."
NO_SUCH_MODEL = "That model is no longer installed."
NOT_A_FORM = "{label} answered in a form FrameFound could not read."
# A runtime's record that cannot be read names its file; this is the sentence
# for everyone (the file is in the job's details, for an administrator).
RECORD_DAMAGED = (
    "FrameFound's record of which version is in use is damaged. An administrator can install "
    "a version to replace it."
)
# How long the model library is given to say where a model's files are. A
# share that has hung would otherwise hold the lane, which runs one job at a
# time; a drive spinning up answers well within it.
LIBRARY_WAIT_S = 60.0
LIBRARY_SILENT = (
    "The model library did not answer within a minute. Check that the drive or share is "
    "connected; it will be asked again in a minute."
)


# --- the model server, as FrameFound's own code reaches it ------------------------


class ServerUnreachable(RuntimeError):
    """Nothing answered on the model server's port (it is stopped, or restarting)."""


class ServerRefused(RuntimeError):
    """The model server answered, and refused or failed: its status, code and sentence.

    `detail` is the runtime's own last words, when there are any.
    """

    def __init__(self, status: int, code: str, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class ModelServer(Protocol):
    def status(self) -> dict[str, Any] | None:
        """What is loaded; None when the server is not answering."""
        ...

    def chat(self, body: dict[str, Any], *, idle_s: float, timeout_s: float) -> dict[str, Any]:
        """One chat. Raises `ServerRefused` or `ServerUnreachable`."""
        ...

    def unload(self) -> bool:
        """Stop the loaded model; False when none was loaded."""
        ...


class HttpModelServer:
    """The model server on this machine's loopback, reached with FrameFound's token.

    One client per call and nothing kept open: it is asked a few times a
    minute at most. Proxy settings in the environment are never used — the
    server is on 127.0.0.1, and a proxy in between would see the token.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._transport = transport

    def status(self) -> dict[str, Any] | None:
        try:
            with self._client(read_s=30.0) as client:
                reply = client.get("/status")
        except httpx.HTTPError:
            return None
        found = _json(reply)
        return found if reply.status_code == 200 and found is not None else None

    def chat(self, body: dict[str, Any], *, idle_s: float, timeout_s: float) -> dict[str, Any]:
        headers = {
            IDLE_HEADER: str(round(_within(idle_s, 1, 86_400))),
            TIMEOUT_HEADER: str(round(_within(timeout_s, 1, 3_600))),
        }
        read_s = QUEUE_WAIT_S + LOAD_ALLOWANCE_S + _within(timeout_s, 1, 3_600) + 30.0
        reply = self._send(
            "POST", "/v1/chat/completions", json=body, headers=headers, read_s=read_s
        )
        answer = _json(reply)
        if answer is None:
            raise ServerRefused(
                502,
                "runtime_error",
                "The model server answered in a form FrameFound could not read.",
            )
        return answer

    def unload(self) -> bool:
        reply = self._send("POST", "/unload", read_s=QUEUE_WAIT_S + 60.0)
        found = _json(reply)
        return bool(found and found.get("unloaded"))

    def _send(self, method: str, path: str, *, read_s: float, **more: Any) -> httpx.Response:
        try:
            with self._client(read_s=read_s) as client:
                reply = client.request(method, path, **more)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ServerUnreachable(SERVER_DOWN) from None
        except httpx.TimeoutException:
            raise ServerRefused(
                504,
                "timeout",
                f"The model server gave no answer in {round(read_s / 60)} minutes.",
            ) from None
        except httpx.HTTPError:
            # The connection broke half way: the service stopped or was
            # restarted under the request. Asking again later can work.
            raise ServerUnreachable(SERVER_DOWN) from None
        if reply.status_code != 200:
            raise _refusal(reply)
        return reply

    def _client(self, *, read_s: float) -> httpx.Client:
        return httpx.Client(
            base_url=self._base,
            headers={TOKEN_HEADER: self._token},
            timeout=httpx.Timeout(read_s, connect=CONNECT_S),
            transport=self._transport,
            trust_env=False,
        )


def _refusal(reply: httpx.Response) -> ServerRefused:
    """The server's own `{"error": {...}}`, or a plain sentence when it sent none."""
    found = _json(reply)
    error = found.get("error") if found is not None else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        code = error.get("code")
        detail = error.get("detail")
        return ServerRefused(
            reply.status_code,
            code if isinstance(code, str) and code else "runtime_error",
            error["message"],
            detail if isinstance(detail, str) else "",
        )
    return ServerRefused(
        reply.status_code, "runtime_error", f"The model server answered {reply.status_code}."
    )


def _json(reply: httpx.Response) -> dict[str, Any] | None:
    try:
        found = reply.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _within(value: float, low: float, high: float) -> float:
    return min(max(float(value), low), high)


_FAKE_LOCK = threading.Lock()
_FAKE: ModelServer | None = None


def server_for(settings: Settings | None = None) -> ModelServer:
    """The model server this install asks: the real one on loopback, or in the
    demo the one process-wide stand-in (made here and nowhere else, so the
    demo cannot reach a real install)."""
    global _FAKE
    settings = settings or get_settings()
    if availability(settings).mode == "demo":
        with _FAKE_LOCK:
            if _FAKE is None:
                # Imported here, inside the demo branch, so nothing else ever builds it.
                from framefound.local_models import fake

                _FAKE = fake.FakeModelServer()
            return _FAKE
    return HttpModelServer(
        f"http://127.0.0.1:{settings.model_server_port}", token_for(settings.secret_key)
    )


# --- what comes back --------------------------------------------------------------


@dataclass(frozen=True)
class Answer:
    text: str  # exactly what the model wrote
    parsed: BaseModel | None  # validated against `schema`, when one was given
    model_id: uuid.UUID
    repo_id: str
    revision: str
    runtime: str
    runtime_version: str
    prompt_version: str
    seconds: float
    load_seconds: float
    memory_gb: float | None
    # How `memory_gb` was measured: "runtime" as the program reports it,
    # "process" the size of its process, None not measured. Two different
    # measurements that must never share a column unlabelled.
    memory_source: Literal["runtime", "process"] | None
    # The runtime's word for how the answer ended ("stop", "length", ...; "" if none).
    finish_reason: str
    ran_from: Literal["this_machine", "library"]
    attempts: int  # 2 when the repair turn was needed
    demo: bool

    def provenance(self) -> dict[str, str]:
        """The five fields every stored result carries, under their column names (13.3)."""
        return {
            "model": self.repo_id,
            "model_revision": self.revision,
            "runtime": self.runtime,
            "runtime_version": self.runtime_version,
            "prompt_version": self.prompt_version,
        }


class NotNow(RuntimeError):
    """The model may not run at the moment. Nothing is wrong with the item.

    The message is read by everyone who can see the job, so it never names a
    path on the server; `detail` holds what only an administrator is shown.
    """

    def __init__(
        self,
        reason: Later | Never,
        message: str,
        not_before: datetime | None = None,
        *,
        detail: str = "",
    ) -> None:
        if reason not in _REASONS:
            raise ValueError(f"{reason!r} is not a reason a model may not run.")
        super().__init__(message)
        self.reason: Later | Never = reason
        self.not_before = not_before
        self.detail = detail

    @property
    def retry(self) -> bool:
        """Asking again later can work by itself (the reason is one of `Later`)."""
        return self.reason in _LATER


class ModelFailed(RuntimeError):
    """The model was asked and the result is unusable, or the runtime broke."""

    def __init__(
        self,
        reason: str,
        message: str,
        text: str = "",
        detail: str = "",
        finish_reason: str = "",
    ) -> None:
        if reason not in _FAILURES:
            raise ValueError(f"{reason!r} is not a way a model can fail.")
        super().__init__(message)
        self.reason = cast(Failure, reason)
        self.text = text
        self.detail = detail
        self.finish_reason = finish_reason


# --- asking ----------------------------------------------------------------------


def extract_json(text: str) -> str:
    """The JSON object in what a model wrote: code fences and any words before
    or after removed, from the first "{" to the last "}". Without both, the
    text as it is (which then fails validation, and is repaired once)."""
    fenced = re.search(r"```(?:json|JSON)?\s*\n?(.*?)```", text, re.DOTALL)
    inner = fenced.group(1) if fenced and "{" in fenced.group(1) else text
    start, end = inner.find("{"), inner.rfind("}")
    if start == -1 or end < start:
        return inner.strip()
    return inner[start : end + 1]


@dataclass(frozen=True)
class _Running:
    """What step 5 found: the program that runs the model, at which version."""

    kind: RuntimeKind
    version: str


async def ask(
    db: AsyncSession,
    who: str | uuid.UUID,
    *,
    prompt: str,
    prompt_version: str,
    instructions: str = "",
    pictures: Sequence[bytes] = (),
    schema: type[BaseModel] | None = None,
    max_tokens: int = 600,
    timeout_s: float = 300.0,
    server: ModelServer | None = None,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> Answer:
    """Ask a role's model ("looker", "editor") or one model by its id a question.

    Returns an `Answer`; raises `NotNow` (it may not run now) or `ModelFailed`
    (it ran and the result is unusable). `ValueError` only for a caller's
    mistake (no prompt version, too many or too large pictures, not JPEG, an
    unknown role). Writes nothing but the model's `measured_memory_gb` and
    `last_loaded_with`, flushed: the caller commits.
    """
    role, model_id = _who(who)
    _check_arguments(prompt_version, pictures, max_tokens, timeout_s)
    settings = settings or get_settings()
    now = now or datetime.now()

    # 1. Can this install run models at all?
    if not availability(settings).ok:
        raise NotNow("unavailable", UNAVAILABLE)
    # 2. The switch on the page.
    config = await load_config(db)
    if config.paused:
        raise NotNow("paused", schedule.PAUSED)
    # 3. Which model, and is it whole.
    model = await _model(db, role, model_id)
    if model.state != registry.INSTALLED:
        raise NotNow(
            "not_installed",
            registry.STILL_DOWNLOADING
            if model.state == registry.DOWNLOADING
            else registry.NOT_COMPLETE,
        )
    # 4. Still approved, judged by today's lists (D10).
    verdict = registry.judged(model, config.lists())
    if not verdict.allowed:
        raise NotNow("not_approved", f"It is no longer approved: {verdict.reason}")
    try:
        files = registry.snapshot(model)
    except ValueError as err:
        raise NotNow("not_installed", str(err)) from None
    # 5. The program that runs it.
    running = await asyncio.to_thread(_running, model.runtime, settings)
    # 6. The memory budget for the hour.
    decision = schedule.decide(registry.memory_gb(model), now, config.budgets())
    if decision.why == "window":
        raise NotNow("window", decision.message, decision.not_before)
    if not decision.run:
        raise NotNow("never_fits", decision.message)
    # 7. The folder to run it from.
    try:
        folder, ran_from = await asyncio.wait_for(
            asyncio.to_thread(_folder, files, role is not None, settings), LIBRARY_WAIT_S
        )
    except TimeoutError:
        raise NotNow("library_away", LIBRARY_SILENT) from None

    # 8. The request.
    text = "\n\n".join(part for part in (instructions.strip(), prompt) if part)
    if schema is not None:
        text = f"{text}\n\n{JSON_ONLY}"
    question: dict[str, Any] = {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            *(running.kind.picture_part(bytes(jpeg)) for jpeg in pictures),
        ],
    }
    body: dict[str, Any] = {
        "model": str(folder),
        "messages": [question],
        "temperature": 0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if schema is not None:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.__name__,
                "strict": True,
                "schema": schema.model_json_schema(),
            },
        }

    # 9. Asking; 10. one repair turn when the answer is not the object asked for.
    server = server or server_for(settings)
    idle_s = float(config.idle_unload_minutes) * 60
    said = await _chat(
        db, server, body, model, running, idle_s=idle_s, timeout_s=timeout_s, now=now
    )
    seen = [said]
    parsed: BaseModel | None = None
    if schema is not None:
        parsed = _parsed(schema, said.text)
        if parsed is None:
            body["messages"] = [
                question,
                {"role": "assistant", "content": said.text},
                {"role": "user", "content": REPAIR},
            ]
            said = await _chat(
                db, server, body, model, running, idle_s=idle_s, timeout_s=timeout_s, now=now
            )
            seen.append(said)
            parsed = _parsed(schema, said.text)
            if parsed is None:
                raise ModelFailed(
                    "bad_answer",
                    f"Its answer was not the form asked for, twice{_cut_off(said)}.",
                    text=said.text,
                    finish_reason=said.finish_reason,
                )

    # 11. The answer, with what ran it. The memory figure is the larger of
    # the two turns', with how that one was measured: the two kinds of
    # figure must never be mixed.
    measured = [each for each in seen if each.memory_gb is not None]
    most = max(measured, key=lambda each: each.memory_gb or 0.0) if measured else None
    return Answer(
        text=said.text,
        parsed=parsed,
        model_id=model.id,
        repo_id=model.repo_id,
        revision=model.revision,
        runtime=said.runtime,
        runtime_version=said.runtime_version,
        prompt_version=prompt_version,
        seconds=round(sum(each.seconds for each in seen), 2),
        load_seconds=round(sum(each.load_seconds for each in seen), 2),
        memory_gb=None if most is None else most.memory_gb,
        memory_source=None if most is None else most.memory_source,
        finish_reason=said.finish_reason,
        ran_from=ran_from,
        attempts=len(seen),
        demo=any(each.demo for each in seen),
    )


def _who(who: str | uuid.UUID) -> tuple[str | None, uuid.UUID | None]:
    if isinstance(who, uuid.UUID):
        return None, who
    if who in ROLES:
        return who, None
    try:
        return None, uuid.UUID(who)
    except ValueError:
        raise ValueError(f"There is no role called {who!r}, and it is not a model's id.") from None


def _check_arguments(
    prompt_version: str, pictures: Sequence[bytes], max_tokens: int, timeout_s: float
) -> None:
    """A caller's mistakes, refused before anything is read."""
    if not prompt_version.strip():
        raise ValueError("A question needs its prompt version (a short name such as look-1).")
    if len(pictures) > MAX_PICTURES:
        raise ValueError(f"A model is shown at most {MAX_PICTURES} pictures at a time.")
    for jpeg in pictures:
        if len(jpeg) > MAX_PICTURE_BYTES:
            raise ValueError(
                f"A picture for a model must be at most {MAX_PICTURE_BYTES:,} bytes; make it "
                "with ai.tag_review.preview_from_file."
            )
        if not bytes(jpeg[:3]) == b"\xff\xd8\xff":
            raise ValueError("Pictures are given to a model as JPEG.")
    if max_tokens < 1:
        raise ValueError("A model must be allowed at least one token to answer with.")
    if not timeout_s > 0:
        raise ValueError("A question needs a time limit above zero.")


async def _model(db: AsyncSession, role: str | None, model_id: uuid.UUID | None) -> LocalModel:
    if role is not None:
        held = (await registry.roles(db))[role].model_id
        if held is None:
            raise NotNow(
                "no_model",
                f"No model is the {ROLE_TITLES[role]} yet. Choose one under Who does what on "
                "the AI models page.",
            )
        model_id = held
    model = None if model_id is None else await registry.get_model(db, model_id)
    if model is None:
        raise NotNow("not_installed", NO_SUCH_MODEL)
    return model


def program_problem(
    runtime: str, settings: Settings, available: Mapping[str, RuntimeKind] | None = None
) -> str | None:
    """None when the program that runs `runtime` models is installed here; else
    the sentence a model's turn would stop with at step 5, word for word (never
    a path). Disk work: call it in a thread.

    For a page that offers to run a model now (Compare), so it can say so
    before anything is started rather than after the turn has failed.
    """
    try:
        _running(runtime, settings, available)
    except NotNow as err:
        return str(err)
    return None


def _running(
    runtime: str, settings: Settings, available: Mapping[str, RuntimeKind] | None = None
) -> _Running:
    """Step 5, in a thread: the program that runs this model, installed here.
    `available` is the install's kinds when the caller already has them."""
    kind = (kinds(settings) if available is None else available).get(runtime)
    if kind is None:
        raise NotNow("no_runtime", registry.NO_PROGRAM_HERE)
    root: Path | None = None
    try:
        root = runtimes.root(settings)
        found = runtimes.current_dir(root, kind.name)
    except runtimes.RuntimeProblem as err:
        said = str(err)
        if root is not None and str(root) in said:
            # The damaged-record sentence names its file under the runtimes
            # folder: a path everyone who can see the job would read.
            raise NotNow("no_runtime", RECORD_DAMAGED, detail=said) from None
        raise NotNow("no_runtime", said) from None
    if found is None:
        raise NotNow(
            "no_runtime",
            f"{kind.label} is not installed. Install it under Software that runs models on "
            "the AI models page.",
        )
    return _Running(kind, found[1])


def _folder(
    files: library.ModelFiles, for_role: bool, settings: Settings
) -> tuple[Path, Literal["this_machine", "library"]]:
    """Step 7, in a thread: a role's model runs only from this machine; a model
    asked by id (a Test) may run from the library."""
    try:
        folder = library.serving_dir(files, need_local=for_role, settings=settings)
    except library.LibraryProblem as err:
        message = str(err)
        if for_role and message == library.NOT_ON_THIS_MACHINE:
            raise NotNow("not_here", message) from None
        if not err.resumable:
            raise NotNow("not_installed", message) from None
        raise NotNow("library_away", message) from None
    elsewhere = library.copies_to_this_machine(settings) and folder != library.local_dir(
        files, settings
    )
    return folder, "library" if elsewhere else "this_machine"


@dataclass(frozen=True)
class _Said:
    """One answer from the model server, read."""

    text: str
    finish_reason: str
    runtime: str
    runtime_version: str
    seconds: float
    load_seconds: float
    memory_gb: float | None
    memory_source: Literal["runtime", "process"] | None
    demo: bool


async def _chat(
    db: AsyncSession,
    server: ModelServer,
    body: dict[str, Any],
    model: LocalModel,
    running: _Running,
    *,
    idle_s: float,
    timeout_s: float,
    now: datetime,
) -> _Said:
    """Step 9: one request, its refusals turned into the two exceptions, and
    what the model was seen to use recorded on its row."""
    began = time.monotonic()
    try:
        answer = await asyncio.to_thread(server.chat, body, idle_s=idle_s, timeout_s=timeout_s)
    except ServerUnreachable:
        raise NotNow("server_down", SERVER_DOWN, now + SERVER_DOWN_WAIT) from None
    except ServerRefused as err:
        raise _refused(err) from None
    took = time.monotonic() - began

    said = _read(answer, running, took)
    await registry.note_loaded(db, model.id, said.runtime, said.runtime_version)
    if said.memory_gb is not None:
        await registry.note_memory(db, model.id, said.memory_gb)
    await db.flush()
    return said


def _refused(err: ServerRefused) -> NotNow | ModelFailed:
    message = str(err)
    if err.code == "busy":
        return NotNow("busy", message)
    if err.code == "library_away":
        return NotNow("library_away", message)
    if err.code == "no_runtime":
        return NotNow("no_runtime", message)
    failure: Failure
    if err.code in ("load_failed", "load_timeout", "port_in_use"):
        failure = "load_failed"
    elif err.code in ("bad_files", "not_allowed"):
        failure = "bad_files"
    elif err.code in ("died", "timeout"):
        failure = cast(Failure, err.code)
    else:
        failure = "runtime_error"
    return ModelFailed(failure, message, detail=err.detail)


def _read(answer: Mapping[str, Any], running: _Running, took: float) -> _Said:
    """The text and how it ended, from the OpenAI shape; the facts from the
    server's own `framefound` object (what it actually loaded and how long
    that took)."""
    choices = answer.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(choice, dict) or not isinstance(message, dict):
        raise ModelFailed("runtime_error", NOT_A_FORM.format(label=running.kind.label))
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise ModelFailed("runtime_error", NOT_A_FORM.format(label=running.kind.label))
    finish = choice.get("finish_reason")

    facts = answer.get("framefound")
    facts = facts if isinstance(facts, dict) else {}
    load_seconds = _number(facts.get("load_seconds")) or 0.0
    answered = _number(facts.get("answer_seconds"))
    memory = facts.get("memory_gb")
    source = facts.get("memory_source")
    runtime = facts.get("runtime")
    version = facts.get("runtime_version")
    return _Said(
        text=content,
        finish_reason=finish if isinstance(finish, str) else "",
        runtime=runtime if isinstance(runtime, str) and runtime else running.kind.name,
        runtime_version=version if isinstance(version, str) and version else running.version,
        seconds=answered if answered is not None else max(0.0, took - load_seconds),
        load_seconds=load_seconds,
        memory_gb=_number(memory),
        memory_source=source if source in ("runtime", "process") else None,
        demo=facts.get("demo") is True,
    )


def _number(value: object) -> float | None:
    """A non-negative finite number from the server's facts, else None."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _parsed(schema: type[BaseModel], text: str) -> BaseModel | None:
    try:
        return schema.model_validate_json(extract_json(text))
    except (ValidationError, ValueError):
        return None


def _cut_off(said: _Said) -> str:
    # "length" means it ran out of room: a different way out from a model
    # that wrote the wrong thing, and the remedy differs.
    return " (it ran out of room before it finished)" if said.finish_reason == "length" else ""


# --- the sweep ---------------------------------------------------------------------


async def sweep(
    db: AsyncSession,
    server: ModelServer,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> str | None:
    """Unload a model that is loaded but may not be now: paused since, or over
    the budget in force (the window closed). Returns why, or None.

    The lane's tick runs this once a minute, so the rule does not wait for
    the next question (D3). A status it cannot read, or a loaded folder that
    is no model's row, is left alone.
    """
    settings = settings or get_settings()
    if not availability(settings).ok:
        return None
    status = await asyncio.to_thread(server.status)
    # Only a model at rest. One that is loading or answering was allowed when
    # it was asked; unloading it would wait out that answer and hold up the
    # lane's tick. The next sweep looks again.
    if not isinstance(status, dict) or status.get("state") != "ready":
        return None
    config = await load_config(db)
    if config.paused:
        why = schedule.PAUSED
    else:
        model = await loaded_model(db, status)
        if model is None:
            return None
        decision = schedule.decide(
            registry.memory_gb(model), now or datetime.now(), config.budgets()
        )
        if decision.run:
            return None
        why = decision.message
    try:
        unloaded = await asyncio.to_thread(server.unload)
    except (ServerRefused, ServerUnreachable):
        # It is answering a request (or went away): the next sweep looks again.
        return None
    return why if unloaded else None


async def loaded_model(db: AsyncSession, status: Mapping[str, Any]) -> LocalModel | None:
    """The row for the model the server says is loaded, by its repository and
    build; which build of the two a variant folder holds is read from the
    folder's last part (`<revision>[/<variant>]`, library.py)."""
    repo_id, revision, folder = status.get("repo_id"), status.get("revision"), status.get("folder")
    if not isinstance(repo_id, str) or not isinstance(revision, str) or not isinstance(folder, str):
        return None
    where = PurePath(folder)
    if where.name == revision:
        variant = ""
    elif where.parent.name == revision:
        variant = where.name
    else:
        return None
    return await registry.find_build(db, repo_id, revision, variant)
