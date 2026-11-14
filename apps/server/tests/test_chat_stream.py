"""The one function that calls the Claude API, against the real SDK.

The SDK is given a transport that answers from memory, so its own request
building, event parsing and error mapping all run and no socket is opened.
The answers are the API's documented server-sent events, byte for byte as
they come down the wire; what is pinned is what the engine relies on: the
plain events, the finished answer with its thinking block intact, the four
token counts, and a sentence (never the key) when it goes wrong.
"""

import importlib
import json
from collections.abc import Callable
from typing import Any

import pytest

anthropic = pytest.importorskip("anthropic")
import httpx2  # noqa: E402 - the SDK's own HTTP client, installed with it

from framefound.chat import claude_stream  # noqa: E402
from framefound.chat.claude_stream import ClaudeUnavailable  # noqa: E402

KEY = "sk-ant-test-not-a-real-key"
REQUEST = {
    "model": "claude-opus-5-5",
    "max_tokens": 16_000,
    "thinking": {"type": "adaptive"},
    "output_config": {"effort": "medium"},
    "system": [{"type": "text", "text": "Be brief.", "cache_control": {"type": "ephemeral"}}],
    "tools": [
        {
            "name": "get_media",
            "description": "One item.",
            "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
            "cache_control": {"type": "ephemeral"},
        }
    ],
    "messages": [{"role": "user", "content": [{"type": "text", "text": "What is abcd1234?"}]}],
}


def _event(name: str, **data: Any) -> tuple[str, dict[str, Any]]:
    return name, {"type": name, **data}


def _delta(index: int, **delta: Any) -> tuple[str, dict[str, Any]]:
    return _event("content_block_delta", index=index, delta=delta)


START = _event(
    "message_start",
    message={
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": [],
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 7400,
            "output_tokens": 1,
        },
    },
)
THINKING = [
    _event(
        "content_block_start",
        index=0,
        content_block={"type": "thinking", "thinking": "", "signature": ""},
    ),
    _delta(0, type="signature_delta", signature="EuYBCkQYAiJA"),
    _event("content_block_stop", index=0),
]
TEXT = [
    _event("content_block_start", index=1, content_block={"type": "text", "text": ""}),
    _event("ping"),
    _delta(1, type="text_delta", text="Let me "),
    _delta(1, type="text_delta", text="look."),
    _event("content_block_stop", index=1),
]


def _tool(*fragments: str) -> list[tuple[str, dict[str, Any]]]:
    return [
        _event(
            "content_block_start",
            index=2,
            content_block={"type": "tool_use", "id": "toolu_01", "name": "get_media", "input": {}},
        ),
        *[_delta(2, type="input_json_delta", partial_json=piece) for piece in fragments],
        _event("content_block_stop", index=2),
    ]


END = [
    _event(
        "message_delta",
        delta={"stop_reason": "tool_use", "stop_sequence": None},
        usage={"output_tokens": 61},
    ),
    _event("message_stop"),
]
WHOLE = [START, *THINKING, *TEXT, *_tool('{"id": "ab', 'cd1234"}'), *END]


def sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def streamed(events: list[tuple[str, dict[str, Any]]], tail: bytes = b"") -> httpx2.Response:
    return httpx2.Response(
        200, content=sse(events) + tail, headers={"content-type": "text/event-stream"}
    )


