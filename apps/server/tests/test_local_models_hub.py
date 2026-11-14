"""Hugging Face behind one interface (local_models/hub.py), and its stand-in (fake.FakeHub).

Nothing here reaches a network: the real client is driven through
`httpx.MockTransport`, and a fixture breaks every socket for the length of
each test to prove it. The model pages are the fixtures in
`local_models/demo_data`. No blocked publisher's model is named: the one
fixture that needs one has the publisher filled in from
`policy.DEFAULT_BLOCKED[0]` when it is read.
"""

import json
import re
import socket
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from framefound.local_models import fake, files, hub, policy
from framefound.local_models.hub import HttpHub, HubError, Stopped

FIRST = "mlx-community/gemma-4-e4b-it-4bit"
BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"
TOKEN = "not-a-real-token"
BLOCKED = policy.DEFAULT_BLOCKED[0]

Answer = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class Site:
    """Stands where the network would be: remembers each request, answers from a function."""

    def __init__(self, answer: Answer) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def hub(self, token: str | None = None) -> HttpHub:
        return HttpHub(token, transport=httpx.MockTransport(self))


def page(name: str = "gemma-4-e4b-it-4bit") -> dict[str, Any]:
    return fake.fixture(name)


def bad_answer(payload: Any) -> HubError:
    with pytest.raises(HubError) as caught:
        hub.parse_info(payload)
    assert caught.value.problem == "bad_answer"
    return caught.value


# --- reading a model's page ----------------------------------------------------


def test_the_first_models_page_reads_as_it_was_recorded() -> None:
    info = hub.parse_info(page())

    assert info.repo_id == FIRST
    assert info.revision == BUILD
    assert info.publisher == "mlx-community"
    assert (info.private, info.disabled, info.gated) == (False, False, "")
    # Given as a string on this card, and again in two tags: one base, once.
    assert info.bases == ("google/gemma-4-E4B-it",)
    assert (info.licence, info.licence_name, info.licence_url) == ("gemma", "", None)
    assert (info.library, info.pipeline) == ("mlx", "image-text-to-text")
    assert "mlx-vlm" in info.tags
    assert info.custom_code is False
    assert info.modified_at == datetime(2026, 7, 6, tzinfo=UTC)

    by_path = {file.path: file for file in info.files}
    assert len(by_path) == 10
    weights = by_path["model.safetensors"]
    assert weights.size == 5_146_800_534
    assert weights.sha256 == "932b8271fc3fe65adcc78b96c10c6268bbfb13e8f67d1358727c0d6ee97e1eff"
    assert weights.blob_id is None
    config = by_path["config.json"]
    assert (config.size, config.sha256) == (6628, None)
    assert config.blob_id == "4ee08502c4f98810dd43800ec849bd69f94adc98"


def test_a_base_given_as_a_list_reads_like_one_given_as_a_string() -> None:
    info = hub.parse_info(page("gemma-4-E4B-it"))

    assert info.publisher == "google"
    assert info.bases == ("google/gemma-4-E4B",)
    assert info.licence == "apache-2.0"
    assert info.licence_url == "https://www.apache.org/licenses/LICENSE-2.0"
    assert hub.parse_info(page("gemma-4-E4B")).bases == ()


def test_a_gated_model_under_the_makers_own_licence_says_so() -> None:
    info = hub.parse_info(page("gated-blocked-base"))

    assert info.gated == "auto"
    assert (info.licence, info.licence_name) == ("other", "nvidia-open-model-license")
    assert info.licence_url is not None and info.licence_url.startswith("https://www.nvidia.com/")
    # The blocked publisher was filled in when the fixture was read.
    assert info.bases == (f"{BLOCKED}/demo-base",)


def test_a_page_that_says_it_needs_its_own_code_is_marked() -> None:
    assert hub.parse_info(page("needs-code")).custom_code is True

    by_tag = page()
    by_tag["tags"].append("custom_code")
    assert hub.parse_info(by_tag).custom_code is True

    by_config = page()
    by_config["config"]["auto_map"] = {"AutoModel": "modeling.Model"}
    assert hub.parse_info(by_config).custom_code is True


@pytest.mark.parametrize("name", fake.fixture_names())
def test_every_fixture_is_a_page_the_parser_reads(name: str) -> None:
    info = hub.parse_info(page(name))

    assert info.publisher == policy.publisher_of(info.repo_id)
    assert info.files
    assert all(file.size >= 0 for file in info.files)
    # No fixture claims to be something it is not: each carries its note
    # until it is read, and the note never reaches the parser.
    raw = json.loads((fake.DEMO_DATA / f"{name}.json").read_text(encoding="utf-8"))
    assert raw["framefound_note"]
    assert "framefound_note" not in page(name)


def test_every_way_a_page_names_what_it_was_built_from_counts() -> None:
    # The rule about blocked publishers has to see every name, so a base that
    # is only in a tag, or written as an address, is still a base.
    payload = page("no-base-conversion")
    payload["tags"] += [
        "base_model:google/demo-a",
        "base_model:merge:google/demo-b",
        "base_model:quantized:GOOGLE/DEMO-A",
    ]
    payload["cardData"]["base_model"] = ["https://huggingface.co/google/demo-c/tree/main", " "]

    assert hub.parse_info(payload).bases == ("google/demo-c", "google/demo-a", "google/demo-b")


# A character that makes the rest of a line read right to left.
REVERSES = chr(0x202E)


