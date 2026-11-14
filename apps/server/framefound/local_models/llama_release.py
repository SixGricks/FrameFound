"""Getting llama.cpp: one pinned build from GitHub, checked against its checksum, unpacked safely.

llama.cpp is installed from the archive its project attaches to each daily
build on GitHub (`llama-b12110-bin-macos-arm64.tar.gz`), not from Homebrew:
an archive in a folder of its own can be kept beside the previous one and
rolled back to, and upgrading it can never upgrade a library FrameFound's
own programs use (L6 design D2).

The project publishes two kinds of release, and the page offers only one:

- **numbered versions** ("0.9.0"), every few weeks. GitHub's "latest
  release" is always one of these. A numbered release carries no program,
  only `nightly-tag.txt`, a few bytes naming the daily build it was cut from;
- **daily builds** ("b12318"), several a day, each with the archives. One
  can be installed by its number when a model needs something newer.

So installing "0.9.0" is: ask GitHub about v0.9.0, read the daily build its
pointer names, find that build's Mac archive and the sha256 GitHub lists for
it, download it, and only then unpack.

The rules this file keeps, each because the obvious code breaks it:

- **No listed checksum, no install.** GitHub's `digest` may be null; a
  build that cannot be verified is refused, as an unverifiable model file is.
  The bytes are hashed while they are written, and a mismatch (or a size
  other than the one listed) deletes them before anything is unpacked.
- **Only GitHub's own hosts**, over https, are asked; a redirect anywhere
  else is not followed. Redirects are followed by hand for that reason.
- **Unpacking never writes outside its folder.** Every member is checked
  before anything is written: no absolute name, no `..` in a name, no link
  that points out, no name or link that runs through one of the archive's
  own links (spelling says where a name lands only while no link is in
  the way), no hard link to a file the archive does not hold, nothing but
  files, folders and links, at most `MAX_MEMBERS` of them and
  `MAX_UNPACKED_BYTES` in all, and exactly one regular file named
  `llama-server`. A refusal there writes nothing. Then Python's own `data`
  filter extracts, as a second net that resolves what is really on the
  disk; should it refuse, what was written is removed.
- **What was installed is recorded** (`llama.json`: version, daily build,
  archive, sha256), because a daily release on GitHub can be edited later.

This module is a program as well as a library: `runtimes.install` runs it as
`python -m framefound.local_models.llama_release fetch|check|about`, with
FrameFound's own interpreter and an environment holding nothing of
FrameFound's (L6 D4). So it reads no setting, no token and no environment
variable of FrameFound's, and imports nothing of FrameFound's but
`native.proc`: a test reads its imports. Everything here blocks.

What GitHub answers was read on 2026-11-12 through a fetch tool, not through
this code; what is inside the Mac archive has not been seen (L6 design 12).
Each of those facts lives in one function — `resolve` for the release
listing, `unpack` for the archive — so a correction is one edit.
"""

import argparse
import contextlib
import gzip
import hashlib
import json
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from framefound.native import proc

API = "https://api.github.com/repos/ggml-org/llama.cpp"
ASSET = "llama-{tag}-bin-macos-arm64.tar.gz"
POINTER = "nightly-tag.txt"
RECORD = "llama.json"
PROGRAM = "llama-server"
# Where the archive is unpacked, inside the version folder.
DIST = "dist"
DOWNLOAD = "download"
# "0.9.0", as the page offers it; GitHub's tag is "v0.9.0".
NUMBERED = re.compile(r"\d+\.\d+(\.\d+)?")
# A daily build, by its number: "b12318".
DAILY = re.compile(r"b\d{3,7}")
# The Mac archive was 16.9 MB on 2026-11-12. Anything near these limits is
# not llama.cpp, whatever its name says.
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 2000
# GitHub's API, its download pages, and the two hosts its downloads are
# redirected to. Nothing else is ever asked.
HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
)
MAX_REDIRECTS = 5
# A release listing with forty archives is tens of kilobytes; the pointer is seven bytes.
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_POINTER_BYTES = 1024
CHUNK_BYTES = 1024 * 1024
TIMEOUT = httpx.Timeout(60.0, connect=10.0)
# `llama-server --version` answers at once; a program that does not is stuck.
CHECK_TIMEOUT_S = 60.0
GITHUB_JSON = "application/vnd.github+json"

