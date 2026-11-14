"""Getting llama.cpp (local_models/llama_release.py): which archive, its checksum, unpacking it.

Nothing here reaches GitHub or starts a program. GitHub is an
`httpx.MockTransport` answering with release listings written from what
GitHub returned on 2026-11-12 (L6 design G4), and every archive is built in
the test with `tarfile`. Archives that are extracted hold only files and
folders: making a link on Windows needs a privilege, so the link refusals
are tested on the listing, which is where they happen (L6 P19). A fixture
makes starting a process or opening a socket fail the test.
"""

import ast
import hashlib
import io
import json
import socket
import stat
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from framefound.local_models import fake, kinds, llama_release
from framefound.local_models.llama_release import API, Release, ReleaseProblem

NUMBERED = "0.9.0"
DAILY = "b12110"
NEWER_DAILY = "b12318"
DOWNLOADS = "https://github.com/ggml-org/llama.cpp/releases/download"
ASSET_HOST = "https://release-assets.githubusercontent.com/github-production-release-asset"


@pytest.fixture(autouse=True)
def _nothing_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test may start a program or open a connection; trying to is a failure."""

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a test tried to start a process or open a connection")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


# --- archives built here ---------------------------------------------------------


def member(name: str, data: bytes = b"", **fields: Any) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = 0o755 if name.endswith("llama-server") else 0o644
    for key, value in fields.items():
        setattr(info, key, value)
    return info, data


def folder(name: str) -> tuple[tarfile.TarInfo, bytes]:
    return member(name, type=tarfile.DIRTYPE, mode=0o755)


def link(name: str, target: str, *, hard: bool = False) -> tuple[tarfile.TarInfo, bytes]:
    return member(name, type=tarfile.LNKTYPE if hard else tarfile.SYMTYPE, linkname=target)


def tar_gz(members: Iterable[tuple[tarfile.TarInfo, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if info.isreg() else None)
    return buffer.getvalue()


def the_mac_build(tag: str = DAILY) -> bytes:
    """What the Mac archive is believed to hold: a folder with the program and its libraries."""
    top = f"llama-{tag}"
    return tar_gz(
        [
            folder(top),
            member(f"{top}/llama-server", b"#!/bin/sh\necho version\n"),
            member(f"{top}/llama-cli", b"another program\n"),
            member(f"{top}/libllama.dylib", b"\xcf\xfa\xed\xfe library bytes"),
            member(f"{top}/LICENSE", b"MIT\n"),
        ]
    )


def written(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def snapshot(root: Path) -> dict[str, bytes | None]:
    """Every file and folder under `root`: what "left as it was" is compared against."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes() if path.is_file() else None
        for path in sorted(root.rglob("*"))
    }


# --- GitHub, as a transport --------------------------------------------------------


def asset(name: str, url: str, data: bytes, **override: Any) -> dict[str, Any]:
    """One release asset with the keys GitHub gave on 2026-11-12."""
    body: dict[str, Any] = {
        "url": f"{API}/releases/assets/1",
        "id": 1,
        "name": name,
        "label": "",
        "content_type": "application/octet-stream",
        "state": "uploaded",
        "size": len(data),
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
        "download_count": 3,
        "browser_download_url": url,
    }
    body.update(override)
    return body


def mac_name(tag: str) -> str:
    return f"llama-{tag}-bin-macos-arm64.tar.gz"


def daily_release(tag: str, archive: bytes, **override: Any) -> dict[str, Any]:
    name = mac_name(tag)
    other = f"llama-{tag}-bin-macos-x64.tar.gz"
    return {
        "tag_name": tag,
        "prerelease": True,
        "immutable": False,
        "assets": [
            asset(other, f"{DOWNLOADS}/{tag}/{other}", b"intel"),
            asset(name, f"{DOWNLOADS}/{tag}/{name}", archive, **override),
        ],
    }


def numbered_release(version: str = NUMBERED, *, pointer: bool = True) -> dict[str, Any]:
    assets = []
    if pointer:
        assets.append(
            asset("nightly-tag.txt", f"{DOWNLOADS}/v{version}/nightly-tag.txt", b"b12110\n")
        )
    return {"tag_name": f"v{version}", "prerelease": False, "assets": assets}