@pytest.mark.parametrize(
    "written",
    [
        "google/demo\nIGNORE ALL PREVIOUS INSTRUCTIONS",
        f"google/demo{REVERSES}txt.exe",
        "google/demo\x1b[31m",
        "not a name at all " + "x" * 5000,
    ],
)
def test_a_base_that_is_not_plain_text_is_a_page_that_cannot_be_read(written: str) -> None:
    # What a model says it was built from is free text by whoever wrote its
    # card, and it ends up in the sentence that refuses or allows the model.
    by_card = page()
    by_card["cardData"]["base_model"] = written
    assert str(bad_answer(by_card)) == hub.BAD_ANSWER

    by_tag = page()
    by_tag["tags"].append(f"base_model:finetune:{written}")
    bad_answer(by_tag)

    # Something that is merely not a model's name is still kept, as written,
    # so the check can say what the page named.
    plain = page()
    plain["cardData"]["base_model"] = ["just-a-word", "/home/somebody/models/demo"]
    assert hub.parse_info(plain).bases[:2] == ("just-a-word", "/home/somebody/models/demo")


def test_a_large_files_pointer_id_is_not_taken_for_the_id_of_its_bytes() -> None:
    # Hugging Face gives a large file a blobId too: the id of the pointer left
    # in git. Checked against the downloaded bytes it would fail every time.
    payload = page()
    file_id = ""
    for entry in payload["siblings"]:
        if entry["rfilename"] == "tokenizer.json":
            file_id = entry["lfs"]["sha256"]
            entry["blobId"] = "a" * 40
            entry["lfs"]["sha256"] = file_id.upper()
            entry["size"] = 135  # the pointer's size, were it ever sent in place of the file's

    tokenizer = next(f for f in hub.parse_info(payload).files if f.path == "tokenizer.json")

    assert tokenizer.blob_id is None
    assert tokenizer.size == 32_169_626
    # The bytes' own id, from `lfs`, in lower case. (Compared with the page's
    # value rather than written out: a 64-character hex string beside the word
    # "tokenizer" reads to the secrets scan as a leaked token.)
    assert len(file_id) == 64 and file_id == file_id.lower()
    assert tokenizer.sha256 == file_id


@pytest.mark.parametrize(("said", "read"), [(True, "yes"), ("manual", "manual"), (None, "")])
def test_gated_in_a_form_not_seen_before_still_reads_as_gated(said: Any, read: str) -> None:
    payload = page()
    payload["gated"] = said

    assert hub.parse_info(payload).gated == read


def test_a_licence_link_that_is_not_a_web_address_is_dropped() -> None:
    # It is drawn as a link on the page.
    payload = page("gated-blocked-base")
    payload["cardData"]["license_link"] = "javascript:alert(1)"

    assert hub.parse_info(payload).licence_url is None


def _without(key: str) -> dict[str, Any]:
    payload = page()
    del payload[key]
    return payload


def _with(**changes: Any) -> dict[str, Any]:
    payload = page()
    payload.update(changes)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        ["not", "a", "page"],
        _without("id"),
        _without("sha"),
        _without("siblings"),
        _with(sha="main"),
        _with(tags="mlx"),
        _with(tags=["mlx", 7]),
        _with(cardData=["base_model"]),
        _with(cardData={"base_model": {"name": "google/gemma-4-E4B-it"}}),
        _with(cardData={"base_model": ["google/gemma-4-E4B-it", 3]}),
        _with(config="none"),
        _with(siblings=[{"rfilename": "model.safetensors"}]),
        _with(siblings=[{"rfilename": "model.safetensors", "size": "big"}]),
        _with(siblings=[{"size": 3}]),
        _with(siblings=["model.safetensors"]),
    ],
)
def test_a_page_that_cannot_be_read_for_certain_is_a_bad_answer(payload: Any) -> None:
    # Each of these could hide a base model or a file. Guessing is not allowed.
    error = bad_answer(payload)

    assert str(error) == hub.BAD_ANSWER


# --- asking the real site ------------------------------------------------------


def test_a_models_page_is_asked_for_with_its_files_and_nothing_identifying() -> None:
    site = Site(lambda _request: httpx.Response(200, json=page()))

    info = site.hub().info(FIRST)

    assert info.repo_id == FIRST
    (request,) = site.requests
    assert str(request.url) == f"https://huggingface.co/api/models/{FIRST}?blobs=true"
    assert "authorization" not in request.headers
    assert "cookie" not in request.headers
    assert request.headers["user-agent"].startswith("python-httpx/")


def test_a_pasted_link_is_reduced_to_its_name() -> None:
    site = Site(lambda _request: httpx.Response(200, json=page()))
    pasted = f"https://huggingface.co/{FIRST}/blob/main/config.json?download=true"

    site.hub().info(policy.clean_repo_id(pasted))

    assert site.requests[0].url.path == f"/api/models/{FIRST}"


def test_a_build_is_asked_for_by_its_commit_and_the_token_goes_with_it() -> None:
    site = Site(lambda _request: httpx.Response(200, json=page()))

    site.hub(f"  {TOKEN}\n").info(FIRST, BUILD)

    (request,) = site.requests
    assert request.url.path == f"/api/models/{FIRST}/revision/{BUILD}"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


def test_a_name_that_now_points_elsewhere_is_not_followed() -> None:
    # Following it would judge another publisher's model under this name.
    site = Site(
        lambda _request: httpx.Response(
            307, headers={"location": "/api/models/example-lab/renamed-model?blobs=true"}
        )
    )

    with pytest.raises(HubError) as caught:
        site.hub().info("google/old-name")

    assert len(site.requests) == 1
    assert caught.value.problem == "moved"
    assert caught.value.moved_to == "example-lab/renamed-model"
    assert str(caught.value) == (
        "This name now points to example-lab/renamed-model on Hugging Face. "
        "Check that name instead."
    )

    nowhere = Site(lambda _request: httpx.Response(302, headers={"location": "https://[oops"}))
    with pytest.raises(HubError) as caught:
        nowhere.hub().info("google/old-name")
    assert caught.value.moved_to is None
    assert str(caught.value) == hub.MOVED


