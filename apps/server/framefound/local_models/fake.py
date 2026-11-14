"""Stand-ins for everything outside FrameFound that local models touch.

In the package, not under tests/, so three users share one set: the tests of
this phase, the tests of every later phase that asks a model something, and
the demo mode a developer reviews the pages in. Nothing here is reachable on
a real install unless a test or the demo builds it.

`FakeHub` is Hugging Face: model pages kept as the JSON the real site
answers with (so the real parser reads them), file bytes kept in a dict, a
record of every call, and switches for the ways the real thing goes wrong.

`DemoKind` is the software that runs models, for the demo: it says which
models it would run by the real program's rules, and runs none.
`DemoGgufKind` is the same for GGUF builds, by llama.cpp's rules.

`FakeRunner` is `proc.run`; `FakeSpawner` and `FakeChild` are `proc.spawn` and
the process it starts, for the model server's tests: each start is recorded
and the "process" dies, hangs or stops only as it is scripted to.

`FakeModelServer` is the model server, for everything that asks a model
(`ask.ask(server=...)`, and the demo): it answers at once, the same answer
for the same question, and a question with a schema gets a valid instance
of it (`fake_answer`), bounds kept to. A later phase says what it answers
for its own schema with `FakeModelServer.answer_for`.

The pages in `demo_data/*.json` are trimmed model-info answers. Each file's
`framefound_note` says which of its values were recorded from the live site
on 2026-11-12 and which are stand-ins; `fixture()` removes that key and
`framefound_contents` (the text of files a test needs to read) before anyone
parses the page.
"""

import copy
import hashlib
import json
import math
import random
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from framefound.local_models import gguf, llama_release, policy
from framefound.local_models import server as model_server
from framefound.local_models.files import WEIGHTS, FilePlan, read_manifest
from framefound.local_models.hub import (
    NO_ACCESS,
    UNREACHABLE,
    FetchJson,
    HubError,
    Problem,
    RepoFile,
    RepoInfo,
    Stopped,
    accept_terms_first,
    no_such_build,
    no_such_file,
    no_such_model,
    now_points_to,
    parse_info,
)
from framefound.local_models.kinds import Fit, LlamaCpp, MlxVlm, Serve, Support, Variant
from framefound.native.proc import CommandFailed

DEMO_DATA = Path(__file__).with_name("demo_data")
# In a fixture, the publisher part of a model that a blocked publisher made.
# Filled in when the file is read, so no such publisher's name is written
# anywhere but policy.DEFAULT_BLOCKED.
BLOCKED_PLACEHOLDER = "__BLOCKED_PUBLISHER__"


def fixture_names() -> list[str]:
    """The fixture files at the top of `demo_data`, without `.json`: the MLX-era ones.

    The GGUF repositories live in `demo_data/gguf/` (`gguf_fixture_names`), so
    the tests that walk every fixture under MLX-VLM's rules alone keep their
    list.
    """
    return sorted(path.stem for path in DEMO_DATA.glob("*.json"))


def gguf_fixture_names() -> list[str]:
    """The GGUF repositories' fixtures, as `fixture` reads them (`"gguf/<name>"`)."""
    return sorted(f"gguf/{path.stem}" for path in (DEMO_DATA / "gguf").glob("*.json"))


def fixture(name: str) -> dict[str, Any]:
    """One model page from `demo_data`, as Hugging Face would answer it."""
    return _read(name)[0]


def fixture_contents(name: str) -> dict[str, bytes]:
    """The files of that model whose text the fixture carries, by path."""
    return _read(name)[1]


