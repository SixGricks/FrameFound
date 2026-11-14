"""Hugging Face behind one small interface.

Three questions are ever put to it: what does this model's page say (`info`),
what is in one of its small settings files (`read_text`), and give me this
file (`fetch`). `Hub` is those three; `HttpHub` asks the real site; the tests
and the demo use `fake.FakeHub`. Nothing else in FrameFound talks to Hugging
Face, and its own client library is not used: everything here is plain HTTPS
with a transport a test can replace.

Rules this file keeps, each because the obvious code breaks it:

- **A name that has moved is not followed.** Hugging Face forwards an old
  name to wherever the model lives now. Following it would judge one
  publisher's model under another's name, so the operator is told to check
  the new name instead.
- **The token goes to Hugging Face and nowhere else.** A file's bytes come
  from a storage host that Hugging Face redirects to; the redirect is
  followed by hand so the `Authorization` header is never sent there.
- **An error is a sentence for the operator** and never holds the token or
  a signed storage address. The network library's own error is dropped
  rather than chained for that reason: its text and its request carry both.
- **Nothing identifying is sent**: the client's default User-Agent, the
  model's name, and the token when one is stored.

Everything here blocks. Call it with `asyncio.to_thread` from a request or a
job.

The shapes read here were seen on 2026-11-12 through a fetch tool, not
through this code (docs/local-models.md). Each fact lives in one function so
a correction on first real use is one edit: `parse_info` for the page,
`_problem` for what a refusal means, `HttpHub.fetch` for how a file resumes.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

import httpx

from framefound.local_models import policy

HUB = "https://huggingface.co"

CONNECT_S = 10.0
READ_S = 30.0
# A large file's host may pause between pieces; a small read may not.
CHUNK_WAIT_S = 60.0
CHUNK_BYTES = 4 * 1024 * 1024
MAX_REDIRECTS = 5
# A model's page with every file listed is a few hundred kilobytes.
MAX_JSON_BYTES = 16 * 1024 * 1024

Problem = Literal[
    "not_found",
    "moved",
    "needs_token",
    "no_access",
    "rate_limited",
    "unreachable",
    "bad_answer",
]

MOVED = "This name now points somewhere else on Hugging Face. Check that name instead."
NO_ACCESS = (
    "Hugging Face would not let this token read the model. Check that you accepted the "
    "terms on the model's page with the same account."
)
# 401 with a token: Hugging Face did not take the token at all, which a model
# whose terms were never accepted and a token that has been revoked both
# look like from here. The second half is the way out of the second.
NOT_TAKEN = (
    f"{NO_ACCESS} If you did, the token itself may have stopped working: paste a new one "
    "under Hugging Face account."
)
RATE_LIMITED = (
    "Hugging Face is turning requests away for now (too many in a short time). Try again "
    "in a few minutes."
)
UNREACHABLE = (
    "Hugging Face could not be reached. Check this machine's internet connection and try again."
)
NOT_ANSWERING = "Hugging Face is not answering properly just now. Try again in a few minutes."
BAD_ANSWER = (
    "Hugging Face answered in a form FrameFound could not read. Try again; if it keeps "
    "happening, FrameFound needs an update."
)
PASTE_AGAIN = (
    "The Hugging Face token stored here is not one Hugging Face could accept. Paste it "
    "again under Hugging Face account."
)


# The sentences that name a model. Functions here, and nowhere copied: the
# stand-in (fake.FakeHub) and the check say them through these, so a sentence
# reworded for the real site is reworded in the tests and the demo as well.


def no_such_model(repo_id: str) -> str:
    return f"Hugging Face has no model called {repo_id}. Check the spelling."


def no_such_build(repo_id: str) -> str:
    return f"Hugging Face no longer has this build of {repo_id}."


def no_such_file(repo_id: str, path: str) -> str:
    return f"Hugging Face has no file called {path} in this build of {repo_id}."


def accept_terms_first(repo_id: str) -> str:
    """What a model that asks for an account says when no token is stored."""
    return (
        f"{policy.publisher_of(repo_id)} asks you to accept their terms before "
        "downloading. Do that on the model's page at Hugging Face, then paste a read "
        "token under Hugging Face account, below, and check again."
    )


def now_points_to(repo_id: str) -> str:
    """What a name that Hugging Face forwards says: check the model it leads to."""
    return f"This name now points to {repo_id} on Hugging Face. Check that name instead."


_COMMIT_LENGTH = 40
_SHA256_LENGTH = 64
_LONGEST_NAME = 300
# How a page's tags say what a model was made from: `base_model:<repo>`, or
# `base_model:<how>:<repo>` with one of these.
_RELATIONS = ("finetune", "quantized", "adapter", "merge")


@dataclass(frozen=True)
class RepoFile:
    """One file of a model, as Hugging Face lists it."""

    path: str
    size: int
    # For a large (LFS) file: the SHA-256 of its bytes.
    sha256: str | None
    # For a small file kept in git itself: its git blob id.
    blob_id: str | None


@dataclass(frozen=True)
class RepoInfo:
    """What a model's page says, reduced to what FrameFound decides on."""

    # The hub's own spelling.
    repo_id: str
    # The commit this answer describes: the build.
    revision: str
    publisher: str | None
    private: bool
    disabled: bool
    # "" when anyone may download; else how access is granted ("auto", ...).
    gated: str
    # Every model this one says it was built from.
    bases: tuple[str, ...]
    licence: str
    licence_name: str
    licence_url: str | None
    library: str
    pipeline: str
    tags: tuple[str, ...]
    # The page says the model needs program code from its own repository.
    custom_code: bool
    modified_at: datetime | None
    files: tuple[RepoFile, ...]
    # A GGUF repository's own summary of its model (`architecture`,
    # `context_length`, ...), as the page gave it, without the chat template.
    # Shown as information and never decided on: what `total` counts is not
    # documented anywhere read. None when the page has none. Left out of the
    # hash (a mapping has none), not out of equality.
    gguf: Mapping[str, Any] | None = field(default=None, hash=False)