class GitHub:
    """GitHub's API and download hosts, as an `httpx.MockTransport`; records every address asked."""

    def __init__(
        self,
        releases: Mapping[str, dict[str, Any]],
        files: Mapping[str, bytes] | None = None,
        *,
        answers: Mapping[str, Callable[[httpx.Request], httpx.Response]] | None = None,
    ) -> None:
        self.releases = dict(releases)
        self.files = dict(files or {})
        self.answers = dict(answers or {})
        self.asked: list[str] = []
        self.transport = httpx.MockTransport(self._answer)

    def _answer(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.asked.append(url)
        if url in self.answers:
            return self.answers[url](request)
        if url.startswith(f"{API}/releases/tags/"):
            tag = url.rsplit("/", 1)[1]
            if tag in self.releases:
                return httpx.Response(200, json=self.releases[tag])
            return httpx.Response(404, json={"message": "Not Found"})
        if url.startswith(f"{DOWNLOADS}/"):
            # A download page sends the bytes on to the asset host, as GitHub does.
            name = url.rsplit("/", 1)[1]
            return httpx.Response(302, headers={"location": f"{ASSET_HOST}/{name}?sig=x"})
        if url.startswith(f"{ASSET_HOST}/"):
            name = url.split("?")[0].rsplit("/", 1)[1]
            if name in self.files:
                return httpx.Response(200, content=self.files[name])
        return httpx.Response(404)


def github(tag: str = DAILY, archive: bytes | None = None, **override: Any) -> GitHub:
    """GitHub as it was: 0.9.0 points at b12110, whose Mac archive is `archive`."""
    archive = the_mac_build(tag) if archive is None else archive
    return GitHub(
        {f"v{NUMBERED}": numbered_release(), tag: daily_release(tag, archive, **override)},
        {mac_name(tag): archive, "nightly-tag.txt": f"{DAILY}\n".encode()},
    )


def lines() -> tuple[list[str], Callable[[str], None]]:
    said: list[str] = []
    return said, said.append


# --- the first test: what Python's own filter does on this Python --------------------


def test_pythons_data_filter_refuses_a_member_that_climbs_out_on_this_python(
    tmp_path: Path,
) -> None:
    # The unpacking rests on `filter="data"` as its second net (L6 G12): if
    # this Python did not refuse, every other refusal here would be the only one.
    archive = written(tmp_path / "climb.tar.gz", tar_gz([member("../climbed.txt", b"out")]))
    into = tmp_path / "into"
    into.mkdir()
    with tarfile.open(archive, "r:gz") as tar, pytest.raises(tarfile.OutsideDestinationError):
        tar.extractall(into, filter="data")
    assert not (tmp_path / "climbed.txt").exists()
    assert list(into.iterdir()) == []


# --- which archive a version means -----------------------------------------------


def test_a_numbered_version_is_followed_to_the_daily_build_it_names() -> None:
    archive = the_mac_build()
    listings = {f"v{NUMBERED}": numbered_release(), DAILY: daily_release(DAILY, archive)}
    asked: list[str] = []

    def get_json(url: str) -> Mapping[str, Any]:
        asked.append(url)
        return listings[url.rsplit("/", 1)[1]]

    def get_text(url: str) -> str:
        asked.append(url)
        return f"  {DAILY}\n"

    release = llama_release.resolve("v0.9.0", get_json=get_json, get_text=get_text)

    assert asked == [
        f"{API}/releases/tags/v0.9.0",
        f"{DOWNLOADS}/v0.9.0/nightly-tag.txt",
        f"{API}/releases/tags/{DAILY}",
    ]
    assert release == Release(
        version=NUMBERED,
        tag=DAILY,
        asset=mac_name(DAILY),
        url=f"{DOWNLOADS}/{DAILY}/{mac_name(DAILY)}",
        sha256=hashlib.sha256(archive).hexdigest(),
        size=len(archive),
    )


def test_a_daily_build_is_fetched_by_its_own_number(tmp_path: Path) -> None:
    hub = github(NEWER_DAILY)

    release = llama_release.fetch(
        NEWER_DAILY, tmp_path, transport=hub.transport, say=lambda _line: None
    )

    assert (release.version, release.tag) == (NEWER_DAILY, NEWER_DAILY)
    # No numbered release and no pointer was asked about.
    assert hub.asked == [
        f"{API}/releases/tags/{NEWER_DAILY}",
        f"{DOWNLOADS}/{NEWER_DAILY}/{mac_name(NEWER_DAILY)}",
        f"{ASSET_HOST}/{mac_name(NEWER_DAILY)}?sig=x",
    ]
    assert llama_release.record(tmp_path)["version"] == NEWER_DAILY  # type: ignore[index]


def test_a_numbered_version_with_no_pointer_has_no_mac_build(tmp_path: Path) -> None:
    hub = GitHub({f"v{NUMBERED}": numbered_release(pointer=False)})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(NUMBERED, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "llama.cpp 0.9.0 has no build for Apple-silicon Macs attached."


def test_a_version_with_no_mac_build_is_refused_by_name(tmp_path: Path) -> None:
    listing = daily_release(NEWER_DAILY, b"")
    listing["assets"] = [a for a in listing["assets"] if "arm64" not in a["name"]]
    hub = GitHub({NEWER_DAILY: listing})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(NEWER_DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "llama.cpp b12318 has no build for Apple-silicon Macs attached."
    assert snapshot(tmp_path) == {}


@pytest.mark.parametrize(
    "digest",
    [None, "", "md5:0cc175b9c0f1b6a831c399e269772661", "sha256:abc123", "sha256:" + "g" * 64, 7],
)
def test_a_build_with_no_listed_checksum_is_refused(tmp_path: Path, digest: Any) -> None:
    hub = github(digest=digest)

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "GitHub lists no checksum for this build, so it cannot be verified."
    # Refused before a byte of it was asked for.
    assert not any(ASSET_HOST in url for url in hub.asked)


def test_a_build_with_its_checksum_key_missing_is_refused_too() -> None:
    listing = daily_release(DAILY, the_mac_build())
    del listing["assets"][1]["digest"]

    with pytest.raises(ReleaseProblem, match="lists no checksum"):
        llama_release.resolve(DAILY, get_json=lambda _url: listing, get_text=lambda _url: "")


def test_a_build_far_larger_than_llama_cpp_is_not_downloaded() -> None:
    listing = daily_release(DAILY, b"x", size=llama_release.MAX_ARCHIVE_BYTES + 1)

    with pytest.raises(ReleaseProblem) as err:
        llama_release.resolve(DAILY, get_json=lambda _url: listing, get_text=lambda _url: "")

    assert str(err.value) == (
        "GitHub lists this build at 315 MB, far more than llama.cpp is, so it was not downloaded."
    )


@pytest.mark.parametrize(
    ("status", "sentence"),
    [
        (404, "llama.cpp has no version called 0.9.1."),
        (403, "GitHub is limiting requests from this address. Try again in an hour."),
        (429, "GitHub is limiting requests from this address. Try again in an hour."),
        (502, "GitHub did not answer (502). Try again later."),
    ],
)
def test_githubs_refusals_are_the_operators_sentences(
    tmp_path: Path, status: int, sentence: str
) -> None:
    hub = GitHub(
        {}, answers={f"{API}/releases/tags/v0.9.1": lambda _request: httpx.Response(status)}
    )

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch("0.9.1", tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == sentence


def test_github_out_of_reach_is_a_sentence_without_the_network_librarys_words(
    tmp_path: Path,
) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to 140.82.112.6", request=request)

    hub = GitHub({}, answers={f"{API}/releases/tags/{DAILY}": down})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == (
        "llama.cpp's download page could not be reached. "
        "Check this machine's internet connection and try again."
    )
    assert err.value.__context__ is None


@pytest.mark.parametrize(
    "listing",
    [
        {"message": "no assets key"},
        {"tag_name": "b99999", "assets": []},
        {"tag_name": DAILY, "assets": "not a list"},
    ],
)
def test_a_listing_that_cannot_be_read_is_refused(listing: dict[str, Any]) -> None:
    with pytest.raises(ReleaseProblem) as err:
        llama_release.resolve(DAILY, get_json=lambda _url: listing, get_text=lambda _url: "")

    assert str(err.value) == (
        "GitHub answered in a form FrameFound could not read, "
        "so llama.cpp b12110 was not installed."
    )


def test_a_pointer_that_names_no_daily_build_is_refused() -> None:
    def get_json(url: str) -> Mapping[str, Any]:
        return numbered_release()

    with pytest.raises(ReleaseProblem, match="could not read"):
        llama_release.resolve(NUMBERED, get_json=get_json, get_text=lambda _url: "--index-url=evil")


def test_a_pointer_to_a_daily_build_github_no_longer_has_says_so(tmp_path: Path) -> None:
    # GitHub has just listed 0.9.0, so "no version called 0.9.0" would be false.
    hub = GitHub({f"v{NUMBERED}": numbered_release()}, {"nightly-tag.txt": f"{DAILY}\n".encode()})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(NUMBERED, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "llama.cpp 0.9.0 names build b12110, which GitHub no longer has."
    assert hub.asked[-1] == f"{API}/releases/tags/{DAILY}"
    assert snapshot(tmp_path) == {}


# --- the download ---------------------------------------------------------------


def test_an_archive_that_does_not_match_its_checksum_is_thrown_away(tmp_path: Path) -> None:
    archive = the_mac_build()
    tampered = archive[:-1] + bytes([archive[-1] ^ 1])
    hub = github(archive=archive)
    hub.files[mac_name(DAILY)] = tampered
    before = snapshot(tmp_path)

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "The download did not match its checksum, so it was thrown away."
    # Nothing was unpacked and nothing of the download is left.
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("change", [b"", b"and some more"])
def test_an_archive_of_another_size_than_listed_is_thrown_away(
    tmp_path: Path, change: bytes
) -> None:
    archive = the_mac_build()
    hub = github(archive=archive)
    hub.files[mac_name(DAILY)] = archive[:-5] if not change else archive + change

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == (
        "The download was not the size GitHub lists for it, so it was thrown away."
    )
    assert snapshot(tmp_path) == {}


@pytest.mark.parametrize(
    "location",
    [
        "https://downloads.example.net/llama.tar.gz",
        "http://release-assets.githubusercontent.com/llama.tar.gz",
        "https://github.com.example.net/llama.tar.gz",
    ],
)
def test_a_redirect_to_a_host_that_is_not_githubs_is_not_followed(
    tmp_path: Path, location: str
) -> None:
    hub = github()
    page = f"{DOWNLOADS}/{DAILY}/{mac_name(DAILY)}"
    hub.answers[page] = lambda _request: httpx.Response(302, headers={"location": location})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert "which is not one of GitHub's addresses, so it was not followed." in str(err.value)
    assert location not in hub.asked
    assert snapshot(tmp_path) == {}


def test_a_listed_download_address_that_is_not_githubs_is_never_asked(tmp_path: Path) -> None:
    hub = github(browser_download_url="https://downloads.example.net/llama.tar.gz")

    with pytest.raises(ReleaseProblem, match="downloads.example.net, which is not one of"):
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert not any("example.net" in url for url in hub.asked)


def test_a_download_sent_round_and_round_is_given_up(tmp_path: Path) -> None:
    hub = github()
    page = f"{DOWNLOADS}/{DAILY}/{mac_name(DAILY)}"
    hub.answers[page] = lambda _request: httpx.Response(302, headers={"location": page})

    with pytest.raises(ReleaseProblem) as err:
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert str(err.value) == "GitHub sent the download on too many times, so it was not followed."
    assert hub.asked.count(page) == llama_release.MAX_REDIRECTS + 1


def test_a_download_that_runs_past_its_listed_size_is_stopped_not_read_to_the_end(
    tmp_path: Path,
) -> None:
    pulled: list[int] = []

    def endless() -> Iterator[bytes]:
        for number in range(200):
            pulled.append(number)
            yield b"x" * llama_release.CHUNK_BYTES

    release = Release(DAILY, DAILY, mac_name(DAILY), f"{ASSET_HOST}/a", "0" * 64, size=1000)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=endless()))
    target = tmp_path / "download" / mac_name(DAILY)

    with httpx.Client(transport=transport) as client, pytest.raises(ReleaseProblem) as err:
        llama_release.download(release, target, client=client)

    assert str(err.value) == (
        "The download was not the size GitHub lists for it, so it was thrown away."
    )
    # Stopped at the first piece past the size, not after 200 MB on the disk.
    assert len(pulled) < 5
    assert list(target.parent.iterdir()) == []


def test_a_connection_that_breaks_part_way_leaves_no_partial_file(tmp_path: Path) -> None:
    def cut_off() -> Iterator[bytes]:
        yield b"x" * 1000
        raise httpx.ReadError("connection reset by 185.199.108.133")

    release = Release(DAILY, DAILY, mac_name(DAILY), f"{ASSET_HOST}/a", "0" * 64, size=5000)
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=cut_off()))
    target = tmp_path / "download" / mac_name(DAILY)

    with httpx.Client(transport=transport) as client, pytest.raises(ReleaseProblem) as err:
        llama_release.download(release, target, client=client)

    assert (
        str(err.value) == "The download stopped part-way: GitHub could not be reached. Try again."
    )
    assert list(target.parent.iterdir()) == []


def test_a_download_that_never_connects_says_github_could_not_be_reached(tmp_path: Path) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    release = Release(DAILY, DAILY, mac_name(DAILY), f"{ASSET_HOST}/a", "0" * 64, size=5000)
    target = tmp_path / "download" / mac_name(DAILY)

    with (
        httpx.Client(transport=httpx.MockTransport(down)) as client,
        pytest.raises(ReleaseProblem) as err,
    ):
        llama_release.download(release, target, client=client)

    # Not "stopped part-way": nothing had started.
    assert str(err.value) == llama_release.UNREACHABLE
    assert list(target.parent.iterdir()) == []


def test_a_download_that_does_not_match_leaves_no_partial_file(tmp_path: Path) -> None:
    data = b"not the archive GitHub listed"
    release = Release(DAILY, DAILY, mac_name(DAILY), f"{ASSET_HOST}/a", "0" * 64, len(data))
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, content=data))
    target = tmp_path / mac_name(DAILY)

    with (
        httpx.Client(transport=transport) as client,
        pytest.raises(ReleaseProblem, match="checksum"),
    ):
        llama_release.download(release, target, client=client)

    assert list(tmp_path.iterdir()) == []