@pytest.fixture()
def api(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Anthropic, answering from memory. Set `api["answer"]` to a callable
    taking the request; what the SDK sent is left in `api["sent"]`."""
    state: dict[str, Any] = {"sent": [], "answer": lambda _request: streamed(WHOLE)}

    def handler(request: httpx2.Request) -> httpx2.Response:
        state["sent"].append(request)
        answer: Callable[[httpx2.Request], httpx2.Response] = state["answer"]
        return answer(request)

    def client(sdk: Any, api_key: str) -> Any:
        return sdk.AsyncAnthropic(
            api_key=api_key,
            max_retries=0,
            http_client=sdk.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(claude_stream, "_client", client)
    return state


async def collect(request: dict[str, Any] = REQUEST) -> list[dict[str, Any]]:
    return [event async for event in claude_stream.stream_message(request, KEY)]


def shown(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The events with something in them for the panel: not the signs of
    life, not the running count."""
    return [event for event in events if event["type"] not in ("alive", "usage")]


async def test_an_answer_arrives_as_plain_events_and_ends_with_the_whole_message(
    api: dict[str, Any],
) -> None:
    events = await collect()
    assert shown(events)[:-1] == [
        {"type": "thinking"},
        {"type": "text", "text": "Let me "},
        {"type": "text", "text": "look."},
        {"type": "tool_use", "id": "toolu_01", "name": "get_media"},
    ]
    # The count comes first, before any of the answer: from that moment the
    # request is billed, whatever becomes of it. It comes again at the end
    # with the output added.
    began, finished = [event["usage"] for event in events if event["type"] == "usage"]
    assert events[0] == {"type": "usage", "usage": began}
    assert began == {
        "input_tokens": 12,
        "output_tokens": 1,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 7400,
    }
    assert finished == {**began, "output_tokens": 61}
    assert events[-1]["type"] == "message"
    message = events[-1]["message"]
    assert message["id"] == "msg_01" and message["model"] == "claude-opus-5-5"
    assert message["stop_reason"] == "tool_use"
    # Every block as it came, the empty thinking block and its signature included:
    # this is what is stored and sent back.
    assert message["content"] == [
        {"type": "thinking", "thinking": "", "signature": "EuYBCkQYAiJA"},
        {"type": "text", "text": "Let me look."},
        {"type": "tool_use", "id": "toolu_01", "name": "get_media", "input": {"id": "abcd1234"}},
    ]
    assert message["usage"] == {
        "input_tokens": 12,
        "output_tokens": 61,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 7400,
    }
    json.dumps(message)  # plain data, storable as it is

    # What the SDK put on the wire: the request as built, streaming, and the
    # key in the authorisation header only.
    (sent,) = api["sent"]
    body = json.loads(sent.content)
    assert body == {**REQUEST, "stream": True}
    assert sent.url.path == "/v1/messages"
    assert KEY in sent.headers.get("authorization", "") + sent.headers.get("x-api-key", "")
    assert KEY not in sent.content.decode()


async def test_a_refused_key_is_a_sentence_for_the_operator_and_never_the_key(
    api: dict[str, Any],
) -> None:
    api["answer"] = lambda _request: httpx2.Response(
        401,
        json={
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid x-api-key"},
        },
    )
    with pytest.raises(ClaudeUnavailable) as caught:
        await collect()
    assert caught.value.fatal
    assert "Security page" in str(caught.value) and KEY not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "kind", "said", "expected", "fatal"),
    [
        (400, "invalid_request_error", "Your credit balance is too low", "cannot pay", True),
        (404, "not_found_error", "model: claude-nope", "model", True),
        (413, "request_too_large", "Request exceeds the maximum", "too large", True),
        (429, "rate_limit_error", "Rate limited", "Wait a minute", False),
        (529, "overloaded_error", "Overloaded", "overloaded", False),
        (400, "invalid_request_error", "messages.3: unexpected tool_use_id", "rejected", True),
    ],
)
async def test_each_way_the_api_says_no_reads_as_what_to_do_about_it(
    api: dict[str, Any], status: int, kind: str, said: str, expected: str, fatal: bool
) -> None:
    api["answer"] = lambda _request: httpx2.Response(
        status, json={"type": "error", "error": {"type": kind, "message": said}}
    )
    with pytest.raises(ClaudeUnavailable) as caught:
        await collect()
    assert expected in str(caught.value) and caught.value.fatal is fatal


async def test_an_error_part_way_through_an_answer_stops_it(api: dict[str, Any]) -> None:
    overloaded = (
        b'event: error\ndata: {"type":"error","error":'
        b'{"type":"overloaded_error","message":"Overloaded"}}\n\n'
    )
    api["answer"] = lambda _request: streamed([START, *THINKING, *TEXT], overloaded)
    heard: list[dict[str, Any]] = []
    with pytest.raises(ClaudeUnavailable) as caught:
        async for event in claude_stream.stream_message(REQUEST, KEY):
            heard.append(event)
    assert [event["type"] for event in shown(heard)] == ["thinking", "text", "text"]
    assert not any(event["type"] == "message" for event in heard), "half an answer is no answer"
    assert "overloaded" in str(caught.value) and not caught.value.fatal
    # But it was begun, and so billed: the listener was told what had been
    # counted before it was told anything else.
    assert heard[0]["type"] == "usage" and heard[0]["usage"]["cache_read_input_tokens"] == 7400


