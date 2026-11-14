"""What may come onto the disk, and how a model's folder is known to be whole
(local_models/files.py).

The rule behind every test here: no program code from a model's repository
ever runs, and that is kept by what is allowed onto the disk — which files
are downloaded, what their settings may say, that each arrived as listed,
and that a folder holds nothing but what its manifest lists.
"""

import errno
import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from framefound.local_models import fake, files, hub
from framefound.local_models.hub import RepoFile

SUM = "ab" * 32
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"


def weights(path: str = "model.safetensors", size: int = 4_000_000_000) -> RepoFile:
    return RepoFile(path, size, SUM, None)


def data(path: str, size: int = 1200) -> RepoFile:
    return RepoFile(path, size, None, "c" * 40)


def plan_of(name: str) -> files.FilePlan:
    return files.plan_files(hub.parse_info(fake.fixture(name)).files)


# --- which files would be downloaded -------------------------------------------


def test_the_first_models_plan_is_its_weights_and_its_settings() -> None:
    plan = plan_of("gemma-4-e4b-it-4bit")

    assert plan.problems == ()
    assert [file.path for file in plan.keep] == [
        # Smallest first: a wrong settings file is found before the weights arrive.
        "generation_config.json",
        "README.md",
        "processor_config.json",
        "tokenizer_config.json",
        "config.json",
        "chat_template.jinja",
        "model.safetensors.index.json",
        "tokenizer.json",
        "model.safetensors",
    ]
    assert [file.path for file in plan.weights] == ["model.safetensors"]
    assert plan.left_out == ((".gitattributes", "not needed to run the model"),)
    assert plan.weights_bytes == 5_146_800_534
    # The size of the download is the files kept, not the repository's history.
    assert plan.total_bytes == 5_179_239_942
    assert plan.suffixes == (".safetensors",)


def test_code_files_are_never_in_the_plan() -> None:
    plan = plan_of("needs-code")

    assert not any(file.path.endswith(".py") for file in plan.keep)
    assert ("modeling_demo.py", "program code") in plan.left_out
    assert ("processing_demo.py", "program code") in plan.left_out
    # Leaving code out is not what refuses this model: its page and its
    # settings say it cannot load without that code, and those are checked
    # elsewhere. The plan only promises the code is never fetched.
    assert plan.problems == ()

    mixed = files.plan_files(
        [
            weights(),
            data("config.json"),
            data("setup.py"),
            data("run.sh"),
            data("demo.ipynb"),
            data("assets/example.png"),
            data("docs/guide.md"),
            data("onnx/model.onnx", 9_000_000),
        ]
    )
    assert [file.path for file in mixed.keep] == ["config.json", "model.safetensors"]
    assert dict(mixed.left_out) == {
        "setup.py": "program code",
        "run.sh": "program code",
        "demo.ipynb": "program code",
        "assets/example.png": "a picture",
        "docs/guide.md": "not needed to run the model",
        "onnx/model.onnx": "an older weights format",
    }


@pytest.mark.parametrize("name", fake.fixture_names())
def test_every_listed_file_is_either_kept_or_left_out_with_a_reason(name: str) -> None:
    listed = hub.parse_info(fake.fixture(name)).files
    plan = files.plan_files(listed)

    kept = [file.path for file in plan.keep]
    left = [path for path, _why in plan.left_out]
    assert sorted(kept + left) == sorted(file.path for file in listed)
    assert all(why for _path, why in plan.left_out)
    assert all(files.safe_path(path) for path in kept)
    assert plan.total_bytes == sum(file.size for file in plan.keep)
    assert set(plan.weights) <= set(plan.keep)