def test_the_token_is_never_sent_to_a_redirect_target(tmp_path: Path) -> None:
    body = b"the bytes of the model"

    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "huggingface.co" and request.url.path.startswith("/api/resolve"):
            return httpx.Response(302, headers={"location": "https://cdn.example/blob?sig=abc"})
        if request.url.host == "huggingface.co":
            # Relative: still Hugging Face, so the token still belongs there.
            return httpx.Response(307, headers={"location": "/api/resolve-cache/model"})
        return httpx.Response(200, stream=httpx.ByteStream(body))

    site = Site(answer)
    target = tmp_path / "model.safetensors.partial"

    site.hub(TOKEN).fetch(FIRST, BUILD, "model.safetensors", target)

    assert target.read_bytes() == body
    first, second, storage = site.requests
    assert first.url.path == f"/{FIRST}/resolve/{BUILD}/model.safetensors"
    assert first.headers["authorization"] == f"Bearer {TOKEN}"
    assert second.headers["authorization"] == f"Bearer {TOKEN}"
    assert storage.url.host == "cdn.example"
    assert "authorization" not in storage.headers
    assert TOKEN not in str(storage.url) + repr(dict(storage.headers))

    # The same for a settings file read during a check.
    site.requests.clear()
    assert site.hub(TOKEN).read_text(FIRST, BUILD, "config.json") == body.decode()
    assert [("authorization" in r.headers) for r in site.requests] == [True, True, False]


def test_a_port_or_scheme_that_is_not_hugging_faces_gets_no_token(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.port is None:
            return httpx.Response(302, headers={"location": "https://huggingface.co:8443/x"})
        return httpx.Response(200, stream=httpx.ByteStream(b"x"))

    site = Site(answer)
    site.hub(TOKEN).fetch(FIRST, BUILD, "config.json", tmp_path / "config.json")

    assert "authorization" not in site.requests[1].headers


def test_a_redirect_off_https_or_without_end_is_refused(tmp_path: Path) -> None:
    plain = Site(lambda _r: httpx.Response(302, headers={"location": "http://cdn.example/blob"}))
    with pytest.raises(HubError) as caught:
        plain.hub(TOKEN).fetch(FIRST, BUILD, "model.safetensors", tmp_path / "a")
    assert caught.value.problem == "bad_answer"
    assert len(plain.requests) == 1

    circle = Site(lambda r: httpx.Response(302, headers={"location": f"{r.url.path}x"}))
    with pytest.raises(HubError) as caught:
        circle.hub().read_text(FIRST, BUILD, "config.json")
    assert caught.value.problem == "bad_answer"
    assert len(circle.requests) == hub.MAX_REDIRECTS + 1


def test_a_download_resumes_from_what_is_already_there(tmp_path: Path) -> None:
    body = bytes(range(200))
    target = tmp_path / "model.safetensors.partial"
    target.write_bytes(body[:60])

    def answer(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == "bytes=60-"
        return httpx.Response(
            206, stream=httpx.ByteStream(body[60:]), headers={"content-range": "bytes 60-199/200"}
        )

    site = Site(answer)
    counted: list[int] = []
    site.hub().fetch(FIRST, BUILD, "model.safetensors", target, on_bytes=counted.append)

    assert target.read_bytes() == body
    assert sum(counted) == 140
    # Compressed, the positions asked for would not be positions in the file.
    assert site.requests[0].headers["accept-encoding"] == "identity"


def test_a_server_that_ignores_the_range_starts_the_file_again(tmp_path: Path) -> None:
    body = bytes(range(200))
    target = tmp_path / "model.safetensors.partial"
    target.write_bytes(b"\xff" * 60)
    site = Site(lambda _request: httpx.Response(200, stream=httpx.ByteStream(body)))
    counted: list[int] = []

    site.hub().fetch(FIRST, BUILD, "model.safetensors", target, on_bytes=counted.append)

    assert target.read_bytes() == body
    # What was there was thrown away, and the caller's running total is told so.
    assert counted[0] == -60
    assert sum(counted) == len(body) - 60


def test_a_range_answered_from_the_wrong_place_is_asked_for_again_whole(tmp_path: Path) -> None:
    body = bytes(range(200))
    target = tmp_path / "model.safetensors.partial"
    target.write_bytes(body[:60])

    def answer(request: httpx.Request) -> httpx.Response:
        if "range" in request.headers:
            return httpx.Response(
                206,
                stream=httpx.ByteStream(body[100:]),
                headers={"content-range": "bytes 100-199/200"},
            )
        return httpx.Response(200, stream=httpx.ByteStream(body))

    site = Site(answer)
    site.hub().fetch(FIRST, BUILD, "model.safetensors", target)

    assert target.read_bytes() == body
    assert ["range" in request.headers for request in site.requests] == [True, False]

    # And a server that answers every question with a slice is given up on.
    stubborn = Site(
        lambda _r: httpx.Response(
            206, stream=httpx.ByteStream(b"x"), headers={"content-range": "?"}
        )
    )
    with pytest.raises(HubError) as caught:
        stubborn.hub().fetch(FIRST, BUILD, "model.safetensors", target)
    assert caught.value.problem == "bad_answer"
    assert target.read_bytes() == body


def test_a_partial_that_is_already_whole_is_left_alone(tmp_path: Path) -> None:
    target = tmp_path / "config.json.partial"
    target.write_bytes(b"all of it")
    site = Site(lambda _request: httpx.Response(416))
    counted: list[int] = []

    site.hub().fetch(FIRST, BUILD, "config.json", target, on_bytes=counted.append)

    assert target.read_bytes() == b"all of it"
    assert counted == []


def test_a_download_can_be_stopped_and_keeps_what_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "CHUNK_BYTES", 8)
    body = bytes(range(64))
    target = tmp_path / "deep" / "model.safetensors.partial"

    def answer(request: httpx.Request) -> httpx.Response:
        start = int(request.headers.get("range", "bytes=0-")[6:-1])
        status = 206 if start else 200
        headers = {"content-range": f"bytes {start}-63/64"} if start else {}
        return httpx.Response(status, stream=httpx.ByteStream(body[start:]), headers=headers)

    site = Site(answer)
    counted: list[int] = []
    with pytest.raises(Stopped):
        site.hub().fetch(
            FIRST,
            BUILD,
            "model.safetensors",
            target,
            on_bytes=counted.append,
            should_stop=lambda: sum(counted) >= 24,
        )
    assert target.read_bytes() == body[:24]

    # Asked to stop before it starts, it asks for nothing.
    with pytest.raises(Stopped):
        site.hub().fetch(FIRST, BUILD, "model.safetensors", target, should_stop=lambda: True)
    assert len(site.requests) == 1

    site.hub().fetch(FIRST, BUILD, "model.safetensors", target)
    assert target.read_bytes() == body
    assert site.requests[-1].headers["range"] == "bytes=24-"


class Dropping(httpx.SyncByteStream):
    """A body that arrives in pieces and then the line goes."""

    def __init__(self, pieces: list[bytes], secret: str) -> None:
        self.pieces = pieces
        self.secret = secret

    def __iter__(self) -> Iterator[bytes]:
        yield from self.pieces
        raise httpx.ReadError(
            f"connection reset reading https://cdn.example/blob?sig={self.secret}"
        )


def test_a_connection_that_drops_part_way_keeps_what_arrived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub, "CHUNK_BYTES", 8)
    target = tmp_path / "model.safetensors.partial"
    site = Site(
        lambda _r: httpx.Response(200, stream=Dropping([b"12345678", b"abcdefgh"], "s3cr3t"))
    )

    with pytest.raises(HubError) as caught:
        site.hub(TOKEN).fetch(FIRST, BUILD, "model.safetensors", target)

    assert caught.value.problem == "unreachable"
    assert str(caught.value) == hub.UNREACHABLE
    assert target.read_bytes() == b"12345678abcdefgh"
    # Nothing of the library's own error rides along: its text had the signed address.
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_a_compressed_answer_is_not_written_as_the_file(tmp_path: Path) -> None:
    target = tmp_path / "config.json.partial"
    site = Site(
        lambda _r: httpx.Response(
            200, stream=httpx.ByteStream(b"\x1f\x8b"), headers={"content-encoding": "gzip"}
        )
    )

    with pytest.raises(HubError) as caught:
        site.hub().fetch(FIRST, BUILD, "config.json", target)

    assert caught.value.problem == "bad_answer"
    assert not target.exists()