# --- unpacking ------------------------------------------------------------------


def shown(members: list[tuple[tarfile.TarInfo, bytes]]) -> str:
    return ", ".join(
        f"{info.name}->{info.linkname}" if info.linkname else info.name for info, _data in members
    )


@pytest.mark.parametrize(
    "bad",
    [
        [member("../llama-server", b"x")],
        [member("bin/../../escaped", b"x")],
        [member("/usr/local/bin/llama-server", b"x")],
        [member("C:/Windows/llama-server", b"x")],
        [member("bin\\..\\..\\escaped", b"x")],
        [link("bin/lib", "../../outside")],
        [link("bin/passwd", "/etc/passwd")],
        [link("bin/hard", "../outside", hard=True)],
        [link("bin/hard-abs", "/etc/passwd", hard=True)],
        # A name that goes up and back down reads as inside, but lands where
        # it reads only while nothing on the way is a link.
        [member("bin/../escaped", b"x")],
        # Through a link the archive itself makes: "b/../escaped" reads as
        # "escaped", but with b -> . it is one folder above.
        [link("b", "."), member("b/../escaped", b"x")],
        [link("c", "."), link("c/z", "..")],
        [link("x", "."), link("x/y", "..")],
        # The same, the link written after the name that runs through it,
        # and a target that runs through a link: y -> x/.. is the folder
        # above once x -> . exists, though "x/.." reads as ".".
        [link("y", "x/.."), link("x", ".")],
        [member("d/file", b"x"), link("d", ".")],
    ],
    ids=shown,
)
def test_an_archive_that_climbs_out_of_its_folder_is_refused_by_its_listing_alone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad: list[tuple[tarfile.TarInfo, bytes]],
) -> None:
    # The module's own check must hold by itself: Python's filter is turned
    # off before anything is written, and extracting is not reached at all.
    monkeypatch.setattr(tarfile, "data_filter", lambda info, _dest: info)

    def never(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an archive that climbs out reached extraction")

    monkeypatch.setattr(tarfile.TarFile, "extractall", never)
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz([folder("bin"), member("bin/llama-server", b"program"), *bad]),
    )

    with pytest.raises(ReleaseProblem) as err:
        llama_release.unpack(archive, tmp_path / "dist")

    assert "which would be written outside its own folder, so it was thrown away." in str(err.value)
    # The refusal comes from the listing: nothing was written anywhere.
    assert snapshot(tmp_path) == {"a.tar.gz": archive.read_bytes()}