def _read(name: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    text = (DEMO_DATA / f"{name}.json").read_text(encoding="utf-8")
    payload = json.loads(text.replace(BLOCKED_PLACEHOLDER, policy.DEFAULT_BLOCKED[0]))
    payload.pop("framefound_note", None)
    contents = payload.pop("framefound_contents", {})
    return payload, {path: body.encode() for path, body in contents.items()}


@dataclass
class _Switches:
    """What is going wrong. One object, shared by every view of the same hub."""

    drop_after: int | None = None
    lies: dict[str, int] = field(default_factory=dict)
    gated: dict[str, frozenset[str] | None] = field(default_factory=dict)
    broken: dict[str, Problem] = field(default_factory=dict)
    seconds_per_chunk: float = 0.0
    small: int | None = None
    forward_capitals: bool = False


def _ignore(_count: int) -> None:
    return None


def _never() -> bool:
    return False


class FakeHub:
    """Hugging Face, in memory. Implements `hub.Hub`.

    `payloads` maps a model's name to its model-info answer; `contents` maps
    `(name, path)` to a file's bytes; `calls` records every question as a
    tuple starting `"info"`, `"read_text"` or `"fetch"`.

    Names are matched without regard to case, and a page asked for in other
    capitals is answered under its own spelling. That is an assumption about
    the real site, not something seen: it may instead forward such a name
    (a redirect, which `HttpHub` reports as `moved`). `forward_capitals()`
    makes this hub behave that second way, and the check is tested against
    both.

    The sentences of its errors are `hub.py`'s own (`no_such_model`,
    `accept_terms_first`, …), so what a test or the demo shows is what the
    real client would say.

    Asking `fetch` for a file that has no bytes is an `AssertionError`
    naming the fix (`shrink` or `small_files`): the real sizes in the
    fixtures are gigabytes, and a fake that invented them silently would
    fill a test's disk.
    """

    def __init__(
        self,
        payloads: Mapping[str, Mapping[str, Any]] | None = None,
        contents: Mapping[tuple[str, str], bytes] | None = None,
        *,
        token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        chunk_bytes: int = 1024,
    ) -> None:
        self.payloads: dict[str, dict[str, Any]] = {
            name: copy.deepcopy(dict(payload)) for name, payload in (payloads or {}).items()
        }
        self.contents: dict[tuple[str, str], bytes] = dict(contents or {})
        self.calls: list[tuple[Any, ...]] = []
        self.token = token
        self.chunk_bytes = chunk_bytes
        self._sleep = sleep
        self._switches = _Switches()

    @classmethod
    def from_fixtures(
        cls,
        *,
        token: str | None = None,
        sleep: Callable[[float], None] = time.sleep,
        chunk_bytes: int = 1024,
    ) -> "FakeHub":
        """A hub that knows every model in `demo_data`, the GGUF repositories included."""
        hub = cls(token=token, sleep=sleep, chunk_bytes=chunk_bytes)
        for name in (*fixture_names(), *gguf_fixture_names()):
            payload, contents = _read(name)
            hub.payloads[payload["id"]] = payload
            for path, body in contents.items():
                hub.contents[(payload["id"], path)] = body
        return hub

    def with_token(self, token: str | None) -> "FakeHub":
        """The same hub as seen with another token (or none): `Deps.hub_for`'s shape.

        Pages, files, the record of calls and the switches are shared, so a
        test keeps one hub and reads everything off it.
        """
        view = copy.copy(self)
        view.token = token
        return view

    # --- switches -------------------------------------------------------------

    def fail_after(self, n_bytes: int) -> None:
        """The connection drops once `n_bytes` more have been served. Once: a resume works."""
        self._switches.drop_after = n_bytes

    def lie_about(self, path: str, times: int = 1) -> None:
        """Serve the wrong bytes (same length) for `path`, for that many whole downloads."""
        self._switches.lies[path] = times

    def need_token(self, repo_id: str, accepts: Collection[str] | None = None) -> None:
        """Gate a model, as a maker who asks for an account does.

        Its page still answers anyone, and says the model is gated (if it
        did not already, it now says `"auto"`): on the real site the two go
        together, and a page that called a gated model open would have the
        check report "no account needed". Its files answer `needs_token`
        with no token and `no_access` with a token that is not in `accepts`
        (None: any token will do).
        """
        self._switches.gated[repo_id.casefold()] = None if accepts is None else frozenset(accepts)
        for name, payload in self.payloads.items():
            if name.casefold() == repo_id.casefold() and not payload.get("gated"):
                payload["gated"] = "auto"

    def forward_capitals(self) -> None:
        """Answer a name asked for in other capitals as moved to its own spelling.

        The other way the real site may treat such a name (see the class's
        note). Only `info` is asked by a name somebody typed; files are
        always asked for under the spelling `info` gave.
        """
        self._switches.forward_capitals = True

    def fail_info(self, repo_id: str, problem: Problem = "unreachable") -> None:
        """Asking about this model fails that way, as the network does part-way through a chain."""
        self._switches.broken[repo_id.casefold()] = problem

    def slow(self, seconds_per_chunk: float) -> None:
        """Wait that long before each piece of a file, through the `sleep` this hub was given."""
        self._switches.seconds_per_chunk = seconds_per_chunk

    def small_files(self, n_bytes: int = 4096) -> None:
        """Write only the first `n_bytes` of each file, but report progress against its listed size.

        For the demo: a nine-gigabyte model "downloads" with a bar that moves
        like the real thing and leaves a few kilobytes on disk. Such a file
        does not pass `files.check_file`, and is not meant to.
        """
        self._switches.small = n_bytes

    def shrink(self, repo_id: str, n_bytes: int = 2048) -> None:
        """Make a model small and true: a few kilobytes per file, the page rewritten to match.

        Every listed file gets bytes (its own from `contents` if it has
        them), and its size and checksum on the page become those of the
        bytes. After this a download of the model really passes
        `files.check_file`, which is what a test of downloading needs.
        """
        key, payload = self._page(repo_id)
        for entry in payload["siblings"]:
            path = entry["rfilename"]
            body = self.contents.get((key, path))
            if body is None:
                body = _filler(key, path, min(int(entry.get("size") or n_bytes), n_bytes))
            self.contents[(key, path)] = body
            entry["size"] = len(body)
            entry.pop("blobId", None)
            if "lfs" in entry or path.lower().endswith(WEIGHTS):
                entry["lfs"] = {"sha256": hashlib.sha256(body).hexdigest(), "size": len(body)}
            else:
                entry["blobId"] = _git_blob_id(body)

    # --- what a Hub does ------------------------------------------------------

    @property
    def fetches(self) -> int:
        """How many times a file was asked for. A check must leave this at zero."""
        return sum(1 for call in self.calls if call[0] == "fetch")

    def info(self, repo_id: str, revision: str | None = None) -> RepoInfo:
        self.calls.append(("info", repo_id, revision))
        problem = self._switches.broken.get(repo_id.casefold())
        if problem is not None:
            raise HubError(problem, UNREACHABLE)
        key, payload = self._page(repo_id)
        if self._switches.forward_capitals and key != repo_id:
            raise HubError("moved", now_points_to(key), 307, moved_to=key)
        if revision is not None and revision != payload.get("sha"):
            raise HubError("not_found", no_such_build(repo_id), 404)
        return parse_info(payload)

    def read_text(self, repo_id: str, revision: str, path: str, max_bytes: int = 4_000_000) -> str:
        self.calls.append(("read_text", repo_id, revision, path))
        key, _file = self._listed(repo_id, revision, path)
        self._admit(key)
        body = self.contents.get((key, path))
        if body is None:
            # A settings file nobody wrote for the test: an empty, clean one.
            body = b"{}" if path.lower().endswith(".json") else b""
        return body[:max_bytes].decode("utf-8-sig", errors="replace")

    def fetch(
        self,
        repo_id: str,
        revision: str,
        path: str,
        target: Path,
        *,
        on_bytes: Callable[[int], None] = _ignore,
        should_stop: Callable[[], bool] = _never,
    ) -> None:
        start = target.stat().st_size if target.is_file() else 0
        self.calls.append(("fetch", repo_id, revision, path, start))
        key, file = self._listed(repo_id, revision, path)
        self._admit(key)
        switches = self._switches
        body = self.contents.get((key, path))
        if switches.small is not None and body is None:
            body = _filler(key, path, min(file.size, switches.small))
        if body is None:
            raise AssertionError(
                f"FakeHub has no bytes for {path} of {key}. Put them in `contents`, or call "
                "shrink() for a small true model, or small_files() for the demo."
            )
        if switches.lies.get(path, 0) > 0:
            body = bytes(byte ^ 0xFF for byte in body)

        # With small_files the bytes written are few and the progress
        # reported is the listed size, spread evenly over them.
        full = file.size if switches.small is not None else len(body)

        def reported(written: int) -> int:
            return full if written >= len(body) else written * full // max(len(body), 1)

        target.parent.mkdir(parents=True, exist_ok=True)
        if start > len(body):
            # More is there than the file holds. The real client asks for the
            # bytes from there on, is told there are none (416), and takes
            # the file for whole: nothing is written and nothing reported.
            # The caller's `files.check_file` is what finds it the wrong
            # size. Starting it again here would test a path that does not
            # exist.
            return
        position = start
        with target.open("ab") as out:
            while position < len(body):
                if should_stop():
                    raise Stopped()
                if switches.seconds_per_chunk:
                    self._sleep(switches.seconds_per_chunk)
                chunk = body[position : position + self.chunk_bytes]
                dropped = switches.drop_after is not None and switches.drop_after < len(chunk)
                if switches.drop_after is not None:
                    chunk = chunk[: switches.drop_after]
                    switches.drop_after = None if dropped else switches.drop_after - len(chunk)
                out.write(chunk)
                on_bytes(reported(position + len(chunk)) - reported(position))
                position += len(chunk)
                if dropped:
                    raise HubError("unreachable", UNREACHABLE)
        if switches.lies.get(path, 0) > 0:
            switches.lies[path] -= 1

    # --- inside ---------------------------------------------------------------

    def _page(self, repo_id: str) -> tuple[str, dict[str, Any]]:
        for name, payload in self.payloads.items():
            if name.casefold() == repo_id.casefold():
                return name, payload
        raise HubError("not_found", no_such_model(repo_id), 404)

    def _listed(self, repo_id: str, revision: str, path: str) -> tuple[str, RepoFile]:
        key, payload = self._page(repo_id)
        if revision != payload.get("sha"):
            raise HubError("not_found", no_such_build(repo_id), 404)
        for file in parse_info(payload).files:
            if file.path == path:
                return key, file
        raise HubError("not_found", no_such_file(repo_id, path), 404)

    def _admit(self, key: str) -> None:
        if key.casefold() not in self._switches.gated:
            return
        accepted = self._switches.gated[key.casefold()]
        if not self.token:
            raise HubError("needs_token", accept_terms_first(key), 401)
        if accepted is not None and self.token not in accepted:
            raise HubError("no_access", NO_ACCESS, 403)


class DemoKind:
    """The runtime kind of the demo: it describes models and runs nothing.

    Implements `kinds.RuntimeKind`. It judges a repository by the real
    kind's rules, so a developer reviewing the pages sees the sentences an
    owner will see; only its own name says it is not real. `kinds.kinds()`
    builds it in demo mode and nowhere else.
    """

    name = "demo"
    label = "Demo runtime (not a real model)"
    summary = (
        "Stands in for the software that runs models, so these pages can be looked over "
        "on any machine. It runs nothing."
    )
    demo = True
    weights: tuple[str, ...] = MlxVlm.weights
    # Nothing is installed, so no installer is asked for.
    needs_uv = False
    needs_compare = False

    def supported_here(self, system: str, machine: str) -> str | None:
        # Wherever the demo is switched on: that is the point of it.
        return None

    def supports(self, info: RepoInfo, plan: FilePlan) -> Support:
        return MlxVlm().supports(info, plan)

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        return MlxVlm().prefer(sized)

    def latest(self, fetch: FetchJson) -> str:
        # Made up, and nothing is asked: the demo never reaches a network.
        return "2.0"

    # --- the running half: the real kind's words, and nothing that runs -------

    update_note = ""
    # The demo's own newest version (`latest`), as the field's example.
    version_hint = "2.0"
    health_path = MlxVlm.health_path
    chat_path = MlxVlm.chat_path

    def clean_version(self, text: str) -> str:
        return MlxVlm().clean_version(text)

    def is_newer(self, latest: str, installed: str) -> bool:
        return MlxVlm().is_newer(latest, installed)

    def about(self, folder: Path) -> str:
        return ""

    def install_steps(self, folder: Path, version: str, uv: str) -> list[list[str]]:
        # Installing the demo makes its folder and nothing else.
        return []

    def check_step(self, folder: Path) -> list[str]:
        # Never run: an install with no steps has nothing to check.
        return []

    def freeze_step(self, folder: Path, uv: str) -> list[str] | None:
        return None

    def program(self, folder: Path) -> Path | None:
        # Its install is its folder, so the folder is all there is to find.
        return folder if folder.is_dir() else None

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        # The demo's model server is FakeModelServer, which starts nothing.
        # Reaching here means a real supervisor was handed the stand-in.
        raise RuntimeError("The demo runtime is never started.")

    def auth_headers(self, key: str) -> dict[str, str]:
        return MlxVlm().auth_headers(key)

    def is_ready(self, status: int, body: Mapping[str, Any]) -> bool:
        return MlxVlm().is_ready(status, body)

    def request(self, body: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
        return MlxVlm().request(body, model_dir)

    def picture_part(self, jpeg: bytes) -> dict[str, Any]:
        return MlxVlm().picture_part(jpeg)

    def memory_gb(self, answer: Mapping[str, Any]) -> float | None:
        return MlxVlm().memory_gb(answer)

    def owns(self, command_line: str, folder: Path) -> bool:
        # It starts nothing, so no process is ever its own to stop.
        return False


class DemoGgufKind:
    """The demo's llama.cpp: GGUF builds judged by the real kind's rules, nothing run (L6 D15).

    Implements `kinds.RuntimeKind`. Which repositories it takes, which build
    it suggests, what a version looks like and when one is newer are
    `LlamaCpp`'s own, so the Check, the build list and the version field
    say what an owner will see. Installing it makes a folder; it starts
    nothing. Like `DemoKind`, `kinds.kinds()` builds it in demo mode and
    nowhere else (L6 P21).
    """

    name = "demo-gguf"
    label = "Demo llama.cpp (not a real program)"
    summary = (
        "Stands in for llama.cpp, the software for GGUF builds, so these pages can be looked "
        "over on any machine. It runs nothing."
    )
    demo = True
    weights: tuple[str, ...] = LlamaCpp.weights
    needs_uv = False
    # As the real one: a GGUF model nobody compared is asked about before a role.
    needs_compare = True
    update_note = LlamaCpp.update_note
    version_hint = LlamaCpp.version_hint
    health_path = LlamaCpp.health_path
    chat_path = LlamaCpp.chat_path
    # The newest numbered version it says is published: one past the real 0.9.0,
    # so a reviewer who installs 0.9.0 has an update to take and then to go back from.
    newest = "0.10.0"

    def supported_here(self, system: str, machine: str) -> str | None:
        return None

    def supports(self, info: RepoInfo, plan: FilePlan) -> Support:
        return LlamaCpp().supports(info, plan)

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        return LlamaCpp().prefer(sized)

    def latest(self, fetch: FetchJson) -> str:
        # Made up, and GitHub is never asked.
        return self.newest

    def clean_version(self, text: str) -> str:
        return LlamaCpp().clean_version(text)

    def is_newer(self, latest: str, installed: str) -> bool:
        return LlamaCpp().is_newer(latest, installed)

    def about(self, folder: Path) -> str:
        # The real one reads the build from what its install recorded; nothing
        # is recorded here, so the made-up build of FakeRunner's llama.cpp is
        # said instead. A daily build is its own build, said by its name alone.
        version = folder.name
        if llama_release.DAILY.fullmatch(version) or not llama_release.NUMBERED.fullmatch(version):
            return ""
        return f"build {llama_build(version)}"

    def install_steps(self, folder: Path, version: str, uv: str) -> list[list[str]]:
        # Installing the demo makes its folder and nothing else: no GitHub, no program.
        return []

    def check_step(self, folder: Path) -> list[str]:
        return []

    def freeze_step(self, folder: Path, uv: str) -> list[str] | None:
        return None

    def program(self, folder: Path) -> Path | None:
        return folder if folder.is_dir() else None

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        raise RuntimeError("The demo runtime is never started.")

    def auth_headers(self, key: str) -> dict[str, str]:
        return LlamaCpp().auth_headers(key)

    def is_ready(self, status: int, body: Mapping[str, Any]) -> bool:
        return LlamaCpp().is_ready(status, body)

    def request(self, body: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
        return LlamaCpp().request(body, model_dir)

    def picture_part(self, jpeg: bytes) -> dict[str, Any]:
        return LlamaCpp().picture_part(jpeg)

    def memory_gb(self, answer: Mapping[str, Any]) -> float | None:
        return LlamaCpp().memory_gb(answer)

    def owns(self, command_line: str, folder: Path) -> bool:
        return False


# What a scripted program does: (returncode, stdout), (returncode, stdout,
# stderr), an exception to raise (as `proc.run` raises `CommandFailed` for a
# program that is not there or ran out of time), or a function of the argv
# giving one of those.
Outcome = tuple[int, str] | tuple[int, str, str] | BaseException
Script = Outcome | Callable[[list[str]], Outcome]


@dataclass(frozen=True)
class Ran:
    """One command a `FakeRunner` was asked to run, as it was asked."""

    argv: list[str]
    env: dict[str, str] | None
    cwd: Path | None
    capture: bool
    check: bool
    timeout: float | None


class FakeRunner:
    """`proc.run`, scripted. Runs nothing; records everything.

    Each script is keyed by the program's file name, optionally followed by
    the words that come after it: `"uv"`, `"uv pip install"`, `"python"`.
    The key with the most words that matches a command answers it, so
    `"uv pip install"` can fail while `"uv"` (the venv) succeeds. A command
    nothing answers is an `AssertionError` naming it: a test must say what
    every program it causes to run does, and a new command is never run by
    accident.

    A module run by an interpreter (`<python> -m framefound.local_models.
    llama_release fetch …`) can also be keyed by the module's last name and
    what follows it (`"llama_release fetch"`): the interpreter is
    `sys.executable`, `python.exe` on one machine and `python3.12` on
    another, so a key on its file name would hold on one of them only.

    `check=True` behaves as `proc.run` does: a non-zero exit raises
    `proc.CommandFailed`. With `make_venvs`, `uv venv <folder>` creates
    `<folder>/bin/python`, as the real one does, so what comes next finds the
    interpreter it expects. With `make_llama`, llama.cpp's three steps that no
    script answers do what the real ones would, and a `fetch` that succeeds
    (scripted or not) leaves what the real one leaves: `llama.json` and an
    empty `dist/llama-<build>/llama-server`, so `llama_release.program`,
    `check` and `about` find them.
    """

    def __init__(
        self,
        scripts: Mapping[str, Script] | None = None,
        *,
        make_venvs: bool = False,
        make_llama: bool = False,
    ) -> None:
        self.scripts: dict[str, Script] = dict(scripts or {})
        self.make_venvs = make_venvs
        self.make_llama = make_llama
        self.ran: list[Ran] = []

    def answer(self, program: str, outcome: Script) -> None:
        """What `program` (a key as above) does from now on."""
        self.scripts[program] = outcome

    @property
    def calls(self) -> list[list[str]]:
        """The argv of every command run, in order."""
        return [ran.argv for ran in self.ran]

    def __call__(
        self,
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        capture: bool = False,
        check: bool = True,
        stdout: Any = None,
        stdin_text: str | None = None,
        timeout: float | None = None,
    ) -> "subprocess.CompletedProcess[str]":
        words = [str(word) for word in argv]
        self.ran.append(
            Ran(
                argv=words,
                env=dict(env) if env is not None else None,
                cwd=cwd,
                capture=capture,
                check=check,
                timeout=timeout,
            )
        )
        llama = _llama_step(words) if self.make_llama else None
        script = self._script_for(words, default=None if llama is None else _llama_default)
        outcome = script(words) if callable(script) else script
        if isinstance(outcome, BaseException):
            raise outcome
        returncode, out = outcome[0], outcome[1]
        err = outcome[2] if len(outcome) == 3 else ""
        if returncode == 0 and self.make_venvs and words[1:3] == ["venv", "--python"]:
            interpreter = Path(words[-1]) / "bin" / "python"
            interpreter.parent.mkdir(parents=True, exist_ok=True)
            interpreter.touch()
        if returncode == 0 and llama is not None and llama[0] == "fetch":
            _llama_fetched(llama[1], _llama_version(words))
        if check and returncode != 0:
            tail = "\n".join(err.strip().splitlines()[-8:]) if capture else ""
            detail = f":\n{tail}" if tail else ""
            raise CommandFailed(f"{words[0]} failed (exit {returncode}){detail}")
        return subprocess.CompletedProcess(
            words, returncode, out if capture else None, err if capture else None
        )

    def _script_for(self, words: list[str], *, default: Script | None = None) -> Script:
        ways = [[Path(words[0]).name, *words[1:]]]
        if len(words) > 2 and words[1] == "-m":
            ways.append([words[2].rpartition(".")[2], *words[3:]])
        best: tuple[int, Script] | None = None
        for key, script in self.scripts.items():
            wanted = key.split()
            if any(said[: len(wanted)] == wanted for said in ways) and (
                best is None or len(wanted) > best[0]
            ):
                best = (len(wanted), script)
        if best is None and default is not None:
            return default
        if best is None:
            raise AssertionError(
                f"FakeRunner was asked to run {words!r}, and no script answers "
                f"{Path(words[0]).name!r}. Give it one with answer()."
            )
        return best[1]


# The daily build a numbered llama.cpp version names, for FakeRunner's
# llama.cpp: 0.9.0 is cut from b12110 (L6 G4); others are made up.
LLAMA_BUILDS = {"0.9.0": "b12110", "0.10.0": "b12400"}


def llama_build(version: str) -> str:
    """The daily build FakeRunner's `fetch` says `version` comes from."""
    if llama_release.DAILY.fullmatch(version):
        return version
    made_up = 12000 + int(hashlib.sha256(version.encode()).hexdigest(), 16) % 1000
    return LLAMA_BUILDS.get(version, f"b{made_up}")


def _llama_step(words: list[str]) -> tuple[str, Path] | None:
    """(`fetch`/`check`/`about`, the version folder) when `words` runs llama_release."""
    if len(words) < 6 or words[1:3] != ["-m", "framefound.local_models.llama_release"]:
        return None
    command, flag = words[3], {"fetch": "--into", "check": "--in", "about": "--in"}.get(words[3])
    if flag is None or flag not in words:
        return None
    return command, Path(words[words.index(flag) + 1])


def _llama_version(words: list[str]) -> str:
    return words[words.index("--version") + 1] if "--version" in words else ""


def _llama_default(words: list[str]) -> Outcome:
    """What llama_release's step would print, without GitHub and without the program."""
    step = _llama_step(words)
    assert step is not None
    command, folder = step
    if command == "fetch":
        version = _llama_version(words)
        tag = llama_build(version)
        asset = llama_release.ASSET.format(tag=tag)
        return 0, (
            f"Asking GitHub about {version}…\nDownloading {asset} (17 MB)…\nUnpacking…\n"
            f"llama.cpp {version} (build {tag}) is in {folder}.\n"
        )
    record = llama_release.record(folder)
    recorded = record.get("version") if record is not None else None
    if command == "check":
        if llama_release.program(folder) is None or not isinstance(recorded, str):
            return 1, "", llama_release.NOTHING_INSTALLED.format(folder=folder)
        return 0, f"{recorded}\n"
    about = llama_release.about(folder)
    said = "version: 1 (fake llama-server, not a real program)" if recorded else ""
    return 0, "\n".join(line for line in (about, said) if line) + "\n"


def _llama_fetched(folder: Path, version: str) -> None:
    """What a `fetch` that worked leaves in the version folder, as llama_release.fetch writes it."""
    if llama_release.record(folder) is not None:
        return
    tag = llama_build(version)
    server = f"llama-{tag}/{llama_release.PROGRAM}"
    program = folder / llama_release.DIST / server
    program.parent.mkdir(parents=True, exist_ok=True)
    program.touch()
    body = {
        "version": version,
        "tag": tag,
        "asset": llama_release.ASSET.format(tag=tag),
        "sha256": hashlib.sha256(tag.encode()).hexdigest(),
        "size": 16_896_103,
        "server": server,
        "fetched_at": "2026-11-12T18:39:10Z",
    }
    (folder / llama_release.RECORD).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


class FakeChild:
    """A started program, as the model server holds one: `subprocess.Popen`'s stopping half.

    It runs nothing. It is running until it is told to stop or is scripted
    to stop by itself: `exits_with` after `after_polls` looks at it (a
    runtime that dies while loading), or `die()` at any moment (one that
    dies later). A `stubborn` child ignores `terminate` and only `kill`
    ends it, as a hung program does; one that `survives_kill` outlives
    even that (stuck in the kernel, reading a share that has hung) until
    `die()`. `signals` records what it was sent.
    """

    def __init__(
        self,
        pid: int = 4242,
        *,
        exits_with: int | None = None,
        after_polls: int = 0,
        stubborn: bool = False,
        survives_kill: bool = False,
    ) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.stubborn = stubborn
        self.survives_kill = survives_kill
        self._exits_with = exits_with
        self._polls_left = after_polls

    @property
    def running(self) -> bool:
        return self.returncode is None

    def die(self, returncode: int = 1) -> None:
        """It stops by itself, now."""
        if self.returncode is None:
            self.returncode = returncode

    def poll(self) -> int | None:
        if self.returncode is None and self._exits_with is not None:
            if self._polls_left <= 0:
                self.returncode = self._exits_with
            else:
                self._polls_left -= 1
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("terminate")
        if not self.stubborn and self.returncode is None:
            self.returncode = -15

    def kill(self) -> None:
        self.signals.append("kill")
        if not self.survives_kill and self.returncode is None:
            self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        # Nothing to wait for: it has stopped or it will not (a real wait
        # with no timeout would hang here; a test should never ask for one).
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd="fake runtime", timeout=timeout or 0.0)
        return self.returncode


@dataclass(frozen=True)
class Spawned:
    """One start a `FakeSpawner` was asked for, as it was asked."""

    argv: list[str]
    env: dict[str, str]
    cwd: Path | None


class FakeSpawner:
    """`proc.spawn`, scripted: records each start and hands back a `FakeChild`.

    The children given are handed out in order, then fresh ones. `says` is
    written to the child's output file as it starts — the runtime's last
    words, as the server's log would hold them; a function is given the
    start (its environment holds the runtime's key, so a test can make a
    runtime that prints its key). `fails` is raised instead of starting
    (a program that is not there is `proc.CommandFailed`).
    """

    def __init__(
        self,
        *children: FakeChild,
        says: str | Callable[[Spawned], str] = "",
        fails: BaseException | None = None,
    ) -> None:
        self.queued = list(children)
        self.says = says
        self.fails = fails
        self.spawned: list[Spawned] = []
        self.children: list[FakeChild] = []

    def __call__(
        self,
        argv: Sequence[str | Path],
        *,
        env: Mapping[str, str],
        cwd: Path | None = None,
        stdout: Any = None,
        stderr: Any = None,
    ) -> FakeChild:
        start = Spawned(argv=[str(word) for word in argv], env=dict(env), cwd=cwd)
        self.spawned.append(start)
        if self.fails is not None:
            raise self.fails
        said = self.says(start) if callable(self.says) else self.says
        if said and stdout is not None and hasattr(stdout, "write"):
            stdout.write(said.encode())
            stdout.flush()
        child = self.queued.pop(0) if self.queued else FakeChild(pid=4242 + len(self.spawned))
        self.children.append(child)
        return child

    @property
    def last(self) -> Spawned:
        return self.spawned[-1]


# What `FakeModelServer.refuse(code)` answers with, as the real server does.
_REFUSALS: dict[str, tuple[int, str]] = {
    "busy": (503, "The model was busy with another request for more than 2 minutes."),
    "no_runtime": (409, "MLX-VLM is not installed."),
    "load_failed": (409, "It would not load."),
    "load_timeout": (504, "It did not finish loading in 5 minutes, so it was stopped."),
    "port_in_use": (409, "Port 54806 is in use by another program."),
    "bad_files": (409, "This copy of the model is missing model.safetensors."),
    "not_allowed": (400, "Pictures are passed as data, never as an address."),
    "library_away": (503, "The model's folder could not be read just now. Try again in a moment."),
    "died": (
        502,
        "The model stopped unexpectedly while answering (it may have run out of memory).",
    ),
    "timeout": (504, "It took longer than 5 minutes to answer, so it was stopped."),
    "runtime_error": (502, "MLX-VLM could not answer (it said 500)."),
}
PROSE = (
    "The picture shows a bright room with a large window and wooden floors; it looks like "
    "an evening shot."
)


class FakeModelServer:
    """The model server, in process, for the tests and the demo: `ask.ModelServer`.

    Deterministic: the same question gets the same answer. With a schema in
    the request's `response_format` the answer is a valid instance of it
    (`fake_answer`), unless a later phase said what to answer for that
    schema (`answer_for`). Nothing is loaded and nothing is started; the
    "model" it reports is the one whose folder the request named (read from
    the folder's manifest when it has one).

    `requests` records every body as it was sent (the real server's own
    check runs on each, so a body the real one would refuse is refused here
    too). Switches: `refuse(code)` (the next chat is refused as the real
    server would), `prose_first(times)` (that many answers in words before
    the JSON — a repair turn's), `slow(seconds)` and `down` (nothing answers).
    `memory_gb`, `load_seconds`, `answer_seconds`, `finish_reason` and
    `runtime_version` are what it reports; a test sets them.
    """

    # schema name -> what to answer for it. Class-level so the demo's one
    # server and a later phase's tests share what a phase registered; read
    # only by this fake, so it does nothing on a real install.
    _answers: ClassVar[dict[str, Callable[[dict[str, Any]], str]]] = {}

    def __init__(
        self,
        *,
        sleep: Callable[[float], None] = time.sleep,
        load_seconds: float = 12.0,
        answer_seconds: float = 1.5,
        memory_gb: float | None = None,
        memory_source: str = "runtime",
        runtime_version: str | None = None,
    ) -> None:
        self.requests: list[dict[str, Any]] = []
        self.unloads = 0
        self.loaded: str | None = None
        self.down = False
        self.load_seconds = load_seconds
        self.answer_seconds = answer_seconds
        self.memory_gb = memory_gb
        # What a particular model is reported to use, from what its folder's
        # manifest says it is (repo_id, revision, runtime). None: `memory_gb`
        # for every model. The demo sets it, so a small model and a large one
        # do not report the same figure.
        self.memory_for: Callable[[Mapping[str, Any]], float | None] | None = None
        self.memory_source = memory_source
        # None: the server does not say, and `ask` records the version it found installed.
        self.runtime_version = runtime_version
        self.finish_reason = "stop"
        self._sleep = sleep
        self._delay = 0.0
        self._refusals: list[tuple[str, str | None]] = []
        self._prose = 0
        self._facts: dict[str, Any] = {}
        self._lock = threading.Lock()

    # --- switches ----------------------------------------------------------------

    @classmethod
    def answer_for(cls, schema_name: str, answer: Callable[[dict[str, Any]], str] | None) -> None:
        """What to answer for requests whose `json_schema.name` is `schema_name` (the
        schema class's name, as `ask` sends it): `answer(body)` gives the text.
        None forgets it."""
        if answer is None:
            cls._answers.pop(schema_name, None)
        else:
            cls._answers[schema_name] = answer

    def refuse(self, code: str, message: str | None = None, *, times: int = 1) -> None:
        """The next `times` chats are refused with `code`, in the real server's words."""
        if code not in _REFUSALS:
            raise ValueError(f"The model server has no refusal called {code!r}.")
        self._refusals.extend([(code, message)] * times)

    def prose_first(self, times: int = 1) -> None:
        """The next `times` answers to a question with a schema are words, not JSON."""
        self._prose = times

    def slow(self, seconds: float) -> None:
        """Every chat takes this long (through `sleep`)."""
        self._delay = seconds

    # --- what a ModelServer does -------------------------------------------------

    def status(self) -> dict[str, Any] | None:
        if self.down:
            return None
        with self._lock:
            loaded, facts = self.loaded, dict(self._facts)
        if loaded is None:
            return {
                "state": "idle",
                "folder": None,
                "repo_id": None,
                "revision": None,
                "runtime": None,
                "runtime_version": None,
                "memory_gb": None,
                "problem": None,
            }
        return {
            "state": "ready",
            "folder": loaded,
            "repo_id": facts.get("repo_id"),
            "revision": facts.get("revision"),
            "runtime": facts.get("runtime"),
            "runtime_version": self.runtime_version,
            "memory_gb": self._memory(facts),
            "problem": None,
        }

    def chat(self, body: dict[str, Any], *, idle_s: float, timeout_s: float) -> dict[str, Any]:
        # Imported here: `kinds.kinds()` imports this module in demo mode,
        # and the model server's process must not be handed a database.
        from framefound.local_models.ask import ServerRefused, ServerUnreachable

        if self.down:
            raise ServerUnreachable("The model server is not answering.")
        try:
            model_server._check_body(body)
        except model_server.Refused as err:
            raise ServerRefused(err.status, err.code, err.message, err.detail) from None
        with self._lock:
            self.requests.append(copy.deepcopy(body))
            refusal = self._refusals.pop(0) if self._refusals else None
        if refusal is not None:
            code, message = refusal
            status, said = _REFUSALS[code]
            raise ServerRefused(status, code, message or said)
        if self._delay:
            self._sleep(self._delay)

        folder = str(body.get("model", ""))
        with self._lock:
            loading = folder != self.loaded
            if loading:
                self.loaded = folder
                self._facts = _manifest_facts(folder)
            facts = dict(self._facts)
        text = self._text(body)
        memory = self._memory(facts)
        return {
            "id": "chatcmpl-made-up",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": self.finish_reason,
                }
            ],
            "framefound": {
                "repo_id": facts.get("repo_id"),
                "revision": facts.get("revision"),
                "runtime": facts.get("runtime"),
                "runtime_version": self.runtime_version,
                "load_seconds": self.load_seconds if loading else 0.0,
                "answer_seconds": self.answer_seconds,
                "memory_gb": memory,
                "memory_source": self.memory_source if memory is not None else None,
                # Nothing it says was ever thought by a model.
                "demo": True,
            },
        }

    def unload(self) -> bool:
        if self.down:
            from framefound.local_models.ask import ServerUnreachable

            raise ServerUnreachable("The model server is not answering.")
        with self._lock:
            was = self.loaded is not None
            self.loaded = None
            self._facts = {}
            self.unloads += 1
        return was

    def _memory(self, facts: Mapping[str, Any]) -> float | None:
        if self.memory_for is not None:
            return self.memory_for(facts)
        return self.memory_gb

    def _text(self, body: Mapping[str, Any]) -> str:
        """What the "model" writes: words, or JSON for the schema asked for."""
        messages = body.get("messages") or []
        question = messages[0] if messages else {}
        seed = json.dumps(question.get("content"), sort_keys=True)
        response = body.get("response_format")
        named = response.get("json_schema") if isinstance(response, Mapping) else None
        if not isinstance(named, Mapping):
            digest = hashlib.sha256(seed.encode()).hexdigest()[:8]
            return f"A made-up answer from the demo model ({digest}). Nothing was looked at."
        with self._lock:
            prose = self._prose > 0
            if prose:
                self._prose -= 1
        if prose:
            return PROSE
        name = str(named.get("name", ""))
        answer = self._answers.get(name)
        if answer is not None:
            return answer(dict(body))
        schema = named.get("schema")
        return fake_answer(schema if isinstance(schema, Mapping) else None, f"{name}\0{seed}")


