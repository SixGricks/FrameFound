"""The programs that run models, on disk (local_models/runtimes.py), and how one is
started and spoken to (the running half of local_models/kinds.py).

Nothing here starts a process or reaches a network: every command goes to
`fake.FakeRunner`, which records it and runs nothing, and a fixture makes
starting a real process or opening a socket fail the test. What is asserted
is the commands and environments as data — the part that can be known off a
Mac. Whether MLX-VLM really installs and serves this way is design L1,
section 12.
"""

import json
import os
import socket
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from framefound.config import Settings, get_settings
from framefound.local_models import UNAVAILABLE, fake, hub, kinds, runtimes
from framefound.local_models.kinds import PASSED_ON, MlxVlm
from framefound.local_models.runtimes import RuntimeProblem
from framefound.native import envfile, proc, services
from framefound.native.layout import Layout

NAME = "mlx-vlm"
UNCHANGED = "Nothing in use was changed."
KEY = "per-start-key-not-a-real-one"
OURS = ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_LOCAL_AI_DEMO", "FRAMEFOUND_DOMAIN")


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may start a program or open a connection; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a test tried to start a process or open a connection")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture()
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in OURS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def prints_its_own_version(argv: list[str]) -> tuple[int, str]:
    # The check command is the version folder's own interpreter
    # (<root>/<name>/<version>/venv/bin/python): a real install prints the
    # version that was put there.
    return 0, f"{Path(argv[0]).parents[2].name}\n"


def runner(**scripts: fake.Script) -> fake.FakeRunner:
    """Everything an MLX-VLM install runs, succeeding; `scripts` replaces any of it."""
    answers: dict[str, fake.Script] = {
        "uv venv": (0, "", "Creating virtual environment at: venv\n"),
        "uv pip install": (0, "", "Installed 41 packages in 2.1s\n"),
        "uv pip freeze": (0, "mlx==0.32.4\nmlx-vlm==0.7.12\n"),
        "python": prints_its_own_version,
    }
    answers.update({key.replace("_", " "): value for key, value in scripts.items()})
    return fake.FakeRunner(answers, make_venvs=True)


def installed(root: Path, *versions: str, run: fake.FakeRunner | None = None) -> None:
    """Install and switch to each version in turn, as the page's Install and Update do."""
    for version in versions:
        runtimes.install(MlxVlm(), version, root, run=run or runner())
        runtimes.activate(root, NAME, version)


def snapshot(folder: Path) -> dict[str, bytes]:
    """Every file under `folder` with its bytes: what "left alone" is compared against."""
    return {
        str(path.relative_to(folder)): path.read_bytes()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


# --- installing ------------------------------------------------------------------


def test_a_runtime_installs_into_its_own_folder_and_environment(tmp_path: Path) -> None:
    run = runner()
    said: list[str] = []

    runtimes.install(MlxVlm(), "0.7.12", tmp_path, run=run, say=said.append)

    folder = tmp_path / NAME / "0.7.12"
    python = str(folder / "venv" / "bin" / "python")
    assert run.calls == [
        ["uv", "venv", "--python", "3.12", str(folder / "venv")],
        ["uv", "pip", "install", "--python", python, "mlx-vlm==0.7.12"],
        [
            python,
            "-c",
            "import mlx_vlm.server; from importlib.metadata import version; "
            "print(version('mlx-vlm'))",
        ],
        ["uv", "pip", "freeze", "--python", python],
    ]
    # Every command is given the allow-listed environment and nothing else,
    # with its output kept for the page and a limit on how long it may take.
    for ran in run.ran:
        assert ran.env is not None
        assert set(ran.env) <= set(PASSED_ON)
        assert ran.capture is True
    assert [ran.timeout for ran in run.ran] == [runtimes.STEP_TIMEOUT_S] * 3 + [
        runtimes.FREEZE_TIMEOUT_S
    ]
    assert said == [
        "Making its own environment…",
        "Installing 0.7.12 — a few minutes…",
        "Checking it starts…",
    ]

    mark = json.loads((folder / "installed.json").read_text(encoding="utf-8"))
    assert mark["version"] == "0.7.12"
    assert mark["freeze"] == "mlx==0.32.4\nmlx-vlm==0.7.12\n"
    assert mark["installed_at"].endswith("Z")

    # Installed is not in use: only `activate` switches.
    assert runtimes.describe(tmp_path, NAME) == runtimes.Installed(None, None, ("0.7.12",), None)
    assert not (tmp_path / NAME / "state.json").exists()

    runtimes.activate(tmp_path, NAME, "0.7.12")
    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous) == ("0.7.12", None)
    assert now.installed_at == mark["installed_at"]
    assert runtimes.current_dir(tmp_path, NAME) == (folder, "0.7.12")


