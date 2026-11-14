"""Which file of a GGUF repository is what (local_models/gguf.py).

The file names in the first table are the live names of two real GGUF
repositories, read on 2026-11-12. The fixtures under
`local_models/demo_data/gguf/` carry invented names with real sizes. Nothing
here reaches a network or starts a process.
"""

import ast
import sys
from pathlib import Path

import pytest

from framefound.local_models import fake, files, gguf, hub
from framefound.local_models.hub import RepoFile
from framefound.local_models.kinds import Fit, Variant

GB = 1024**3
SUM = "ab" * 32


def model(path: str, size: int = 4 * GB) -> RepoFile:
    return RepoFile(path, size, SUM, None)


def text(path: str, size: int = 2000) -> RepoFile:
    return RepoFile(path, size, None, "c" * 40)


def info_of(name: str) -> hub.RepoInfo:
    return hub.parse_info(fake.fixture(f"gguf/{name}"))


def plan_of(name: str) -> files.FilePlan:
    return files.plan_files(info_of(name).files)


def ids(found: list[gguf.Build]) -> list[str]:
    return [build.id for build in found]


# --- the build id ------------------------------------------------------------------

FIRST_PREFIX = "Nemotron-3-Nano-Omni-30B-A3B-Reasoning"
SECOND_PREFIX = "NVIDIA-Nemotron-3-Nano-Omni-30B-A3B-Reasoning"

# Every model file of the two repositories read for the design (3 + 21), as
# Hugging Face listed them, and the id each must come to.
LIVE = [
    (f"{FIRST_PREFIX}-Q4_K_M.gguf", "Q4_K_M"),
    (f"{FIRST_PREFIX}-Q6_K.gguf", "Q6_K"),
    (f"{FIRST_PREFIX}-Q8_0.gguf", "Q8_0"),
    (f"{SECOND_PREFIX}-MXFP4_MOE.gguf", "MXFP4_MOE"),
    (f"{SECOND_PREFIX}-Q8_0.gguf", "Q8_0"),
    (f"{SECOND_PREFIX}-UD-IQ2_M.gguf", "UD-IQ2_M"),
    (f"{SECOND_PREFIX}-UD-IQ2_XXS.gguf", "UD-IQ2_XXS"),
    (f"{SECOND_PREFIX}-UD-IQ3_S.gguf", "UD-IQ3_S"),
    (f"{SECOND_PREFIX}-UD-IQ3_XXS.gguf", "UD-IQ3_XXS"),
    (f"{SECOND_PREFIX}-UD-IQ4_NL.gguf", "UD-IQ4_NL"),
    (f"{SECOND_PREFIX}-UD-IQ4_NL_XL.gguf", "UD-IQ4_NL_XL"),
    (f"{SECOND_PREFIX}-UD-IQ4_XS.gguf", "UD-IQ4_XS"),
    (f"{SECOND_PREFIX}-UD-Q2_K_XL.gguf", "UD-Q2_K_XL"),
    (f"{SECOND_PREFIX}-UD-Q3_K_M.gguf", "UD-Q3_K_M"),
    (f"{SECOND_PREFIX}-UD-Q3_K_XL.gguf", "UD-Q3_K_XL"),
    (f"{SECOND_PREFIX}-UD-Q4_K_M.gguf", "UD-Q4_K_M"),
    (f"{SECOND_PREFIX}-UD-Q4_K_S.gguf", "UD-Q4_K_S"),
    (f"{SECOND_PREFIX}-UD-Q4_K_XL.gguf", "UD-Q4_K_XL"),
    (f"{SECOND_PREFIX}-UD-Q5_K_M.gguf", "UD-Q5_K_M"),
    (f"{SECOND_PREFIX}-UD-Q5_K_S.gguf", "UD-Q5_K_S"),
    (f"{SECOND_PREFIX}-UD-Q5_K_XL.gguf", "UD-Q5_K_XL"),
    (f"{SECOND_PREFIX}-UD-Q6_K.gguf", "UD-Q6_K"),
    (f"{SECOND_PREFIX}-UD-Q6_K_XL.gguf", "UD-Q6_K_XL"),
    (f"{SECOND_PREFIX}-UD-Q8_K_XL.gguf", "UD-Q8_K_XL"),
]