def test_pythons_filter_is_the_second_net_when_extracting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[Any] = []
    extract = tarfile.TarFile.extractall

    def spy(self: tarfile.TarFile, *args: Any, **kwargs: Any) -> None:
        asked.append(kwargs.get("filter"))
        extract(self, *args, **kwargs)

    monkeypatch.setattr(tarfile.TarFile, "extractall", spy)

    llama_release.unpack(written(tmp_path / "a.tar.gz", the_mac_build()), tmp_path / "dist")

    assert asked == ["data"]


def test_the_second_net_holds_by_itself_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The listing's own check switched off: Python's filter alone must stop
    # the climb, remove what was written, and the operator must be told the
    # archive climbs out, not that it cannot be read.
    def unchecked(tar: tarfile.TarFile, _into: Path) -> tuple[list[tarfile.TarInfo], str]:
        return tar.getmembers(), "llama-server"

    monkeypatch.setattr(llama_release, "_checked", unchecked)
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz([member("llama-server", b"program"), member("../climbed.txt", b"out")]),
    )

    with pytest.raises(ReleaseProblem) as err:
        llama_release.unpack(archive, tmp_path / "dist")

    assert str(err.value) == (
        "The download holds ../climbed.txt, which would be written outside its own folder, "
        "so it was thrown away."
    )
    assert snapshot(tmp_path) == {"a.tar.gz": archive.read_bytes()}