def test_a_version_already_on_disk_is_not_installed_again(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.12")
    run = fake.FakeRunner()  # answers nothing: any command fails the test
    said: list[str] = []

    runtimes.install(MlxVlm(), "0.7.12", tmp_path, run=run, say=said.append)

    assert run.calls == []
    assert said == ["MLX-VLM 0.7.12 is already installed."]


def test_a_freeze_that_fails_does_not_fail_the_install(tmp_path: Path) -> None:
    outcomes: list[fake.Script] = [(2, "", "error: no such option"), proc.CommandFailed("late")]
    for number, outcome in enumerate(outcomes):
        root = tmp_path / str(number)
        runtimes.install(MlxVlm(), "0.7.12", root, run=runner(uv_pip_freeze=outcome))

        mark = json.loads((root / NAME / "0.7.12" / "installed.json").read_text("utf-8"))
        assert mark["freeze"] == ""


FAILURES: dict[str, tuple[str, fake.Script, str, str]] = {
    "the environment cannot be made": (
        "uv venv",
        (2, "", "error: No interpreter found for Python 3.12\n"),
        "MLX-VLM 0.7.13 could not be installed: uv stopped with exit 2. " + UNCHANGED,
        "error: No interpreter found for Python 3.12",
    ),
    "the package will not install": (
        "uv pip install",
        (1, "", "Resolved 40 packages\nerror: no wheel for opencv-python\n"),
        "MLX-VLM 0.7.13 could not be installed: uv stopped with exit 1. " + UNCHANGED,
        "Resolved 40 packages\nerror: no wheel for opencv-python",
    ),
    "it will not start": (
        "python",
        # What went wrong is on stderr; what it printed before that is not the point.
        (1, "loading…\n", "Traceback (most recent call last):\nImportError: libmlx missing\n"),
        "MLX-VLM 0.7.13 was installed but would not start, so it was removed. " + UNCHANGED,
        "Traceback (most recent call last):\nImportError: libmlx missing",
    ),
    "the installer is not there": (
        "uv venv",
        proc.CommandFailed("uv is not installed or not on PATH"),
        "MLX-VLM 0.7.13 could not be installed: uv is not installed or not on PATH. " + UNCHANGED,
        "",
    ),
    "the installer hangs": (
        "uv pip install",
        proc.CommandFailed("uv did not finish within 1800 s"),
        "MLX-VLM 0.7.13 could not be installed: uv did not finish within 1800 s. " + UNCHANGED,
        "",
    ),
}


@pytest.mark.parametrize("failure", list(FAILURES))
def test_a_failed_install_leaves_nothing_and_the_running_version_alone(
    tmp_path: Path, failure: str
) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")
    before = snapshot(tmp_path)
    program, outcome, message, detail = FAILURES[failure]

    with pytest.raises(RuntimeProblem) as caught:
        runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=runner(**{program: outcome}))

    assert str(caught.value) == message
    assert caught.value.detail == detail
    # Not a byte changed: the record of what is in use, the version in use,
    # the one kept to go back to. And nothing of the new one is left.
    assert snapshot(tmp_path) == before
    assert not (tmp_path / NAME / "0.7.13").exists()
    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous) == ("0.7.12", "0.7.11")