@pytest.mark.parametrize(
    ("status", "token", "problem", "sentence"),
    [
        (404, None, "not_found", f"Hugging Face has no model called {FIRST}. Check the spelling."),
        (
            401,
            None,
            "needs_token",
            "mlx-community asks you to accept their terms before downloading. Do that on the "
            "model's page at Hugging Face, then paste a read token under Hugging Face account, "
            "below, and check again.",
        ),
        (403, None, "needs_token", None),
        (
            # Terms never accepted, or a token that no longer works: both ways out are said.
            401,
            TOKEN,
            "no_access",
            "Hugging Face would not let this token read the model. Check that you accepted the "
            "terms on the model's page with the same account. If you did, the token itself may "
            "have stopped working: paste a new one under Hugging Face account.",
        ),
        (403, TOKEN, "no_access", hub.NO_ACCESS),
        (429, None, "rate_limited", hub.RATE_LIMITED),
        (500, None, "unreachable", hub.NOT_ANSWERING),
        (503, TOKEN, "unreachable", hub.NOT_ANSWERING),
        (418, None, "bad_answer", hub.BAD_ANSWER),
    ],
)
def test_each_refusal_is_its_own_problem_and_sentence(
    status: int, token: str | None, problem: str, sentence: str | None
) -> None:
    site = Site(lambda _request: httpx.Response(status, json={"error": "no"}))

    with pytest.raises(HubError) as caught:
        site.hub(token).info(FIRST)

    assert (caught.value.problem, caught.value.status) == (problem, status)
    if sentence is not None:
        assert str(caught.value) == sentence


def test_a_missing_file_or_build_is_named_as_what_it_is(tmp_path: Path) -> None:
    gone = Site(lambda _request: httpx.Response(404))

    with pytest.raises(HubError) as caught:
        gone.hub().read_text(FIRST, BUILD, "config.json")
    assert str(caught.value) == (
        f"Hugging Face has no file called config.json in this build of {FIRST}."
    )
    with pytest.raises(HubError) as caught:
        gone.hub().info(FIRST, BUILD)
    assert str(caught.value) == f"Hugging Face no longer has this build of {FIRST}."


def test_a_name_hugging_face_does_not_have_is_not_taken_for_one_that_wants_an_account() -> None:
    # Without a token Hugging Face answers 401 for a name it does not have,
    # and says which in a header. "Paste a token" would be the wrong advice
    # for a spelling mistake.
    site = Site(lambda _r: httpx.Response(401, headers={"x-error-code": "RepoNotFound"}))
    with pytest.raises(HubError) as caught:
        site.hub().info("google/no-such-model")
    assert caught.value.problem == "not_found"
    assert str(caught.value) == (
        "Hugging Face has no model called google/no-such-model. Check the spelling."
    )

    gated = Site(lambda _r: httpx.Response(404, headers={"x-error-code": "GatedRepo"}))
    with pytest.raises(HubError) as caught:
        gated.hub().read_text(FIRST, BUILD, "config.json")
    assert caught.value.problem == "needs_token"
    with pytest.raises(HubError) as caught:
        gated.hub(TOKEN).read_text(FIRST, BUILD, "config.json")
    assert caught.value.problem == "no_access"


def _leaky(request: httpx.Request) -> httpx.Response:
    said = request.headers.get("authorization", "")
    raise httpx.ConnectError(f"could not connect to {request.url} with {said}")


