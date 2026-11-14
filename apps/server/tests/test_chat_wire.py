"""The whole turn, through the real SDK, down to the bytes on the wire.

`test_chat.py` scripts Claude at the engine's edge and `test_chat_stream.py`
checks the one API function alone. This joins them: the engine, the real
`stream_message`, the real `anthropic` SDK, and a transport that answers
from memory. It exists for the properties neither half can show: that
what Anthropic sent is what goes back to it on the next request, after a
round trip through the SDK's objects, the database and the SDK again; and
that the turn's patience is measured against what actually comes down the
wire, where a scripted Claude that has stopped and a real one that is
busy writing look the same.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

pytest.importorskip("mcp.server.mcpserver")
pytest.importorskip("anthropic")
import httpx2  # noqa: E402 - the SDK's own HTTP client
from test_chat import KEY, Chat, chat, ended, env, told  # noqa: E402, F401 - fixtures
from test_chat_stream import END, START, THINKING, _event, sse, streamed  # noqa: E402

from framefound.ai import spend  # noqa: E402
from framefound.chat import claude_stream, engine  # noqa: E402

REAL_STREAM = claude_stream.stream_message

ASKS = [
    START,
    *THINKING,
    _event(
        "content_block_start",
        index=1,
        content_block={"type": "tool_use", "id": "toolu_01", "name": "list_tags", "input": {}},
    ),
    _event(
        "content_block_delta", index=1, delta={"type": "input_json_delta", "partial_json": "{}"}
    ),
    _event("content_block_stop", index=1),
    *END,
]
ANSWERS = [
    START,
    _event("content_block_start", index=0, content_block={"type": "text", "text": ""}),
    _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "Barn and Silo."}),
    _event("content_block_stop", index=0),
    _event(
        "message_delta",
        delta={"stop_reason": "end_turn", "stop_sequence": None},
        usage={"output_tokens": 9},
    ),
    _event("message_stop"),
]


def slowly(*parts: Any) -> httpx2.Response:
    """An answer that comes down the wire in pieces: a list is events sent
    together, a number is that many seconds in which nothing is sent."""

    async def body() -> AsyncIterator[bytes]:
        for part in parts:
            if isinstance(part, int | float):
                await asyncio.sleep(part)
            else:
                yield sse(part)

    return httpx2.Response(200, content=body(), headers={"content-type": "text/event-stream"})


def anthropic_answers(monkeypatch: pytest.MonkeyPatch, *answers: Any) -> list[dict[str, Any]]:
    """Put the real `stream_message` and SDK back, over a transport that
    gives these answers in order (a list of events, or a ready response).
    Returns the list the request bodies are added to."""
    sent: list[dict[str, Any]] = []
    queued = list(answers)

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.append(json.loads(request.content))
        nxt = queued.pop(0)
        return nxt if isinstance(nxt, httpx2.Response) else streamed(nxt)

    def client(sdk: Any, api_key: str) -> Any:
        assert api_key == KEY
        return sdk.AsyncAnthropic(
            api_key=api_key,
            max_retries=0,
            http_client=sdk.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
        )

    monkeypatch.setattr(claude_stream, "stream_message", REAL_STREAM)
    monkeypatch.setattr(claude_stream, "_client", client)
    return sent


async def test_what_anthropic_sent_goes_back_to_it_unchanged_on_the_next_request(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = anthropic_answers(monkeypatch, ASKS, ANSWERS)

    conversation = await chat.new()
    run = await chat.say(conversation, "Which tags are there?")

    assert ended(run) == {"reason": "end", "message": None}
    assert [event.data["delta"] for event in run.events if event.name == "text"] == [
        "Barn and Silo."
    ]
    first, second = sent
    for body in sent:
        assert body["stream"] is True and body["model"] == "claude-opus-5-5"
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "medium"}
        assert "tool_choice" not in body and "temperature" not in body
    assert json.dumps(second["system"]) == json.dumps(first["system"])
    assert json.dumps(second["tools"]) == json.dumps(first["tools"])

    # Claude's turn as the API sent it: the thinking block with its
    # signature and empty text, then the call. Nothing added, nothing lost.
    assert second["messages"][1] == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "EuYBCkQYAiJA"},
            {"type": "tool_use", "id": "toolu_01", "name": "list_tags", "input": {}},
        ],
    }
    (result,) = second["messages"][2]["content"]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "toolu_01"
    assert result["cache_control"] == {"type": "ephemeral"}
    assert "Silo" in result["content"][0]["text"]

    # The ledger has what the API said each request used, cache reads included.
    usage = told(run, "usage")
    assert [u["cache_read_tokens"] for u in usage] == [7400, 7400]
    assert [u["output_tokens"] for u in usage] == [61, 9]
    assert (await chat.conversation(conversation)).output_tokens == 70


def _piece(text: str) -> list[tuple[str, dict[str, Any]]]:
    """A fragment of a tool call's arguments, with a ping beside it as the
    API sends them."""
    return [
        _event("ping"),
        _event(
            "content_block_delta",
            index=1,
            delta={"type": "input_json_delta", "partial_json": text},
        ),
    ]


async def test_a_long_tool_call_is_not_taken_for_a_stream_that_died(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude starts a tool call and then spends longer writing its
    arguments than the turn will wait in silence. Nothing in that time can
    be shown, but the answer is arriving, and giving up on it would bill
    the request, discard it, and tell the operator to ask again for an
    answer that would take just as long the second time."""
    monkeypatch.setattr(engine, "STREAM_IDLE_S", 1.0)
    began = ASKS[:5]  # the answer starts, Claude thinks, the tool call begins
    writing = slowly(
        began, 0.4, _piece("{"), 0.4, _piece(" "), 0.4, _piece(" "), 0.4, _piece("}"), ASKS[6:]
    )
    anthropic_answers(monkeypatch, writing, ANSWERS)

    conversation = await chat.new()
    run = await chat.say(conversation, "Which tags are there?")

    assert told(run, "error") == [], "1.6 seconds of arguments, never 1.0 of silence"
    assert ended(run) == {"reason": "end", "message": None}
    (ran,) = told(run, "tool_result")
    assert (ran["name"], ran["ok"]) == ("list_tags", True)
    assert [row.ok for row in await chat.ledger()] == [True, True]