def test_an_install_that_reports_another_version_is_refused(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.12")
    before = snapshot(tmp_path)
    # The index was asked for 0.7.13 and something else arrived.
    run = runner(python=(0, "Loading…\n0.7.12\n"))

    with pytest.raises(RuntimeProblem) as caught:
        runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=run)

    assert str(caught.value) == (
        "The installer reported a different version (0.7.12) from the one asked for "
        "(0.7.13), so it was removed. " + UNCHANGED
    )
    assert snapshot(tmp_path) == before
    assert not (tmp_path / NAME / "0.7.13").exists()


def test_an_install_that_prints_nothing_is_refused_too(tmp_path: Path) -> None:
    with pytest.raises(RuntimeProblem) as caught:
        runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=runner(python=(0, "")))

    assert "reported a different version (none)" in str(caught.value)
    assert not (tmp_path / NAME / "0.7.13").exists()


def test_a_disk_that_fails_mid_install_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(tmp_path, "0.7.12")
    before = snapshot(tmp_path)

    def full(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(runtimes, "_write_json", full)
    with pytest.raises(RuntimeProblem) as caught:
        runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=runner())

    assert str(caught.value) == (
        "MLX-VLM 0.7.13 could not be installed: No space left on device. " + UNCHANGED
    )
    assert snapshot(tmp_path) == before


def test_a_half_finished_install_is_never_read_as_a_version_and_is_cleared(
    tmp_path: Path,
) -> None:
    installed(tmp_path, "0.7.12")
    # What a machine that went down mid-install leaves: a folder, no mark.
    leftover = tmp_path / NAME / "0.7.13"
    (leftover / "venv" / "lib").mkdir(parents=True)
    (leftover / "venv" / "lib" / "half-written.py").write_text("x", encoding="utf-8")

    assert runtimes.describe(tmp_path, NAME).versions == ("0.7.12",)
    with pytest.raises(RuntimeProblem) as caught:
        runtimes.activate(tmp_path, NAME, "0.7.13")
    assert str(caught.value) == "Version 0.7.13 is not installed, so it cannot be used."

    runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=runner())

    assert not (leftover / "venv" / "lib" / "half-written.py").exists()
    assert (leftover / "installed.json").is_file()


@pytest.mark.parametrize(
    "text",
    [
        "",
        "latest",
        "7",
        "--index-url=http://elsewhere.example",
        "0.7.12; rm -rf ~",
        "../0.7.12",
        "0.7.12 0.7.13",
        "0.7.12.dev1",
        "v0.7.12",
    ],
)
def test_something_that_is_not_a_version_is_refused_before_anything_runs(
    tmp_path: Path, text: str
) -> None:
    run = fake.FakeRunner()

    with pytest.raises(RuntimeProblem) as caught:
        runtimes.install(MlxVlm(), text, tmp_path, run=run)

    assert str(caught.value) == "That is not a version number."
    assert run.calls == []
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="That is not a version number."):
        runtimes.clean_version(MlxVlm(), text)


def test_a_version_is_tidied_before_it_is_used(tmp_path: Path) -> None:
    assert runtimes.clean_version(MlxVlm(), " 0.7.12\n") == "0.7.12"
    for version in ("0.7.12rc1", "1.0b2", "0.7.12.post1", "2.0"):
        assert runtimes.clean_version(MlxVlm(), version) == version

    run = runner()
    runtimes.install(MlxVlm(), " 0.7.12 ", tmp_path, run=run)
    assert run.calls[1][-1] == "mlx-vlm==0.7.12"


# --- switching and going back -------------------------------------------------------


def test_rolling_back_swaps_to_the_kept_version_in_one_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")
    replaced: list[Path] = []
    real_replace = os.replace

    def watched(source: Any, destination: Any) -> None:
        replaced.append(Path(destination))
        real_replace(source, destination)

    monkeypatch.setattr(runtimes.os, "replace", watched)

    assert runtimes.rollback(tmp_path, NAME) == "0.7.11"

    # One replacement of the record and nothing else written: a reader
    # sees the old pair or the new pair, never one of each.
    assert replaced == [tmp_path / NAME / "state.json"]
    record = json.loads((tmp_path / NAME / "state.json").read_text(encoding="utf-8"))
    assert record == {"version": 1, "current": "0.7.11", "previous": "0.7.12"}
    # Both are still there, so going back again is the same click.
    assert runtimes.rollback(tmp_path, NAME) == "0.7.12"
    assert runtimes.describe(tmp_path, NAME).previous == "0.7.11"
    assert list((tmp_path / NAME).glob("*.tmp")) == []