@pytest.mark.parametrize(
    "path",
    [
        "../model.safetensors",
        "sub/../../model.safetensors",
        "/etc/model.safetensors",
        "C:/model.safetensors",
        "sub\\model.safetensors",
        "model:stream.safetensors",
        "sub//model.safetensors",
        "sub/model.safetensors/",
        "nul\0.safetensors",
        "new\nline.safetensors",
        ".hidden/model.safetensors",
        "sub/.model.safetensors",
        "sub /model.safetensors",
        "sub./model.safetensors",
        "x" * 300 + ".safetensors",
        "",
    ],
)
def test_a_path_that_climbs_out_of_the_folder_is_refused(path: str) -> None:
    assert files.safe_path(path) is False

    # (A name that ends in a slash is not a file's name at all: it is simply
    # never asked for.)
    if path and not path.endswith("/"):
        plan = files.plan_files([RepoFile(path, 10, SUM, None), weights()])
        assert [file.path for file in plan.keep] == ["model.safetensors"]
        assert len(plan.problems) == 1
        assert plan.problems[0].startswith(
            "One of its files has a name FrameFound will not write to disk ("
        )
        assert "\0" not in plan.problems[0] and "\n" not in plan.problems[0]


@pytest.mark.parametrize(
    "path",
    [
        "model.safetensors",
        "text_encoder/model-00001-of-00002.safetensors",
        "config.json",
        "LICENSE",
        "a dir/a file.json",
        "Modèle/config.json",
    ],
)
def test_an_ordinary_name_in_an_ordinary_folder_is_fine(path: str) -> None:
    assert files.safe_path(path) is True


def test_hidden_files_are_left_out_rather_than_refusing_the_model() -> None:
    plan = files.plan_files([weights(), data(".cache/info.json"), data(".gitattributes")])

    assert plan.problems == ()
    assert dict(plan.left_out) == {
        ".cache/info.json": "a hidden file",
        ".gitattributes": "not needed to run the model",
    }
    # A settings file that climbs is not "hidden": it is refused.
    climbing = files.plan_files([weights(), data("../.cache/info.json")])
    assert len(climbing.problems) == 1


def test_weights_that_cannot_be_verified_are_refused() -> None:
    plan = files.plan_files([RepoFile("model.safetensors", 4_000_000_000, None, "c" * 40)])

    assert plan.keep == ()
    assert plan.problems == (
        "Hugging Face lists no checksum for model.safetensors, so what is downloaded cannot "
        "be verified.",
    )
    # A small settings file without one is still taken: it is checked by size,
    # and read for what it asks for.
    assert files.plan_files([weights(), RepoFile("config.json", 90, None, None)]).problems == ()


def test_older_weight_formats_are_refused_in_plain_words() -> None:
    pickled = files.plan_files(
        [data("config.json"), data("pytorch_model.bin", 5_000_000_000), data("extra.pt", 10)]
    )
    assert [file.path for file in pickled.keep] == ["config.json"]
    assert pickled.weights == () and pickled.suffixes == ()
    assert pickled.problems == (
        "Its weights are in an older file format (.bin, .pt) that can carry program code. "
        "FrameFound only accepts safetensors and GGUF files.",
    )

    one = files.plan_files([data("weights.npz", 3_000_000_000)])
    assert one.problems == (
        "Its weights are in an older file format (.npz) that can carry program code. "
        "FrameFound only accepts safetensors and GGUF files.",
    )

    nothing = files.plan_files([data("config.json"), data("README.md")])
    assert nothing.problems == (
        "It has no model weights FrameFound can use. FrameFound only accepts safetensors "
        "and GGUF files.",
    )

    # With good weights beside them, the old files are simply not fetched.
    both = files.plan_files([weights(), data("pytorch_model.bin", 5_000_000_000)])
    assert both.problems == ()
    assert both.left_out == (("pytorch_model.bin", "an older weights format"),)

    gguf = files.plan_files([weights("model-Q4_K_M.gguf"), weights("mmproj-f16.GGUF", 9)])
    assert gguf.suffixes == (".gguf",) and len(gguf.weights) == 2