def _slow(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout(f"timed out with {request.headers.get('authorization', '')}")


@pytest.mark.parametrize(
    "answer",
    [
        _leaky,
        _slow,
        lambda _r: httpx.Response(401, json={"error": f"bad credentials: {TOKEN}"}),
        lambda _r: httpx.Response(503, text=f"upstream said {TOKEN}"),
        lambda _r: httpx.Response(200, stream=httpx.ByteStream(f"<html>{TOKEN}</html>".encode())),
        lambda _r: httpx.Response(302, headers={"location": f"http://cdn.example/?t={TOKEN}"}),
        lambda _r: httpx.Response(302, headers={"location": f"/api/models/a/b?t={TOKEN}"}),
    ],
)
def test_an_error_never_contains_the_token(answer: Answer, tmp_path: Path) -> None:
    site = Site(answer)
    asks: list[Callable[[], object]] = [
        lambda: site.hub(TOKEN).info(FIRST),
        lambda: site.hub(TOKEN).info(FIRST, BUILD),
        lambda: site.hub(TOKEN).read_text(FIRST, BUILD, "config.json"),
        lambda: site.hub(TOKEN).fetch(FIRST, BUILD, "model.safetensors", tmp_path / "m.partial"),
    ]

    errors: list[HubError] = []
    for ask in asks:
        # Not every answer is an error for every question: a file is whatever
        # bytes come back with a 200.
        try:
            ask()
        except HubError as error:
            errors.append(error)

    assert errors, "nothing failed, so nothing was proved"
    assert all(request.headers["authorization"] for request in site.requests)
    for error in errors:
        told = f"{error} {error!r} {error.args} {error.moved_to} {vars(error)}"
        assert TOKEN not in told
        assert error.__cause__ is None and error.__context__ is None


def test_a_token_that_could_not_be_a_header_is_refused_without_being_quoted() -> None:
    site = Site(lambda _request: httpx.Response(200, json=page()))
    pasted = "not a real\ttoken — with a dash"

    with pytest.raises(HubError) as caught:
        site.hub(pasted).info(FIRST)

    assert caught.value.problem == "no_access"
    assert "token" in str(caught.value) and pasted not in str(caught.value)
    assert site.requests == []


@pytest.mark.parametrize(
    ("repo_id", "revision", "path"),
    [
        ("gemma-4-e4b-it-4bit", BUILD, "config.json"),
        ("../../etc/passwd", BUILD, "config.json"),
        ("google/demo?x=1", BUILD, "config.json"),
        ("https://example.com/a/b", BUILD, "config.json"),
        (FIRST, "main", "config.json"),
        (FIRST, f"{BUILD}/../..", "config.json"),
        (FIRST, BUILD, "../config.json"),
        (FIRST, BUILD, "/etc/passwd"),
        (FIRST, BUILD, ".git/config"),
    ],
)
def test_what_could_not_be_part_of_an_address_is_never_asked_for(
    repo_id: str, revision: str, path: str, tmp_path: Path
) -> None:
    # What a model says it was built from is free text, and every name in it
    # is asked about in turn.
    site = Site(lambda _request: httpx.Response(200, json=page()))
    target = tmp_path / "x.partial"
    asks: list[Callable[[], object]] = [
        lambda: site.hub(TOKEN).info(repo_id, revision),
        lambda: site.hub(TOKEN).read_text(repo_id, revision, path),
        lambda: site.hub(TOKEN).fetch(repo_id, revision, path, target),
    ]
    if path != "config.json":
        asks = asks[1:]

    for ask in asks:
        with pytest.raises(HubError) as caught:
            ask()
        assert caught.value.problem == "not_found"
    assert site.requests == []
    assert not target.exists()


def test_a_settings_file_is_read_as_text_and_only_so_much_of_it() -> None:
    text = '{"model_type": "gemma4", "note": "détail"}'
    site = Site(lambda _request: httpx.Response(200, content=b"\xef\xbb\xbf" + text.encode()))

    assert site.hub().read_text(FIRST, BUILD, "config.json") == text
    assert site.requests[0].url.path == f"/{FIRST}/resolve/{BUILD}/config.json"

    assert site.hub().read_text(FIRST, BUILD, "README.md", max_bytes=16) == text[:13]
    assert site.hub().read_text(FIRST, BUILD, "a dir/a file.json") == text
    assert (
        site.requests[-1].url.raw_path == f"/{FIRST}/resolve/{BUILD}/a%20dir/a%20file.json".encode()
    )


@pytest.mark.parametrize("body", [b"<html>Sign in</html>", b'["a", "list"]', b"[" * 200_000])
def test_an_answer_that_is_not_a_models_page_is_a_bad_answer(body: bytes) -> None:
    site = Site(lambda _request: httpx.Response(200, content=body))

    with pytest.raises(HubError) as caught:
        site.hub().info(FIRST)

    assert caught.value.problem == "bad_answer"


def test_a_page_too_large_to_be_a_page_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub, "MAX_JSON_BYTES", 64)
    site = Site(lambda _request: httpx.Response(200, json=page()))

    with pytest.raises(HubError) as caught:
        site.hub().info(FIRST)

    assert caught.value.problem == "bad_answer"


# --- the newest version of a program -------------------------------------------


def test_the_newest_version_is_read_from_a_public_index() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/pypi/mlx_vlm/json":
            return httpx.Response(301, headers={"location": "/pypi/mlx-vlm/json"})
        return httpx.Response(200, json={"info": {"version": "0.7.12"}})

    site = Site(answer)
    found = hub.fetch_json(
        "https://pypi.org/pypi/mlx_vlm/json", transport=httpx.MockTransport(site)
    )

    assert found["info"]["version"] == "0.7.12"
    assert all("authorization" not in request.headers for request in site.requests)