def test_there_is_nothing_to_roll_back_to_until_a_second_version_was_used(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeProblem, match=runtimes.NOTHING_TO_GO_BACK_TO):
        runtimes.rollback(tmp_path, NAME)

    installed(tmp_path, "0.7.12")
    with pytest.raises(RuntimeProblem, match=runtimes.NOTHING_TO_GO_BACK_TO):
        runtimes.rollback(tmp_path, NAME)
    assert runtimes.describe(tmp_path, NAME).current == "0.7.12"


def test_only_the_current_and_previous_versions_are_kept(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.10", "0.7.11", "0.7.12")

    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous, now.versions) == ("0.7.12", "0.7.11", ("0.7.12", "0.7.11"))
    assert sorted(path.name for path in (tmp_path / NAME).iterdir()) == [
        "0.7.11",
        "0.7.12",
        "state.json",
    ]


def test_installing_a_particular_older_version_keeps_the_one_it_replaces(
    tmp_path: Path,
) -> None:
    # The way out when the newest is broken and nothing older is kept.
    installed(tmp_path, "0.7.12", "0.7.13", "0.7.9")

    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous) == ("0.7.9", "0.7.13")
    assert now.versions == ("0.7.13", "0.7.9")


def test_switching_to_the_version_in_use_changes_nothing(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")

    runtimes.activate(tmp_path, NAME, "0.7.12")

    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous) == ("0.7.12", "0.7.11")