def test_a_settings_file_far_too_large_to_be_one_is_refused() -> None:
    plan = files.plan_files([weights(), data("tokenizer.json", files.MAX_DATA_BYTES + 1)])

    assert [file.path for file in plan.keep] == ["model.safetensors"]
    assert plan.problems == (
        "tokenizer.json is 128 MB, far larger than a settings file should be. FrameFound "
        "will not download it.",
    )
    assert (
        files.plan_files([weights(), data("tokenizer.json", files.MAX_DATA_BYTES)]).problems == ()
    )
    # Weights have no such limit.
    assert files.plan_files([weights(size=40 * 1024**3)]).problems == ()


def test_two_names_that_this_machines_disk_cannot_tell_apart_are_refused() -> None:
    plan = files.plan_files([weights(), data("Config.json"), data("config.json")])
    assert plan.problems == (
        "Two of its files have names that differ only in capitals (Config.json and "
        "config.json). This machine's disk cannot hold both.",
    )

    twice = files.plan_files([weights(), data("config.json"), data("config.json")])
    assert twice.problems == ("Hugging Face lists config.json twice.",)


def test_a_repository_file_named_like_framefounds_own_list_is_left_out() -> None:
    # It would be overwritten by the manifest and the folder would never
    # match its own list again.
    plan = files.plan_files([weights(), data("manifest.json"), data("sub/manifest.json")])

    assert plan.problems == ()
    assert [file.path for file in plan.keep] == ["sub/manifest.json", "model.safetensors"]
    assert plan.left_out == (("manifest.json", "FrameFound keeps its own file under this name"),)


# --- what a settings file may say ----------------------------------------------


def test_a_config_that_names_a_model_file_is_refused() -> None:
    # The page of this model says nothing about code. Only the file does.
    config = fake.fixture_contents("model-file")["config.json"].decode()
    assert hub.parse_info(fake.fixture("model-file")).custom_code is False

    assert files.inspect_config("config.json", config) == [files.NEEDS_CODE]
    assert files.NEEDS_CODE == (
        "This model only runs with program code from its own repository. FrameFound never "
        "runs code that comes with a model, so it has to wait for a build that does not "
        "need any."
    )


@pytest.mark.parametrize(
    "config",
    [
        {"auto_map": {"AutoModel": "modeling_demo.DemoModel"}},
        {"model_file": "demo_model.py"},
        {"custom_pipelines": {"demo": {"impl": "pipeline.DemoPipeline"}}},
        {"vision_config": {"auto_map": {"AutoModel": "vision.Tower"}}},
        {"processors": [{"name": "a"}, {"auto_map": ["processing.Processor"]}]},
    ],
)
def test_a_settings_file_that_asks_for_repository_code_anywhere_in_it_is_refused(
    config: dict[str, object],
) -> None:
    assert files.inspect_config("processor_config.json", json.dumps(config)) == [files.NEEDS_CODE]


def test_a_clean_settings_file_is_let_through() -> None:
    clean = {
        "model_type": "gemma4",
        "architectures": ["Gemma4ForConditionalGeneration"],
        # Named, and empty: no code is asked for.
        "auto_map": None,
        "custom_pipelines": {},
        "vision_config": {"hidden_size": 1152, "model_file": ""},
        "note": "model_file and auto_map are only words here",
    }

    assert files.inspect_config("config.json", json.dumps(clean)) == []
    assert files.inspect_config("config.json", "{}") == []


@pytest.mark.parametrize("text", ["", "{", "[1, 2]", '"auto_map"', "[" * 100_000, "\x00"])
def test_a_settings_file_that_cannot_be_read_cannot_be_cleared(text: str) -> None:
    assert files.inspect_config("config.json", text) == [
        "config.json could not be read, so FrameFound cannot tell whether this model needs "
        "program code from its repository."
    ]


def test_a_settings_file_however_deep_does_not_bring_the_check_down() -> None:
    deep: dict[str, object] = {"model_file": "deep.py"}
    for _ in range(900):
        deep = {"inside": deep}

    assert files.inspect_config("config.json", json.dumps(deep)) == [files.NEEDS_CODE]