@pytest.mark.skipif(sys.platform == "win32", reason="Windows keeps no such mode bits")
def test_what_pythons_filter_takes_off_a_file_is_taken_off(tmp_path: Path) -> None:
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz([member("llama-server", b"program", mode=0o6777)]),
    )

    llama_release.unpack(archive, tmp_path / "dist")

    mode = (tmp_path / "dist" / "llama-server").stat().st_mode
    assert mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert mode & stat.S_IXUSR


def test_a_hard_link_to_a_file_the_archive_does_not_hold_is_refused(tmp_path: Path) -> None:
    for missing in (
        [link("lib/hard", "lib/nothere", hard=True)],
        # Named before the file it links to: `tarfile` could not make it either.
        [link("lib/early", "lib/real", hard=True), member("lib/real", b"x")],
        [folder("lib"), link("lib/to-a-folder", "lib", hard=True)],
    ):
        archive = written(
            tmp_path / "a.tar.gz", tar_gz([member("llama-server", b"program"), *missing])
        )

        with pytest.raises(ReleaseProblem) as err:
            llama_release.unpack(archive, tmp_path / "dist")

        assert str(err.value).endswith(
            ", a link to a file it does not hold, so it was thrown away."
        )
        assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize("kind", [tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE])
def test_an_archive_holding_a_device_or_a_pipe_is_refused(tmp_path: Path, kind: bytes) -> None:
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz([member("llama-server", b"program"), member("dev", type=kind)]),
    )

    with pytest.raises(ReleaseProblem) as err:
        llama_release.unpack(archive, tmp_path / "dist")

    assert str(err.value) == (
        "The download holds dev, which is not an ordinary file or folder, so it was thrown away."
    )
    assert not (tmp_path / "dist").exists()


@pytest.mark.parametrize(
    ("members", "sentence"),
    [
        (
            [member("bin/llama-cli", b"x"), folder("bin/llama-server")],
            "The download has no llama-server program in it, so it was thrown away.",
        ),
        (
            [member("bin/llama-cli", b"x"), link("bin/llama-server", "llama-cli")],
            "The download has no llama-server program in it, so it was thrown away.",
        ),
        (
            [member("a/llama-server", b"x"), member("b/llama-server", b"y")],
            "The download has more than one llama-server program in it, so it was thrown away.",
        ),
        (
            [member("llama-server", b"x"), member("./llama-server", b"y")],
            "The download holds llama-server twice, so it was thrown away.",
        ),
    ],
)
def test_an_archive_without_the_program_is_refused(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes]], sentence: str
) -> None:
    archive = written(tmp_path / "a.tar.gz", tar_gz(members))

    with pytest.raises(ReleaseProblem) as err:
        llama_release.unpack(archive, tmp_path / "dist")

    assert str(err.value) == sentence
    assert not (tmp_path / "dist").exists()