def _manifest_facts(folder: str) -> dict[str, Any]:
    """Which model a folder holds, by its manifest; nothing when it has none."""
    try:
        manifest = read_manifest(Path(folder)) if folder else None
    except OSError:
        manifest = None
    if manifest is None:
        return {}
    return {
        "repo_id": manifest.repo_id,
        "revision": manifest.revision,
        "runtime": manifest.runtime,
        # Which build, and its files: a GGUF repository holds many, and only
        # one build's model and picture reader are loaded.
        "variant": manifest.variant,
        "files": [entry.path for entry in manifest.files],
    }


# Words for a made-up string: what a caption about footage might use.
_WORDS = (
    "kitchen",
    "window",
    "evening",
    "light",
    "garden",
    "porch",
    "stairs",
    "timber",
    "harbour",
    "field",
    "street",
    "drone",
    "sunset",
    "living",
    "room",
    "view",
)
_MAX_DEPTH = 24


def fake_answer(schema: Mapping[str, Any] | None, seed: str) -> str:
    """A valid instance of a JSON schema (pydantic's `model_json_schema()`), as JSON
    text, chosen by `seed`: the same seed gives the same answer.

    It honours `minimum`, `maximum` (and their exclusive forms),
    `multipleOf`, `minLength`/`maxLength`, `minItems`/`maxItems`,
    `uniqueItems`, `enum`, `const`, `$ref`/`$defs`, `anyOf`/`oneOf` and
    `allOf`; it picks numbers inside a range rather than at an end, and
    varies booleans with the seed — so a made-up answer to a bounded schema
    (sliders, say) reads like something a model might say rather than
    "every slider at its stop". It does not honour `pattern`. With no
    schema it is an empty object.
    """
    if schema is None:
        return "{}"
    # Reproducible on purpose, and secret from nobody: a made-up answer.
    rng = random.Random(hashlib.sha256(seed.encode()).digest())  # noqa: S311 - not a secret
    defs: dict[str, Any] = {}
    for key in ("definitions", "$defs"):
        found = schema.get(key)
        if isinstance(found, Mapping):
            defs.update(found)
    return json.dumps(_made_up(schema, defs, rng, 0))


