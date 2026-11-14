"""The programs that run models, each described by one class.

A runtime kind is everything FrameFound has to know about one such program:
which model builds it can run, where its newest version is announced, and
(the half a later step adds) how it is installed, started and spoken to.
Every way two programs differ is a method of the kind, so supporting another
program is one more class here and one more line in `kinds()` — nothing in
the check, the download, the model server or the pages names a program.

A kind has two halves. The describing half says, from a model's page and its
list of files alone, whether a program here can run it and what would be
downloaded for it. The running half says how the program is installed into a
folder of its own (`runtimes.py` runs those commands), how it is started on a
model folder, and how it is spoken to. Nothing in this module starts a
process or writes anything: every method returns data — argv lists, an
environment, a request body — that the callers run and the tests read. The
only things read from the disk are the small records an install and a
download leave (`program`, `about`, and llama.cpp's `serve`).

Two real kinds, asked in this order: MLX-VLM, which runs models packaged for
Apple silicon, and llama.cpp, which runs GGUF builds — the form most
community conversions come in — for models that have no MLX build.
"""

import base64
import math
import os
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from framefound.config import Settings

# gguf and llama_release as modules, not names: gguf.py is built on this
# module's `Variant` and imports it the same way, so either may be imported
# first (see gguf.py's top). llama_release imports nothing of FrameFound's
# but `native.proc`.
from framefound.local_models import availability, gguf, llama_release
from framefound.local_models.files import NEEDS_CODE, FilePlan, read_manifest
from framefound.local_models.hub import FetchJson, HubError, RepoFile, RepoInfo

# Which memory budget a build fits (schedule.fits).
Fit = Literal["day", "night", "never"]

# What `choose` says when there is no kind at all to ask: an install that
# cannot run models. The pages refuse long before this; it is here so the
# function always has a sentence to give.
NO_PROGRAM = "This install has no software that runs models, so nothing here can run it."

# A released version as a Python package index writes one: "0.7.12",
# "1.0rc1", "0.7.12.post1". It becomes a folder name and is asked for by
# name (`mlx-vlm==<version>`), so nothing else is ever accepted — no
# `--index-url=…`, no spaces, no path. `runtimes.VERSION` is this pattern.
VERSION = re.compile(r"\d+(\.\d+){1,3}((a|b|rc)\d+|\.post\d+)?")
NOT_A_VERSION = "That is not a version number."
# MLX-VLM's newest version is the package index's to say; this is what the
# page says when it cannot be asked. (llama.cpp's is GitHub's, in its own words.)
INDEX_UNREACHABLE = "The package index could not be reached, so the newest version is not known."

# The only variables a runtime's child process is given from FrameFound's
# own environment. An allow-list, never "everything but": the model server
# runs with framefound.env loaded — the database and queue passwords, the
# key that seals secrets, HF_HOME pointing at the shared download cache —
# and none of it may reach a program that runs a model somebody else made.
PASSED_ON = ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL")


@dataclass(frozen=True)
class Serve:
    """How to start a runtime on one model: everything `proc.spawn` is given."""

    argv: list[str]
    # The WHOLE environment of the child, not an addition to anyone's.
    env: dict[str, str]
    cwd: Path


@dataclass(frozen=True)
class Variant:
    """One runnable set of files in a repository: a build.

    An MLX repository is one build. A GGUF repository holds many (one per
    quantisation), which is why sizes are always a variant's and never the
    repository's.
    """

    # "" when the repository is one build.
    id: str
    label: str
    # Exactly what would be downloaded for this build.
    files: tuple[RepoFile, ...]
    weights_bytes: int
    # The build's own name for itself ("Q4_K_M"); "" when it has none.
    quant: str = ""
    # A caveat the check shows beside this build.
    note: str = ""
    # What this build takes, when that differs from the repository's; () = the same.
    takes: tuple[str, ...] = ()


@dataclass(frozen=True)
class Support:
    """Whether one kind can run a repository, in a sentence either way."""

    ok: bool
    # The sentence the check shows.
    note: str
    # "text", "image", "audio", "video" — as the model's page says.
    takes: tuple[str, ...]
    # The builds this kind could run. Filled in even when `ok` is False
    # wherever the files are of its format, so a refused model's size can
    # still be shown beside everything else that is wrong with it.
    variants: tuple[Variant, ...]
    # It may run, but its page does not say so: `note` is then a warning the
    # operator acknowledges, not a confirmation.
    caveat: bool = False