def test_an_archive_that_would_unpack_too_large_or_too_many_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz([member("llama-server", b"x" * 600), member("lib.dylib", b"y" * 600)]),
    )
    monkeypatch.setattr(llama_release, "MAX_UNPACKED_BYTES", 1000)
    with pytest.raises(ReleaseProblem, match="would unpack to more than"):
        llama_release.unpack(archive, tmp_path / "dist")

    monkeypatch.setattr(llama_release, "MAX_UNPACKED_BYTES", 10_000)
    monkeypatch.setattr(llama_release, "MAX_MEMBERS", 1)
    with pytest.raises(ReleaseProblem) as err:
        llama_release.unpack(archive, tmp_path / "dist")
    assert str(err.value) == "The download holds more than 1 files, so it was thrown away."
    assert not (tmp_path / "dist").exists()


def test_a_download_that_is_not_an_archive_is_refused(tmp_path: Path) -> None:
    for name, data in [("html", b"<html>Rate limited</html>"), ("cut", the_mac_build()[:40])]:
        archive = written(tmp_path / f"{name}.tar.gz", data)

        with pytest.raises(ReleaseProblem) as err:
            llama_release.unpack(archive, tmp_path / "dist")

        assert str(err.value) == (
            "The download is not an archive FrameFound can read, so it was thrown away."
        )
        assert not (tmp_path / "dist").exists()


def test_links_that_stay_inside_and_a_leading_dot_folder_pass_the_listing(tmp_path: Path) -> None:
    # Read, not extracted: making links needs a privilege on Windows (L6 P19).
    archive = written(
        tmp_path / "a.tar.gz",
        tar_gz(
            [
                folder("."),
                folder("./build/bin"),
                member("./build/bin/llama-server", b"program"),
                member("./build/lib/libllama.0.dylib", b"lib"),
                link("./build/lib/libllama.dylib", "libllama.0.dylib"),
                link("./build/bin/libllama.dylib", "../lib/libllama.0.dylib"),
                # A link to a link: the second is judged by its own check.
                link("./build/lib/libllama.1.dylib", "libllama.dylib"),
                link("./build/bin/server", "build/bin/llama-server", hard=True),
            ]
        ),
    )

    with tarfile.open(archive, "r:gz") as tar:
        members, program = llama_release._checked(tar, tmp_path / "dist")

    assert program == "build/bin/llama-server"
    assert len(members) == 8


def test_the_program_is_unpacked_runnable_and_its_path_is_given(tmp_path: Path) -> None:
    archive = written(tmp_path / "a.tar.gz", the_mac_build())

    program = llama_release.unpack(archive, tmp_path / "dist")

    assert program == f"llama-{DAILY}/llama-server"
    unpacked = tmp_path / "dist" / f"llama-{DAILY}"
    assert sorted(path.name for path in unpacked.iterdir()) == [
        "LICENSE",
        "libllama.dylib",
        "llama-cli",
        "llama-server",
    ]
    assert (unpacked / "llama-server").read_bytes() == b"#!/bin/sh\necho version\n"
    if sys.platform != "win32":
        assert (unpacked / "llama-server").stat().st_mode & 0o100


# --- the whole fetch, and its record ----------------------------------------------


def test_what_was_installed_is_recorded_with_its_checksum(tmp_path: Path) -> None:
    archive = the_mac_build()
    hub = github(archive=archive)
    said, say = lines()

    release = llama_release.fetch("v0.9.0", tmp_path, transport=hub.transport, say=say)

    sha256 = hashlib.sha256(archive).hexdigest()
    body = json.loads((tmp_path / "llama.json").read_text(encoding="utf-8"))
    assert body.pop("fetched_at").endswith("Z")
    assert body == {
        "version": NUMBERED,
        "tag": DAILY,
        "asset": mac_name(DAILY),
        "sha256": sha256,
        "size": len(archive),
        "server": f"llama-{DAILY}/llama-server",
    }
    assert release.sha256 == sha256
    # The archive itself is not kept; the program is where the record says.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["dist", "llama.json"]
    assert llama_release.program(tmp_path) == tmp_path / "dist" / f"llama-{DAILY}" / "llama-server"
    assert llama_release.about(tmp_path) == "build b12110"
    assert said == [
        "Asking GitHub about 0.9.0…",
        "Downloading llama-b12110-bin-macos-arm64.tar.gz (1 MB)…",
        "Unpacking…",
    ]


def test_a_fetch_whose_archive_is_refused_leaves_its_folder_as_it_was(tmp_path: Path) -> None:
    hub = github(archive=tar_gz([member("llama-cli", b"no server here")]))

    with pytest.raises(ReleaseProblem, match="no llama-server program"):
        llama_release.fetch(DAILY, tmp_path, transport=hub.transport, say=lambda _line: None)

    assert snapshot(tmp_path) == {}


def test_a_record_that_cannot_be_written_leaves_nothing_unpacked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full_disk(_path: Path, _body: dict[str, Any]) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(llama_release, "_write_json", full_disk)

    with pytest.raises(OSError, match="No space left"):
        llama_release.fetch(DAILY, tmp_path, transport=github().transport, say=lambda _line: None)

    assert snapshot(tmp_path) == {}


