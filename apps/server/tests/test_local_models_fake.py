"""The stand-ins themselves (local_models/fake.py), where a later step relies on what they write.

A stand-in that drifts from the real thing makes every test built on it
prove the wrong thing, so the two that stand in for llama.cpp's world are
held here to what the real code then reads: a shrunk GGUF repository must
download into files the real `files.check_file` accepts, and
`FakeRunner(make_llama=True)` must leave what the real `llama_release`
reads back.
"""

import sys
from pathlib import Path
from typing import Any

import pytest

from framefound.local_models import fake, files, gguf, hub, llama_release, runtimes
from framefound.local_models.kinds import LlamaCpp
from framefound.native.proc import CommandFailed

VISION = "google/demo-vision-gguf"


def test_a_shrunk_gguf_repository_downloads_into_files_that_pass_the_real_check(
    tmp_path: Path,
) -> None:
    hub = fake.FakeHub(payloads={VISION: fake.fixture("gguf/demo-vision-gguf")})
    hub.shrink(VISION)
    info = hub.info(VISION)
    ggufs = [file for file in info.files if file.path.lower().endswith(".gguf")]
    assert ggufs
    for file in info.files:
        target = tmp_path / file.path
        hub.fetch(VISION, info.revision, file.path, target)
        assert files.check_file(target, file) is None, file.path
    for file in ggufs:
        assert gguf.looks_like_gguf(tmp_path / file.path), file.path


def test_the_demos_small_files_still_start_as_gguf() -> None:
    body = fake._filler(VISION, "demo-vision-Q4_K_M.gguf", 4096)
    assert body.startswith(gguf.MAGIC) and len(body) == 4096
    # Other files are what they were.
    assert not fake._filler(VISION, "model.safetensors", 64).startswith(gguf.MAGIC)
    assert fake._filler(VISION, "a.gguf", 2) == gguf.MAGIC


def test_the_fake_llama_install_leaves_what_the_real_one_reads_back(tmp_path: Path) -> None:
    run = fake.FakeRunner(make_llama=True)
    runtimes.install(LlamaCpp(), "0.9.0", tmp_path, run=run)
    folder = runtimes.version_dir(tmp_path, "llama-cpp", "0.9.0")

    record = llama_release.record(folder)
    assert record is not None
    assert (record["version"], record["tag"], record["server"]) == (
        "0.9.0",
        "b12110",
        "llama-b12110/llama-server",
    )
    assert record["asset"] == "llama-b12110-bin-macos-arm64.tar.gz"
    assert llama_release.program(folder) == folder / "dist" / "llama-b12110" / "llama-server"
    assert llama_release.about(folder) == "build b12110"
    assert fake.llama_build("b12318") == "b12318"


def test_a_scripted_llama_step_wins_and_a_failed_fetch_writes_nothing(tmp_path: Path) -> None:
    folder = tmp_path / "0.9.0"
    folder.mkdir()
    fetch = LlamaCpp().install_steps(folder, "0.9.0", "uv")[0]
    # Keyed by the module, whatever the interpreter's own file is called.
    assert Path(fetch[0]) == Path(sys.executable)
    run = fake.FakeRunner({"llama_release fetch": (1, "", "no build")}, make_llama=True)
    with pytest.raises(CommandFailed):
        run(fetch, capture=True)
    assert list(folder.iterdir()) == []

    # Without make_llama nothing answers a step nobody scripted.
    with pytest.raises(AssertionError, match="no script answers"):
        fake.FakeRunner()(fetch)


# --- the demo's llama.cpp ----------------------------------------------------------


def test_the_demos_llama_cpp_judges_by_the_real_rules_and_asks_nobody() -> None:
    stand_in, real = fake.DemoGgufKind(), LlamaCpp()

    assert (stand_in.name, stand_in.demo, stand_in.needs_compare) == ("demo-gguf", True, True)
    assert stand_in.label == "Demo llama.cpp (not a real program)"
    assert (stand_in.weights, stand_in.version_hint, stand_in.update_note) == (
        real.weights,
        real.version_hint,
        real.update_note,
    )
    # Wherever the demo is on, and with nothing to install it with.
    assert stand_in.supported_here("Windows", "AMD64") is None and stand_in.needs_uv is False
    for name in fake.gguf_fixture_names():
        info = hub.parse_info(fake.fixture(name))
        plan = files.plan_files(info.files)
        assert stand_in.supports(info, plan) == real.supports(info, plan), name
        support = real.supports(info, plan)
        if support.variants:
            sized = [(variant, "night") for variant in support.variants]
            assert stand_in.prefer(sized) == real.prefer(sized), name

    # Versions are llama.cpp's: a number or a daily build, and a daily build is never behind.
    assert stand_in.clean_version("v0.9.0") == "0.9.0"
    assert stand_in.clean_version("B12318") == "b12318"
    with pytest.raises(ValueError):
        stand_in.clean_version("2.0rc1")
    assert stand_in.is_newer(fake.DemoGgufKind.newest, "0.9.0") is True
    assert stand_in.is_newer(fake.DemoGgufKind.newest, "b12318") is False

    def never(_url: str) -> Any:
        raise AssertionError("the demo asked GitHub for a version")

    assert stand_in.latest(never) == fake.DemoGgufKind.newest


def test_installing_the_demos_llama_cpp_makes_a_folder_and_starts_nothing(tmp_path: Path) -> None:
    stand_in = fake.DemoGgufKind()
    # Nothing scripted answers anything: running a step at all would fail.
    runtimes.install(stand_in, "0.9.0", tmp_path, run=fake.FakeRunner())
    runtimes.install(stand_in, "b12318", tmp_path, run=fake.FakeRunner())
    numbered = runtimes.version_dir(tmp_path, "demo-gguf", "0.9.0")
    daily = runtimes.version_dir(tmp_path, "demo-gguf", "b12318")

    assert [path.name for path in numbered.iterdir()] == [runtimes.WHOLE]
    assert stand_in.program(numbered) == numbered
    # The build a numbered version names, as FakeRunner's llama.cpp says it;
    # a daily build is its own build and says nothing more.
    assert stand_in.about(numbered) == "build b12110" and stand_in.about(daily) == ""
    with pytest.raises(RuntimeError, match="never started"):
        stand_in.serve(numbered, tmp_path, "Q4_K_M", 54806, "key", tmp_path)
    assert stand_in.owns(f"llama-server -m {tmp_path}", numbered) is False
    # It speaks as the real one would, had it been started.
    body = stand_in.request({"model": str(tmp_path), "messages": []}, tmp_path)
    assert body["model"] == "framefound" and stand_in.memory_gb({}) is None