# The variables a program started from here is given from this process:
# `kinds.PASSED_ON`, repeated because this module imports nothing of
# FrameFound's but `proc` (a test keeps the two the same). llama.cpp reads a
# `LLAMA_ARG_*` variable for almost every option, so nothing else passes.
PASSED_ON = ("PATH", "HOME", "TMPDIR", "USER", "LANG", "LC_ALL")

NOT_A_VERSION = (
    "That is not a llama.cpp version: give a number such as 0.9.0, or a daily build such as b12318."
)
NO_SUCH_VERSION = "llama.cpp has no version called {version}."
NO_MAC_BUILD = "llama.cpp {version} has no build for Apple-silicon Macs attached."
POINTS_NOWHERE = "llama.cpp {version} names build {tag}, which GitHub no longer has."
NO_CHECKSUM = "GitHub lists no checksum for this build, so it cannot be verified."
TOO_LARGE = (
    "GitHub lists this build at {mb} MB, far more than llama.cpp is, so it was not downloaded."
)
LIMITED = "GitHub is limiting requests from this address. Try again in an hour."
NOT_ANSWERING = "GitHub did not answer ({status}). Try again later."
UNREACHABLE = (
    "llama.cpp's download page could not be reached. "
    "Check this machine's internet connection and try again."
)
NEWEST_UNREACHABLE = (
    "llama.cpp's download page could not be reached, so the newest version is not known."
)
UNREADABLE = (
    "GitHub answered in a form FrameFound could not read, so llama.cpp {version} was not installed."
)
NEWEST_UNREADABLE = (
    "GitHub answered in a form FrameFound could not read, so the newest version of "
    "llama.cpp is not known."
)
NOT_GITHUB = (
    "The download was sent on to {host}, which is not one of GitHub's addresses, "
    "so it was not followed."
)
TOO_MANY_HOPS = "GitHub sent the download on too many times, so it was not followed."
STOPPED = "The download stopped part-way: GitHub could not be reached. Try again."
MISMATCH = "The download did not match its checksum, so it was thrown away."
WRONG_SIZE = "The download was not the size GitHub lists for it, so it was thrown away."
NOT_AN_ARCHIVE = "The download is not an archive FrameFound can read, so it was thrown away."
CLIMBS_OUT = (
    "The download holds {name}, which would be written outside its own folder, "
    "so it was thrown away."
)
ODD_MEMBER = (
    "The download holds {name}, which is not an ordinary file or folder, so it was thrown away."
)
LINKS_TO_NOTHING = (
    "The download holds {name}, a link to a file it does not hold, so it was thrown away."
)
TWICE = "The download holds {name} twice, so it was thrown away."
TOO_MANY_MEMBERS = "The download holds more than {count} files, so it was thrown away."
UNPACKS_TOO_LARGE = "The download would unpack to more than {mb} MB, so it was thrown away."
NO_PROGRAM = "The download has no llama-server program in it, so it was thrown away."
TWO_PROGRAMS = "The download has more than one llama-server program in it, so it was thrown away."
NOTHING_INSTALLED = "Nothing is installed in {folder}: its record is missing or unreadable."
WOULD_NOT_START = "llama.cpp {version} would not start (exit {code})."

# `proc.run`'s shape: what `check` is given to run the program with.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]
# A GET of a public JSON document (the shape of `hub.FetchJson`).
FetchJson = Callable[[str], Mapping[str, Any]]


class ReleaseProblem(RuntimeError):
    """Something the operator has to fix; the message says what.

    `detail` holds what the program itself printed, for the page to show
    as it is ("" when there is nothing).
    """

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


class _Unreadable(Exception):
    """GitHub answered, but not in a form this module understands."""