def test_pruning_never_touches_what_is_not_a_whole_version(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")
    half = tmp_path / NAME / "0.7.13"
    half.mkdir()
    stranger = tmp_path / NAME / "notes.txt"
    stranger.write_text("mine", encoding="utf-8")

    assert runtimes.prune(tmp_path, NAME) == []
    assert half.is_dir()
    assert stranger.read_text(encoding="utf-8") == "mine"


@pytest.mark.parametrize(
    "record",
    [
        "{not json",
        "[]",
        '{"version": 1, "current": 7}',
        '{"version": 1, "current": "../../elsewhere"}',
        '{"version": 1, "current": null, "previous": "0.7.11"}',
    ],
)
def test_a_damaged_record_is_not_guessed_at(tmp_path: Path, record: str) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")
    state = tmp_path / NAME / "state.json"
    state.write_text(record, encoding="utf-8")

    with pytest.raises(RuntimeProblem) as caught:
        runtimes.describe(tmp_path, NAME)
    assert str(caught.value).startswith(
        f"FrameFound's record of which version is in use ({state}) is damaged: "
    )
    assert str(caught.value).endswith("Install a version to replace it.")
    with pytest.raises(RuntimeProblem):
        runtimes.current_dir(tmp_path, NAME)
    with pytest.raises(RuntimeProblem):
        runtimes.rollback(tmp_path, NAME)
    # Which version was in use is not known, so none is removed.
    assert runtimes.prune(tmp_path, NAME) == []

    # Switching to a version writes a fresh record — the way out — and
    # still removes nothing that might have been the one in use.
    runtimes.install(MlxVlm(), "0.7.13", tmp_path, run=runner())
    runtimes.activate(tmp_path, NAME, "0.7.13")
    now = runtimes.describe(tmp_path, NAME)
    assert (now.current, now.previous) == ("0.7.13", None)
    assert now.versions == ("0.7.13", "0.7.12", "0.7.11")


def test_a_version_in_use_that_has_gone_from_the_disk_is_said_plainly(tmp_path: Path) -> None:
    installed(tmp_path, "0.7.12")
    (tmp_path / NAME / "0.7.12" / "installed.json").unlink()

    with pytest.raises(RuntimeProblem) as caught:
        runtimes.describe(tmp_path, NAME)
    assert str(caught.value) == (
        "The version recorded as in use (0.7.12) is missing or incomplete on disk. Install "
        "it again, or install another version."
    )


def test_a_change_waits_for_another_and_a_dead_ones_hold_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed(tmp_path, "0.7.11", "0.7.12")
    lock = tmp_path / NAME / "state.json.lock"
    monkeypatch.setattr(runtimes, "LOCK_WAIT_S", 0.05)

    lock.touch()
    with pytest.raises(RuntimeProblem, match="Try again in a moment"):
        runtimes.rollback(tmp_path, NAME)
    assert runtimes.describe(tmp_path, NAME).current == "0.7.12"

    # Held far longer than any write takes: its owner died holding it.
    old = lock.stat().st_mtime - runtimes.LOCK_STALE_S - 5
    os.utime(lock, (old, old))
    assert runtimes.rollback(tmp_path, NAME) == "0.7.11"
    assert not lock.exists()


def test_folders_are_only_ever_plain_names_under_the_runtimes_folder(tmp_path: Path) -> None:
    assert runtimes.version_dir(tmp_path, NAME, "0.7.12") == tmp_path / NAME / "0.7.12"
    for name, version in ((NAME, ".."), ("..", "0.7.12"), (NAME, "a/b"), ("scratch", "1.0")):
        with pytest.raises(ValueError):
            runtimes.version_dir(tmp_path, name, version)
    assert runtimes.scratch_dir(tmp_path) == tmp_path / "scratch"


@pytest.mark.usefixtures("_clean_environment")
def test_the_runtimes_folder_is_the_setting_or_the_demos_own(tmp_path: Path) -> None:
    folder = tmp_path / "runtimes"
    assert runtimes.root(Settings(runtimes_dir=folder, data_dir=tmp_path)) == folder
    assert runtimes.root(Settings(local_ai_demo=True, data_dir=tmp_path)) == (
        tmp_path / "tmp" / "local-ai" / "demo-runtimes"
    )
    with pytest.raises(RuntimeProblem) as caught:
        runtimes.root(Settings(data_dir=tmp_path))
    assert str(caught.value) == UNAVAILABLE


def test_the_demo_installs_nothing_and_starts_nothing(tmp_path: Path) -> None:
    demo = fake.DemoKind()
    run = fake.FakeRunner()  # answers nothing: any command fails the test

    runtimes.install(demo, "2.0", tmp_path, run=run)
    runtimes.activate(tmp_path, "demo", "2.0")

    assert run.calls == []
    assert runtimes.describe(tmp_path, "demo").current == "2.0"
    with pytest.raises(RuntimeError, match="never started"):
        demo.serve(tmp_path / "demo" / "2.0", tmp_path / "m", "", 54806, KEY, tmp_path)
    assert demo.owns("python -m mlx_vlm.server --model x", tmp_path) is False
    assert demo.is_newer("2.1", "2.0") is True


# --- the program's newest version ---------------------------------------------------


def test_the_newest_version_is_read_from_the_index() -> None:
    asked: list[str] = []

    def index(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json={"info": {"version": "0.7.13", "name": "mlx-vlm"}})

    transport = httpx.MockTransport(index)
    newest = MlxVlm().latest(lambda url: hub.fetch_json(url, transport=transport))

    assert newest == "0.7.13"
    assert asked == ["https://pypi.org/pypi/mlx-vlm/json"]
    assert MlxVlm().is_newer(newest, "0.7.12") is True


def test_a_newest_version_the_install_would_refuse_is_not_offered() -> None:
    # "Update to X" must never be a button that cannot work.
    with pytest.raises(hub.HubError) as caught:
        MlxVlm().latest(lambda _url: {"info": {"version": "0.8.0.dev3"}})
    assert caught.value.problem == "bad_answer"


@pytest.mark.parametrize(
    ("latest", "installed_version", "newer"),
    [
        ("0.7.12", "0.7.9", True),
        ("0.7.9", "0.7.12", False),
        ("0.7.12", "0.7.12", False),
        ("0.8", "0.7.12", True),
        ("0.8", "0.8.0", False),
        ("0.8.0", "0.8", False),
        ("1.0.0", "0.99.99", True),
        ("0.8.0", "0.8.0rc2", True),
        ("0.8.0rc2", "0.8.0rc1", True),
        ("0.8.0rc1", "0.8.0b3", True),
        ("0.8.0rc1", "0.7.12", True),
        ("0.8.0.post1", "0.8.0", True),
        ("0.8.0", "0.8.0.post1", False),
        ("not-a-version", "0.7.12", False),
        ("0.7.13", "", False),
    ],
)
def test_versions_are_compared_as_numbers(latest: str, installed_version: str, newer: bool) -> None:
    assert MlxVlm().is_newer(latest, installed_version) is newer


@pytest.mark.parametrize(
    "newest_first",
    [
        ("0.8.1", "0.8", "0.7.12", "0.7.9"),
        ("0.7.12.post1", "0.7.12", "0.7.12rc1"),
        ("1.0", "1.0rc1", "1.0b2", "1.0a1"),
    ],
)
def test_the_versions_on_disk_are_listed_by_the_rule_updates_use(
    tmp_path: Path, newest_first: tuple[str, ...]
) -> None:
    """One rule for which release follows which: the list of what is
    installed never disagrees with "update available"."""
    for version in sorted(newest_first):  # installed in no particular order
        runtimes.install(MlxVlm(), version, tmp_path, run=runner())

    listed = runtimes.describe(tmp_path, NAME).versions

    assert listed == newest_first
    for newer, older in zip(listed, listed[1:], strict=False):
        assert MlxVlm().is_newer(newer, older), (newer, older)


# --- starting it and speaking to it -------------------------------------------------


def serve(tmp_path: Path) -> tuple[kinds.Serve, Path, Path, Path]:
    folder = tmp_path / "runtimes" / NAME / "0.7.12"
    model = tmp_path / "models" / "local-ai" / "mlx-community" / "gemma-4-e4b-it-4bit" / "475b908"
    scratch = tmp_path / "runtimes" / "scratch"
    return MlxVlm().serve(folder, model, "", 54806, KEY, scratch), folder, model, scratch


def test_the_runtime_is_started_on_loopback_with_the_model_folder(tmp_path: Path) -> None:
    started, folder, model, scratch = serve(tmp_path)

    assert started.argv == [
        str(folder / "venv" / "bin" / "python"),
        "-m",
        "mlx_vlm.server",
        "--host",
        "127.0.0.1",
        "--port",
        "54806",
        "--model",
        str(model),
        "--log-level",
        "WARNING",
    ]
    assert "0.0.0.0" not in started.argv  # noqa: S104 - asserting it is absent
    assert started.cwd == scratch

    # The model is asked for by the very string it was started with, or the
    # program reads it as a second model and loads it again.
    body = {"model": "anything", "stream": True, "messages": [], "temperature": 0}
    sent = MlxVlm().request(body, model)
    assert sent == {"model": str(model), "stream": False, "messages": [], "temperature": 0}
    assert sent["model"] == started.argv[started.argv.index("--model") + 1]
    assert body["model"] == "anything"  # what FrameFound built is not changed


def test_the_key_travels_in_the_environment_never_on_the_command_line(tmp_path: Path) -> None:
    started, _folder, _model, _scratch = serve(tmp_path)

    assert started.env["MLX_VLM_SERVER_API_KEY"] == KEY
    assert not any(KEY in word for word in started.argv)
    assert "--api-key" not in started.argv
    assert MlxVlm().auth_headers(KEY) == {"Authorization": f"Bearer {KEY}"}


def test_the_runtime_is_given_none_of_framefounds_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The model server's own environment on a Mac: framefound.env, what
    # every service gets on top, and a stored Hugging Face token.
    layout = Layout(home=tmp_path / "FrameFound", source=tmp_path / "src")
    settings_file = envfile.parse(envfile.initial(layout, "/opt/homebrew/opt/postgresql@17/bin"))
    parent = {
        **settings_file,
        **services.process_environment(layout, settings_file),
        "HF_TOKEN": "not-a-real-token",
        "HOME": str(tmp_path / "home"),
        "LANG": "en_GB.UTF-8",
    }
    for name, value in parent.items():
        monkeypatch.setenv(name, value)

    started, _folder, _model, scratch = serve(tmp_path)

    allowed = set(PASSED_ON) | {
        "MLX_VLM_SERVER_API_KEY",
        "HF_HUB_OFFLINE",
        "TRANSFORMERS_OFFLINE",
        "HF_HUB_DISABLE_TELEMETRY",
        "HF_HOME",
    }
    assert set(started.env) <= allowed
    assert not set(settings_file) & set(started.env)
    secrets = [
        settings_file[name]
        for name in (
            "FRAMEFOUND_SECRET_KEY",
            "FRAMEFOUND_SETUP_TOKEN",
            "POSTGRES_PASSWORD",
            "REDIS_PASSWORD",
        )
    ] + ["not-a-real-token"]
    shown = [*started.env.values(), *started.argv]
    for secret in secrets:
        assert secret
        assert not any(secret in value for value in shown)
    # Its own empty home, never the shared download cache; nothing fetched.
    assert started.env["HF_HOME"] == str(scratch / "hf-home")
    assert started.env["HF_HOME"] != parent["HF_HOME"]
    assert started.env["HF_HUB_OFFLINE"] == "1"
    assert started.env["HOME"] == parent["HOME"]
    assert started.env["LANG"] == "en_GB.UTF-8"

    # The installer is a program from the internet too.
    run = runner()
    runtimes.install(MlxVlm(), "0.7.12", tmp_path / "runtimes", run=run)
    for ran in run.ran:
        assert ran.env is not None
        assert not set(settings_file) & set(ran.env)
        assert "HF_TOKEN" not in ran.env


def test_trust_remote_code_is_never_passed_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # What `--trust-remote-code` would set, already in the parent's environment.
    monkeypatch.setenv("MLX_TRUST_REMOTE_CODE", "true")

    started, _folder, _model, _scratch = serve(tmp_path)

    assert "--trust-remote-code" not in started.argv
    assert "--model-dir" not in started.argv
    assert "MLX_TRUST_REMOTE_CODE" not in started.env


def test_child_env_passes_on_only_the_allow_list() -> None:
    parent = {
        "PATH": "/usr/bin",
        "HOME": "/Users/editor",
        "TMPDIR": "/tmp/x",  # noqa: S108 - a value in a dict, nothing is written there
        "USER": "editor",
        "LANG": "en_GB.UTF-8",
        "LC_ALL": "C",
        "POSTGRES_PASSWORD": "secret",
        "PYTHONPATH": "/somewhere",
        "DYLD_INSERT_LIBRARIES": "/evil.dylib",
    }

    env = kinds.child_env({"EXTRA": "1", "PATH": "/opt/bin"}, parent=parent)

    assert env == {
        "PATH": "/opt/bin",
        "HOME": "/Users/editor",
        "TMPDIR": "/tmp/x",  # noqa: S108 - as above
        "USER": "editor",
        "LANG": "en_GB.UTF-8",
        "LC_ALL": "C",
        "EXTRA": "1",
    }
    assert kinds.child_env({}, parent={}) == {}


def test_a_picture_is_always_data_and_never_an_address() -> None:
    part = MlxVlm().picture_part(b"\xff\xd8\xff\xe0jpeg")

    assert part == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,/9j/4GpwZWc="},
    }