def test_the_build_id_is_read_from_the_end_of_the_name() -> None:
    assert len(LIVE) == 24
    for name, expected in LIVE:
        assert gguf.build_id(name.removesuffix(".gguf")) == expected, name

    # Each repository's own files, as one listing: one build per file, each
    # under its own id.
    first = [model(name) for name, _id in LIVE[:3]]
    second = [model(name) for name, _id in LIVE[3:]]
    for listing, expected_ids in ((first, LIVE[:3]), (second, LIVE[3:])):
        found, left_out = gguf.builds(listing)
        assert sorted(ids(found)) == sorted(build for _name, build in expected_ids)
        assert left_out == []

    # Other separators, other capitals, the precisions of the Hub's table.
    assert gguf.build_id("name.Q2_K") == "Q2_K"
    assert gguf.build_id("name_q4_k_m") == "Q4_K_M"
    assert gguf.build_id("name-ud-iq1_s") == "UD-IQ1_S"
    assert gguf.build_id("name-TQ1_0") == "TQ1_0"
    assert [gguf.build_id(f"m-{p}") for p in ("BF16", "F16", "F32", "F64")] == [
        "BF16",
        "F16",
        "F32",
        "F64",
    ]
    # A projector's precision is read the same way.
    assert gguf.build_id(f"mmproj-{FIRST_PREFIX}-BF16") == "BF16"
    # No quantisation at the end: no id (the whole name is used instead).
    assert gguf.build_id("demo-model") is None
    assert gguf.build_id("Q4_K_M-demo") is None
    assert gguf.build_id("modelQ4_K_M") is None


def test_a_file_named_mmproj_is_the_picture_reader_not_a_model() -> None:
    listing = [
        model("demo-Q4_K_M.gguf"),
        model("mmproj-demo-BF16.gguf", GB),
        model("vision/MMPROJ-F16.GGUF", GB),
        model("model.safetensors"),
        text("README.md"),
    ]

    assert gguf.is_projector("mmproj-demo-BF16.gguf")
    assert gguf.is_projector("vision/MMPROJ-F16.GGUF")
    assert not gguf.is_projector("demo-Q4_K_M.gguf")
    # Not a GGUF file at all, whatever it is called.
    assert not gguf.is_projector("mmproj-notes.md")

    found, left_out = gguf.builds(listing)
    assert ids(found) == ["Q4_K_M"]
    assert left_out == []
    assert [file.path for file in gguf.projectors(listing)] == [
        "mmproj-demo-BF16.gguf",
        "vision/MMPROJ-F16.GGUF",
    ]


def test_each_quantisation_is_one_build_with_its_own_size() -> None:
    listing = [
        model("demo-Q8_0.gguf", 8 * GB),
        model("demo-Q4_K_M.gguf", 4 * GB),
        model("demo-Q6_K.gguf", 6 * GB),
        model("mmproj-demo-F16.gguf", GB),
    ]

    found, _left_out = gguf.builds(listing)

    # Smallest first, each the size of its own file and nothing else.
    assert [(build.id, build.bytes) for build in found] == [
        ("Q4_K_M", 4 * GB),
        ("Q6_K", 6 * GB),
        ("Q8_0", 8 * GB),
    ]
    assert all(len(build.parts) == 1 for build in found)


def test_split_parts_are_one_build_and_the_first_part_is_what_is_loaded() -> None:
    listing = [
        model("Q8_0/demo-Q8_0-00002-of-00003.gguf", 10 * GB),
        model("Q8_0/demo-Q8_0-00003-of-00003.gguf", 5 * GB),
        model("Q8_0/demo-Q8_0-00001-of-00003.gguf", 10 * GB),
        model("demo-Q4_K_M.gguf", 9 * GB),
    ]

    found, left_out = gguf.builds(listing)

    assert left_out == []
    assert ids(found) == ["Q4_K_M", "Q8_0"]
    split = found[1]
    assert split.bytes == 25 * GB
    assert [file.path for file in split.parts] == [
        "Q8_0/demo-Q8_0-00001-of-00003.gguf",
        "Q8_0/demo-Q8_0-00002-of-00003.gguf",
        "Q8_0/demo-Q8_0-00003-of-00003.gguf",
    ]
    assert gguf.roles(split.parts) == ("Q8_0/demo-Q8_0-00001-of-00003.gguf", None)