def test_the_settings_files_to_inspect_are_found_wherever_they_sit() -> None:
    listed = [
        weights(),
        data("config.json"),
        data("generation_config.json"),
        data("vision/preprocessor_config.json"),
        data("tokenizer_config.json"),
        data("tokenizer.json"),
    ]

    assert [file.path for file in files.to_inspect(listed)] == [
        "config.json",
        "vision/preprocessor_config.json",
        "tokenizer_config.json",
    ]


def test_a_settings_file_spelled_in_other_capitals_is_inspected_all_the_same() -> None:
    # Hugging Face tells Config.json from config.json. The Mac's disk and the
    # share do not: the program that opens "config.json" there is handed this
    # file, so it is a settings file whatever its capitals.
    listed = [
        weights(),
        data("Config.json"),
        data("TOKENIZER_CONFIG.json"),
        data("vision/Preprocessor_Config.JSON"),
        data("Tokenizer.json"),
    ]

    plan = files.plan_files(listed)
    assert plan.problems == ()
    assert sorted(file.path for file in files.to_inspect(plan.keep)) == [
        "Config.json",
        "TOKENIZER_CONFIG.json",
        "vision/Preprocessor_Config.JSON",
    ]


def test_more_settings_files_than_one_model_has_is_a_refusal() -> None:
    # Each is read over the network before a model is accepted, and how many
    # a repository lists is its publisher's choice: a repository that keeps
    # every training checkpoint has hundreds.
    def checkpoints(count: int) -> list[RepoFile]:
        return [weights(), *(data(f"checkpoint-{n}/config.json") for n in range(count))]

    assert files.plan_files(checkpoints(files.MAX_INSPECTED)).problems == ()
    assert files.plan_files(checkpoints(files.MAX_INSPECTED + 1)).problems == (
        "It has 17 settings files (config.json and the like), far more than one model has. "
        "FrameFound reads every one of them before it accepts a model, and will not read "
        "that many.",
    )


# --- that a file arrived as listed ---------------------------------------------


def test_a_small_file_is_identified_the_way_git_identifies_it(tmp_path: Path) -> None:
    # Known values: what `git hash-object` prints for these two files.
    hello = tmp_path / "hello.txt"
    hello.write_bytes(b"hello\n")
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")

    assert files.git_blob_id(hello) == "ce013625030ba8dba906f756967f9e9ca394464a"
    assert files.git_blob_id(empty) == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert files.sha256_of(hello) == hashlib.sha256(b"hello\n").hexdigest()


def test_a_file_is_checked_by_size_then_by_the_strongest_checksum_listed(tmp_path: Path) -> None:
    body = b"hello\n"
    path = tmp_path / "config.json"
    path.write_bytes(body)
    sha256 = hashlib.sha256(body).hexdigest()
    blob = "ce013625030ba8dba906f756967f9e9ca394464a"
    wrong = "is not the file Hugging Face lists (its checksum is different), so it was thrown away."

    assert files.check_file(path, RepoFile("config.json", 6, sha256, None)) is None
    assert files.check_file(path, RepoFile("config.json", 6, None, blob)) is None
    assert files.check_file(path, RepoFile("config.json", 6, None, None)) is None

    assert files.check_file(path, RepoFile("config.json", 7, sha256, blob)) == (
        "config.json is not the size Hugging Face lists for it, so it was thrown away."
    )
    assert files.check_file(path, RepoFile("config.json", 6, "0" * 64, None)) == (
        f"config.json {wrong}"
    )
    assert files.check_file(path, RepoFile("config.json", 6, None, "0" * 40)) == (
        f"config.json {wrong}"
    )
    # When both are listed the SHA-256 decides; the blob id is not consulted.
    assert files.check_file(path, RepoFile("config.json", 6, sha256, "0" * 40)) is None
    assert files.check_file(path, RepoFile("config.json", 6, "0" * 64, blob)) is not None

    assert files.check_file(tmp_path / "gone.json", RepoFile("gone.json", 6, None, None)) == (
        "gone.json is not there."
    )
    assert files.check_file(tmp_path, RepoFile("a-folder", 0, None, None)) == (
        "a-folder is not there."
    )