@pytest.mark.parametrize(
    ("status", "body", "ready"),
    [
        (200, {"status": "healthy", "loaded_model": "x"}, True),
        (200, {"status": "loading"}, False),
        (503, {"status": "healthy"}, False),
        (401, {}, False),
    ],
)
def test_ready_means_the_program_says_it_is_healthy(
    status: int, body: Mapping[str, Any], ready: bool
) -> None:
    assert MlxVlm().is_ready(status, body) is ready
    assert (MlxVlm().health_path, MlxVlm().chat_path) == ("/health", "/v1/chat/completions")


@pytest.mark.parametrize(
    ("answer", "gb"),
    [
        ({"timings": {"peak_memory": 5.2}}, 5.2),
        ({"timings": {"peak_memory": 6}}, 6.0),
        ({"timings": {"peak_memory": 0}}, None),
        ({"timings": {"peak_memory": -1.0}}, None),
        ({"timings": {"peak_memory": float("nan")}}, None),
        ({"timings": {"peak_memory": float("inf")}}, None),
        ({"timings": {"peak_memory": True}}, None),
        ({"timings": {"peak_memory": "5.2"}}, None),
        ({"timings": None}, None),
        ({}, None),
    ],
)
def test_memory_is_read_only_from_a_real_figure(
    answer: Mapping[str, Any], gb: float | None
) -> None:
    assert MlxVlm().memory_gb(answer) == gb