def test_a_split_build_with_a_part_missing_is_left_out() -> None:
    missing = [
        model("demo-Q8_0-00001-of-00003.gguf"),
        model("demo-Q8_0-00003-of-00003.gguf"),
        model("demo-Q4_K_M.gguf"),
    ]

    found, left_out = gguf.builds(missing)

    assert ids(found) == ["Q4_K_M"]
    assert left_out == [
        ("demo-Q8_0-00001-of-00003.gguf", "part 2 of 3 is missing"),
        ("demo-Q8_0-00003-of-00003.gguf", "part 2 of 3 is missing"),
    ]

    # Parts that disagree about how many there are, or a part past the end,
    # are not one model either.
    for uneven in (
        [model("demo-F16-00001-of-00002.gguf"), model("demo-F16-00002-of-00003.gguf")],
        [model("demo-F16-00001-of-00001.gguf"), model("demo-F16-00002-of-00001.gguf")],
        [model("demo-F16-00000-of-00001.gguf"), model("demo-F16-00001-of-00001.gguf")],
    ):
        found, left_out = gguf.builds(uneven)
        assert found == []
        assert {why for _path, why in left_out} == {gguf.UNEVEN_PARTS}
        assert len(left_out) == 2


def test_the_projector_named_for_a_build_is_preferred_then_f16() -> None:
    # The vision fixture: two projectors of one size, so only the rule decides.
    plan = plan_of("demo-vision-gguf")
    found, _left_out = gguf.builds(plan.weights)
    pictures = gguf.projectors(plan.weights)
    chosen = {build.id: gguf.projector_for(build, pictures) for build in found}

    # F16 is named in mmproj-…-F16 (and not in …-BF16); the others take F16
    # by precision, before BF16.
    assert {build: file.path for build, file in chosen.items() if file} == {
        "Q4_K_M": "mmproj-demo-vision-F16.gguf",
        "Q8_0": "mmproj-demo-vision-F16.gguf",
        "F16": "mmproj-demo-vision-F16.gguf",
    }

    # A projector named for the build wins over every precision.
    q4 = gguf.Build("Q4_K_M", (model("demo-Q4_K_M.gguf"),), 4 * GB)
    named = [model("mmproj-F16.gguf"), model("mmproj-demo-Q4_K_M.gguf")]
    assert gguf.projector_for(q4, named) == named[1]
    # "Q4_K" is not named in "Q4_K_M", nor "F16" in "BF16".
    q4k = gguf.Build("Q4_K", (model("demo-Q4_K.gguf"),), 4 * GB)
    assert gguf.projector_for(q4k, [model("mmproj-Q4_K_M.gguf"), model("mmproj-BF16.gguf")]) == (
        model("mmproj-BF16.gguf")
    )
    # Then the precisions in order, then the first by name.
    order = [model("mmproj-F32.gguf"), model("mmproj-Q8_0.gguf"), model("mmproj-BF16.gguf")]
    assert gguf.projector_for(q4k, order) == model("mmproj-BF16.gguf")
    assert gguf.projector_for(q4k, order[:2]) == model("mmproj-Q8_0.gguf")
    odd = [model("mmproj-z.gguf"), model("mmproj-a.gguf")]
    assert gguf.projector_for(q4k, odd) == model("mmproj-a.gguf")
    assert gguf.projector_for(q4k, []) is None


def test_a_builds_files_are_its_weights_its_projector_and_the_licence_texts_only() -> None:
    plan = files.plan_files(
        [
            model("demo-Q4_K_M.gguf", 4 * GB),
            model("demo-Q8_0.gguf", 8 * GB),
            model("mmproj-demo-F16.gguf", GB),
            text("README.md", 2000),
            text("LICENSE", 3000),
            text("config.json", 900),
            text("tokenizer.json", 9000),
            text("docs/README.md", 100),
            model("model.safetensors", 3 * GB),
        ]
    )

    variants = gguf.variants(plan)

    assert [variant.id for variant in variants] == ["Q4_K_M", "Q8_0"]
    small = variants[0]
    # No settings or tokenizer file (llama.cpp reads none), no other build,
    # no safetensors, and only the texts at the top of the repository.
    assert [file.path for file in small.files] == [
        "demo-Q4_K_M.gguf",
        "mmproj-demo-F16.gguf",
        "README.md",
        "LICENSE",
    ]
    assert (small.quant, small.takes, small.note) == ("Q4_K_M", ("text", "image"), "")