# --- a disk that cannot be read is not a model that is wrong ---------------------


def breaking(monkeypatch: pytest.MonkeyPatch, method: str, name: str, code: int) -> None:
    """Make one way of reading fail for paths called `name`, as a stalled share does."""
    real = getattr(Path, method)

    def broken(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == name:
            raise OSError(code, os.strerror(code))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, broken)


# Input/output error, timed out, refused: what a share that has stalled or
# gone stale answers. None of them says the file is not there.
AWAY = [errno.EIO, errno.ETIMEDOUT, errno.EACCES]


@pytest.mark.parametrize("code", AWAY)
def test_a_share_that_stops_answering_is_not_a_model_that_is_not_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    # Read as "no manifest", this would offer to download again a model that
    # is sitting there whole.
    a_model(tmp_path)
    breaking(monkeypatch, "stat", files.MANIFEST, code)

    with pytest.raises(OSError) as raised:
        files.read_manifest(tmp_path)
    assert raised.value.errno == code
    with pytest.raises(OSError):
        files.folder_problem(tmp_path)


@pytest.mark.parametrize("code", AWAY)
def test_a_manifest_that_cannot_be_opened_is_not_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    a_model(tmp_path)
    breaking(monkeypatch, "open", files.MANIFEST, code)

    with pytest.raises(OSError):
        files.read_manifest(tmp_path)


@pytest.mark.parametrize("code", AWAY)
# A listed file; and a folder inside the model, which is looked at to see
# that it is a folder and not a link to somewhere else.
@pytest.mark.parametrize("name", ["model.safetensors", "tokenizer"])
def test_a_file_the_share_will_not_answer_for_is_not_a_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int, name: str
) -> None:
    # "This copy of the model is missing model.safetensors" would send the
    # operator to download nine gigabytes that are already there.
    a_model(tmp_path)
    breaking(monkeypatch, "lstat", name, code)

    with pytest.raises(OSError) as raised:
        files.folder_problem(tmp_path)
    assert raised.value.errno == code


@pytest.mark.parametrize("code", AWAY)
def test_a_settings_file_that_cannot_be_opened_is_not_a_missing_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    a_model(tmp_path)
    breaking(monkeypatch, "open", "config.json", code)

    with pytest.raises(OSError):
        files.folder_problem(tmp_path)


@pytest.mark.parametrize("code", AWAY)
def test_a_folder_that_cannot_be_listed_is_not_a_folder_that_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    a_model(tmp_path)
    real = os.scandir

    def broken(path: Any = ".") -> Any:
        if Path(os.fspath(path)).name == "tokenizer":
            raise OSError(code, os.strerror(code))
        return real(path)

    monkeypatch.setattr(os, "scandir", broken)

    with pytest.raises(OSError) as raised:
        files.folder_problem(tmp_path)
    assert raised.value.errno == code


@pytest.mark.parametrize("code", AWAY)
def test_a_downloaded_file_the_disk_will_not_answer_for_is_not_a_wrong_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"hello\n")
    breaking(monkeypatch, "stat", "config.json", code)

    with pytest.raises(OSError):
        files.check_file(path, RepoFile("config.json", 6, None, None))


def test_a_folder_that_goes_while_it_is_looked_through_was_not_read_in_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Gone is gone, and gets a sentence: nobody saw what was in it.
    a_model(tmp_path)
    (tmp_path / "empty").mkdir()
    real = os.scandir

    def vanished(path: Any = ".") -> Any:
        if Path(os.fspath(path)).name == "empty":
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT))
        return real(path)

    monkeypatch.setattr(os, "scandir", vanished)

    assert files.folder_problem(tmp_path) == "This copy of the model could not be read in full."


