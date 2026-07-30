"""Telling the operator their file is broken, rather than that we failed.

Found in deployment: 21 assets in one library were interrupted recordings — a
drone battery dying mid-flight, a card pulled before the container was
finalised. FrameFound was behaving correctly and reporting "The file could not
be processed", which is true and useless. The operator needed to know the file
itself is damaged, because it will not open in Premiere either and a repair
tool might get the footage back.
"""

import pytest

from framefound.processing.ffmpeg import damage_verdict


def test_a_missing_moov_atom_is_named_as_an_interrupted_recording() -> None:
    """The DJI case: 19 drone MP4s in one library, all cut off mid-write."""
    verdict = damage_verdict(
        "[mov,mp4,m4a,3gp,3g2,mj2 @ 0x5f8] moov atom not found\n"
        "DJI_0166.MP4: Invalid data found when processing input"
    )
    assert verdict is not None
    assert "interrupted" in verdict
    assert "editor" in verdict, "the operator should know Premiere will fail too"
    assert "epair" in verdict, "and that recovery may be possible"


def test_invalid_data_alone_is_reported_as_damage() -> None:
    verdict = damage_verdict("clip.mov: Invalid data found when processing input")
    assert verdict is not None
    assert "damaged" in verdict


def test_missing_codec_parameters_counts_as_damage() -> None:
    assert damage_verdict("Could not find codec parameters for stream 0") is not None


def test_the_match_is_case_insensitive() -> None:
    # ffmpeg's capitalisation varies between builds and versions.
    assert damage_verdict("MOOV ATOM NOT FOUND") is not None


@pytest.mark.parametrize(
    "stderr",
    [
        "",
        "Conversion failed!",
        "Error while opening encoder for output stream #0:0",
        "No space left on device",
        "Permission denied",
    ],
)
def test_ordinary_failures_are_not_blamed_on_the_file(stderr: str) -> None:
    """A full disk or a bad encoder setting is our problem, and telling the
    operator their footage is damaged would send them chasing a ghost."""
    assert damage_verdict(stderr) is None


def test_a_damaged_verdict_reads_as_a_sentence() -> None:
    # It goes straight into the UI, so it has to be readable by someone who
    # has never heard of a moov atom.
    verdict = damage_verdict("moov atom not found")
    assert verdict is not None
    assert verdict[0].isupper() and verdict.rstrip().endswith(".")
    assert "moov" not in verdict, "jargon belongs in the log, not the interface"