class HubError(RuntimeError):
    """Hugging Face could not be read. The message is a sentence for the
    operator and never contains the token or a signed URL."""

    def __init__(
        self,
        problem: Problem,
        message: str,
        status: int | None = None,
        *,
        moved_to: str | None = None,
    ) -> None:
        super().__init__(message)
        self.problem: Problem = problem
        self.status = status
        # For "moved": the name the old one now points at, when it could be read.
        self.moved_to = moved_to


class Stopped(Exception):
    """`fetch` was asked to stop. What had arrived is kept, to be resumed."""


def _ignore(_count: int) -> None:
    return None


def _never() -> bool:
    return False


class Hub(Protocol):
    def info(self, repo_id: str, revision: str | None = None) -> RepoInfo:
        """What the model's page says, at its newest build or at `revision`."""
        ...

    def read_text(self, repo_id: str, revision: str, path: str, max_bytes: int = 4_000_000) -> str:
        """The start of one file, as text: at most `max_bytes` of it."""
        ...

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
        """Append the file to `target` from the size `target` already has.

        A server that will not resume starts the file again. `on_bytes` is
        told how many bytes each piece added; a negative number means that
        much of what was already there has just been thrown away, so the
        numbers always add up to how much `target` grew. `should_stop` is
        asked between pieces; when it says yes `Stopped` is raised and what
        has arrived stays. Nothing here checks the bytes: the caller does
        (`files.check_file`).

        A `target` that already holds as much as the file, or more, is left
        as it is and nothing is reported: asked for "the rest", the server
        says there is none. Only the caller's check tells a whole file from
        one that is too long, and a file it refuses has to be deleted before
        it is fetched again.
        """
        ...


