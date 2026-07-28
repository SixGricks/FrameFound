from framefound.media.detect import extension_of, media_type_for


def test_common_types() -> None:
    assert media_type_for("IMG_0001.JPG") == "image"
    assert media_type_for("drone/DJI_0042.MP4") == "video"
    assert media_type_for("sermon.wav") == "audio"
    assert media_type_for("raw/shoot.CR3") == "image"
    assert media_type_for("broadcast.mxf") == "video"


def test_unsupported_returns_none() -> None:
    assert media_type_for("notes.txt") is None
    assert media_type_for("project.prproj") is None
    assert media_type_for("no_extension") is None


def test_extension_of_is_lowercased() -> None:
    assert extension_of("A.JPeG") == "jpeg"
    assert extension_of("archive.tar.gz") == "gz"