# --- the manifest and the folder -----------------------------------------------


def a_model(
    folder: Path, extra: dict[str, bytes] | None = None, *, without: tuple[str, ...] = ()
) -> files.Manifest:
    """A finished model folder: three files and the manifest that lists them."""
    bodies = {
        "config.json": b'{"model_type": "gemma4"}',
        "model.safetensors": b"\x00" * 64,
        "tokenizer/tokenizer_config.json": b"{}",
        **(extra or {}),
    }
    for name in without:
        del bodies[name]
    listed = []
    for path, body in bodies.items():
        target = folder / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        listed.append(RepoFile(path, len(body), hashlib.sha256(body).hexdigest(), None))
    manifest = files.Manifest(
        repo_id=FIRST,
        revision=BUILD,
        variant="",
        runtime="mlx-vlm",
        files=tuple(listed),
        completed_at="2026-11-12T18:39:10Z",
    )
    files.write_manifest(folder, manifest)
    return manifest


def test_a_manifest_comes_back_as_it_was_written(tmp_path: Path) -> None:
    folder = tmp_path / "library" / "mlx-community" / "gemma-4-e4b-it-4bit" / BUILD
    assert files.read_manifest(folder) is None

    manifest = a_model(folder)

    assert files.read_manifest(folder) == manifest
    assert manifest.demo is False
    # Written beside its name and moved into place: nothing half-written is left.
    assert not (folder / "manifest.json.partial").exists()
    raw = (folder / files.MANIFEST).read_bytes()
    assert b"\r" not in raw and raw.endswith(b"}\n")
    assert json.loads(raw)["version"] == 1

    demo = files.Manifest(FIRST, BUILD, "", "demo", manifest.files, "2026-11-12T18:39:10Z", True)
    files.write_manifest(folder, demo)
    assert files.read_manifest(folder) == demo


def test_a_manifest_from_a_newer_version_still_reads(tmp_path: Path) -> None:
    manifest = a_model(tmp_path)
    raw = json.loads((tmp_path / files.MANIFEST).read_text(encoding="utf-8"))
    raw["version"] = 2
    raw["added_later"] = {"anything": True}
    (tmp_path / files.MANIFEST).write_text(json.dumps(raw), encoding="utf-8")

    assert files.read_manifest(tmp_path) == manifest


@pytest.mark.parametrize(
    "change",
    [
        lambda raw: raw.pop("files"),
        lambda raw: raw.pop("revision"),
        lambda raw: raw.update(repo_id=""),
        lambda raw: raw.update(runtime=7),
        lambda raw: raw["files"].append("model.py"),
        lambda raw: raw["files"].append({"path": "../outside.json", "size": 2}),
        lambda raw: raw["files"].append({"path": "manifest.json", "size": 2}),
        lambda raw: raw["files"].append({"path": "CONFIG.JSON", "size": 2}),
        lambda raw: raw["files"].append({"path": "extra.json", "size": -1}),
        lambda raw: raw["files"].append({"path": "extra.json", "size": True}),
        lambda raw: raw["files"].append({"path": "extra.json", "size": 2, "sha256": "short"}),
        lambda raw: raw["files"].append({"path": "extra.json", "size": 2, "blob_id": 5}),
    ],
)
def test_a_manifest_that_cannot_be_trusted_is_no_manifest(
    tmp_path: Path, change: Callable[[dict[str, Any]], object]
) -> None:
    # No manifest means "not installed": the safe reading of every one of these.
    a_model(tmp_path)
    raw = json.loads((tmp_path / files.MANIFEST).read_text(encoding="utf-8"))
    change(raw)
    (tmp_path / files.MANIFEST).write_text(json.dumps(raw), encoding="utf-8")

    assert files.read_manifest(tmp_path) is None
    assert files.folder_problem(tmp_path) == (
        "This copy of the model has no list of its files, so it is not complete."
    )