@dataclass(frozen=True)
class Release:
    """One Mac archive of llama.cpp, as GitHub lists it."""

    # As asked for: "0.9.0" or "b12318".
    version: str
    # The daily build the program comes from: "b12110".
    tag: str
    asset: str
    url: str
    # Lower-case hex, from GitHub's `digest`.
    sha256: str
    size: int


def clean_version(text: str) -> str:
    """`text` as a folder name: "v0.9.0" → "0.9.0", "B12318" → "b12318"; `ValueError` otherwise.

    The answer is a folder name and goes into GitHub addresses, so nothing
    but a number or a daily build is ever accepted.
    """
    version = text.strip()
    if version[:1] in ("v", "V") and NUMBERED.fullmatch(version[1:]):
        return version[1:]
    if NUMBERED.fullmatch(version):
        return version
    if DAILY.fullmatch(version.lower()):
        return version.lower()
    raise ValueError(NOT_A_VERSION)


def is_newer(latest: str, installed: str) -> bool:
    """Is `latest` a later numbered version than `installed`?

    A daily build is never older or newer than a numbered one (L6 D14): the
    two are counted differently, and offering every daily build as an update
    would teach the operator to ignore the button. So with a daily build
    installed, or anything that is not a version, the answer is False.
    """
    newest, ours = _numbered(latest), _numbered(installed)
    if newest is None or ours is None:
        return False
    return newest > ours


def newest(fetch: FetchJson) -> str:
    """The newest numbered version ("0.9.0"); `ReleaseProblem` when GitHub's answer cannot be read.

    GitHub's "latest release" is the newest that is neither a draft nor a
    pre-release, and every daily build is marked pre-release, so this is
    never a daily build. Do not list releases and take the first instead:
    that is today's daily build, and tomorrow's another (L6 P7).
    An `httpx` error from `fetch` becomes a sentence too; anything else it
    raises (`hub.fetch_json`'s `HubError`) passes through as it is.
    """
    answer = _asked(lambda: fetch(f"{API}/releases/latest"), "", newest=True)
    tag = answer.get("tag_name")
    if (
        answer.get("prerelease") is not False
        or not isinstance(tag, str)
        or not tag.startswith("v")
        or not NUMBERED.fullmatch(tag[1:])
    ):
        raise ReleaseProblem(NEWEST_UNREADABLE)
    return tag[1:]


def resolve(version: str, *, get_json: FetchJson, get_text: Callable[[str], str]) -> Release:
    """Which archive `version` means, and the checksum GitHub lists for it.

    A daily build is looked up by its own tag. A numbered version is looked
    up as `v<version>`, and the daily build its pointer file names is then
    looked up the same way. `get_json` and `get_text` raise
    `httpx.HTTPStatusError` for a refusal (404: no such version; 403 or 429:
    GitHub is limiting requests) and `httpx.HTTPError` when GitHub cannot be
    reached; each becomes a `ReleaseProblem` with the operator's sentence.
    """
    try:
        version = clean_version(version)
    except ValueError as err:
        raise ReleaseProblem(str(err)) from None
    if DAILY.fullmatch(version):
        tag = version
    else:
        numbered = _release(version, f"v{version}", get_json)
        pointer = _asset(numbered, POINTER)
        if pointer is None:
            raise ReleaseProblem(NO_MAC_BUILD.format(version=version))
        address = _string(pointer.get("browser_download_url"), version)
        tag = _asked(lambda: get_text(address), version).strip()
        if not DAILY.fullmatch(tag):
            raise ReleaseProblem(UNREADABLE.format(version=version))
    # A numbered version GitHub has just listed must not be called missing
    # when it is the daily build it names that has gone.
    gone = None if tag == version else POINTS_NOWHERE.format(version=version, tag=tag)
    daily = _release(version, tag, get_json, missing=gone)
    name = ASSET.format(tag=tag)
    asset = _asset(daily, name)
    if asset is None:
        raise ReleaseProblem(NO_MAC_BUILD.format(version=version))
    digest = asset.get("digest")
    found = re.fullmatch(r"sha256:([0-9a-fA-F]{64})", digest) if isinstance(digest, str) else None
    if found is None:
        raise ReleaseProblem(NO_CHECKSUM)
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ReleaseProblem(UNREADABLE.format(version=version))
    if size > MAX_ARCHIVE_BYTES:
        raise ReleaseProblem(TOO_LARGE.format(mb=_megabytes(size)))
    return Release(
        version=version,
        tag=tag,
        asset=name,
        url=_string(asset.get("browser_download_url"), version),
        sha256=found.group(1).lower(),
        size=size,
    )