@pytest.mark.parametrize(
    ("answer", "problem", "sentence"),
    [
        (
            lambda _r: httpx.Response(503),
            "unreachable",
            "pypi.org did not answer (503). Try again later.",
        ),
        (
            _leaky,
            "unreachable",
            "pypi.org could not be reached. Check this machine's internet connection and "
            "try again.",
        ),
        (lambda _r: httpx.Response(200, text="<html>"), "bad_answer", hub.BAD_ANSWER),
        (lambda _r: httpx.Response(200, json=["0.7.12"]), "bad_answer", hub.BAD_ANSWER),
        (
            # The answer picks the version that gets installed: not over plain text.
            lambda _r: httpx.Response(302, headers={"location": "http://pypi.org/pypi/x/json"}),
            "bad_answer",
            hub.BAD_ANSWER,
        ),
    ],
)
def test_an_index_that_cannot_be_read_is_a_sentence_naming_it(
    answer: Answer, problem: str, sentence: str
) -> None:
    with pytest.raises(HubError) as caught:
        hub.fetch_json("https://pypi.org/pypi/mlx-vlm/json", transport=httpx.MockTransport(answer))

    assert (caught.value.problem, str(caught.value)) == (problem, sentence)
    assert caught.value.__context__ is None or problem == "bad_answer"

    with pytest.raises(HubError) as caught:
        hub.fetch_json("http://pypi.org/pypi/mlx-vlm/json", transport=httpx.MockTransport(answer))
    assert "not https" in str(caught.value)


# --- the stand-in --------------------------------------------------------------


def test_no_blocked_publisher_is_written_into_a_fixture() -> None:
    # As a model's publisher, an author, a tag, a licence's name: anywhere.
    # (tests/test_local_models_preflight.py reads the tests and the modules
    # the same way.)
    for path in sorted(fake.DEMO_DATA.glob("*.json")):
        raw = path.read_bytes()
        text = raw.decode("utf-8").casefold()
        for name in policy.DEFAULT_BLOCKED:
            written = re.search(
                rf"(?<![a-z0-9_.-]){re.escape(name.casefold())}(?![a-z0-9_.-])", text
            )
            assert written is None, f"{path.name} names the blocked publisher {name}"
        # The bytes, not the text: reading as text turns every line ending
        # into "\n" before anything can look at it.
        assert b"\r" not in raw, f"{path.name} has Windows line endings"

    # One fixture needs one; it is filled in from the list when it is read.
    raw = (fake.DEMO_DATA / "gated-blocked-base.json").read_text(encoding="utf-8")
    assert fake.BLOCKED_PLACEHOLDER in raw
    assert fake.BLOCKED_PLACEHOLDER not in json.dumps(page("gated-blocked-base"))


def test_the_fake_reads_pages_with_the_real_parser_and_records_every_question() -> None:
    fake_hub = fake.FakeHub.from_fixtures()

    assert fake_hub.info(FIRST) == hub.parse_info(page())
    # A name in any capitals is answered under the page's own spelling. That
    # is this stand-in's assumption about the real site, which may forward
    # such a name instead: see the next test.
    assert fake_hub.info(FIRST.upper()).repo_id == FIRST
    assert fake_hub.info(FIRST, BUILD).revision == BUILD
    assert "model_file" in fake_hub.read_text(
        "mlx-community/demo-model-file", "5" * 40, "config.json"
    )
    # A settings file no fixture wrote is an empty, clean one.
    assert (
        files.inspect_config("config.json", fake_hub.read_text(FIRST, BUILD, "config.json")) == []
    )

    for ask in (
        lambda: fake_hub.info("google/no-such-model"),
        lambda: fake_hub.info(FIRST, "0" * 40),
        lambda: fake_hub.read_text(FIRST, BUILD, "no-such-file.json"),
        lambda: fake_hub.info(f"{BLOCKED}/demo-base"),
    ):
        with pytest.raises(HubError) as caught:
            ask()
        assert caught.value.problem == "not_found"

    assert [call[0] for call in fake_hub.calls].count("info") == 6
    assert fake_hub.calls[0] == ("info", FIRST, None)
    assert fake_hub.fetches == 0


def test_the_fake_can_forward_a_name_in_other_capitals_as_the_real_site_may() -> None:
    # Whether Hugging Face answers a mis-capitalised name outright or with a
    # redirect has not been seen. The stand-in can do either, so nothing
    # built on it rests on one of the two.
    fake_hub = fake.FakeHub.from_fixtures()
    fake_hub.forward_capitals()

    with pytest.raises(HubError) as caught:
        fake_hub.info(FIRST.upper())

    assert (caught.value.problem, caught.value.moved_to) == ("moved", FIRST)
    assert str(caught.value) == (
        f"This name now points to {FIRST} on Hugging Face. Check that name instead."
    )
    assert fake_hub.info(FIRST).repo_id == FIRST


def test_the_fake_says_what_the_real_client_would_say() -> None:
    # The check hands these sentences to the operator as they come, and the
    # demo is the stand-in: a sentence reworded for the real site and not
    # here would go on being shown, and tested, in its old words.
    fake_hub = fake.FakeHub.from_fixtures()
    fake_hub.need_token(FIRST)
    fake_hub.forward_capitals()

    def real(status: int, **headers: str) -> HttpHub:
        return Site(lambda _request: httpx.Response(status, headers=headers)).hub()

    moved = real(307, location=f"/api/models/{FIRST}")
    for theirs, ours in (
        (
            lambda: real(404).info("google/no-such-model"),
            lambda: fake_hub.info("google/no-such-model"),
        ),
        (lambda: real(404).info(FIRST, "0" * 40), lambda: fake_hub.info(FIRST, "0" * 40)),
        (
            lambda: real(404).read_text(FIRST, BUILD, "no-such-file.json"),
            lambda: fake_hub.read_text(FIRST, BUILD, "no-such-file.json"),
        ),
        (
            lambda: real(401).read_text(FIRST, BUILD, "config.json"),
            lambda: fake_hub.read_text(FIRST, BUILD, "config.json"),
        ),
        (lambda: moved.info(FIRST.upper()), lambda: fake_hub.info(FIRST.upper())),
    ):
        with pytest.raises(HubError) as real_error:
            theirs()
        with pytest.raises(HubError) as fake_error:
            ours()
        assert str(fake_error.value) == str(real_error.value)
        assert fake_error.value.problem == real_error.value.problem