def test_what_a_stopped_fetch_left_behind_is_cleared_first(tmp_path: Path) -> None:
    (tmp_path / "download").mkdir()
    written(tmp_path / "download" / f"{mac_name(DAILY)}.partial", b"half")
    (tmp_path / "dist" / "old").mkdir(parents=True)
    written(tmp_path / "llama.json", b"{}")

    llama_release.fetch(DAILY, tmp_path, transport=github().transport, say=lambda _line: None)

    assert sorted(path.name for path in tmp_path.iterdir()) == ["dist", "llama.json"]
    assert not (tmp_path / "dist" / "old").exists()


def test_a_record_that_points_outside_its_folder_names_no_program(tmp_path: Path) -> None:
    written(tmp_path / "elsewhere", b"not ours")
    (tmp_path / "dist").mkdir()
    for server in ["../elsewhere", "/bin/sh", "", 7, "llama-server"]:
        written(tmp_path / "llama.json", json.dumps({"version": DAILY, "server": server}).encode())
        assert llama_release.program(tmp_path) is None
    written(tmp_path / "llama.json", b"not json")
    assert llama_release.record(tmp_path) is None
    assert llama_release.program(tmp_path) is None


# --- checking it starts ------------------------------------------------------------


def installed(folder: Path, version: str = NUMBERED) -> Path:
    """A version folder as `fetch` leaves it, without asking anyone."""
    program = folder / "dist" / "llama-server"
    program.parent.mkdir(parents=True)
    program.write_bytes(b"program")
    record = {"version": version, "tag": DAILY, "server": "llama-server"}
    written(folder / "llama.json", json.dumps(record).encode())
    return program


def test_the_check_runs_the_program_and_reports_the_version_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    program = installed(tmp_path)
    monkeypatch.setenv("LLAMA_ARG_MODEL_URL", "https://example.net/other.gguf")
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "not-for-a-child")
    run = fake.FakeRunner({"llama-server": (0, "", "version: 7166 (abc123)\n")})

    assert llama_release.check(tmp_path, run=run) == NUMBERED

    (ran,) = run.ran
    assert ran.argv == [str(program), "--version"]
    assert ran.env is not None
    assert set(ran.env) <= set(kinds.PASSED_ON)
    assert (ran.capture, ran.check, ran.timeout) == (True, False, 60.0)


def test_a_program_that_will_not_start_fails_the_check_with_its_own_words(
    tmp_path: Path,
) -> None:
    installed(tmp_path)
    run = fake.FakeRunner(
        {"llama-server": (134, "", "dyld: Library not loaded: @rpath/libllama.dylib\n")}
    )

    with pytest.raises(ReleaseProblem) as err:
        llama_release.check(tmp_path, run=run)

    assert str(err.value) == "llama.cpp 0.9.0 would not start (exit 134)."
    assert "Library not loaded" in err.value.detail


def test_a_folder_with_nothing_installed_is_not_checked(tmp_path: Path) -> None:
    run = fake.FakeRunner()

    with pytest.raises(ReleaseProblem, match="Nothing is installed in"):
        llama_release.check(tmp_path, run=run)

    assert run.ran == []
    assert llama_release.about(tmp_path) == ""


# --- versions -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("0.9.0", "0.9.0"),
        (" v0.9.0 ", "0.9.0"),
        ("V1.2", "1.2"),
        ("b12318", "b12318"),
        ("B12318", "b12318"),
        ("b123", "b123"),
    ],
)
def test_a_version_is_a_number_or_a_daily_build(text: str, clean: str) -> None:
    assert llama_release.clean_version(text) == clean


@pytest.mark.parametrize(
    "text",
    ["", "latest", "b12", "b12345678", "0.9.0/../x", "--index-url=evil", "0.9.0 b12318", "v", "1"],
)
def test_anything_else_is_not_a_version(text: str) -> None:
    with pytest.raises(ValueError) as err:
        llama_release.clean_version(text)
    assert str(err.value) == (
        "That is not a llama.cpp version: give a number such as 0.9.0, "
        "or a daily build such as b12318."
    )


@pytest.mark.parametrize(
    ("latest", "installed_version", "newer"),
    [
        ("0.9.0", "0.8.2", True),
        ("0.10.0", "0.9.0", True),
        ("1.0", "0.9.9", True),
        ("0.9.0", "0.9", False),
        ("0.9.0", "0.9.0", False),
        ("0.8.9", "0.9.0", False),
        # A daily build is never older or newer than a numbered one (L6 D14).
        ("0.9.0", "b12318", False),
        ("0.9.0", "b100", False),
        ("b12319", "b12318", False),
        ("0.9.0", "not a version", False),
    ],
)
def test_a_daily_build_is_never_offered_an_update_to_a_numbered_one(
    latest: str, installed_version: str, newer: bool
) -> None:
    assert llama_release.is_newer(latest, installed_version) is newer