def download(release: Release, target: Path, *, client: httpx.Client) -> None:
    """The archive, into `target`, hashed on the way; a mismatch keeps nothing.

    Written to `target` + ".partial" and moved into place only once its
    size and sha256 are the ones GitHub lists. Any failure deletes the
    partial file and raises `ReleaseProblem`.
    """
    partial = target.with_name(target.name + ".partial")
    target.parent.mkdir(parents=True, exist_ok=True)
    whole = False
    try:
        digest = hashlib.sha256()
        written = 0
        try:
            response = _get(client, release.url, accept="application/octet-stream")
        except httpx.HTTPError:
            # Nothing arrived: GitHub was never reached, not cut off part-way.
            raise ReleaseProblem(UNREACHABLE) from None
        try:
            try:
                if response.status_code != httpx.codes.OK:
                    raise _refusal(response.status_code, None)
                with partial.open("wb") as out:
                    for chunk in response.iter_bytes(CHUNK_BYTES):
                        written += len(chunk)
                        if written > release.size:
                            raise ReleaseProblem(WRONG_SIZE)
                        digest.update(chunk)
                        out.write(chunk)
            finally:
                response.close()
        except httpx.HTTPError:
            raise ReleaseProblem(STOPPED) from None
        if written != release.size:
            raise ReleaseProblem(WRONG_SIZE)
        if digest.hexdigest() != release.sha256:
            raise ReleaseProblem(MISMATCH)
        os.replace(partial, target)
        whole = True
    finally:
        if not whole:
            partial.unlink(missing_ok=True)


def unpack(archive: Path, into: Path) -> str:
    """Unpack the `.tar.gz` at `archive` into the new folder `into`; the program's path within it.

    Every member is checked before anything is written (see the module's
    rules); a refusal, from that check or from Python's filter while
    extracting, raises `ReleaseProblem` and leaves no `into`. The path
    returned is relative to `into`, in POSIX form ("build/bin/llama-server").
    `into` must not exist yet.
    """
    try:
        with tarfile.open(archive, "r:gz") as tar:
            members, program = _checked(tar, into)
            into.mkdir(parents=True)
            try:
                try:
                    tar.extractall(into, members=members, filter="data")
                except tarfile.FilterError as err:
                    # The second net caught what the listing let through.
                    raise _filtered(err) from None
                except KeyError:
                    # `tarfile`'s answer to a hard link whose file is not
                    # there; the listing refuses those, this is the backstop.
                    raise ReleaseProblem(NOT_AN_ARCHIVE) from None
                path = into / PurePosixPath(program)
                if path.is_symlink() or not path.is_file():
                    raise ReleaseProblem(NO_PROGRAM)
                # The program is run, so it must be runnable by its owner
                # whatever the archive said.
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            except BaseException:
                shutil.rmtree(into, ignore_errors=True)
                raise
    except (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile):
        raise ReleaseProblem(NOT_AN_ARCHIVE) from None
    return program