async def test_a_stream_that_really_stops_is_given_up_on_and_what_it_cost_is_counted(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API read the prompt, began to answer, and then sent nothing
    more. The request is abandoned, and it is not free: the prompt was
    read (here mostly from the cache) and is billed, so it goes on the
    ledger the budget check reads, at what the API said it had counted."""
    monkeypatch.setattr(engine, "STREAM_IDLE_S", 0.5)
    anthropic_answers(monkeypatch, slowly([START, *THINKING], 30))

    conversation = await chat.new()
    run = await chat.say(conversation, "Which tags are there?")

    assert told(run, "error") == [{"kind": "api", "message": engine.QUIET}]
    assert ended(run)["reason"] == "error"
    # 12 read, 7,400 read from the cache, 1 written, on Opus 5.5.
    billed = (12 * 4.0 + 7400 * 0.20 + 1 * 20.0) / 1_000_000
    (failed,) = await chat.ledger()
    assert (failed.ok, failed.purpose) == (False, "chat")
    assert failed.cost_usd == pytest.approx(billed)
    assert failed.cache_read_tokens == 7400 and "stopped answering" in failed.note
    (shown,) = told(run, "usage")
    assert shown["cost_usd"] == pytest.approx(billed) and shown["cache_read_tokens"] == 7400
    assert [event.name for event in run.events][-3:] == ["usage", "error", "done"]
    assert (await chat.conversation(conversation)).spent_usd == pytest.approx(billed)
    async with chat.db() as db:
        assert await spend.spent_this_month(db) == pytest.approx(billed), (
            "the cap counts it; asking again and again is not free"
        )


async def test_stop_closes_the_connection_to_anthropic_while_claude_is_still_thinking(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The moment Stop matters most: Claude is thinking, nothing is coming
    down the wire, and it will be minutes before anything does. A stop
    that waited for the next event to act on would let all of that be
    generated and billed. The read itself has to be given up, through the
    real SDK, and the request priced at what Anthropic had counted."""
    silent, hung_up = asyncio.Event(), asyncio.Event()

    class Line(httpx2.AsyncByteStream):
        """The connection the answer comes down. It says when the SDK is
        waiting on it with nothing arriving, and when it was closed."""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield sse([START, *THINKING])
            silent.set()
            await asyncio.sleep(120)

        async def aclose(self) -> None:
            hung_up.set()

    thinking_for_minutes = httpx2.Response(
        200, stream=Line(), headers={"content-type": "text/event-stream"}
    )
    anthropic_answers(monkeypatch, thinking_for_minutes)
    conversation = await chat.new()
    run = await engine.send(chat.factory, conversation, chat.caller, text="Which tags are there?")
    await asyncio.wait_for(silent.wait(), 5)
    assert not hung_up.is_set() and not run.finished

    assert engine.stop(conversation) is run
    await asyncio.wait_for(run.wait(), 5)  # not the two minutes the line would have stayed open

    assert hung_up.is_set(), "nothing is still being read, or paid for"
    assert ended(run) == {"reason": "stopped", "message": "Stopped."}
    assert told(run, "error") == []
    # 12 read, 7,400 read from the cache, 1 written, on Opus 5.5.
    billed = (12 * 4.0 + 7400 * 0.20 + 1 * 20.0) / 1_000_000
    (dropped,) = await chat.ledger()
    assert (dropped.ok, dropped.purpose) == (False, "chat")
    assert dropped.cost_usd == pytest.approx(billed) and "stopped by the operator" in dropped.note
    assert [event.name for event in run.events][-2:] == ["usage", "done"]
    assert (await chat.conversation(conversation)).spent_usd == pytest.approx(billed)