@pytest.mark.parametrize("text", ["", "{", "[]", '{"files": 3}'])
def test_half_a_manifest_is_no_manifest(tmp_path: Path, text: str) -> None:
    (tmp_path / files.MANIFEST).write_text(text, encoding="utf-8")

    assert files.read_manifest(tmp_path) is None


def test_a_whole_folder_may_be_loaded(tmp_path: Path) -> None:
    a_model(tmp_path)

    assert files.folder_problem(tmp_path) is None


def test_a_folder_with_a_file_its_manifest_does_not_list_is_refused(tmp_path: Path) -> None:
    a_model(tmp_path)
    (tmp_path / "model.py").write_text("print('run me')\n", encoding="utf-8")
    assert files.folder_problem(tmp_path) == (
        "This copy of the model holds something that is not part of it (model.py). FrameFound "
        "will not load a model from a folder like that."
    )

    (tmp_path / "model.py").unlink()
    (tmp_path / "tokenizer" / "extra.json").write_text("{}", encoding="utf-8")
    problem = files.folder_problem(tmp_path)
    assert problem is not None and "(tokenizer/extra.json)" in problem

    # A download that was interrupted and never tidied is not a finished model.
    (tmp_path / "tokenizer" / "extra.json").unlink()
    (tmp_path / "model.safetensors.partial").write_bytes(b"")
    problem = files.folder_problem(tmp_path)
    assert problem is not None and "(model.safetensors.partial)" in problem

    (tmp_path / "model.safetensors.partial").unlink()
    (tmp_path / "__pycache__").mkdir()
    assert files.folder_problem(tmp_path) is None
    (tmp_path / "__pycache__" / "model.cpython-312.pyc").write_bytes(b"\x00")
    assert files.folder_problem(tmp_path) is not None


def test_what_the_finder_leaves_behind_does_not_stop_a_model_loading(tmp_path: Path) -> None:
    # Opening the folder on the Mac, or on the NAS from the Mac, leaves these.
    # No runtime reads them, and "it stopped working after I looked at it"
    # would be a fault with no visible cause.
    a_model(tmp_path)
    (tmp_path / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
    (tmp_path / "._model.safetensors").write_bytes(b"\x00\x05\x16\x07")
    (tmp_path / "tokenizer" / ".DS_Store").write_bytes(b"\x00")

    assert files.folder_problem(tmp_path) is None

    (tmp_path / ".hidden.py").write_bytes(b"")
    assert files.folder_problem(tmp_path) is not None


def test_a_folder_missing_a_file_or_holding_one_of_the_wrong_size_is_refused(
    tmp_path: Path,
) -> None:
    a_model(tmp_path)

    (tmp_path / "model.safetensors").write_bytes(b"\x00" * 63)
    assert files.folder_problem(tmp_path) == (
        "model.safetensors in this copy of the model is not the size it should be."
    )
    (tmp_path / "model.safetensors").unlink()
    assert files.folder_problem(tmp_path) == "This copy of the model is missing model.safetensors."
    (tmp_path / "model.safetensors").mkdir()
    assert files.folder_problem(tmp_path) == (
        "model.safetensors in this copy of the model is not an ordinary file."
    )


def test_a_link_in_a_model_folder_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "model"
    a_model(folder)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "model.safetensors").write_bytes(b"\x00" * 64)
    (elsewhere / "code.py").write_bytes(b"")
    try:
        (folder / "model.safetensors").unlink()
        os.symlink(elsewhere / "model.safetensors", folder / "model.safetensors")
    except OSError:
        pytest.skip("this machine does not allow making links")

    assert files.folder_problem(folder) == (
        "model.safetensors in this copy of the model is not an ordinary file."
    )

    (folder / "model.safetensors").unlink()
    (folder / "model.safetensors").write_bytes(b"\x00" * 64)
    os.symlink(elsewhere, folder / "linked", target_is_directory=True)
    problem = files.folder_problem(folder)
    assert problem is not None and "(linked)" in problem