def _resolved(node: Mapping[str, Any], defs: Mapping[str, Any]) -> dict[str, Any]:
    """The node with its `$ref` and `allOf` folded in (its own keys win)."""
    merged: dict[str, Any] = {}
    ref = node.get("$ref")
    if isinstance(ref, str):
        target = defs.get(ref.rsplit("/", 1)[-1])
        if isinstance(target, Mapping):
            merged.update(_resolved(target, defs))
    parts = node.get("allOf")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, Mapping):
                merged.update(_resolved(part, defs))
    merged.update({key: value for key, value in node.items() if key not in ("$ref", "allOf")})
    return merged


def _made_up(node: object, defs: Mapping[str, Any], rng: random.Random, depth: int) -> Any:
    if not isinstance(node, Mapping) or depth > _MAX_DEPTH:
        return None
    node = _resolved(node, defs)
    if "const" in node:
        return node["const"]
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        return rng.choice(enum)
    options = node.get("anyOf") or node.get("oneOf")
    if isinstance(options, list) and options:
        real = [part for part in options if not _is_null(part)]
        return _made_up(rng.choice(real), defs, rng, depth + 1) if real else None
    kind = node.get("type")
    if isinstance(kind, list):
        kinds_given = [each for each in kind if each != "null"]
        kind = kinds_given[0] if kinds_given else "null"
    if kind is None:
        kind = "object" if "properties" in node else "array" if "items" in node else "string"
    if kind == "object":
        properties = node.get("properties")
        if not isinstance(properties, Mapping):
            return {}
        return {name: _made_up(sub, defs, rng, depth + 1) for name, sub in properties.items()}
    if kind == "array":
        return _array(node, defs, rng, depth)
    if kind == "integer":
        return _integer(node, rng)
    if kind == "number":
        return _number(node, rng)
    if kind == "boolean":
        return rng.random() < 0.5
    if kind == "null":
        return None
    return _string(node, rng)