def test_a_shrunk_model_really_downloads_and_passes_the_real_check(tmp_path: Path) -> None:
    fake_hub = fake.FakeHub.from_fixtures(chunk_bytes=500)
    fake_hub.shrink(FIRST)
    info = fake_hub.info(FIRST)
    plan = files.plan_files(info.files)
    assert plan.problems == ()
    assert plan.total_bytes < 20_000

    for file in plan.keep:
        target = tmp_path / f"{file.path}.partial"
        counted: list[int] = []
        fake_hub.fetch(FIRST, BUILD, file.path, target, on_bytes=counted.append)
        assert sum(counted) == file.size
        assert files.check_file(target, file) is None
    assert fake_hub.fetches == len(plan.keep)
    # The settings files are still settings files once they are on disk.
    config = (tmp_path / "config.json.partial").read_text(encoding="utf-8")
    assert files.inspect_config("config.json", config) == []
    # Neither the fixture nor a page handed in by a test is changed by it.
    assert hub.parse_info(page()).files[5].size == 5_146_800_534
    mine = page()
    fake.FakeHub({FIRST: mine}).shrink(FIRST)
    assert mine == page()


def test_the_fake_can_drop_the_line_lie_and_be_stopped(tmp_path: Path) -> None:
    fake_hub = fake.FakeHub.from_fixtures(chunk_bytes=500)
    fake_hub.shrink(FIRST)
    weights = next(f for f in fake_hub.info(FIRST).files if f.path == "model.safetensors")
    target = tmp_path / "model.safetensors.partial"

    fake_hub.fail_after(700)
    with pytest.raises(HubError) as caught:
        fake_hub.fetch(FIRST, BUILD, weights.path, target)
    assert caught.value.problem == "unreachable"
    assert target.stat().st_size == 700
    # Once: the next try picks up where the line dropped, and the file is right.
    fake_hub.fetch(FIRST, BUILD, weights.path, target)
    assert files.check_file(target, weights) is None
    assert fake_hub.calls[-1] == ("fetch", FIRST, BUILD, weights.path, 700)

    target.unlink()
    fake_hub.lie_about(weights.path)
    fake_hub.fetch(FIRST, BUILD, weights.path, target)
    assert target.stat().st_size == weights.size
    assert files.check_file(target, weights) == (
        "model.safetensors is not the file Hugging Face lists (its checksum is different), "
        "so it was thrown away."
    )
    target.unlink()
    fake_hub.fetch(FIRST, BUILD, weights.path, target)
    assert files.check_file(target, weights) is None

    target.unlink()
    seen: list[int] = []
    with pytest.raises(Stopped):
        fake_hub.fetch(
            FIRST,
            BUILD,
            weights.path,
            target,
            on_bytes=seen.append,
            should_stop=lambda: len(seen) == 2,
        )
    assert target.stat().st_size == 1000

    # More on disk than the file holds. As with the real client (which is told
    # there is nothing beyond what it has, and takes the file for whole), it
    # is left as it is and nothing is reported: the check is what refuses it,
    # and only deleting it gets a good one.
    target.write_bytes(b"x" * (weights.size + 10))
    seen.clear()
    fake_hub.fetch(FIRST, BUILD, weights.path, target, on_bytes=seen.append)
    assert seen == []
    assert target.stat().st_size == weights.size + 10
    assert files.check_file(target, weights) == (
        "model.safetensors is not the size Hugging Face lists for it, so it was thrown away."
    )
    target.unlink()
    fake_hub.fetch(FIRST, BUILD, weights.path, target, on_bytes=seen.append)
    assert files.check_file(target, weights) is None
    assert sum(seen) == weights.size


def test_a_file_longer_than_it_should_be_is_left_for_the_check_by_both_hubs(
    tmp_path: Path,
) -> None:
    # The stand-in and the real client must take the same path here, or a
    # test of resuming a download is a test of something that never happens.
    fake_hub = fake.FakeHub.from_fixtures()
    fake_hub.shrink(FIRST)
    weights = next(f for f in fake_hub.info(FIRST).files if f.path == "model.safetensors")
    too_long = b"x" * (weights.size + 10)

    def beyond_the_end(request: httpx.Request) -> httpx.Response:
        assert request.headers["range"] == f"bytes={len(too_long)}-"
        return httpx.Response(416)

    for name, site in (("fake", fake_hub), ("real", Site(beyond_the_end).hub())):
        target = tmp_path / name / "model.safetensors.partial"
        target.parent.mkdir()
        target.write_bytes(too_long)
        counted: list[int] = []

        site.fetch(FIRST, BUILD, weights.path, target, on_bytes=counted.append)

        assert counted == [], name
        assert target.read_bytes() == too_long, name
        assert files.check_file(target, weights) is not None, name


def test_the_fake_gates_files_the_way_a_maker_who_asks_for_an_account_does() -> None:
    fake_hub = fake.FakeHub.from_fixtures()
    gated = "nvidia/demo-gated"
    build = "6" * 40
    fake_hub.need_token(gated, accepts=["a-token-with-access"])

    # The page answers anyone; the files do not.
    assert fake_hub.info(gated).gated == "auto"
    with pytest.raises(HubError) as caught:
        fake_hub.read_text(gated, build, "config.json")
    assert caught.value.problem == "needs_token"
    assert str(caught.value).startswith("nvidia asks you to accept their terms")

    with pytest.raises(HubError) as caught:
        fake_hub.with_token("a-token-without").read_text(gated, build, "config.json")
    assert (caught.value.problem, str(caught.value)) == ("no_access", hub.NO_ACCESS)

    allowed = fake_hub.with_token("a-token-with-access")
    assert allowed.read_text(gated, build, "config.json") == "{}"
    # One hub seen three ways: every question is on the one record.
    assert [call[0] for call in fake_hub.calls] == ["info", "read_text", "read_text", "read_text"]
    assert allowed.calls is fake_hub.calls

    # Gating a model whose page calls it open makes the page say so too, as
    # the real page of such a model does. Left "open", the check would report
    # "no account needed" for a model whose files turn everyone away.
    assert fake_hub.info(FIRST).gated == ""
    fake_hub.need_token(FIRST)
    assert fake_hub.info(FIRST).gated == "auto"
    # And a page that already says how it is gated keeps its own word.
    manual = fake.FakeHub.from_fixtures()
    manual.payloads[FIRST]["gated"] = "manual"
    manual.need_token(FIRST)
    assert manual.info(FIRST).gated == "manual"