def test_a_settings_file_changed_on_disk_to_ask_for_code_is_refused(tmp_path: Path) -> None:
    # Same length as the file the manifest lists, so its size gives nothing
    # away: the folder is read again before every load, not trusted from the
    # day it was downloaded.
    clean = b'{"model_type": "gemma4", "padding": "xxxxxxxxxxxxx"}'
    changed = b'{"model_type": "gemma4", "model_file": "evil.py"}   '
    assert len(clean) == len(changed)
    a_model(tmp_path, {"config.json": clean})
    assert files.folder_problem(tmp_path) is None

    (tmp_path / "config.json").write_bytes(changed)
    assert files.folder_problem(tmp_path) == files.NEEDS_CODE

    # The same for one in a folder of its own, and for one nobody can read.
    (tmp_path / "config.json").write_bytes(clean)
    (tmp_path / "tokenizer" / "tokenizer_config.json").write_bytes(b"[]")
    assert files.folder_problem(tmp_path) == (
        "tokenizer/tokenizer_config.json could not be read, so FrameFound cannot tell whether "
        "this model needs program code from its repository."
    )


@pytest.mark.parametrize("name", ["Config.json", "CONFIG.JSON", "vision/Preprocessor_Config.JSON"])
def test_a_settings_file_in_other_capitals_is_read_again_before_a_load(
    tmp_path: Path, name: str
) -> None:
    # On this disk the program that opens "config.json" is handed Config.json.
    # A file skipped for a capital letter would be the one file nobody looked at.
    a_model(tmp_path, {name: b'{"model_file": "notes.txt"}'}, without=("config.json",))

    assert files.folder_problem(tmp_path) == files.NEEDS_CODE


# --- a GGUF file has to be one --------------------------------------------------------


def test_a_gguf_file_that_does_not_start_as_one_is_thrown_away_whatever_its_checksum(
    tmp_path: Path,
) -> None:
    # A storage host's error page saved under the model's name has the size
    # nobody checked and a checksum someone could have listed for it.
    for name, body in (
        ("demo-Q4_K_M.gguf", b"<html>Access denied</html>"),
        ("vision/MMPROJ-F16.GGUF", b"gguf" + bytes(22)),
    ):
        path = tmp_path / name.replace("/", "-")
        path.write_bytes(body)
        listed = RepoFile(name, len(body), hashlib.sha256(body).hexdigest(), None)

        refused = files.check_file(path, listed)
        assert refused == f"{name} is not a GGUF file, so it was thrown away."

    good = b"GGUF\x03\x00\x00\x00" + bytes(24)
    path = tmp_path / "demo-Q8_0.gguf"
    path.write_bytes(good)
    checksum = hashlib.sha256(good).hexdigest()
    assert files.check_file(path, RepoFile(path.name, 32, checksum, None)) is None
    # The size is still checked first, and the checksum still after.
    assert files.check_file(path, RepoFile(path.name, 33, None, None)) == (
        "demo-Q8_0.gguf is not the size Hugging Face lists for it, so it was thrown away."
    )
    wrong_sum = files.check_file(path, RepoFile(path.name, 32, SUM, None))
    assert wrong_sum is not None and "its checksum is different" in wrong_sum
    # Only a .gguf file is held to it: safetensors have no such mark.
    other = tmp_path / "model.safetensors"
    other.write_bytes(b"{}")
    assert files.check_file(other, RepoFile(other.name, 2, None, None)) is None


@pytest.mark.parametrize("code", AWAY)
def test_a_gguf_file_that_cannot_be_read_is_not_a_wrong_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    path = tmp_path / "demo-Q4_K_M.gguf"
    path.write_bytes(b"GGUF" + bytes(4))
    breaking(monkeypatch, "open", path.name, code)

    with pytest.raises(OSError) as raised:
        files.check_file(path, RepoFile(path.name, 8, None, None))
    assert raised.value.errno == code