def parse_info(payload: Mapping[str, Any]) -> RepoInfo:
    """A model-info answer as a `RepoInfo`; `HubError("bad_answer")` on a shape it cannot read.

    Strict where a wrong reading could let something through — the build,
    what the model was built from, the tags, the file list — and lenient
    about the rest. The same field changes type from one page to the next
    (`base_model` is a string here and a list there; `gated` is `false` or
    a word), so each is read for every shape seen.
    """
    if not isinstance(payload, Mapping):
        raise _unreadable()
    repo_id = payload.get("id")
    revision = _hex(payload.get("sha"), _COMMIT_LENGTH)
    if not isinstance(repo_id, str) or not _plain(repo_id) or revision is None:
        raise _unreadable()

    card = _mapping(payload.get("cardData"))
    config = _mapping(payload.get("config"))
    tags = payload.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise _unreadable()
    siblings = payload.get("siblings")
    if not isinstance(siblings, list):
        raise _unreadable()

    author = payload.get("author")
    licence = _licence(card.get("license"))
    if not licence:
        licence = next((tag[len("license:") :] for tag in tags if tag.startswith("license:")), "")
    gated = payload.get("gated")
    return RepoInfo(
        repo_id=repo_id,
        revision=revision,
        publisher=author if isinstance(author, str) and author else policy.publisher_of(repo_id),
        private=bool(payload.get("private")),
        disabled=bool(payload.get("disabled")),
        # Anything but false or absent is gated: a word not seen before must
        # not read as "open to anyone".
        gated=gated.strip() if isinstance(gated, str) else ("yes" if gated else ""),
        bases=_bases(card.get("base_model"), tags),
        licence=licence,
        licence_name=_text(card.get("license_name")),
        licence_url=_link(card.get("license_link")),
        library=_text(payload.get("library_name")),
        pipeline=_text(payload.get("pipeline_tag")),
        tags=tuple(tags),
        custom_code="custom_code" in tags or "auto_map" in config,
        modified_at=_when(payload.get("lastModified")),
        files=tuple(_file(entry) for entry in siblings),
        gguf=_gguf(payload.get("gguf")),
    )