def test_a_chain_can_be_made_to_fail_part_way() -> None:
    fake_hub = fake.FakeHub.from_fixtures()
    fake_hub.fail_info("google/gemma-4-E4B")

    assert fake_hub.info("google/gemma-4-E4B-it").bases == ("google/gemma-4-E4B",)
    with pytest.raises(HubError) as caught:
        fake_hub.info("google/gemma-4-E4B")
    assert caught.value.problem == "unreachable"


def test_the_demos_small_files_report_the_size_the_page_shows(tmp_path: Path) -> None:
    waits: list[float] = []
    fake_hub = fake.FakeHub.from_fixtures(sleep=waits.append)
    fake_hub.small_files(4096)
    fake_hub.slow(0.25)
    target = tmp_path / "model.safetensors.partial"
    counted: list[int] = []

    fake_hub.fetch(FIRST, BUILD, "model.safetensors", target, on_bytes=counted.append)

    assert target.stat().st_size == 4096
    assert len(counted) == 4 and all(count > 0 for count in counted)
    assert sum(counted) == 5_146_800_534
    assert waits == [0.25] * 4


def test_the_fake_will_not_invent_gigabytes(tmp_path: Path) -> None:
    fake_hub = fake.FakeHub.from_fixtures()

    with pytest.raises(AssertionError, match="shrink"):
        fake_hub.fetch(FIRST, BUILD, "model.safetensors", tmp_path / "model.partial")

    assert not (tmp_path / "model.partial").exists()


# --- the package ---------------------------------------------------------------


def test_hugging_faces_own_library_is_not_used() -> None:
    # It is not a dependency and not importable in CI; everything is plain HTTPS.
    name = "huggingface" + "_hub"
    package = Path(hub.__file__).parent

    for source in package.rglob("*"):
        if source.is_file() and source.suffix in (".py", ".json"):
            assert name not in source.read_text(encoding="utf-8"), source.name


# --- GGUF repositories (L6) -------------------------------------------------------------

GGUF_FIXTURES = sorted(f"gguf/{path.stem}" for path in (fake.DEMO_DATA / "gguf").glob("*.json"))


def test_the_gguf_fixtures_are_the_six_of_the_design() -> None:
    assert GGUF_FIXTURES == [
        "gguf/demo-gated-gguf",
        "gguf/demo-many-builds-GGUF",
        "gguf/demo-omni-30b",
        "gguf/demo-omni-30b-GGUF",
        "gguf/demo-projector-only",
        "gguf/demo-vision-gguf",
    ]
    # Kept out of fake.fixture_names() (L1's every-fixture tests) until the
    # step that teaches the check about GGUF builds lists them there.
    assert not any(name.startswith("gguf/") for name in fake.fixture_names())


@pytest.mark.parametrize("name", GGUF_FIXTURES)
def test_every_gguf_fixture_is_a_page_the_parser_reads(name: str) -> None:
    info = hub.parse_info(page(name))

    assert info.publisher == policy.publisher_of(info.repo_id)
    assert info.files
    raw_bytes = (fake.DEMO_DATA / f"{name}.json").read_bytes()
    assert json.loads(raw_bytes)["framefound_note"]
    assert b"\r" not in raw_bytes
    text = raw_bytes.decode("utf-8").casefold()
    for blocked in policy.DEFAULT_BLOCKED:
        written = re.search(
            rf"(?<![a-z0-9_.-]){re.escape(blocked.casefold())}(?![a-z0-9_.-])", text
        )
        assert written is None, f"{name} names the blocked publisher {blocked}"
    # Every large file carries the checksum a download is verified by.
    assert all(file.sha256 for file in info.files if file.path.endswith(".gguf"))


def test_a_gguf_pages_own_summary_is_kept_without_its_chat_template() -> None:
    payload = page("gguf/demo-omni-30b-GGUF")
    assert "chat_template" in payload["gguf"]

    info = hub.parse_info(payload)

    assert info.gguf == {
        "total": 31_577_940_288,
        "architecture": "demo_omni_moe",
        "context_length": 262_144,
        "bos_token": "<s>",
        "eos_token": "</s>",
        "totalFileSize": 24_515_129_536,
    }
    # The rest of a GGUF page reads as any other: no library, no task, an
    # empty config, its base from a list.
    assert (info.library, info.pipeline, info.custom_code) == ("", "", False)
    assert info.bases == ("nvidia/demo-omni-30b",)
    assert (info.licence, info.licence_name) == ("other", "nvidia-open-model-agreement")
    # Still a value that can be hashed and compared, like every other page.
    assert hash(info) == hash(hub.parse_info(payload))
    assert info == hub.parse_info(payload)
    # Reading did not change the page it was given.
    assert "chat_template" in payload["gguf"]


def test_a_gguf_summary_of_a_shape_not_seen_costs_the_line_not_the_model() -> None:
    # It decides nothing, so it is never a reason to call the page unreadable.
    assert hub.parse_info(page()).gguf is None
    for odd in (None, [], "gguf", 7, True):
        payload = page("gguf/demo-omni-30b-GGUF")
        payload["gguf"] = odd
        info = hub.parse_info(payload)
        assert info.gguf is None
        assert info.repo_id == "example-gguf/demo-omni-30b-GGUF"
    payload = page("gguf/demo-omni-30b-GGUF")
    payload["gguf"] = {"chat_template": "{{ x }}"}
    assert hub.parse_info(payload).gguf == {}