def fetch(
    version: str,
    into: Path,
    *,
    transport: httpx.BaseTransport | None = None,
    say: Callable[[str], None] = print,
) -> Release:
    """Download, verify and unpack `version` into the version folder `into`, and record it.

    `into` is the folder `runtimes.install` made. Afterwards it holds
    `dist/` (the archive as it came) and `llama.json` (`RECORD`), written
    last. Any failure removes what this made and raises `ReleaseProblem`;
    `say` is told each stage, and those lines are what the page shows of a
    failed install.
    """
    try:
        version = clean_version(version)
    except ValueError as err:
        raise ReleaseProblem(str(err)) from None
    if not into.is_dir():
        raise ReleaseProblem(f"{into} is not a folder, so nothing was installed into it.")
    downloads, dist = into / DOWNLOAD, into / DIST
    # Left by a fetch that was stopped part-way; nothing can be using them.
    for leftover in (downloads, dist):
        shutil.rmtree(leftover, ignore_errors=True)
    (into / RECORD).unlink(missing_ok=True)
    done = False
    try:
        with httpx.Client(
            transport=transport,
            timeout=TIMEOUT,
            follow_redirects=False,
            trust_env=transport is None,
        ) as client:
            say(f"Asking GitHub about {version}…")
            release = resolve(
                version,
                get_json=lambda url: _get_json(client, url),
                get_text=lambda url: _get_text(client, url),
            )
            say(f"Downloading {release.asset} ({_megabytes(release.size)} MB)…")
            archive = downloads / release.asset
            download(release, archive, client=client)
        say("Unpacking…")
        server = unpack(archive, dist)
        _write_json(
            into / RECORD,
            {
                "version": release.version,
                "tag": release.tag,
                "asset": release.asset,
                "sha256": release.sha256,
                "size": release.size,
                "server": server,
                "fetched_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
        done = True
        return release
    finally:
        shutil.rmtree(downloads, ignore_errors=True)
        if not done:
            shutil.rmtree(dist, ignore_errors=True)


def record(folder: Path) -> dict[str, Any] | None:
    """`llama.json` of the version folder `folder`; None when there is none or it cannot be read."""
    try:
        body = json.loads((folder / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def program(folder: Path) -> Path | None:
    """The `llama-server` the record of `folder` names; None unless it is a file inside `dist`.

    The record is read back from disk, so its path is checked again: one
    that leaves `dist/` or is a link is not the program.
    """
    body = record(folder)
    server = body.get("server") if body is not None else None
    if not isinstance(server, str) or _climbs_out(server):
        return None
    path = folder / DIST / PurePosixPath(server)
    if path.is_symlink() or not path.is_file():
        return None
    return path


def check(folder: Path, *, run: Runner = proc.run) -> str:
    """Start the installed program once (`--version`); the version recorded, when it ran.

    What it prints is not read: its form is not known (L6 design 12). That
    it exits 0 here is the proof that this Mac runs it. `ReleaseProblem`
    otherwise, with the program's last lines on its `detail`. A program
    that cannot be started at all, or does not finish, raises
    `proc.CommandFailed` from `run`.
    """
    body = record(folder)
    path = program(folder)
    version = body.get("version") if body is not None else None
    if path is None or not isinstance(version, str):
        raise ReleaseProblem(NOTHING_INSTALLED.format(folder=folder))
    done = run(
        [str(path), "--version"],
        env=_child_env(),
        capture=True,
        check=False,
        timeout=CHECK_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise ReleaseProblem(
            WOULD_NOT_START.format(version=version, code=done.returncode), _said(done)
        )
    return version


def about(folder: Path) -> str:
    """One more line about the version in `folder`: "build b12110" for a numbered one, else ""."""
    body = record(folder)
    if body is None:
        return ""
    tag, version = body.get("tag"), body.get("version")
    if not isinstance(tag, str) or not DAILY.fullmatch(tag) or tag == version:
        return ""
    return f"build {tag}"


def main(
    argv: Sequence[str],
    *,
    transport: httpx.BaseTransport | None = None,
    run: Runner = proc.run,
) -> int:
    """`fetch --version V --into DIR`, `check --in DIR` or `about --in DIR`; 0, or 1.

    The only place that prints. On a failure the sentence is the last line
    on stderr. `check` prints the version as its last line (what
    `runtimes.install` compares with the version asked for); `about` prints
    `about` and the program's own `--version` text, for the install's
    record, and nothing for a folder with nothing in it.
    """
    parser = argparse.ArgumentParser(prog="python -m framefound.local_models.llama_release")
    commands = parser.add_subparsers(dest="command", required=True)
    fetching = commands.add_parser("fetch")
    fetching.add_argument("--version", required=True)
    fetching.add_argument("--into", required=True, type=Path)
    for name in ("check", "about"):
        commands.add_parser(name).add_argument("--in", dest="folder", required=True, type=Path)
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as stop:
        # argparse has printed what was wrong.
        return 0 if stop.code == 0 else 1
    try:
        if args.command == "fetch":
            release = fetch(
                args.version,
                args.into,
                transport=transport,
                say=lambda line: print(line, flush=True),
            )
            print(f"llama.cpp {release.version} (build {release.tag}) is in {args.into}.")
        elif args.command == "check":
            print(check(args.folder, run=run))
        else:
            for line in (about(args.folder), _version_text(args.folder, run)):
                if line:
                    print(line)
    except ReleaseProblem as err:
        # The sentence last: the installer keeps the last lines for the page.
        if err.detail:
            print(err.detail, file=sys.stderr)
        print(err, file=sys.stderr)
        return 1
    except proc.CommandFailed as err:
        print(err, file=sys.stderr)
        return 1
    except httpx.HTTPError:
        print(UNREACHABLE, file=sys.stderr)
        return 1
    except OSError as err:
        print(f"The files could not be written: {err.strerror or err}.", file=sys.stderr)
        return 1
    return 0


# --- asking GitHub -----------------------------------------------------------------


def _release(
    version: str, tag: str, get_json: FetchJson, *, missing: str | None = None
) -> Mapping[str, Any]:
    """The listing of `tag`; a 404 is `missing` when given, else "no version called <version>"."""
    answer = _asked(lambda: get_json(f"{API}/releases/tags/{tag}"), version, missing=missing)
    if answer.get("tag_name") != tag or not isinstance(answer.get("assets"), list):
        raise ReleaseProblem(UNREADABLE.format(version=version))
    return answer


def _asset(release: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    for asset in release["assets"]:
        if isinstance(asset, Mapping) and asset.get("name") == name:
            return asset
    return None


def _string(value: Any, version: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReleaseProblem(UNREADABLE.format(version=version))
    return value


def _asked[T](
    work: Callable[[], T], version: str, *, newest: bool = False, missing: str | None = None
) -> T:
    """Run `work`; GitHub's refusals and silences become the operator's sentences.

    `missing` is the sentence for a 404 when it is not "no such version".
    The new error is raised after the handler, so the network library's own
    (with its addresses) is not attached to it.
    """
    try:
        return work()
    except httpx.HTTPStatusError as err:
        status = err.response.status_code
        if missing is not None and status == httpx.codes.NOT_FOUND:
            problem = ReleaseProblem(missing)
        else:
            problem = _refusal(status, None if newest else version)
    except httpx.HTTPError:
        problem = ReleaseProblem(NEWEST_UNREACHABLE if newest else UNREACHABLE)
    except _Unreadable:
        problem = ReleaseProblem(
            NEWEST_UNREADABLE if newest else UNREADABLE.format(version=version)
        )
    raise problem


def _refusal(status: int, version: str | None) -> ReleaseProblem:
    """GitHub's refusal as a sentence. A 404 names `version` when it is the thing asked about."""
    if status in (httpx.codes.FORBIDDEN, httpx.codes.TOO_MANY_REQUESTS):
        return ReleaseProblem(LIMITED)
    if status == httpx.codes.NOT_FOUND and version is not None:
        return ReleaseProblem(NO_SUCH_VERSION.format(version=version))
    return ReleaseProblem(NOT_ANSWERING.format(status=status))


def _get(client: httpx.Client, url: str, *, accept: str) -> httpx.Response:
    """GET `url` from GitHub, following redirects by hand; the caller closes the answer.

    Every hop, the first included, must be https to one of `HOSTS`.
    """
    place = httpx.URL(url)
    for _hop in range(MAX_REDIRECTS + 1):
        if place.scheme != "https" or place.host not in HOSTS:
            raise ReleaseProblem(NOT_GITHUB.format(host=place.host or "an address with no host"))
        request = client.build_request(
            "GET", place, headers={"Accept": accept, "Accept-Encoding": "identity"}
        )
        response = client.send(request, stream=True)
        if not response.has_redirect_location:
            return response
        location = response.headers["location"]
        response.close()
        try:
            place = place.join(location)
        except httpx.InvalidURL:
            raise ReleaseProblem(NOT_GITHUB.format(host="an address that cannot be read")) from None
    raise ReleaseProblem(TOO_MANY_HOPS)


def _read(client: httpx.Client, url: str, *, accept: str, limit: int) -> bytes:
    """The whole answer to a GET; `httpx.HTTPStatusError` for anything but 200."""
    response = _get(client, url, accept=accept)
    try:
        if response.status_code != httpx.codes.OK:
            raise httpx.HTTPStatusError(
                f"GitHub answered {response.status_code}",
                request=response.request,
                response=response,
            )
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) > limit:
                raise _Unreadable
        return bytes(body)
    finally:
        response.close()


def _get_json(client: httpx.Client, url: str) -> Mapping[str, Any]:
    body = _read(client, url, accept=GITHUB_JSON, limit=MAX_JSON_BYTES)
    try:
        answer = json.loads(body)
    except ValueError:
        raise _Unreadable from None
    if not isinstance(answer, dict):
        raise _Unreadable
    return answer


def _get_text(client: httpx.Client, url: str) -> str:
    body = _read(client, url, accept="application/octet-stream", limit=MAX_POINTER_BYTES)
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        raise _Unreadable from None


# --- the archive ------------------------------------------------------------------


def _checked(tar: tarfile.TarFile, into: Path) -> tuple[list[tarfile.TarInfo], str]:
    """Every member of `tar`, each checked, and the program's path within it.

    `ReleaseProblem` for the first member that fails. Reads the whole
    listing and writes nothing.

    Names are judged by their spelling, which is only where they land while
    no link stands in the way. So once the whole listing is read, a name
    that runs *through* one of the archive's own links is refused, wherever
    in the archive that link comes: with `c -> .` already written,
    `c/z -> ..` is a link to the folder above, though "c/.." reads as ".".
    """
    members: list[tarfile.TarInfo] = []
    seen: set[str] = set()
    files: set[str] = set()
    links: dict[str, str] = {}
    programs: list[str] = []
    total = 0
    for member in tar:
        if len(members) >= MAX_MEMBERS:
            raise ReleaseProblem(TOO_MANY_MEMBERS.format(count=MAX_MEMBERS))
        name = posixpath.normpath(member.name)
        if (
            _climbs_out(member.name)
            or ".." in member.name.split("/")
            or (name == "." and not member.isdir())
        ):
            # A name that goes up and comes back down ("b/../x") lands where
            # it reads only if "b" is not a link; no archive needs one.
            raise ReleaseProblem(CLIMBS_OUT.format(name=_shown(member.name)))
        if name in seen:
            # The second would silently replace the first — possibly the
            # program, after it was checked, with a link.
            raise ReleaseProblem(TWICE.format(name=_shown(name)))
        seen.add(name)
        if member.isreg() and not member.issparse():
            total += member.size
            if total > MAX_UNPACKED_BYTES:
                raise ReleaseProblem(UNPACKS_TOO_LARGE.format(mb=_megabytes(MAX_UNPACKED_BYTES)))
            files.add(name)
            if posixpath.basename(name) == PROGRAM:
                programs.append(name)
        elif member.issym():
            # A link's target is read from where the link is.
            if _climbs_out(posixpath.join(posixpath.dirname(name), member.linkname)):
                raise ReleaseProblem(CLIMBS_OUT.format(name=_shown(member.name)))
            links[name] = member.linkname
        elif member.islnk():
            # A hard link names a file the archive has already given; anything
            # else is either outside or nothing (and `tarfile` would answer
            # a missing one with a bare KeyError).
            if _climbs_out(member.linkname) or ".." in member.linkname.split("/"):
                raise ReleaseProblem(CLIMBS_OUT.format(name=_shown(member.name)))
            if posixpath.normpath(member.linkname) not in files:
                raise ReleaseProblem(LINKS_TO_NOTHING.format(name=_shown(member.name)))
        elif not member.isdir():
            raise ReleaseProblem(ODD_MEMBER.format(name=_shown(member.name)))
        try:
            # Python's own rules for unpacking downloaded data, asked
            # before anything is written as well as during.
            tarfile.data_filter(member, str(into))
        except tarfile.FilterError as err:
            raise _filtered(err) from None
        members.append(member)
    for member in members:
        name = posixpath.normpath(member.name)
        if _through_a_link(posixpath.dirname(name).split("/"), links):
            raise ReleaseProblem(CLIMBS_OUT.format(name=_shown(member.name)))
        target = links.get(name)
        if target is not None and _through_a_link(
            [*posixpath.dirname(name).split("/"), *target.split("/")], links, last_may_be=True
        ):
            raise ReleaseProblem(CLIMBS_OUT.format(name=_shown(member.name)))
    if not programs:
        raise ReleaseProblem(NO_PROGRAM)
    if len(programs) > 1:
        raise ReleaseProblem(TWO_PROGRAMS)
    return members, programs[0]


def _through_a_link(
    parts: list[str], links: Mapping[str, str], *, last_may_be: bool = False
) -> bool:
    """Does the path `parts` (from the archive's top) pass through one of `links`, or climb out?

    Walked a part at a time, so ".." is undone only over a part already
    known not to be a link. `last_may_be`: the path's own last part may be
    a link (a link to a link is followed by that link's own check).
    """
    walked: list[str] = []
    for index, part in enumerate(parts):
        if part in ("", "."):
            continue
        if part == "..":
            if not walked:
                return True
            walked.pop()
            continue
        walked.append(part)
        if "/".join(walked) in links and not (last_may_be and index == len(parts) - 1):
            return True
    return False


def _filtered(err: tarfile.FilterError) -> ReleaseProblem:
    """Python's `data` filter's refusal as the operator's sentence."""
    name = _shown(err.tarinfo.name)
    if isinstance(err, tarfile.SpecialFileError):
        return ReleaseProblem(ODD_MEMBER.format(name=name))
    return ReleaseProblem(CLIMBS_OUT.format(name=name))


def _climbs_out(name: str) -> bool:
    """Would `name`, read from inside a folder, lead out of it?

    Absolute names, drive letters and backslashes count as out: a Mac
    program's archive needs none of them, and on another system each can
    land somewhere else than it reads.
    """
    if not name or name.startswith("/") or "\\" in name or re.match(r"[A-Za-z]:", name):
        return True
    normal = posixpath.normpath(name)
    return normal == ".." or normal.startswith("../")


def _shown(name: str) -> str:
    """A member's name, safe to put in a sentence."""
    printable = "".join(char if char.isprintable() else "?" for char in name)
    return printable[:200] or "a member with no name"


# --- small things ----------------------------------------------------------------


def _numbered(text: str) -> tuple[int, int, int] | None:
    try:
        version = clean_version(text)
    except ValueError:
        return None
    if not NUMBERED.fullmatch(version):
        return None
    parts = [int(part) for part in version.split(".")]
    return (parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def _megabytes(size: int) -> int:
    return max(1, round(size / 1_000_000))


def _child_env() -> dict[str, str]:
    return {name: os.environ[name] for name in PASSED_ON if name in os.environ}


def _said(done: "subprocess.CompletedProcess[str]") -> str:
    text = ((done.stderr or "") + "\n" + (done.stdout or "")).strip()
    return "\n".join(text.splitlines()[-20:])


def _version_text(folder: Path, run: Runner) -> str:
    """The program's own `--version` text, for the record of what was installed. Best effort."""
    path = program(folder)
    if path is None:
        return ""
    try:
        done = run(
            [str(path), "--version"],
            env=_child_env(),
            capture=True,
            check=False,
            timeout=CHECK_TIMEOUT_S,
        )
    except proc.CommandFailed:
        return ""
    return _said(done) if done.returncode == 0 else ""


def _write_json(path: Path, body: dict[str, Any]) -> None:
    """One replacement, so a reader sees the whole record or none."""
    handle, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".partial", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(body, out, indent=2)
            out.write("\n")
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