def _is_null(part: object) -> bool:
    return isinstance(part, Mapping) and part.get("type") == "null"


def _array(
    node: Mapping[str, Any], defs: Mapping[str, Any], rng: random.Random, depth: int
) -> list[Any]:
    prefix = node.get("prefixItems")
    if isinstance(prefix, list):
        return [_made_up(part, defs, rng, depth + 1) for part in prefix]
    low = max(int(node.get("minItems", 0)), 0)
    most = node.get("maxItems")
    high = max(int(most) if isinstance(most, int) else low + 3, low)
    # At least one item when one is allowed: an empty list says nothing.
    count = rng.randint(max(low, min(1, high)), high)
    items = node.get("items", {})
    if not node.get("uniqueItems"):
        return [_made_up(items, defs, rng, depth + 1) for _ in range(count)]
    found: dict[str, Any] = {}
    for _ in range(count * 8):
        if len(found) >= count:
            break
        value = _made_up(items, defs, rng, depth + 1)
        found.setdefault(json.dumps(value, sort_keys=True), value)
    return list(found.values())


def _limits(node: Mapping[str, Any], *, whole: bool) -> tuple[float, float, bool, bool]:
    """(low, high, low is open, high is open), with defaults around what is given."""
    low: float | None = None
    high: float | None = None
    low_open = high_open = False
    for key, is_open in (("minimum", False), ("exclusiveMinimum", True)):
        value = node.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        if low is None or value > low or (value == low and is_open):
            low, low_open = float(value), is_open
    for key, is_open in (("maximum", False), ("exclusiveMaximum", True)):
        value = node.get(key)
        if not isinstance(value, int | float) or isinstance(value, bool):
            continue
        if high is None or value < high or (value == high and is_open):
            high, high_open = float(value), is_open
    if low is None:
        low = (10.0 if high is None else high) - 10.0
    if high is None:
        high = low + 10.0
    if whole:
        low = float(math.floor(low) + 1 if low_open else math.ceil(low))
        high = float(math.ceil(high) - 1 if high_open else math.floor(high))
        low_open = high_open = False
    return low, high, low_open, high_open