def test_memory_and_size_are_the_chosen_builds_with_its_projector() -> None:
    # The design's own example (6.1): the first live repository, renamed.
    variants = gguf.variants(plan_of("demo-omni-30b-GGUF"))

    assert [variant.id for variant in variants] == ["Q4_K_M", "Q6_K", "Q8_0"]
    q4 = variants[0]
    # Model plus projector: what has to fit in memory.
    assert q4.weights_bytes == 24_515_129_536 + 1_589_506_176
    # The label is everything that would be downloaded, README included.
    assert sum(file.size for file in q4.files) == 26_104_638_036
    assert [variant.label for variant in variants] == [
        "Q4_K_M — 24.3 GB",
        "Q6_K — 32.7 GB",
        "Q8_0 — 32.8 GB",
    ]
    # The one projector serves every build.
    assert {variant.files[1].path for variant in variants} == {"mmproj-demo-omni-30b-BF16.gguf"}


def test_a_build_without_a_projector_takes_text_only() -> None:
    variants = gguf.variants(plan_of("demo-gated-gguf"))

    assert [(variant.id, variant.takes) for variant in variants] == [("Q4_K_M", ("text",))]
    assert [file.path for file in variants[0].files] == ["demo-gated-Q4_K_M.gguf", "README.md"]
    assert variants[0].weights_bytes == 2_684_354_560


def test_a_repository_of_only_a_projector_has_no_build() -> None:
    plan = plan_of("demo-projector-only")

    assert gguf.variants(plan) == ()
    assert [file.path for file in gguf.projectors(plan.weights)] == ["mmproj-demo-F16.gguf"]


def test_an_imatrix_file_is_not_a_gguf_file_and_is_never_downloaded() -> None:
    # Its suffix is ".gguf_file": a loosened suffix test would make it a build.
    for name, imatrix in (
        ("demo-vision-gguf", "imatrix.gguf_file"),
        ("demo-many-builds-GGUF", "imatrix_unsloth.gguf_file"),
    ):
        plan = plan_of(name)
        assert imatrix in dict(plan.left_out)
        assert all(
            imatrix not in {file.path for file in variant.files} for variant in gguf.variants(plan)
        )


def test_the_many_builds_fixture_reads_as_its_live_original() -> None:
    variants = gguf.variants(plan_of("demo-many-builds-GGUF"))

    assert len(variants) == 21
    assert {variant.id for variant in variants} == {build for _name, build in LIVE[3:]}
    # No build is named for a projector here: each takes F16 by precision.
    assert {variant.files[1].path for variant in variants} == {"mmproj-F16.gguf"}
    # Smallest first.
    sizes = [variant.weights_bytes for variant in variants]
    assert sizes == sorted(sizes)
    assert variants[0].id == "UD-IQ2_M"
    assert variants[-1].id == "UD-Q8_K_XL"


# --- how many bits, and the small-build note ------------------------------------------


def test_a_build_under_four_bits_says_so() -> None:
    assert {
        build: gguf.nominal_bits(build)
        for build in (
            "UD-IQ2_M",
            "Q2_K",
            "IQ3_XXS",
            "UD-Q3_K_XL",
            "TQ1_0",
            "Q4_K_M",
            "IQ4_NL",
            "MXFP4_MOE",
            "Q8_0",
            "F16",
            "BF16",
            "F32",
            "demo-model",
        )
    } == {
        "UD-IQ2_M": 2,
        "Q2_K": 2,
        "IQ3_XXS": 3,
        "UD-Q3_K_XL": 3,
        "TQ1_0": 1,
        "Q4_K_M": 4,
        "IQ4_NL": 4,
        "MXFP4_MOE": 4,
        "Q8_0": 8,
        "F16": 16,
        "BF16": 16,
        "F32": 32,
        "demo-model": None,
    }

    variants = {variant.id: variant for variant in gguf.variants(plan_of("demo-many-builds-GGUF"))}
    small = {build for build, variant in variants.items() if variant.note}
    assert small == {
        "UD-IQ2_M",
        "UD-IQ2_XXS",
        "UD-IQ3_S",
        "UD-IQ3_XXS",
        "UD-Q2_K_XL",
        "UD-Q3_K_M",
        "UD-Q3_K_XL",
    }
    assert {variant.note for variant in variants.values() if variant.note} == {
        "A very small build: it answers noticeably worse than a 4-bit one."
    }
    # A name with no quantisation in it says nothing about its bits, and has none.
    whole = gguf.variants(files.plan_files([model("demo-model.gguf")]))
    assert [(variant.id, variant.quant, variant.note) for variant in whole] == [
        ("demo-model", "", "")
    ]

    # Two 2-bit builds that go by their whole names are still 2-bit builds:
    # this is where builds are hardest to tell apart, so the note must stay.
    clash = files.plan_files([model("text-only-Q2_K.gguf"), model("with-vision-Q2_K.gguf")])
    assert [(variant.id, variant.quant, variant.note) for variant in gguf.variants(clash)] == [
        ("text-only-Q2_K", "Q2_K", gguf.SMALL_BUILD),
        ("with-vision-Q2_K", "Q2_K", gguf.SMALL_BUILD),
    ]
    assert gguf.nominal_bits("with-vision-Q2_K") == 2
    assert gguf.nominal_bits("text-only-UD-IQ3_XXS") == 3
    # A split build's quantisation is read past its part number.
    split = files.plan_files(
        [model("a-Q3_K_M-00001-of-00002.gguf"), model("a-Q3_K_M-00002-of-00002.gguf")]
    )
    (variant,) = gguf.variants(split)
    assert (variant.quant, variant.note) == ("Q3_K_M", gguf.SMALL_BUILD)