def test_a_leftover_is_recognised_by_its_program_and_its_folder(tmp_path: Path) -> None:
    folder = tmp_path / "runtimes" / NAME / "0.7.12"
    ours = f"{folder}/venv/bin/python -m mlx_vlm.server --host 127.0.0.1 --port 54806"
    other_version = ours.replace("0.7.12", "0.7.11")

    assert MlxVlm().owns(ours, folder) is True
    assert MlxVlm().owns(other_version, folder) is False
    assert MlxVlm().owns(f"{folder}/venv/bin/python -m http.server 54806", folder) is False


# --- the two stand-ins: starting a program, and running one ----------------------------


class Recorded:
    """Stands in for `subprocess.Popen` and keeps what it was given."""

    given: list[tuple[list[str], dict[str, Any]]] = []

    def __init__(self, argv: list[str], **options: Any) -> None:
        Recorded.given.append((argv, options))
        self.pid = 4242


def test_spawn_gives_the_child_only_the_environment_it_is_handed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "in-the-parent-only")
    Recorded.given = []
    monkeypatch.setattr(proc.subprocess, "Popen", Recorded)
    log = tmp_path / "model-runtime.log"

    with log.open("ab") as out:
        child = proc.spawn(
            ["python", "-m", "mlx_vlm.server", Path("x")],
            env={"PATH": "/usr/bin"},
            cwd=tmp_path,
            stdout=out,
            stderr=subprocess.STDOUT,
        )

    assert isinstance(child, Recorded)
    ((argv, options),) = Recorded.given
    assert argv == ["python", "-m", "mlx_vlm.server", "x"]
    assert options["env"] == {"PATH": "/usr/bin"}
    assert options["cwd"] == tmp_path
    assert options["stdin"] == subprocess.DEVNULL
    assert options["stderr"] == subprocess.STDOUT
    assert "shell" not in options
    # Inheriting is not a default anyone can fall into: there is no default.
    with pytest.raises(TypeError):
        proc.spawn(["python"])  # type: ignore[call-arg]