class HttpHub:
    """The real site. One instance per use; nothing is kept open between calls."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base: str = HUB,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._token = (token or "").strip()
        self._base = httpx.URL(base.rstrip("/"))
        self._transport = transport

    def info(self, repo_id: str, revision: str | None = None) -> RepoInfo:
        url = f"{self._base}/api/models/{_model_name(repo_id)}"
        if revision is not None:
            url += f"/revision/{_commit(repo_id, revision)}"
        asked = httpx.URL(url, params={"blobs": "true"})

        def ask() -> Mapping[str, Any]:
            with (
                self._client(READ_S) as client,
                client.stream("GET", asked, headers=self._headers(asked, {})) as response,
            ):
                if response.is_redirect:
                    target = _moved_to(response)
                    sentence = now_points_to(target) if target else MOVED
                    raise HubError("moved", sentence, response.status_code, moved_to=target)
                if response.status_code != httpx.codes.OK:
                    raise self._refused(response, repo_id, revision=revision)
                return _json(_body(response, MAX_JSON_BYTES))

        return parse_info(_guarded(ask, UNREACHABLE))

    def read_text(self, repo_id: str, revision: str, path: str, max_bytes: int = 4_000_000) -> str:
        url = self._file_url(repo_id, revision, path)

        def ask() -> bytes:
            with self._client(READ_S) as client:
                response = self._follow(client, url, {})
                try:
                    if response.status_code != httpx.codes.OK:
                        raise self._refused(response, repo_id, revision=revision, path=path)
                    return _body(response, max_bytes, cut=True)
                finally:
                    response.close()

        # utf-8-sig: a byte-order mark at the front would make the JSON
        # reader call a perfectly good settings file unreadable.
        return _guarded(ask, UNREACHABLE).decode("utf-8-sig", errors="replace")

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
        url = self._file_url(repo_id, revision, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        have = target.stat().st_size if target.is_file() else 0

        def attempt(client: httpx.Client, start: int) -> bool:
            """Ask for the file from `start`. False: ask again, from the beginning."""
            if should_stop():
                raise Stopped()
            # identity: a compressed answer's byte positions are not the
            # file's, and a resume would splice two different encodings.
            headers = {"Accept-Encoding": "identity"}
            if start:
                headers["Range"] = f"bytes={start}-"
            response = self._follow(client, url, headers)
            try:
                status = response.status_code
                if start and status == httpx.codes.REQUESTED_RANGE_NOT_SATISFIABLE:
                    # Nothing lies beyond what is there: it is whole already.
                    # The caller's check confirms.
                    return True
                partial = status == httpx.codes.PARTIAL_CONTENT
                if partial and start and _range_start(response) != start:
                    return False
                if (partial and not start) or status not in (
                    httpx.codes.OK,
                    httpx.codes.PARTIAL_CONTENT,
                ):
                    raise self._refused(response, repo_id, revision=revision, path=path)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise HubError("bad_answer", BAD_ANSWER, status)
                if have and not partial:
                    # The server ignored the range: the file starts again.
                    on_bytes(-have)
                with target.open("ab" if partial else "wb") as out:
                    for chunk in response.iter_raw(CHUNK_BYTES):
                        out.write(chunk)
                        on_bytes(len(chunk))
                        if should_stop():
                            raise Stopped()
                return True
            finally:
                response.close()

        def download() -> None:
            # From where the file stands; then, only if the server answers
            # that with a range from somewhere else, once from the start.
            with self._client(CHUNK_WAIT_S) as client:
                if not attempt(client, have) and not attempt(client, 0):
                    raise HubError("bad_answer", BAD_ANSWER)

        # When the line drops, what arrived stays in `target`: the next call resumes.
        _guarded(download, UNREACHABLE)

    def _client(self, read_s: float) -> httpx.Client:
        return httpx.Client(
            transport=self._transport,
            timeout=httpx.Timeout(read_s, connect=CONNECT_S),
            follow_redirects=False,
            # A proxy named in the environment would be used in place of a
            # test's transport, and the test would go to the network.
            trust_env=self._transport is None,
        )

    def _headers(self, url: httpx.URL, extra: Mapping[str, str]) -> dict[str, str]:
        headers = dict(extra)
        if self._token and _same_place(url, self._base):
            if not self._token.isascii() or not self._token.isprintable() or " " in self._token:
                # It could not be a header at all, and the library's own
                # complaint about it would quote it.
                raise HubError("no_access", PASTE_AGAIN)
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _follow(
        self, client: httpx.Client, url: httpx.URL, headers: Mapping[str, str]
    ) -> httpx.Response:
        """The answer to a GET, redirects followed by hand; the caller closes it."""
        return _follow(client, url, lambda place: self._headers(place, headers))

    def _file_url(self, repo_id: str, revision: str, path: str) -> httpx.URL:
        # Imported here: files.py builds on this module's RepoFile.
        from framefound.local_models import files

        name = _model_name(repo_id)
        commit = _commit(repo_id, revision)
        if not files.safe_path(path):
            raise HubError("not_found", f"{_shown(path)} is not a file FrameFound will ask for.")
        return httpx.URL(f"{self._base}/{name}/resolve/{commit}/{quote(path, safe='/')}")

    def _refused(
        self,
        response: httpx.Response,
        repo_id: str,
        *,
        revision: str | None = None,
        path: str | None = None,
    ) -> HubError:
        status = response.status_code
        code = response.headers.get("x-error-code", "")
        problem = _problem(status, code, has_token=bool(self._token))
        if problem == "not_found":
            if code == "RepoNotFound" or (revision is None and path is None):
                sentence = no_such_model(repo_id)
            elif path is not None and code != "RevisionNotFound":
                sentence = no_such_file(repo_id, path)
            else:
                sentence = no_such_build(repo_id)
        elif problem == "needs_token":
            sentence = accept_terms_first(repo_id)
        elif problem == "no_access" and status == httpx.codes.UNAUTHORIZED:
            sentence = NOT_TAKEN
        else:
            sentence = {
                "no_access": NO_ACCESS,
                "rate_limited": RATE_LIMITED,
                "unreachable": NOT_ANSWERING,
            }.get(problem, BAD_ANSWER)
        return HubError(problem, sentence, status)


FetchJson = Callable[[str], Mapping[str, Any]]


def fetch_json(
    url: str, *, transport: httpx.BaseTransport | None = None, timeout_s: float = 15.0
) -> Mapping[str, Any]:
    """A public JSON document (a package index's "newest version"); `HubError` on failure.

    No token and no header of FrameFound's is sent. Redirects are followed,
    over https only: the answer decides which version of a program gets
    installed, so it is not taken from a plain-text hop.
    """
    asked = httpx.URL(url)
    host = _shown(asked.host)

    def ask() -> Mapping[str, Any]:
        if asked.scheme != "https":
            raise HubError("unreachable", f"{host} could not be asked safely (not https).")
        with httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(timeout_s),
            follow_redirects=False,
            trust_env=transport is None,
        ) as client:
            response = _follow(client, asked, lambda _place: {"Accept": "application/json"})
            try:
                status = response.status_code
                if status != httpx.codes.OK:
                    raise HubError(
                        "unreachable", f"{host} did not answer ({status}). Try again later.", status
                    )
                return _json(_body(response, MAX_JSON_BYTES))
            finally:
                response.close()

    return _guarded(
        ask, f"{host} could not be reached. Check this machine's internet connection and try again."
    )


def _guarded[T](work: Callable[[], T], sentence: str) -> T:
    """Run `work`; a failure of the network becomes a `HubError` that says only `sentence`.

    The new error is raised after the handler has finished, not inside it,
    so the network library's own error is not attached to it in any way: its
    text can hold a signed storage address and its request holds the token,
    and a log line that prints an error's whole history would print both.
    """
    try:
        return work()
    except httpx.HTTPError:
        pass
    raise HubError("unreachable", sentence)


def _follow(
    client: httpx.Client, url: httpx.URL, headers_for: Callable[[httpx.URL], dict[str, str]]
) -> httpx.Response:
    """GET `url`, following redirects by hand; the headers are worked out again for each hop.

    That is the whole reason it is by hand: the header that carries the
    token must be decided per address, not copied to wherever a redirect
    points.
    """
    for _hop in range(MAX_REDIRECTS + 1):
        request = client.build_request("GET", url, headers=headers_for(url))
        response = client.send(request, stream=True)
        if not response.has_redirect_location:
            return response
        location = response.headers["location"]
        response.close()
        try:
            url = url.join(location)
        except httpx.InvalidURL:
            url = httpx.URL("")
        if url.scheme != "https":
            raise HubError("bad_answer", BAD_ANSWER, response.status_code)
    raise HubError("bad_answer", BAD_ANSWER)


def _problem(status: int, code: str, *, has_token: bool) -> Problem:
    """What a refusal means.

    `code` is Hugging Face's `X-Error-Code` header. It matters for one case:
    asked without a token about a name it does not have, Hugging Face
    answers 401 (so that a private model's existence is not given away), and
    only the header tells that from a model that wants an account. The
    header's values are recalled from Hugging Face's own client, not seen in
    an answer; without the header the status decides.
    """
    if code in ("RepoNotFound", "RevisionNotFound", "EntryNotFound"):
        return "not_found"
    if code == "GatedRepo" or status in (httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN):
        return "no_access" if has_token else "needs_token"
    if status == httpx.codes.NOT_FOUND:
        return "not_found"
    if status == httpx.codes.TOO_MANY_REQUESTS:
        return "rate_limited"
    if status >= httpx.codes.INTERNAL_SERVER_ERROR:
        return "unreachable"
    return "bad_answer"


def _model_name(repo_id: str) -> str:
    """`repo_id` if it can be part of an address; else it is not asked for at all.

    What a model says it was built from is free text written by whoever
    published it, and the chain is walked by asking for each name in turn.
    """
    publisher, slash, model = repo_id.partition("/")
    if not slash or not policy.NAME.fullmatch(publisher) or not policy.NAME.fullmatch(model):
        raise HubError("not_found", f"Hugging Face has no model called {_shown(repo_id)}.")
    return repo_id


def _commit(repo_id: str, revision: str) -> str:
    commit = _hex(revision, _COMMIT_LENGTH)
    if commit is None:
        raise HubError("not_found", f"That is not a build of {_shown(repo_id)}.")
    return commit


def _same_place(url: httpx.URL, base: httpx.URL) -> bool:
    return (url.scheme, url.host, url.port) == (base.scheme, base.host, base.port)


def _moved_to(response: httpx.Response) -> str | None:
    """The model name a redirect of an info request points at, if it is one."""
    try:
        parts = httpx.URL(response.headers.get("location", "")).path.split("/")
    except httpx.InvalidURL:
        return None
    # "/api/models/<publisher>/<name>", with or without more after it.
    if parts[:3] != ["", "api", "models"]:
        return None
    try:
        return policy.clean_repo_id("/".join(parts[3:5]))
    except ValueError:
        return None


def _range_start(response: httpx.Response) -> int | None:
    """Where a partial answer says it starts (`Content-Range: bytes 100-199/200`)."""
    unit, _, span = response.headers.get("content-range", "").partition(" ")
    first = span.partition("-")[0]
    return int(first) if unit == "bytes" and first.isascii() and first.isdigit() else None


def _body(response: httpx.Response, limit: int, *, cut: bool = False) -> bytes:
    """Up to `limit` bytes of the answer. More than that is cut off, or is a bad answer."""
    data = bytearray()
    for chunk in response.iter_bytes():
        data += chunk
        if len(data) > limit:
            if cut:
                return bytes(data[:limit])
            raise HubError("bad_answer", BAD_ANSWER, response.status_code)
    return bytes(data)


def _json(data: bytes) -> Mapping[str, Any]:
    try:
        payload = json.loads(data)
    except (ValueError, RecursionError):
        payload = None
    if not isinstance(payload, dict):
        raise _unreadable()
    return payload


def _unreadable() -> HubError:
    return HubError("bad_answer", BAD_ANSWER)


def _mapping(value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _unreadable()
    return value


def _gguf(value: object) -> Mapping[str, Any] | None:
    """The page's `gguf` object as read, minus its chat template; None when it is not one.

    Lenient where the rest of the page is strict, because nothing is decided
    on it: a shape not seen before costs the page a line of information, not
    the model its check. The chat template is dropped because it is the
    largest value by far and nothing shows it (llama.cpp reads its own copy
    from the file).
    """
    if not isinstance(value, Mapping):
        return None
    # A plain dict, not a read-only view: a view cannot be copied or turned
    # into JSON, and whatever later serialises a check would trip on it.
    return {key: item for key, item in value.items() if key != "chat_template"}


def _bases(declared: object, tags: list[str]) -> tuple[str, ...]:
    """Every model the page names as what this one was made from.

    From the card and from the tags, whatever word the tag puts in the
    middle: the rule about blocked publishers has to see every name, so a
    shape that cannot be read is an error, never "no base".

    This is free text written by whoever published the model, and it ends up
    in the sentence that refuses or allows it. A "name" that is not plain
    text (a line break, a character that reverses the line) or is longer
    than any name could be is such a shape: an error here, not something to
    tidy and pass on.
    """
    if declared is None:
        named: list[str] = []
    elif isinstance(declared, str):
        named = [declared]
    elif isinstance(declared, list) and all(isinstance(name, str) for name in declared):
        named = list(declared)
    else:
        raise _unreadable()
    for tag in tags:
        if tag.startswith("base_model:"):
            rest = tag[len("base_model:") :]
            relation, colon, name = rest.partition(":")
            named.append(name if colon and relation in _RELATIONS else rest)

    bases: list[str] = []
    seen: set[str] = set()
    for name in named:
        try:
            # A card may give an address where a name belongs.
            base = policy.clean_repo_id(name)
        except ValueError:
            # Not a model's name (a word, a path on somebody's machine). Kept
            # as written so the check can say what the page named.
            base = name.strip()
        if base and not _plain(base):
            raise _unreadable()
        if base and base.casefold() not in seen:
            seen.add(base.casefold())
            bases.append(base)
    return tuple(bases)


def _file(entry: object) -> RepoFile:
    if not isinstance(entry, Mapping):
        raise _unreadable()
    path = entry.get("rfilename")
    lfs = entry.get("lfs")
    if not isinstance(path, str) or not path or (lfs is not None and not isinstance(lfs, Mapping)):
        raise _unreadable()
    # A large file is stored outside git: its size and checksum are in `lfs`,
    # and its `blobId` is the id of the small pointer left in git, not of the
    # bytes that will arrive. Keeping it would fail every such download.
    size = _count(lfs.get("size")) if lfs else None
    if size is None:
        size = _count(entry.get("size"))
    if size is None:
        raise _unreadable()
    return RepoFile(
        path=path,
        size=size,
        sha256=_hex(lfs.get("sha256"), _SHA256_LENGTH) if lfs else None,
        blob_id=None if lfs else _hex(entry.get("blobId"), _COMMIT_LENGTH),
    )


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _hex(value: object, length: int) -> str | None:
    if not isinstance(value, str) or len(value) != length:
        return None
    text = value.lower()
    return text if all(letter in "0123456789abcdef" for letter in text) else None


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _licence(value: object) -> str:
    if isinstance(value, list):
        return next((_text(item) for item in value if _text(item)), "")
    return _text(value)


def _link(value: object) -> str | None:
    # Shown as a link on the page, so only ever a web address.
    text = _text(value)
    return text if text.startswith(("https://", "http://")) and _plain(text) else None


def _when(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _plain(text: str) -> bool:
    return bool(text.strip()) and text.isprintable() and len(text) <= _LONGEST_NAME


def _shown(text: str) -> str:
    """Somebody else's text made fit to put in a sentence."""
    return "".join(letter for letter in text if letter.isprintable())[:120]