def test_the_newest_version_is_the_newest_that_is_not_a_daily_build() -> None:
    asked: list[str] = []

    def answer(body: dict[str, Any]) -> Callable[[str], Mapping[str, Any]]:
        def fetch(url: str) -> Mapping[str, Any]:
            asked.append(url)
            return body

        return fetch

    assert llama_release.newest(answer({"tag_name": "v0.9.0", "prerelease": False})) == "0.9.0"
    # GitHub's own "latest", never the first of the list (L6 P7).
    assert asked == [f"{API}/releases/latest"]
    for body in [
        {"tag_name": "b12318", "prerelease": True},
        {"tag_name": "v0.9.0", "prerelease": True},
        {"tag_name": "v0.9.0"},
        {"tag_name": "release-2026", "prerelease": False},
        {"message": "Not Found"},
    ]:
        with pytest.raises(ReleaseProblem) as err:
            llama_release.newest(answer(body))
        assert str(err.value) == (
            "GitHub answered in a form FrameFound could not read, so the newest version of "
            "llama.cpp is not known."
        )


def test_the_newest_version_when_github_refuses_or_is_away() -> None:
    def refused(url: str) -> Mapping[str, Any]:
        httpx.Response(429, request=httpx.Request("GET", url)).raise_for_status()
        return {}

    def away(url: str) -> Mapping[str, Any]:
        raise httpx.ConnectTimeout("timed out", request=httpx.Request("GET", url))

    with pytest.raises(ReleaseProblem, match="limiting requests"):
        llama_release.newest(refused)
    with pytest.raises(ReleaseProblem) as err:
        llama_release.newest(away)
    assert str(err.value) == (
        "llama.cpp's download page could not be reached, so the newest version is not known."
    )


# --- the module as a program ---------------------------------------------------------


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_the_module_reads_none_of_framefounds_settings() -> None:
    # It runs as a child with an environment holding nothing of FrameFound's
    # (L6 P22): reading a setting there would read defaults, or fail. So it
    # and the one module of FrameFound's it uses import nothing that could.
    # (The package's own __init__ imports framefound.config to define
    # `availability`; importing it reads nothing until that is called.)
    ours = imports_of(Path(llama_release.__file__))
    of_framefound = {name for name in ours if name.startswith("framefound")}
    assert of_framefound == {"framefound.native.proc"}
    outside = {name.split(".")[0] for name in ours} - {"framefound", "httpx"}
    assert outside <= set(sys.stdlib_module_names)
    proc_imports = imports_of(Path(sys.modules["framefound.native.proc"].__file__ or ""))
    assert not any(name.startswith("framefound") for name in proc_imports)
    assert "get_settings" not in Path(llama_release.__file__).read_text(encoding="utf-8")


def test_the_variables_given_to_the_program_are_the_kinds_own() -> None:
    assert llama_release.PASSED_ON == kinds.PASSED_ON


def test_main_says_the_sentence_and_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    hub = github()
    hub.files[mac_name(DAILY)] = b"tampered"

    code = llama_release.main(
        ["fetch", "--version", DAILY, "--into", str(tmp_path)], transport=hub.transport
    )

    out, err = capsys.readouterr()
    assert code == 1
    assert out.splitlines() == [
        "Asking GitHub about b12110…",
        "Downloading llama-b12110-bin-macos-arm64.tar.gz (1 MB)…",
    ]
    assert err.strip().splitlines()[-1] == (
        "The download was not the size GitHub lists for it, so it was thrown away."
    )

    assert llama_release.main(["fetch", "--version", "latest", "--into", str(tmp_path)]) == 1
    assert capsys.readouterr().err.strip().endswith("or a daily build such as b12318.")
    assert llama_release.main(["install", "everything"]) == 1
    assert llama_release.main(["check"]) == 1


def test_main_fetches_then_checks_and_the_check_prints_the_version_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = fake.FakeRunner({"llama-server": (0, "", "version: 7166 (abc123)\nbuilt for arm64\n")})

    fetched = llama_release.main(
        ["fetch", "--version", "0.9.0", "--into", str(tmp_path)], transport=github().transport
    )
    capsys.readouterr()
    checked = llama_release.main(["check", "--in", str(tmp_path)], run=run)
    check_out = capsys.readouterr().out
    about = llama_release.main(["about", "--in", str(tmp_path)], run=run)
    about_out = capsys.readouterr().out

    assert (fetched, checked, about) == (0, 0, 0)
    assert check_out.splitlines()[-1] == "0.9.0"
    assert about_out.splitlines() == ["build b12110", "version: 7166 (abc123)", "built for arm64"]


def test_main_check_with_a_program_that_fails_puts_its_words_before_the_sentence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installed(tmp_path)
    run = fake.FakeRunner({"llama-server": (1, "", "Killed: 9\n")})

    assert llama_release.main(["check", "--in", str(tmp_path)], run=run) == 1

    assert capsys.readouterr().err.strip().splitlines() == [
        "Killed: 9",
        "llama.cpp 0.9.0 would not start (exit 1).",
    ]


def test_about_on_an_empty_folder_prints_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert llama_release.main(["about", "--in", str(tmp_path)], run=fake.FakeRunner()) == 0
    assert capsys.readouterr() == ("", "")