def test_spawn_says_plainly_when_the_program_or_its_folder_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv: list[str], **_options: Any) -> None:
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    monkeypatch.setattr(proc.subprocess, "Popen", missing)

    with pytest.raises(proc.CommandFailed, match="^python3.12 is not installed or not on PATH$"):
        proc.spawn(["python3.12"], env={}, cwd=tmp_path)
    with pytest.raises(proc.CommandFailed, match="is not a folder, so python could not be started"):
        proc.spawn(["python"], env={}, cwd=tmp_path / "gone")


def test_the_fake_runner_answers_only_what_it_was_told() -> None:
    run = fake.FakeRunner({"uv": (0, "general"), "uv pip install": (3, "", "no wheel\n")})

    assert run(["uv", "venv", "x"], capture=True).stdout == "general"
    # The longer key wins, and a failure with check=True is raised as proc.run raises it.
    with pytest.raises(proc.CommandFailed, match=r"uv failed \(exit 3\):\nno wheel"):
        run(["uv", "pip", "install", "x"], capture=True)
    assert run(["uv", "pip", "install", "x"], capture=True, check=False).returncode == 3
    # A program is named by its file name, wherever it lives.
    run.answer("ps", (0, "123\n"))
    assert run(["/bin/ps", "-o", "rss="], capture=True).stdout == "123\n"
    with pytest.raises(AssertionError, match="no script answers 'sysctl'"):
        run(["sysctl", "-n", "hw.memsize"])
    assert run.calls[0] == ["uv", "venv", "x"]