def _inside(low: int, high: int, rng: random.Random) -> int:
    """A whole number in [low, high], away from both ends when there is room."""
    if high < low:
        return low
    if high - low >= 2:
        return rng.randint(low + 1, high - 1)
    return rng.randint(low, high)


def _integer(node: Mapping[str, Any], rng: random.Random) -> int:
    low, high, _, _ = _limits(node, whole=True)
    step = node.get("multipleOf")
    if isinstance(step, int) and not isinstance(step, bool) and step > 0:
        return _inside(math.ceil(low / step), math.floor(high / step), rng) * step
    return _inside(int(low), int(high), rng)


def _number(node: Mapping[str, Any], rng: random.Random) -> float:
    low, high, _, _ = _limits(node, whole=False)
    step = node.get("multipleOf")
    if isinstance(step, int | float) and not isinstance(step, bool) and step > 0:
        count = _inside(math.ceil(low / step), math.floor(high / step), rng)
        return round(count * step, 10)
    if high <= low:
        return low
    # Inside, never at an end: the open ends are kept to for free.
    value = low + (high - low) * rng.uniform(0.2, 0.8)
    rounded = round(value, 2)
    return rounded if low < rounded < high else value


def _string(node: Mapping[str, Any], rng: random.Random) -> str:
    form = node.get("format")
    if form == "date-time":
        return "2026-11-12T21:30:00Z"
    if form == "date":
        return "2026-11-12"
    if form == "time":
        return "21:30:00"
    if form == "uuid":
        return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    shortest = max(int(node.get("minLength", 0)), 0)
    longest = node.get("maxLength")
    text = " ".join(rng.choice(_WORDS) for _ in range(rng.randint(1, 4)))
    while len(text) < shortest:
        text += " " + rng.choice(_WORDS)
    if isinstance(longest, int) and not isinstance(longest, bool):
        text = text[: max(longest, 0)].rstrip()
        if len(text) < shortest:
            text = text.ljust(shortest, "x")[: max(longest, 0)]
    return text


def _filler(repo_id: str, path: str, size: int) -> bytes:
    """Bytes for a file nobody wrote: the same every time, different for every file.

    A `.json` file gets JSON, because a downloaded settings file is read
    again on disk and one that could not be read would refuse the model. A
    `.gguf` file starts as every GGUF file does, because `files.check_file`
    throws away one that does not.
    """
    seed = hashlib.sha256(f"{repo_id}\0{path}".encode()).hexdigest()
    if path.lower().endswith(".json"):
        return json.dumps({"made_up_for": path, "filler": seed}).encode()
    blocks = (size // len(seed)) + 1
    body = (seed * blocks).encode()[: max(size, 1)]
    if path.lower().endswith(".gguf"):
        body = (gguf.MAGIC + body)[: max(size, len(gguf.MAGIC))]
    return body


def _git_blob_id(body: bytes) -> str:
    # What git calls these bytes. It identifies; it does not protect.
    return hashlib.sha1(b"blob %d\0" % len(body) + body, usedforsecurity=False).hexdigest()