class RuntimeKind(Protocol):
    """One program that runs models: how to describe, install, start and speak to it."""

    # Stored on the model's row and in its manifest: never change one.
    name: str
    # What the pages call the program. Every sentence that names it uses this.
    label: str
    # One sentence for the page.
    summary: str
    # True only for the stand-in a developer reviews the pages with.
    demo: bool
    # The weights formats it runs.
    weights: tuple[str, ...]
    # Installed with uv? The install only asks for uv when this says so.
    needs_uv: bool
    # Ask once before a role is given to a model of this kind that nobody
    # has compared on real footage.
    needs_compare: bool
    # A line under "Install a particular version" on the page; "" for none.
    update_note: str
    # What a version of it looks like, as the placeholder of that field.
    version_hint: str
    # Where the running program answers "are you ready" and takes a chat.
    health_path: str
    chat_path: str

    def supported_here(self, system: str, machine: str) -> str | None:
        """None when this machine can run the program; else the sentence saying why not."""
        ...

    def supports(self, info: RepoInfo, plan: FilePlan) -> Support:
        """Can this program run that repository, judged from its page and file list alone?"""
        ...

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        """The id of the build to use when the operator named none.

        `sized` is every build of `supports(...).variants`, in that order,
        with the budget each fits. Never empty.
        """
        ...

    def latest(self, fetch: FetchJson) -> str:
        """The newest version on offer; `HubError` when that cannot be learned.

        Always something `clean_version` accepts, so "Update to X" can never
        offer a version the install would then refuse.
        """
        ...

    # --- the running half ------------------------------------------------------

    def clean_version(self, text: str) -> str:
        """The version as this program writes it; `ValueError(sentence)` for anything else.

        The answer is a folder name and is asked for by name, so it is never
        anything but a version.
        """
        ...

    def is_newer(self, latest: str, installed: str) -> bool:
        """Is `latest` a later version than `installed`? (What "update available" means.)"""
        ...

    def about(self, folder: Path) -> str:
        """One more line about the version installed in `folder`; "" for none."""
        ...

    def install_steps(self, folder: Path, version: str, uv: str) -> list[list[str]]:
        """The commands that install `version` into `folder`, in order. [] installs nothing."""
        ...

    def check_step(self, folder: Path) -> list[str]:
        """A command that exits 0 and prints the installed version as its last line."""
        ...

    def freeze_step(self, folder: Path, uv: str) -> list[str] | None:
        """A command whose output records what was installed; None for none. Best effort."""
        ...

    def program(self, folder: Path) -> Path | None:
        """The file a model is started with, of the version installed in `folder`.

        None when it is not there: the version is installed on paper and can
        run nothing (`framefound doctor` says so). Reads the disk; runs nothing.
        """
        ...

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        """How to start the version in `folder` on the model in `model_dir`.

        On 127.0.0.1 and `port` only; `key` travels in the environment, never
        on the command line; the environment is built with `child_env`.
        """
        ...

    def auth_headers(self, key: str) -> dict[str, str]:
        """The headers that carry `key` to the running program."""
        ...

    def is_ready(self, status: int, body: Mapping[str, Any]) -> bool:
        """Does this answer to `GET health_path` mean the program can take a chat?"""
        ...

    def request(self, body: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
        """FrameFound's chat body as this program wants it. `body` is not changed."""
        ...

    def picture_part(self, jpeg: bytes) -> dict[str, Any]:
        """One picture as a part of a chat message."""
        ...

    def memory_gb(self, answer: Mapping[str, Any]) -> float | None:
        """The memory the program says it used for this answer, in GB; None when it does not say."""
        ...

    def owns(self, command_line: str, folder: Path) -> bool:
        """Is this command line (from `ps`) the program started from `folder`?"""
        ...


def child_env(
    extra: Mapping[str, str], *, parent: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The whole environment of a child process: `PASSED_ON` from `parent`, then `extra`.

    `parent` is this process's environment when not given. Nothing else of
    it is passed on (see `PASSED_ON`).
    """
    source = os.environ if parent is None else parent
    env = {name: source[name] for name in PASSED_ON if name in source}
    env.update(extra)
    return env


class MlxVlm:
    """MLX-VLM: runs models packaged for Apple silicon, pictures included.

    Every fact about the program is in this one class, so the first run on a
    real Mac corrects it in one place.
    """

    name = "mlx-vlm"
    label = "MLX-VLM"
    summary = (
        "Runs models packaged for Apple silicon (MLX builds), including the ones that "
        "read pictures."
    )
    demo = False
    weights: tuple[str, ...] = (".safetensors",)
    needs_uv = True
    needs_compare = False
    update_note = ""
    version_hint = "0.7.12"
    health_path = "/health"
    chat_path = "/v1/chat/completions"

    # Where its newest version is announced: the package index it is installed from.
    index = "https://pypi.org/pypi/mlx-vlm/json"
    # The package asked for by name, and the Python its environment is made with.
    package = "mlx-vlm"
    python = "3.12"

    def supported_here(self, system: str, machine: str) -> str | None:
        if system == "Darwin" and machine == "arm64":
            return None
        return "MLX models need a Mac with Apple silicon."

    def supports(self, info: RepoInfo, plan: FilePlan) -> Support:
        takes = _takes(info.pipeline)
        variants = self._variants(plan)
        if info.custom_code:
            return Support(False, NEEDS_CODE, takes, variants)
        if not variants:
            if ".gguf" in plan.suffixes:
                note = "This build is for llama.cpp, which FrameFound cannot run yet."
            else:
                note = f"It has no safetensors weights, which is the only kind {self.label} runs."
            return Support(False, note, takes, ())
        if info.library != "mlx" and "mlx" not in info.tags:
            return Support(
                False,
                "This is the maker's original build, not one packaged for the Mac. Look for "
                "the same model from an approved converter, such as mlx-community.",
                takes,
                variants,
            )
        if "mlx-vlm" in info.tags:
            return Support(True, f"Its page says it runs on {self.label}.", takes, variants)
        return Support(
            True,
            f"Its page does not name {self.label}. It may still run — Test will tell.",
            takes,
            variants,
            caveat=True,
        )

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        # An MLX repository is one build.
        return sized[0][0].id

    def latest(self, fetch: FetchJson) -> str:
        try:
            answer = fetch(self.index)
        except HubError as err:
            # `hub.fetch_json` names the address it could not reach; the page
            # says which index that was, keeping what went wrong.
            if err.problem not in ("unreachable", "rate_limited"):
                raise
            raise HubError(err.problem, INDEX_UNREACHABLE, err.status) from None
        about = answer.get("info")
        version = about.get("version") if isinstance(about, Mapping) else None
        try:
            # What is read here is later shown on the page and asked for by
            # name when the program is installed: only ever a version the
            # install would accept.
            if not isinstance(version, str):
                raise ValueError(NOT_A_VERSION)
            return self.clean_version(version)
        except ValueError:
            raise HubError(
                "bad_answer",
                "The package index answered in a form FrameFound could not read, so the "
                f"newest version of {self.label} is not known.",
            ) from None

    # --- the running half ------------------------------------------------------

    def clean_version(self, text: str) -> str:
        version = text.strip()
        if not VERSION.fullmatch(version):
            raise ValueError(NOT_A_VERSION)
        return version

    def is_newer(self, latest: str, installed: str) -> bool:
        newest, ours = release_order(latest), release_order(installed)
        if newest is None or ours is None:
            # Either is not a version this kind writes: claiming an update
            # would offer a button that cannot work.
            return False
        return newest > ours

    def about(self, folder: Path) -> str:
        return ""

    def install_steps(self, folder: Path, version: str, uv: str) -> list[list[str]]:
        # Its own environment, made at its final path (a venv cannot be moved
        # once made), then the one package at exactly that version. `uv venv
        # --python` may download that Python: the install needs the internet
        # anyway, and its failure is the install's failure.
        return [
            [uv, "venv", "--python", self.python, str(folder / "venv")],
            [uv, "pip", "install", "--python", str(_python(folder)), f"{self.package}=={version}"],
        ]

    def check_step(self, folder: Path) -> list[str]:
        # Importing the server is what "it starts" means short of loading a
        # model: a missing compiled dependency fails here, not at the first Test.
        return [
            str(_python(folder)),
            "-c",
            "import mlx_vlm.server; from importlib.metadata import version; "
            f"print(version('{self.package}'))",
        ]

    def freeze_step(self, folder: Path, uv: str) -> list[str] | None:
        return [uv, "pip", "freeze", "--python", str(_python(folder))]

    def program(self, folder: Path) -> Path | None:
        # Its own environment's interpreter: a venv deleted by hand leaves
        # the version recorded and nothing to start.
        python = _python(folder)
        return python if python.is_file() else None

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        # Its own defaults are every network interface and port 8080 —
        # FrameFound's own address — so both are always given. A model is
        # only ever named by an absolute folder: a name that is not one makes
        # the program go to Hugging Face for it. It is started with `-m`, not
        # through a console script a venv writes with its own path inside.
        # Never `--trust-remote-code` (and its variable is not passed on), and
        # never `--api-key`: the key would be readable in `ps` by anyone.
        argv = [
            str(_python(folder)),
            "-m",
            "mlx_vlm.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--model",
            str(model_dir),
            "--log-level",
            "WARNING",
        ]
        env = child_env(
            {
                # The program reads its key from here and checks it on every
                # request, its health check included.
                "MLX_VLM_SERVER_API_KEY": key,
                # It can fetch nothing: a model name where a folder belongs
                # fails instead of downloading.
                "HF_HUB_OFFLINE": "1",
                "TRANSFORMERS_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                # An empty home of its own, so no stored token and no shared
                # cache is within its reach.
                "HF_HOME": str(scratch / "hf-home"),
            }
        )
        return Serve(argv=argv, env=env, cwd=scratch)

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {key}"}

    def is_ready(self, status: int, body: Mapping[str, Any]) -> bool:
        return status == 200 and body.get("status") == "healthy"

    def request(self, body: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
        sent = dict(body)
        # The same string as `--model`: any other spelling of the folder reads
        # to the program as a second model, and it would load it again.
        sent["model"] = str(model_dir)
        sent["stream"] = False
        return sent

    def picture_part(self, jpeg: bytes) -> dict[str, Any]:
        # A picture is always data FrameFound built, never an address: the
        # program fetches an http(s) address it is given.
        return _picture(jpeg)

    def memory_gb(self, answer: Mapping[str, Any]) -> float | None:
        # GB (its other handler sets `peak_memory_gb` from the same figure).
        # Checked on the mini 2026-11-14 against Activity Monitor's figure
        # for the `python` process: 9.2 here against 9.08 there for Gemma 4
        # 12B, 5.7 against 5.58 for E4B. About 0.1 GB high, the safe side.
        timings = answer.get("timings")
        peak = timings.get("peak_memory") if isinstance(timings, Mapping) else None
        if isinstance(peak, bool) or not isinstance(peak, int | float):
            return None
        if not math.isfinite(peak) or peak <= 0:
            return None
        return float(peak)

    def owns(self, command_line: str, folder: Path) -> bool:
        return "mlx_vlm.server" in command_line and str(folder) in command_line

    def _variants(self, plan: FilePlan) -> tuple[Variant, ...]:
        """The one build of an MLX repository: every kept file but weights it cannot run.

        For a repository of safetensors that is everything in the plan. One
        that also carries GGUF files would otherwise have those downloaded
        too, gigabytes this program never reads.
        """
        mine = tuple(file for file in plan.weights if file.path.lower().endswith(self.weights))
        if not mine:
            return ()
        others = {file.path for file in plan.weights} - {file.path for file in mine}
        kept = tuple(file for file in plan.keep if file.path not in others)
        return (
            Variant(
                id="",
                label="The only build",
                files=kept,
                weights_bytes=sum(file.size for file in mine),
            ),
        )


# llama.cpp sizes its context from the model when it is not told: 262,144 and
# 1,048,576 tokens in the two GGUF builds read for the design, memory a 24 GB
# model on a 32 GB Mac does not have. So it is always told (L6 P1).
CONTEXT_TOKENS = 16384
# The one name the running program answers to; a request never names a path.
ALIAS = "framefound"
# The flags of `llama-server` that fetch something or read a folder other
# than the model's: never passed, and a test reads every argv for them
# (L6 D6). Without `-m` it is a "router" that loads whatever a request names.
NEVER_PASSED = (
    "-hf",
    "--hf-repo",
    "-hff",
    "--hf-file",
    "-mu",
    "--model-url",
    "-mmu",
    "--mmproj-url",
    "--models-dir",
    "--media-path",
    "--props",
    "--metrics",
    "--api-key",
    "--api-key-file",
)
# The check's sentence for a repository llama.cpp runs; the room it will be
# given follows it (`_room_for`).
RUNS_GGUF = "It is a GGUF build; llama.cpp runs those."


def _room_for(info: RepoInfo) -> str:
    """How much the model is given to read at once, beside what its page says it can take.

    The page's figure (the `gguf` object's `context_length`) is shown, never
    used: `CONTEXT_TOKENS` is what the program is started with (P1).
    """
    given = f"It will be given room for {CONTEXT_TOKENS:,} tokens"
    page = info.gguf.get("context_length") if info.gguf is not None else None
    # A page's figure is somebody else's: only a plain positive number is said.
    if isinstance(page, int) and not isinstance(page, bool) and page > 0:
        return f"{given}; its page says it can take {page:,}."
    return f"{given}."


class LlamaCpp:
    """llama.cpp's `llama-server`: runs GGUF builds, pictures included when a build has a projector.

    Installed from a pinned, checksummed release archive of GitHub's by
    FrameFound's own Python (`llama_release.py`), not with uv and not from
    Homebrew (L6 D2, D4). Every fact about the program is in this class and
    in `llama_release.py`, so the first run on the Mac corrects it there.
    The flags were read from the program's README on `master`, eight days
    newer than the numbered version offered (L6 P3): one it does not know
    makes it exit at once, which a Test shows as "It would not load." with
    its own words.
    """

    name = "llama-cpp"
    label = "llama.cpp"
    summary = (
        "Runs GGUF builds, the form most community conversions come in. Needed only for "
        "models that have no MLX build."
    )
    demo = False
    weights: tuple[str, ...] = (".gguf",)
    # It is a download FrameFound's own Python makes; uv plays no part.
    needs_uv = False
    # New here: asked about once before a role, until Compare has seen it.
    needs_compare = True
    update_note = (
        "llama.cpp brings out a numbered version every few weeks and a daily build most "
        "days. FrameFound offers the numbered ones. A daily build can be installed by its "
        "number when a model needs something newer."
    )
    version_hint = "0.9.0 or a daily build such as b12318"
    health_path = "/health"
    chat_path = "/v1/chat/completions"

    # The module that is run to install, check and describe a version.
    module = "framefound.local_models.llama_release"

    def supported_here(self, system: str, machine: str) -> str | None:
        if system == "Darwin" and machine == "arm64":
            return None
        return "llama.cpp is installed here from its build for Apple-silicon Macs."

    def supports(self, info: RepoInfo, plan: FilePlan) -> Support:
        if ".gguf" not in plan.suffixes:
            return Support(False, "This is not a GGUF build.", _takes(info.pipeline), ())
        variants = gguf.variants(plan)
        takes = _union(variant.takes for variant in variants) or _takes(info.pipeline)
        if info.custom_code:
            # A GGUF file carries no code and llama.cpp would run none, but
            # one rule for the operator: a model that says it needs its own
            # code is refused, whatever runs it (L6 D10).
            return Support(False, NEEDS_CODE, takes, variants)
        if not variants:
            note = (
                "This repository holds only the part that reads pictures, not a model."
                if gguf.projectors(plan.weights)
                else "No GGUF model file was found in it."
            )
            return Support(False, note, takes, ())
        return Support(True, f"{RUNS_GGUF} {_room_for(info)}", takes, variants)

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        return gguf.prefer(sized)

    def latest(self, fetch: FetchJson) -> str:
        try:
            return llama_release.newest(fetch)
        except llama_release.ReleaseProblem as err:
            # The Protocol promises HubError; the sentence is llama_release's own.
            raise HubError("bad_answer", str(err)) from None
        except HubError as err:
            # `hub.fetch_json` names the host it could not reach; the page
            # says it in this program's words, keeping what went wrong.
            if err.problem not in ("unreachable", "rate_limited"):
                raise
            limited = err.status in (403, 429)
            raise HubError(
                "rate_limited" if limited else err.problem,
                llama_release.LIMITED if limited else llama_release.NEWEST_UNREACHABLE,
                err.status,
            ) from None

    # --- the running half ------------------------------------------------------

    def clean_version(self, text: str) -> str:
        return llama_release.clean_version(text)

    def is_newer(self, latest: str, installed: str) -> bool:
        return llama_release.is_newer(latest, installed)

    def about(self, folder: Path) -> str:
        return llama_release.about(folder)

    def install_steps(self, folder: Path, version: str, uv: str) -> list[list[str]]:
        # One step: FrameFound's own interpreter downloads, checks and unpacks
        # the archive into `folder` (llama_release.fetch). It reads nothing of
        # FrameFound's settings, so the bare environment runtimes.py gives
        # every step is enough.
        return [
            [
                sys.executable,
                "-m",
                self.module,
                "fetch",
                "--version",
                version,
                "--into",
                str(folder),
            ]
        ]

    def check_step(self, folder: Path) -> list[str]:
        # Starts the unpacked program once with `--version`, then prints the
        # version recorded: what runtimes.install compares with the one asked for.
        return [sys.executable, "-m", self.module, "check", "--in", str(folder)]

    def freeze_step(self, folder: Path, uv: str) -> list[str] | None:
        return [sys.executable, "-m", self.module, "about", "--in", str(folder)]

    def program(self, folder: Path) -> Path | None:
        return llama_release.program(folder)

    def serve(
        self, folder: Path, model_dir: Path, variant: str, port: int, key: str, scratch: Path
    ) -> Serve:
        """`llama-server` on one GGUF build, closed against every way it can fetch or wander.

        `RuntimeProblem` when the version's program is gone or the model's
        folder does not say which file is the model.
        """
        # Imported here: runtimes.py is built on this module.
        from framefound.local_models.runtimes import RuntimeProblem

        program = llama_release.program(folder)
        if program is None:
            raise RuntimeProblem(
                f"{self.label} {folder.name} is recorded but its program is missing. "
                f"Install {folder.name} again under Software that runs models (Install a "
                "particular version): that installs it afresh."
            )
        manifest = read_manifest(model_dir)
        try:
            if manifest is None:
                raise ValueError("This copy of the model has no list of its files.")
            model, projector = gguf.roles(manifest.files)
        except ValueError as err:
            raise RuntimeProblem(str(err)) from None
        argv = [str(program), "-m", str(_inside(model_dir, model))]
        # A build without a projector says so: left to itself the program
        # goes looking for one.
        argv += (
            ["--mmproj", str(_inside(model_dir, projector))]
            if projector is not None
            else ["--no-mmproj"]
        )
        argv += [
            # Its default is 127.0.0.1 too; given so a changed default cannot matter.
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--alias",
            ALIAS,
            "-c",
            str(CONTEXT_TOKENS),
            # One slot: the model server sends one request at a time, and
            # slots divide the context between them.
            "-np",
            "1",
            # Never the network, never its own pages.
            "--offline",
            "--no-webui",
            "--no-slots",
            # Every answer asked for is short and structured; a model that
            # thinks first can spend all its tokens thinking (L6 P15).
            "--reasoning-budget",
            "0",
        ]
        env = child_env(
            {
                # Read from here and checked on every chat; never `--api-key`,
                # which anyone on the Mac could read in `ps`. Nothing else of
                # the parent's: llama.cpp reads a LLAMA_ARG_* variable for
                # almost every flag (L6 P5), so `child_env` is an allow-list.
                "LLAMA_API_KEY": key,
                "HF_HUB_OFFLINE": "1",
                "HF_HOME": str(scratch / "hf-home"),
                "LLAMA_CACHE": str(scratch / "llama-cache"),
            }
        )
        return Serve(argv=argv, env=env, cwd=scratch)

    def auth_headers(self, key: str) -> dict[str, str]:
        # The form the README's own examples use; not yet seen on a Mac.
        return {"Authorization": f"Bearer {key}"}

    def is_ready(self, status: int, body: Mapping[str, Any]) -> bool:
        # 503 "Loading model" until it can answer. `/health` asks for no key,
        # so this proves nothing about the key: the warm-up request does.
        return status == 200 and body.get("status") == "ok"

    def request(self, body: Mapping[str, Any], model_dir: Path) -> dict[str, Any]:
        sent = dict(body)
        # The alias, never a path: it was started on one file and is no router.
        sent["model"] = ALIAS
        sent["stream"] = False
        return sent

    def picture_part(self, jpeg: bytes) -> dict[str, Any]:
        # llama.cpp would fetch an address or read a path it is given; only
        # ever data FrameFound built.
        return _picture(jpeg)

    def memory_gb(self, answer: Mapping[str, Any]) -> float | None:
        # It reports no memory figure in its answers (L6 D13): the model
        # server measures the process instead.
        return None

    def owns(self, command_line: str, folder: Path) -> bool:
        return "llama-server" in command_line and str(folder) in command_line


# The names of the demo's stand-ins (`fake.DemoKind`, `fake.DemoGgufKind`),
# which only `kinds()` in demo mode builds. A model row on one of them was
# made by the demo: its files are a few kilobytes against the gigabytes
# listed, and nothing said about it was said by a model.
DEMO_RUNTIMES = frozenset({"demo", "demo-gguf"})


def is_demo_runtime(name: str) -> bool:
    """Was a model on the program called `name` made by the demo?"""
    return name in DEMO_RUNTIMES


def kinds(settings: Settings | None = None) -> dict[str, RuntimeKind]:
    """The kinds this install has, in the order they are asked.

    The real ones on an install that can run models (MLX-VLM first, so a
    repository either could run goes to it); the two stand-ins in the demo
    (one for MLX builds, one for GGUF), and only there; none where models
    cannot run.
    """
    mode = availability(settings).mode
    if mode == "ready":
        return {MlxVlm.name: MlxVlm(), LlamaCpp.name: LlamaCpp()}
    if mode == "demo":
        # Imported here, and built nowhere else: the stand-ins must not be
        # reachable on a real install by any other path. In the real kinds'
        # order: the MLX one first.
        from framefound.local_models import fake

        return {fake.DemoKind.name: fake.DemoKind(), fake.DemoGgufKind.name: fake.DemoGgufKind()}
    return {}


def choose(
    info: RepoInfo, plan: FilePlan, available: Mapping[str, RuntimeKind]
) -> tuple[RuntimeKind | None, Support]:
    """Which kind would run this repository, and what it says about it.

    The first kind, in `available`'s order, that can. When none can, no kind
    is returned and the sentence is that of the kind whose weights format
    the repository holds (so a GGUF repository is answered by the program
    for GGUF, not by whichever is listed first), else the first kind's.
    """
    answers: list[tuple[RuntimeKind, Support]] = []
    for kind in available.values():
        support = kind.supports(info, plan)
        if support.ok:
            return kind, support
        answers.append((kind, support))
    if not answers:
        return None, Support(False, NO_PROGRAM, (), ())
    for kind, support in answers:
        if any(suffix in plan.suffixes for suffix in kind.weights):
            return None, support
    return None, answers[0][1]


_PHASE = re.compile(r"(a|b|rc)(\d+)$|\.post(\d+)$")
# How a release's own suffix sorts: alpha, beta, candidate, the release, a
# fix-up of the release.
_PHASES = {"a": 0, "b": 1, "rc": 2}
_FINAL = 3
_POST = 4


def release_order(text: str) -> tuple[tuple[int, ...], int, int] | None:
    """A version as something that sorts the way releases follow each other.

    The dotted parts are compared as numbers (0.7.12 is after 0.7.9) with
    missing parts as nought (0.8 is 0.8.0); a candidate comes before its
    release and a `.post` fix-up after it. None for anything `VERSION` does
    not accept.
    """
    version = text.strip()
    if not VERSION.fullmatch(version):
        return None
    phase, number = _FINAL, 0
    suffix = _PHASE.search(version)
    if suffix is not None:
        version = version[: suffix.start()]
        if suffix.group(3) is not None:
            phase, number = _POST, int(suffix.group(3))
        else:
            phase, number = _PHASES[suffix.group(1)], int(suffix.group(2))
    parts = [int(part) for part in version.split(".")]
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts), phase, number


def _python(folder: Path) -> Path:
    # The interpreter of a version's own environment. macOS and Linux lay a
    # venv out this way; the program only runs on a Mac.
    return folder / "venv" / "bin" / "python"


def _picture(jpeg: bytes) -> dict[str, Any]:
    url = "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")
    return {"type": "image_url", "image_url": {"url": url}}


def _inside(model_dir: Path, path: str) -> Path:
    """A file of the model's folder by its listed name (always forward slashes)."""
    return model_dir.joinpath(*path.split("/"))


def _union(groups: Iterable[tuple[str, ...]]) -> tuple[str, ...]:
    """What any of the builds takes, in the order the pages list it."""
    seen = {what for group in groups for what in group}
    return tuple(what for what in ("text", "image", "audio", "video") if what in seen)


def _takes(pipeline: str) -> tuple[str, ...]:
    """What a model takes, from the task its page files it under."""
    if pipeline == "image-text-to-text":
        return ("text", "image")
    if pipeline == "any-to-any":
        return ("text", "image", "audio")
    return ("text",)