# --- names that could not be a folder, or that two builds would share -----------------


def test_a_build_id_that_could_not_be_a_folder_name_is_left_out() -> None:
    long_name = "x" * 101 + ".gguf"
    listing = [
        model("demo-Q4_K_M.gguf"),
        model("-starts-with-a-dash.gguf"),
        model("has space.gguf"),
        model(long_name),
    ]

    found, left_out = gguf.builds(listing)

    assert ids(found) == ["Q4_K_M"]
    assert left_out == [
        ("-starts-with-a-dash.gguf", gguf.NOT_A_FOLDER_NAME),
        ("has space.gguf", gguf.NOT_A_FOLDER_NAME),
        (long_name, gguf.NOT_A_FOLDER_NAME),
    ]
    # Every id that is kept is a name the model's folder can carry.
    for name, _id in LIVE:
        (build,), _ = gguf.builds([model(name)])
        assert gguf.BUILD_ID.fullmatch(build.id)


def test_two_builds_with_the_same_quantisation_go_by_their_whole_names() -> None:
    listing = [model("text-only-Q4_K_M.gguf"), model("with-vision-Q4_K_M.gguf", 5 * GB)]

    found, left_out = gguf.builds(listing)

    assert ids(found) == ["text-only-Q4_K_M", "with-vision-Q4_K_M"]
    assert left_out == []

    # The same whole name in two folders: neither is kept (the Mac's disk
    # could not keep two folders of that name apart), whichever is listed first.
    clash = [model("a/demo-Q4_K_M.gguf"), model("b/demo-Q4_K_M.gguf"), model("demo-Q8_0.gguf")]
    for listing in (clash, clash[::-1]):
        found, left_out = gguf.builds(listing)
        assert ids(found) == ["Q8_0"]
        assert sorted(left_out) == [
            ("a/demo-Q4_K_M.gguf", gguf.SAME_NAME),
            ("b/demo-Q4_K_M.gguf", gguf.SAME_NAME),
        ]
    # Names that differ only in capitals are the same folder on a Mac.
    found, left_out = gguf.builds([model("demo.gguf"), model("sub/DEMO.gguf")])
    assert found == []
    assert len(left_out) == 2


def test_two_files_that_claim_the_same_place_are_both_shown_whichever_is_listed_first() -> None:
    # `demo.gguf` and `demo.GGUF` share a stem in one folder: neither may
    # silently replace the other, and neither may vanish from the check.
    same = [model("demo.gguf", 1), model("demo.GGUF", 2), model("demo-Q8_0.gguf")]
    for listing in (same, same[::-1]):
        found, left_out = gguf.builds(listing)
        assert ids(found) == ["Q8_0"]
        assert sorted(left_out) == [("demo.GGUF", gguf.SAME_NAME), ("demo.gguf", gguf.SAME_NAME)]

    # Two first parts of one split set: every file is shown, none dropped.
    parts = [
        model("x-00001-of-00002.gguf"),
        model("x-00001-of-00003.gguf"),
        model("x-00002-of-00002.gguf"),
    ]
    for listing in (parts, parts[::-1]):
        found, left_out = gguf.builds(listing)
        assert found == []
        assert sorted(left_out) == sorted((file.path, gguf.UNEVEN_PARTS) for file in parts)


# --- the build suggested -------------------------------------------------------------