async def test_everything_the_api_sends_is_heard_even_when_there_is_nothing_to_show(
    api: dict[str, Any],
) -> None:
    """A long tool call is one visible event (it began) and then, for as
    long as Claude takes to write its arguments, nothing to show. A
    listener that hears only what can be shown cannot tell that from a
    dead stream; so each piece is passed on as a sign of life."""
    pieces = ['{"id"', ": ", '"ab', "cd", "12", '34"', "}"]
    api["answer"] = lambda _request: streamed([START, *THINKING, *TEXT, *_tool(*pieces), *END])
    events = await collect()

    began = events.index({"type": "tool_use", "id": "toolu_01", "name": "get_media"})
    after = events[began + 1 : -1]
    assert [event["type"] for event in after].count("alive") >= len(pieces), (
        "one for each piece of the arguments, at least"
    )
    assert shown(after) == [], "and still nothing the panel would draw"
    assert events[-1]["message"]["content"][-1]["input"] == {"id": "abcd1234"}
    # The same while Claude thinks: the signature and the end of the block.
    thinking = events.index({"type": "thinking"})
    assert events[thinking + 1]["type"] == "alive"

    # An unreachable API was never begun: nothing is heard, nothing counted.
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host", request=request)

    api["answer"] = refuse
    heard: list[dict[str, Any]] = []
    with pytest.raises(ClaudeUnavailable) as caught:
        async for event in claude_stream.stream_message(REQUEST, KEY):
            heard.append(event)
    assert heard == [] and caught.value.usage is None


async def test_a_connection_that_goes_dead_part_way_through_says_so(api: dict[str, Any]) -> None:
    """The check for a dead stream is the HTTP client's read timeout, and
    once an answer is streaming its error reaches us as the client's own,
    not as one of the SDK's. It is the connection that failed, not the
    answer: it must not be reported as an answer that "arrived damaged"."""

    async def then_nothing() -> Any:
        yield sse([START, *THINKING, *TEXT])
        raise httpx2.ReadTimeout("no bytes for 300 seconds")

    api["answer"] = lambda _request: httpx2.Response(
        200, content=then_nothing(), headers={"content-type": "text/event-stream"}
    )
    heard: list[dict[str, Any]] = []
    with pytest.raises(ClaudeUnavailable) as caught:
        async for event in claude_stream.stream_message(REQUEST, KEY):
            heard.append(event)
    assert str(caught.value) == claude_stream.LOST and not caught.value.fatal
    assert "lost" in claude_stream.LOST and KEY not in str(caught.value)
    assert heard[0]["type"] == "usage", "it had begun, so it is billed"
    assert [event["type"] for event in shown(heard)] == ["thinking", "text", "text"]


async def test_an_unreachable_api_says_to_check_the_connection(api: dict[str, Any]) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host", request=request)

    api["answer"] = refuse
    with pytest.raises(ClaudeUnavailable) as caught:
        await collect()
    assert "could not reach Anthropic" in str(caught.value) and not caught.value.fatal


async def test_tool_arguments_that_cannot_be_read_discard_the_answer(
    api: dict[str, Any],
) -> None:
    broken = [START, *THINKING, *TEXT, *_tool('{"id": nonsense'), *END]
    api["answer"] = lambda _request: streamed(broken)
    with pytest.raises(ClaudeUnavailable) as caught:
        await collect()
    assert str(caught.value) == claude_stream.DAMAGED

    # Nor is a stream whose pieces do not fit together (here, a tool call
    # claiming to be the third block of an answer that has no first two).
    api["answer"] = lambda _request: streamed([START, *_tool('{"id": "abcd1234"}'), *END])
    with pytest.raises(ClaudeUnavailable) as caught:
        await collect()
    assert str(caught.value) == claude_stream.DAMAGED


def test_the_client_uses_the_given_key_and_not_one_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-the-environment")
    client = claude_stream._client(anthropic, KEY)
    assert client.api_key == KEY and client.auth_token is None
    assert client.max_retries == claude_stream.MAX_RETRIES
    assert client.timeout.read == claude_stream.TIMEOUT_S
    assert client.timeout.connect == claude_stream.CONNECT_TIMEOUT_S


def test_without_the_sdk_the_panel_says_what_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    assert claude_stream.problem() is None

    def missing(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    assert claude_stream.problem() == claude_stream.NOT_INSTALLED
    assert "chat" in claude_stream.NOT_INSTALLED