def sized(*builds: tuple[str, int, Fit]) -> list[tuple[Variant, Fit]]:
    return [
        (Variant(id=build, label=build, files=(), weights_bytes=size, quant=build), fits)
        for build, size, fits in builds
    ]


def test_q4_k_m_is_suggested_when_it_fits() -> None:
    every_size = sized(("Q2_K", 2, "day"), ("Q4_K_M", 4, "day"), ("Q6_K", 6, "day"))
    assert gguf.prefer(every_size) == "Q4_K_M"
    # Even when it fits only overnight and something smaller fits by day.
    overnight = sized(("Q2_K", 2, "day"), ("Q4_K_M", 4, "night"), ("F16", 9, "never"))
    assert gguf.prefer(overnight) == "Q4_K_M"
    # A build whose id ends in Q4_K_M counts; by day before overnight.
    two = sized(("Q4_K_M", 4, "night"), ("UD-Q4_K_M", 3, "day"), ("Q8_0", 8, "never"))
    assert gguf.prefer(two) == "UD-Q4_K_M"


def test_the_largest_build_that_fits_is_suggested_when_q4_k_m_does_not() -> None:
    too_big = sized(
        ("Q2_K", 2, "day"), ("Q3_K_M", 3, "day"), ("Q4_K_M", 9, "never"), ("Q8_0", 8, "night")
    )
    assert gguf.prefer(too_big) == "Q3_K_M"
    # Nothing fits by day: the largest that fits overnight.
    by_night = sized(("Q5_K_M", 5, "night"), ("Q6_K", 6, "night"), ("F16", 9, "never"))
    assert gguf.prefer(by_night) == "Q6_K"
    # No Q4_K_M at all.
    assert gguf.prefer(sized(("IQ4_XS", 4, "day"), ("Q8_0", 8, "day"))) == "Q8_0"


def test_the_smallest_build_is_suggested_when_nothing_fits() -> None:
    nothing = sized(("Q8_0", 8, "never"), ("Q4_K_M", 4, "never"), ("UD-IQ2_M", 2, "never"))
    assert gguf.prefer(nothing) == "UD-IQ2_M"
    with pytest.raises(ValueError):
        gguf.prefer([])


# --- what the program is given -------------------------------------------------------------


def test_the_program_is_given_the_model_file_and_its_projector() -> None:
    variant = gguf.variants(plan_of("demo-omni-30b-GGUF"))[0]

    assert gguf.roles(variant.files) == (
        "demo-omni-30b-Q4_K_M.gguf",
        "mmproj-demo-omni-30b-BF16.gguf",
    )
    assert gguf.roles([model("demo-Q4_K_M.gguf"), text("README.md")]) == ("demo-Q4_K_M.gguf", None)

    with pytest.raises(ValueError, match="no GGUF model file"):
        gguf.roles([model("mmproj-F16.gguf"), text("README.md")])
    with pytest.raises(ValueError, match="more than one"):
        gguf.roles([model("demo-Q4_K_M.gguf"), model("demo-Q8_0.gguf")])


def test_a_file_that_is_not_gguf_is_recognised_by_its_first_bytes(tmp_path: Path) -> None:
    real = tmp_path / "demo-Q4_K_M.gguf"
    real.write_bytes(b"GGUF\x03\x00\x00\x00" + bytes(64))
    page = tmp_path / "error.gguf"
    page.write_bytes(b"<html>Access denied</html>")
    short = tmp_path / "short.gguf"
    short.write_bytes(b"GG")
    lower = tmp_path / "lower.gguf"
    lower.write_bytes(b"gguf" + bytes(8))

    assert gguf.looks_like_gguf(real)
    assert not gguf.looks_like_gguf(page)
    assert not gguf.looks_like_gguf(short)
    assert not gguf.looks_like_gguf(lower)


# --- what the module stands on ----------------------------------------------------------


def test_gguf_reads_nothing_but_the_standard_library_and_its_own_package() -> None:
    tree = ast.parse(Path(gguf.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "framefound.local_models":
                imported |= {f"{node.module}.{alias.name}" for alias in node.names}
            else:
                imported.add(node.module)

    ours = {name for name in imported if name.startswith("framefound")}
    assert ours == {
        "framefound.local_models.kinds",
        "framefound.local_models.files",
        "framefound.local_models.hub",
    }
    others = {name.split(".")[0] for name in imported - ours}
    assert others <= set(sys.stdlib_module_names) | {"__future__"}
