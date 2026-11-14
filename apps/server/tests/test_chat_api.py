"""The chat panel through its real routes, with Claude played by a script.

`test_chat.py` pins the turn loop; this file pins the door to it: what
each route answers, who may open it, and that a turn arrives as a stream
of events while it happens rather than as one lump at the end.

Most tests use the ordinary test client, which hands a response back only
when it is complete. The streaming tests cannot: they drive the ASGI app
directly (`Wire`) and read each piece at the moment the app sends it, with
Claude held half-way through an answer, so a layer that saved the stream
up would leave the test waiting for something that cannot come.
"""

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from test_chat import (  # noqa: F401 - `chat` is a pytest fixture
    COST,
    MODEL,
    Chat,
    answer,
    chat,
    say,
    think,
    use,
)
from test_mcp import env  # noqa: F401 - pytest fixture

from framefound.auth import service
from framefound.db.models import AuditLog, User

pytest.importorskip("mcp.server.mcpserver")
from test_mcp_video import _video  # noqa: E402 - needs the connector installed

from framefound.api.v1 import chat as chat_api  # noqa: E402
from framefound.chat import claude_stream, engine, store, tools  # noqa: E402
from framefound.mcp import compact  # noqa: E402

URL = "/api/v1/chat"
REPO = Path(__file__).resolve().parents[3]
PASSWORD = "another-strong-password"  # noqa: S105 - a test account


# --- reading a stream -----------------------------------------------------------


@dataclass
class Got:
    id: int
    name: str
    data: dict[str, Any]


def parse(body: str) -> list[Got]:
    """The events in a `text/event-stream` body. Comment lines (the
    keep-alives) are skipped, as a browser's reader skips them."""
    events: list[Got] = []
    for block in body.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if line and not line.startswith(":"):
                key, _, value = line.partition(": ")
                fields[key] = value
        if "event" in fields:
            events.append(Got(int(fields["id"]), fields["event"], json.loads(fields["data"])))
    return events


def named(events: list[Got], name: str) -> list[dict[str, Any]]:
    return [event.data for event in events if event.name == name]


class Wire:
    """One request driven straight into the app, piece by piece.

    The app is called as a server would call it, and every message it
    sends lands in a queue the test reads from. Nothing sits in between to
    collect the body, so "the test has seen this event" means "the app had
    sent it by then".
    """

    def __init__(
        self,
        app: Any,
        method: str,
        path: str,
        *,
        cookies: str,
        body: dict[str, Any] | None = None,
        query: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = json.dumps(body).encode() if body is not None else b""
        sent = {
            "host": "t",
            "cookie": cookies,
            "content-type": "application/json",
            "content-length": str(len(payload)),
            **(headers or {}),
        }
        self.app = app
        self.scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "root_path": "",
            "headers": [(key.encode(), value.encode()) for key, value in sent.items()],
            "client": ("127.0.0.1", 50000),
            "server": ("t", 80),
        }
        self.payload = payload
        self.sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.gone = asyncio.Event()
        self.status = 0
        self.headers: dict[str, str] = {}
        self.text = ""
        self.events: list[Got] = []
        self.finished = False
        self._asked = False
        self.task: asyncio.Task[None] | None = None

    async def _receive(self) -> dict[str, Any]:
        if not self._asked:
            self._asked = True
            return {"type": "http.request", "body": self.payload, "more_body": False}
        await self.gone.wait()
        return {"type": "http.disconnect"}

    async def _send(self, message: dict[str, Any]) -> None:
        await self.sent.put(message)

    async def __aenter__(self) -> "Wire":
        self.task = asyncio.create_task(self.app(self.scope, self._receive, self._send))
        first = await asyncio.wait_for(self.sent.get(), 10)
        assert first["type"] == "http.response.start", first
        self.status = first["status"]
        self.headers = {key.decode(): value.decode() for key, value in first["headers"]}
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.hang_up()
        assert self.task is not None
        try:
            await asyncio.wait_for(self.task, 10)
        except TimeoutError:
            self.task.cancel()
            raise AssertionError("the response did not end when the browser left") from None

    def hang_up(self) -> None:
        """The browser closes the connection."""
        self.gone.set()

    async def until(self, name: str, patience_s: float = 5.0) -> list[Got]:
        """Read what the app sends until an event called `name` has arrived.
        Fails, rather than waits for ever, if it does not."""
        while not any(event.name == name for event in self.events):
            assert not self.finished, f"the stream ended without {name!r}: {self.text!r}"
            try:
                message = await asyncio.wait_for(self.sent.get(), patience_s)
            except TimeoutError:
                seen = [event.name for event in self.events]
                raise AssertionError(f"no {name!r} within {patience_s}s; so far: {seen}") from None
            assert message["type"] == "http.response.body", message
            self.text += message.get("body", b"").decode()
            self.finished = not message.get("more_body", False)
            whole = self.text[: self.text.rfind("\n\n") + 2] if "\n\n" in self.text else ""
            self.events = parse(whole)
        return self.events


def cookies_of(client: AsyncClient) -> str:
    return "; ".join(f"{name}={value}" for name, value in client.cookies.items())


def held(release: asyncio.Event, first: str, rest: str) -> Any:
    """An answer from Claude that stops half-way until the test lets it go."""

    async def play(_request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text", "text": first}
        await release.wait()
        yield {"type": "text", "text": rest}
        yield answer(say(first + rest))[-1]

    return play


@asynccontextmanager
async def signed_in(chat: Chat, email: str, role: str) -> AsyncIterator[AsyncClient]:  # noqa: F811
    """A second account, signed in on its own client."""
    async with chat.db() as db:
        await service.create_user(db, email, PASSWORD, role)
        await db.commit()
    async with AsyncClient(transport=ASGITransport(app=chat.env["app"]), base_url="http://t") as c:
        entered = await c.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert entered.status_code == 200, entered.text
        yield c


async def new_conversation(client: AsyncClient, **body: Any) -> str:
    created = await client.post(f"{URL}/conversations", json=body or None)
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def message(resp: Any) -> str:
    return str(resp.json()["error"]["message"])


# --- one whole turn ---------------------------------------------------------------


async def test_a_question_with_a_change_in_it_goes_from_asked_to_done_through_the_routes(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    kitchen = compact.short_id(chat.ids["kitchen.jpg"])

    created = await client.post(f"{URL}/conversations")
    assert created.status_code == 201, created.text
    fresh = created.json()
    cid = fresh["id"]
    assert (fresh["model"], fresh["pictures"], fresh["state"]) == (MODEL, True, "idle")
    assert (fresh["messages"], fresh["pending"], fresh["run"], fresh["last_turn"]) == (
        [],
        [],
        None,
        None,
    )

    chat.script.then(
        answer(
            think(),
            say("I'll confirm that."),
            use("toolu_read", "list_tags"),
            use("toolu_write", "decide_tag", id=kitchen, tag="Silo", accept=True),
        )
    )
    sent = await client.post(
        f"{URL}/conversations/{cid}/messages",
        json={
            "text": "The kitchen photo does show a silo",
            "context": {"path": "/tags", "tag": "silo", "anything_else": {"is": "dropped"}},
        },
    )
    assert sent.status_code == 200, sent.text
    assert sent.headers["content-type"].startswith("text/event-stream")
    assert sent.headers["cache-control"] == "no-cache, no-transform"
    assert sent.headers["x-accel-buffering"] == "no"
    assert "content-length" not in sent.headers, "a stream has no length to promise"
    events = parse(sent.text)
    assert [event.id for event in events] == list(range(len(events))), "ids count from 0"
    assert [event.name for event in events] == [
        "start",
        "thinking",
        "thinking",
        "text",
        "tool",
        "tool",
        "usage",
        "tool",
        "tool_result",
        "confirm",
        "done",
    ]
    start = events[0].data
    assert {key: start[key] for key in ("conversation_id", "model", "pictures", "seq")} == {
        "conversation_id": cid,
        "model": MODEL,
        "pictures": True,
        "seq": 1,
    }
    asked = {
        "id": "toolu_write",
        "name": "decide_tag",
        "input": {"id": kitchen, "tag": "Silo", "accept": True},
        "summary": f"Confirm the tag “Silo” on item {kitchen}",
    }
    assert named(events, "confirm") == [asked]
    assert events[-1].data == {"reason": "awaiting_confirmation", "message": None}
    assert await chat.tag_source("kitchen.jpg", "Silo") == "suggested", "nothing changed yet"

    # What a page that was reloaded at this point is given.
    waiting = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert waiting["state"] == "awaiting_confirmation"
    assert waiting["last_turn"] == {"reason": "awaiting_confirmation", "message": None}
    assert waiting["pending"] == [asked], "the same four fields as the confirm event"
    assert waiting["title"] == "The kitchen photo does show a silo"
    mine, claudes = waiting["messages"]
    assert (mine["role"], mine["seq"], mine["text"]) == (
        "user",
        1,
        "The kitchen photo does show a silo",
    )
    assert mine["id"] == start["message_id"]
    assert mine["context"] == {"path": "/tags", "tag": "silo"}, "known keys only"
    assert (claudes["role"], claudes["seq"], claudes["text"]) == (
        "assistant",
        2,
        "I'll confirm that.",
    )
    assert claudes["stop_reason"] == "tool_use" and claudes["cost_usd"] == pytest.approx(COST)
    read, write = claudes["tools"]
    assert (read["id"], read["name"], read["state"], read["ok"]) == (
        "toolu_read",
        "list_tags",
        "ran",
        True,
    )
    assert "Silo" in read["summary"] and read["ask"] is None and not read["needs_confirm"]
    assert (write["state"], write["ok"], write["needs_confirm"]) == ("pending", False, True)
    assert write["ask"] == asked["summary"] and write["summary"] == ""
    assert write["input"] == asked["input"]

    # Yes.
    chat.script.then(answer(say("Confirmed.")))
    answered = await client.post(
        f"{URL}/conversations/{cid}/confirm",
        json={"decisions": [{"tool_use_id": "toolu_write", "approve": True}]},
    )
    assert answered.status_code == 200, answered.text
    resumed = parse(answered.text)
    assert [event.name for event in resumed] == [
        "start",
        "tool",
        "tool_result",
        "text",
        "usage",
        "done",
    ]
    assert resumed[0].data["message_id"] is None and resumed[0].data["seq"] == 2, (
        "the same turn, carried on from Claude's answer"
    )
    (ran,) = named(resumed, "tool_result")
    assert (ran["id"], ran["state"], ran["ok"]) == ("toolu_write", "approved", True)
    assert resumed[-1].data == {"reason": "end", "message": None}
    assert await chat.tag_source("kitchen.jpg", "Silo") == "confirmed"

    done = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert (done["state"], done["pending"]) == ("idle", [])
    assert [(m["role"], m["text"]) for m in done["messages"]] == [
        ("user", "The kitchen photo does show a silo"),
        ("assistant", "I'll confirm that."),
        ("assistant", "Confirmed."),
    ], "the turn that carried the tools' answers back to Claude is not shown"
    assert [m["seq"] for m in done["messages"]] == [1, 2, 4]
    assert done["messages"][1]["tools"][1]["state"] == "approved"
    assert done["spent_usd"] == pytest.approx(2 * COST)
    assert done["run"] == {"active": False, "events": 6, "seq": 2}

    # The yes is on the record, with whose it was.
    async with chat.db() as db:
        (logged,) = (
            (await db.execute(select(AuditLog).where(AuditLog.event.like("chat.%"))))
            .scalars()
            .all()
        )
    assert logged.event == "chat.tool_approved" and logged.actor_user_id == chat.user_id
    assert logged.detail == {
        "conversation": cid,
        "tool": "decide_tag",
        "tool_use_id": "toolu_write",
        "what": asked["summary"],
    }

    (listed,) = (await client.get(f"{URL}/conversations")).json()
    assert (listed["id"], listed["title"], listed["state"]) == (cid, waiting["title"], "idle")
    assert "messages" not in listed


async def test_saying_no_through_the_route_is_recorded_and_changes_nothing(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    kitchen = compact.short_id(chat.ids["kitchen.jpg"])
    cid = await new_conversation(client)
    chat.script.then(
        answer(use("toolu_write", "decide_tag", id=kitchen, tag="Silo", accept=True)),
        answer(say("Left as it was.")),
    )
    await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Confirm the silo"})
    declined = await client.post(
        f"{URL}/conversations/{cid}/confirm",
        json={"decisions": [{"tool_use_id": "toolu_write", "approve": False}]},
    )
    (told,) = named(parse(declined.text), "tool_result")
    assert (told["state"], told["ok"]) == ("declined", False)
    assert await chat.tag_source("kitchen.jpg", "Silo") == "suggested"
    async with chat.db() as db:
        events = (await db.execute(select(AuditLog.event))).scalars().all()
    assert "chat.tool_declined" in events and "chat.tool_approved" not in events


# --- the stream is a stream -------------------------------------------------------


async def test_events_reach_the_browser_as_they_happen_not_when_the_turn_ends(
    chat: Chat,  # noqa: F811
) -> None:
    """The public-access gate wraps every response (it is a
    BaseHTTPMiddleware). If it, or anything else this app adds, collected
    a response before passing it on, the panel would show nothing for a
    minute and then everything at once."""
    client = chat.env["client"]
    cid = await new_conversation(client)
    release = asyncio.Event()
    chat.script.then(held(release, "One moment", ", done."))

    async with Wire(
        chat.env["app"],
        "POST",
        f"{URL}/conversations/{cid}/messages",
        cookies=cookies_of(client),
        body={"text": "Hello"},
    ) as wire:
        assert wire.status == 200 and wire.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in wire.headers
        # Claude is stopped half-way through its answer and will not go on
        # until this test says so. The first half must already be here.
        early = await wire.until("text")
        assert [event.name for event in early] == ["start", "text"]
        assert early[1].data == {"delta": "One moment"}
        assert wire.text.startswith(": open\n\n"), "something is sent at once"
        run = engine.current(uuid.UUID(cid))
        assert run is not None and not run.finished and not release.is_set()

        release.set()
        rest = await wire.until("done")
        assert [event.name for event in rest] == ["start", "text", "text", "usage", "done"]
        assert "".join(event["delta"] for event in named(rest, "text")) == "One moment, done."


async def test_closing_the_tab_ends_the_response_and_not_the_turn(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    cid = await new_conversation(client)
    release = asyncio.Event()
    chat.script.then(held(release, "Looking", " it up."))

    async with Wire(
        chat.env["app"],
        "POST",
        f"{URL}/conversations/{cid}/messages",
        cookies=cookies_of(client),
        body={"text": "Hello"},
    ) as wire:
        await wire.until("text")
        wire.hang_up()
        assert wire.task is not None
        await asyncio.wait_for(wire.task, 5)  # the response is over...

    run = engine.current(uuid.UUID(cid))
    assert run is not None and not run.finished, "...and the turn is not"
    release.set()
    await asyncio.wait_for(run.wait(), 10)

    stored = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert stored["state"] == "idle" and stored["last_turn"]["reason"] == "end"
    assert [(m["role"], m["text"]) for m in stored["messages"]] == [
        ("user", "Hello"),
        ("assistant", "Looking it up."),
    ]


async def test_a_quiet_stretch_is_filled_so_nothing_in_between_hangs_up(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(chat_api, "KEEPALIVE_S", 0.02)
    client = chat.env["client"]
    cid = await new_conversation(client)

    async def thinks_a_while(_request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        await asyncio.sleep(0.2)
        for event in answer(say("Here.")):
            yield event

    chat.script.then(thinks_a_while)
    sent = await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Hello"})
    assert ": keep-alive\n\n" in sent.text
    assert [event.name for event in parse(sent.text)] == ["start", "text", "usage", "done"], (
        "a reader that skips comment lines sees only the events"
    )


# --- coming back to a turn --------------------------------------------------------


async def test_a_page_that_comes_back_can_see_and_follow_a_turn_still_running(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    cid = await new_conversation(client)
    assert (await client.get(f"{URL}/conversations/{cid}/events")).status_code == 204, (
        "nothing has run here: there is nothing to follow"
    )
    release = asyncio.Event()
    chat.script.then(held(release, "Half an", " answer."))
    cookies = cookies_of(client)

    async with Wire(
        chat.env["app"],
        "POST",
        f"{URL}/conversations/{cid}/messages",
        cookies=cookies,
        body={"text": "Hello"},
    ) as first:
        await first.until("text")

        # The reloaded page asks what state things are in...
        seen = (await client.get(f"{URL}/conversations/{cid}")).json()
        assert seen["state"] == "running"
        assert seen["run"] == {"active": True, "events": 2, "seq": 1}
        assert [(m["seq"], m["text"]) for m in seen["messages"]] == [(1, "Hello")], (
            "the answer in progress is not stored yet: it comes from the events"
        )
        (listed,) = (await client.get(f"{URL}/conversations")).json()
        assert listed["state"] == "running"

        # ...and follows the same turn from the beginning, in a second stream.
        async with Wire(
            chat.env["app"], "GET", f"{URL}/conversations/{cid}/events", cookies=cookies
        ) as second:
            assert second.status == 200
            caught_up = await second.until("text")
            assert [(event.id, event.name) for event in caught_up] == [(0, "start"), (1, "text")]
            assert caught_up[1].data == {"delta": "Half an"}
            release.set()
            whole = await second.until("done")
            assert [event.name for event in whole] == ["start", "text", "text", "usage", "done"]
        await first.until("done")

    # Finished, the run can still be replayed for a while: from an index...
    tail = await client.get(f"{URL}/conversations/{cid}/events", params={"after": 3})
    assert [(event.id, event.name) for event in parse(tail.text)] == [(3, "usage"), (4, "done")]
    # ...or the way a browser's own EventSource reconnects.
    again = await client.get(f"{URL}/conversations/{cid}/events", headers={"Last-Event-ID": "3"})
    assert [(event.id, event.name) for event in parse(again.text)] == [(4, "done")]
    after = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert after["state"] == "idle" and after["run"] == {"active": False, "events": 5, "seq": 1}

    # A server that was restarted has no events to give: the page reads the
    # stored conversation, which has everything that finished.
    engine._RUNS.clear()
    assert (await client.get(f"{URL}/conversations/{cid}/events")).status_code == 204
    restarted = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert restarted["run"] is None and restarted["messages"][-1]["text"] == "Half an answer."


async def test_a_finished_turn_can_be_replayed_for_ten_minutes_and_no_longer(
    chat: Chat,  # noqa: F811
) -> None:
    """On an install with one person on it, nothing else starts a turn, so
    nothing but the clock can retire a finished one: left to "until the
    next turn starts", a turn from this morning would still be replayed
    this afternoon, and a deleted conversation's words stay in memory."""
    client = chat.env["client"]
    cid = await new_conversation(client)
    chat.script.then(answer(say("Barn and Silo.")))
    await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Which tags?"})

    replay = await client.get(f"{URL}/conversations/{cid}/events")
    assert replay.status_code == 200 and parse(replay.text)[-1].name == "done"

    run = engine.current(uuid.UUID(cid))
    assert run is not None and run.finished_at is not None
    # Ten minutes on, nobody else having used the panel in between.
    run.finished_at -= engine.KEEP_FINISHED_S + 1
    assert engine.KEEP_FINISHED_S == 600
    assert (await client.get(f"{URL}/conversations/{cid}/events")).status_code == 204
    later = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert later["run"] is None and later["messages"][-1]["text"] == "Barn and Silo."
    assert uuid.UUID(cid) not in engine._RUNS, "and its events are let go"

    # Deleting a conversation lets go of its last turn at once.
    other = await new_conversation(client)
    chat.script.then(answer(say("Something only its owner should read.")))
    await client.post(f"{URL}/conversations/{other}/messages", json={"text": "Hello"})
    assert uuid.UUID(other) in engine._RUNS
    assert (await client.delete(f"{URL}/conversations/{other}")).status_code == 204
    assert uuid.UUID(other) not in engine._RUNS


async def test_writing_a_new_message_instead_of_answering_is_a_no_on_the_audit_log(
    chat: Chat,  # noqa: F811
) -> None:
    """The easiest way to turn a request down is to ignore it and type.
    An administrator reading the audit log for what Claude asked to change
    and was refused must find those too."""
    client = chat.env["client"]
    kitchen = compact.short_id(chat.ids["kitchen.jpg"])
    cid = await new_conversation(client)
    chat.script.then(
        answer(use("toolu_write", "decide_tag", id=kitchen, tag="Silo", accept=True)),
        answer(say("All right, the barns instead.")),
    )
    await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Confirm the silo"})
    async with chat.db() as db:
        assert (await db.execute(select(AuditLog).where(AuditLog.event.like("chat.%")))).all() == []

    await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Never mind"})

    async with chat.db() as db:
        (logged,) = (
            (await db.execute(select(AuditLog).where(AuditLog.event.like("chat.%"))))
            .scalars()
            .all()
        )
    assert logged.event == "chat.tool_declined" and logged.actor_user_id == chat.user_id
    assert logged.detail == {
        "conversation": cid,
        "tool": "decide_tag",
        "tool_use_id": "toolu_write",
        "what": f"Confirm the tag “Silo” on item {kitchen}",
        "how": "new message",
    }
    assert await chat.tag_source("kitchen.jpg", "Silo") == "suggested"
    shown = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert shown["pending"] == [] and shown["messages"][1]["tools"][0]["state"] == "declined"


async def test_one_turn_at_a_time_and_no_deleting_under_a_turn(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    cid = await new_conversation(client)
    release = asyncio.Event()
    chat.script.then(held(release, "Working", " on it."))

    async with Wire(
        chat.env["app"],
        "POST",
        f"{URL}/conversations/{cid}/messages",
        cookies=cookies_of(client),
        body={"text": "Hello"},
    ) as wire:
        await wire.until("text")
        second = await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "And?"})
        assert second.status_code == 409 and "still working" in message(second)
        assert second.json()["error"]["code"] == "conflict"
        refused = await client.delete(f"{URL}/conversations/{cid}")
        assert refused.status_code == 409 and "still working" in message(refused)
        release.set()
        await wire.until("done")

    assert len(chat.script.requests) == 1, "the second message asked Claude nothing"
    folder = store.home(uuid.UUID(cid))
    folder.mkdir(parents=True, exist_ok=True)
    assert (await client.delete(f"{URL}/conversations/{cid}")).status_code == 204
    assert (await client.get(f"{URL}/conversations/{cid}")).status_code == 404
    assert (await client.get(f"{URL}/conversations")).json() == []
    assert not folder.exists(), "its pictures and timelines go with it"


def test_the_api_is_one_process_wherever_it_is_started() -> None:
    """Following a turn reads its events from the memory of the process
    running it. A second API process would answer half the requests with
    "nothing to follow" and half the messages with "still working"."""
    for started_in in (
        "docker-compose.yml",
        "apps/server/Dockerfile",
        "apps/server/framefound/native/services.py",
    ):
        text = (REPO / started_in).read_text(encoding="utf-8")
        assert "framefound.main:app" in text, started_in
        assert "--workers" not in text and "WEB_CONCURRENCY" not in text, started_in


# --- stopping a turn --------------------------------------------------------------


async def test_stop_ends_the_turn_for_everyone_following_it_and_the_next_message_works(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"
    cookies = cookies_of(client)

    # Nothing is running yet. Stop is not an error, and changes nothing.
    idle = await client.post(f"{base}/stop")
    assert idle.status_code == 200 and idle.json()["state"] == "idle"
    assert engine.current(uuid.UUID(cid)) is None

    release = asyncio.Event()  # never set: Claude would go on for as long as it liked
    chat.script.then(held(release, "Working", " on it."))
    async with (
        Wire(
            chat.env["app"], "POST", f"{base}/messages", cookies=cookies, body={"text": "Hello"}
        ) as wire,
        Wire(chat.env["app"], "GET", f"{base}/events", cookies=cookies) as other_tab,
    ):
        await wire.until("text")
        await other_tab.until("text")

        stopped = await client.post(f"{base}/stop")
        assert stopped.status_code == 200, stopped.text
        summary = stopped.json()
        assert set(summary) == set(idle.json()), "the same shape as every other summary"
        assert summary["state"] == "idle", "the answer waited for the turn to end"

        for stream in (wire, other_tab):
            events = await stream.until("done")
            assert [event.name for event in events] == ["start", "text", "done"]
            assert events[-1].data == {"reason": "stopped", "message": "Stopped."}
            assert named(events, "error") == []

    shown = (await client.get(base)).json()
    assert shown["state"] == "idle" and shown["pending"] == []
    assert shown["last_turn"] == {"reason": "stopped", "message": "Stopped."}
    assert shown["run"] == {"active": False, "events": 3, "seq": 1}
    assert [(m["role"], m["text"], m["stop_reason"]) for m in shown["messages"]] == [
        ("user", "Hello", None),
        ("assistant", "Working", "stopped"),
    ], "what was on the screen when it was stopped is what a reload shows"

    # A second press says the same and adds nothing.
    again = await client.post(f"{base}/stop")
    assert again.status_code == 200 and again.json() == summary
    replay = await client.get(f"{base}/events")
    assert [event.name for event in parse(replay.text)] == ["start", "text", "done"]

    # And the conversation carries on.
    chat.script.then(answer(say("Barn and Silo.")))
    sent = await client.post(f"{base}/messages", json={"text": "Which tags are there?"})
    assert parse(sent.text)[-1].data == {"reason": "end", "message": None}
    assert len(chat.script.requests) == 2, "the stopped answer was never asked for again"
    roles = [turn["role"] for turn in chat.script.requests[1]["messages"]]
    assert roles == ["user", "assistant", "user"]


async def test_an_answer_stopped_before_a_word_of_it_shows_no_answer(
    chat: Chat,  # noqa: F811
) -> None:
    """Claude was still thinking. Something is stored in its place so the
    conversation stays one the API will read, but it is not anything
    Claude said and the panel must not show it as if it were."""
    client = chat.env["client"]
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"

    async def thinking(_request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "thinking"}
        await asyncio.sleep(60)

    chat.script.then(thinking)
    async with Wire(
        chat.env["app"],
        "POST",
        f"{base}/messages",
        cookies=cookies_of(client),
        body={"text": "Hello"},
    ) as wire:
        await wire.until("thinking")
        assert (await client.post(f"{base}/stop")).json()["state"] == "idle"
        events = await wire.until("done")
        assert [event.name for event in events] == ["start", "thinking", "thinking", "done"]

    shown = (await client.get(base)).json()
    assert [(m["role"], m["text"]) for m in shown["messages"]] == [("user", "Hello")]
    assert shown["last_turn"] == {"reason": "stopped", "message": "Stopped."}
    assert engine.STOP_MARK not in json.dumps(shown)
    stored = await chat.turns(uuid.UUID(cid))
    assert [turn.role for turn in stored] == ["user", "assistant"], "it is there for Claude"


async def test_an_answer_stopped_after_its_tool_ran_shows_the_tool_and_the_words_on_reload(
    chat: Chat,  # noqa: F811
) -> None:
    """The usual place for Stop: Claude has used a tool and is writing up
    what it found. A reload must show what was on the screen, the tool
    with its result and the words under it, and not lose the words because
    the turn before them in the store is a tool's result and not something
    the owner typed."""
    client = chat.env["client"]
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"
    release = asyncio.Event()  # never set
    chat.script.then(answer(use("toolu_1", "list_tags")), held(release, "There are", " three."))
    async with Wire(
        chat.env["app"],
        "POST",
        f"{base}/messages",
        cookies=cookies_of(client),
        body={"text": "Which tags are there?"},
    ) as wire:
        await wire.until("text")
        assert (await client.post(f"{base}/stop")).json()["state"] == "idle"
        events = await wire.until("done")
        assert events[-1].data == {"reason": "stopped", "message": "Stopped."}
        assert [ran["state"] for ran in named(events, "tool_result")] == ["ran"]

    shown = (await client.get(base)).json()
    assert [
        (m["role"], m["text"], m["stop_reason"], [chip["state"] for chip in m["tools"]])
        for m in shown["messages"]
    ] == [
        ("user", "Which tags are there?", None, []),
        ("assistant", "", "tool_use", ["ran"]),
        ("assistant", "There are", "stopped", []),
    ]
    assert shown["last_turn"] == {"reason": "stopped", "message": "Stopped."}


async def test_stop_answers_at_once_while_a_tool_finishes_and_calls_go_on_waiting(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool that is half-way through a change is left to finish, and can
    take minutes. The button must not hang for that: the route says the
    turn is still running, and the stream says when it has ended."""
    monkeypatch.setattr(chat_api, "STOP_WAIT_S", 0.05)
    client = chat.env["client"]
    kitchen = compact.short_id(chat.ids["kitchen.jpg"])
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"
    reached, release = asyncio.Event(), asyncio.Event()

    async def count_clips(_args: dict[str, Any]) -> str:
        reached.set()
        await release.wait()
        return "41 clips."

    monkeypatch.setitem(
        tools.CHAT_ONLY,
        "count_clips",
        tools.ChatTool("count_clips", "A test tool.", {"type": "object"}, count_clips),
    )
    chat.script.then(
        answer(
            use("toolu_count", "count_clips"),
            use("toolu_write", "decide_tag", id=kitchen, tag="Silo", accept=True),
        )
    )
    async with Wire(
        chat.env["app"],
        "POST",
        f"{base}/messages",
        cookies=cookies_of(client),
        body={"text": "Count them, then tag the kitchen"},
    ) as wire:
        await asyncio.wait_for(reached.wait(), 10)
        stopped = await client.post(f"{base}/stop")
        assert stopped.status_code == 200 and stopped.json()["state"] == "running"
        assert not release.is_set(), "it did not wait for the tool"

        release.set()
        events = await wire.until("done")
        assert events[-1].data == {"reason": "stopped", "message": "Stopped."}
        (ran,) = named(events, "tool_result")
        assert (ran["id"], ran["state"], ran["summary"]) == ("toolu_count", "ran", "41 clips.")
        assert [asked["id"] for asked in named(events, "confirm")] == ["toolu_write"]

    shown = (await client.get(base)).json()
    assert shown["state"] == "awaiting_confirmation", "a stop is not a no"
    assert shown["last_turn"]["reason"] == "stopped"
    assert [asked["id"] for asked in shown["pending"]] == ["toolu_write"]
    assert [chip["state"] for chip in shown["messages"][1]["tools"]] == ["ran", "pending"]
    assert (await client.post(f"{base}/stop")).json()["state"] == "awaiting_confirmation"
    assert await chat.tag_source("kitchen.jpg", "Silo") == "suggested"
    async with chat.db() as db:
        assert (await db.execute(select(AuditLog).where(AuditLog.event.like("chat.%")))).all() == []


async def test_only_its_owner_can_stop_a_turn_and_a_read_only_account_cannot(
    chat: Chat,  # noqa: F811
) -> None:
    admin = chat.env["client"]
    cid = await new_conversation(admin)
    release = asyncio.Event()
    chat.script.then(held(release, "Working", " on it."))
    async with Wire(
        chat.env["app"],
        "POST",
        f"{URL}/conversations/{cid}/messages",
        cookies=cookies_of(admin),
        body={"text": "Hello"},
    ) as wire:
        await wire.until("text")
        run = engine.current(uuid.UUID(cid))
        assert run is not None

        async with signed_in(chat, "other@example.com", "user") as other:
            tried = await other.post(f"{URL}/conversations/{cid}/stop")
            assert tried.status_code == 404 and message(tried) == "No such conversation"
        async with AsyncClient(
            transport=ASGITransport(app=chat.env["app"]), base_url="http://t"
        ) as nobody:
            assert (await nobody.post(f"{URL}/conversations/{cid}/stop")).status_code == 401
        assert not run.stop_asked and not run.finished, "someone else's turn carries on"

        release.set()
        events = await wire.until("done")
        assert events[-1].data == {"reason": "end", "message": None}

    async with signed_in(chat, "viewer@example.com", "readonly") as viewer:
        async with chat.db() as db:
            me = (
                (await db.execute(select(User).where(User.email == "viewer@example.com")))
                .scalars()
                .one()
            )
            old = await store.create(db, user_id=me.id, model=MODEL)
            await db.commit()
        refused = await viewer.post(f"{URL}/conversations/{old.id}/stop")
        assert refused.status_code == 403 and refused.json()["error"]["code"] == "forbidden"


# --- a turn the server lost -------------------------------------------------------


async def test_after_a_restart_an_interrupted_conversation_is_free_and_says_what_happened(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The API is restarted by every update. The turn that was running is
    gone with the old process; its conversation still says `running`. The
    owner's next message used to be refused for five minutes."""
    client = chat.env["client"]
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"
    chat.script.then(answer(say("Barn and Silo.")))
    await client.post(f"{base}/messages", json={"text": "Which tags?"})

    release = asyncio.Event()
    chat.script.then(held(release, "Let me", " look."))
    async with Wire(
        chat.env["app"],
        "POST",
        f"{base}/messages",
        cookies=cookies_of(client),
        body={"text": "And the barn?"},
    ) as wire:
        await wire.until("text")
        # The server goes down under the turn: its task is cancelled and
        # the row is left as it was.
        (task,) = [t for t in engine._TASKS if t.get_name() == f"chat-{cid[:8]}"]
        task.cancel()
        await wire.until("done")
    # What the process that comes up next has in memory: nothing.
    engine._RUNS.clear()

    # Still the same process, as far as the clock can tell: the row was
    # touched since it began, so it may be running, and the old rule holds.
    held_up = (await client.get(base)).json()
    assert (held_up["state"], held_up["run"]) == ("running", None)
    refused = await client.post(f"{base}/messages", json={"text": "Hello?"})
    assert refused.status_code == 409 and "still working" in message(refused)

    # A process that began after the row was last touched. Not a second later.
    monkeypatch.setattr(store, "PROCESS_STARTED", store.now() + timedelta(milliseconds=100))
    freed = (await client.get(base)).json()
    assert (freed["state"], freed["run"]) == ("idle", None)
    assert freed["last_turn"] == {"reason": "error", "message": engine.STOPPED}, (
        "not the turn before's 'end', under a question that got no answer"
    )
    (listed,) = (await client.get(f"{URL}/conversations")).json()
    assert listed["state"] == "idle"
    assert (await client.post(f"{base}/stop")).json()["state"] == "idle"

    chat.script.then(answer(say("One barn.")))
    sent = await client.post(f"{base}/messages", json={"text": "Hello?"})
    assert sent.status_code == 200, sent.text
    assert parse(sent.text)[-1].data == {"reason": "end", "message": None}
    after = (await client.get(base)).json()
    assert after["state"] == "idle" and after["last_turn"] == {"reason": "end", "message": None}


# --- who may ----------------------------------------------------------------------


async def test_a_conversation_is_its_owners_and_nobody_elses_not_even_an_admins(
    chat: Chat,  # noqa: F811
) -> None:
    admin = chat.env["client"]
    kitchen = compact.short_id(chat.ids["kitchen.jpg"])
    cid = await new_conversation(admin)
    chat.script.then(
        answer(
            use("toolu_look", "view_media", id=kitchen),
            use("toolu_write", "decide_tag", id=kitchen, tag="Silo", accept=True),
        )
    )
    turn = await admin.post(f"{URL}/conversations/{cid}/messages", json={"text": "Look at this"})
    (shown,) = named(parse(turn.text), "tool_result")
    blob = shown["images"][0]["blob"]
    assert (await admin.get(f"{URL}/conversations/{cid}/blobs/{blob}")).status_code == 200

    yes = {"decisions": [{"tool_use_id": "toolu_write", "approve": True}]}
    async with signed_in(chat, "other@example.com", "user") as other:
        assert (await other.get(f"{URL}/conversations")).json() == []
        base = f"{URL}/conversations/{cid}"
        tried = [
            await other.get(base),
            await other.patch(base, json={"pictures": False}),
            await other.delete(base),
            await other.post(f"{base}/messages", json={"text": "Mine now"}),
            await other.post(f"{base}/confirm", json=yes),
            await other.get(f"{base}/events"),
            await other.get(f"{base}/blobs/{blob}"),
            await other.get(f"{base}/artifacts/toolu_write"),
        ]
        assert [resp.status_code for resp in tried] == [404] * 8
        assert {message(resp) for resp in tried} == {"No such conversation"}, (
            "the same answer as for a conversation that does not exist"
        )
        missing = await other.get(f"{URL}/conversations/{uuid.uuid4()}")
        assert missing.status_code == 404 and message(missing) == "No such conversation"

        # And the other way round: being an administrator opens nothing.
        theirs = await new_conversation(other)
        assert (await admin.get(f"{URL}/conversations/{theirs}")).status_code == 404
        assert [c["id"] for c in (await admin.get(f"{URL}/conversations")).json()] == [cid]

    assert len(chat.script.requests) == 1, "none of that reached Claude"
    assert await chat.tag_source("kitchen.jpg", "Silo") == "suggested", "nor the catalogue"
    untouched = (await admin.get(f"{URL}/conversations/{cid}")).json()
    assert untouched["pictures"] is True and untouched["state"] == "awaiting_confirmation"


async def test_a_read_only_account_can_read_its_conversations_and_start_nothing(
    chat: Chat,  # noqa: F811
) -> None:
    async with signed_in(chat, "viewer@example.com", "readonly") as viewer:
        status = (await viewer.get(f"{URL}/status")).json()
        assert status["can_chat"] is False and status["available"] is True
        assert (await viewer.get(f"{URL}/conversations")).json() == []
        started = await viewer.post(f"{URL}/conversations")
        assert started.status_code == 403 and "read-only" in message(started)

        # A conversation from before the account was made read-only.
        async with chat.db() as db:
            me = (
                (await db.execute(select(User).where(User.email == "viewer@example.com")))
                .scalars()
                .one()
            )
            old = await store.create(db, user_id=me.id, model=MODEL)
            await db.commit()
        base = f"{URL}/conversations/{old.id}"
        assert (await viewer.get(base)).status_code == 200
        assert (await viewer.get(f"{base}/events")).status_code == 204
        writes = [
            await viewer.post(f"{base}/messages", json={"text": "Hello"}),
            await viewer.post(
                f"{base}/confirm", json={"decisions": [{"tool_use_id": "x", "approve": True}]}
            ),
            await viewer.patch(base, json={"pictures": False}),
            await viewer.delete(base),
        ]
        assert [resp.status_code for resp in writes] == [403] * 4
        assert all(resp.json()["error"]["code"] == "forbidden" for resp in writes)
        assert (await viewer.get(base)).json()["pictures"] is True
    assert chat.script.requests == []


async def test_nobody_signed_in_gets_nothing(chat: Chat) -> None:  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=chat.env["app"]), base_url="http://t") as c:
        assert (await c.get(f"{URL}/status")).status_code == 401
        assert (await c.get(f"{URL}/conversations")).status_code == 401
        assert (await c.post(f"{URL}/conversations")).status_code == 401


# --- whether it can work at all ---------------------------------------------------


async def test_status_says_whether_the_panel_can_be_used_and_why_not(
    chat: Chat,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = chat.env["client"]
    ready = (await client.get(f"{URL}/status")).json()
    assert ready == {
        "available": True,
        "configured": True,
        "enabled": True,
        "problem": None,
        "can_chat": True,
        "model": "claude-opus-5-5",
        "models": engine.models(),
        "budget_usd": 5.0,
        "spent_this_month_usd": 0.0,
    }
    assert "claude-opus-5-5" in ready["models"]

    # Switched off on the Security page: a key is there, and it is not used.
    await chat.configure(enabled=False)
    off = (await client.get(f"{URL}/status")).json()
    assert (off["configured"], off["enabled"], off["problem"]) == (
        True,
        False,
        engine.SWITCHED_OFF,
    )
    cid = await new_conversation(client)
    turn = parse(
        (await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Hi"})).text
    )
    assert named(turn, "error") == [{"kind": "config", "message": engine.SWITCHED_OFF}]

    # No key at all: the stream still answers 200 and says so in an event.
    await chat.configure(enabled=True, api_key_sealed="")
    keyless = (await client.get(f"{URL}/status")).json()
    assert (keyless["configured"], keyless["problem"]) == (False, engine.NO_KEY)
    sent = await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Hello"})
    assert sent.status_code == 200
    turn = parse(sent.text)
    assert [event.name for event in turn] == ["start", "error", "done"]
    assert turn[1].data == {"kind": "config", "message": engine.NO_KEY}
    assert turn[2].data == {"reason": "error", "message": engine.NO_KEY}
    assert chat.script.requests == []
    said = (await client.get(f"{URL}/conversations/{cid}")).json()
    assert said["last_turn"] == {"reason": "error", "message": engine.NO_KEY}, (
        "a page that was not there to hear it can still say why"
    )

    # Installed without the SDK: nothing else breaks, and the panel says why.
    monkeypatch.setattr(claude_stream, "problem", lambda: claude_stream.NOT_INSTALLED)
    without = await client.get(f"{URL}/status")
    assert without.status_code == 200
    assert (without.json()["available"], without.json()["problem"]) == (
        False,
        claude_stream.NOT_INSTALLED,
    )
    for refused in (
        await client.post(f"{URL}/conversations"),
        await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Hello"}),
    ):
        assert refused.status_code == 503 and message(refused) == claude_stream.NOT_INSTALLED
        assert refused.json()["error"]["code"] == "unavailable"
    assert (await client.get(f"{URL}/conversations/{cid}")).status_code == 200


async def test_a_request_that_cannot_start_a_turn_is_refused_in_a_sentence(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    cid = await new_conversation(client)
    base = f"{URL}/conversations/{cid}"

    blank = await client.post(f"{base}/messages", json={"text": "   "})
    assert blank.status_code == 400 and message(blank) == "Write a message first."
    assert blank.json()["error"]["code"] == "bad_request"
    long = await client.post(f"{base}/messages", json={"text": "x" * (engine.MAX_TEXT_CHARS + 1)})
    assert long.status_code == 400 and "too long" in message(long)
    assert (await client.post(f"{base}/messages", json={})).status_code == 422

    nothing = await client.post(
        f"{base}/confirm", json={"decisions": [{"tool_use_id": "toolu_none", "approve": True}]}
    )
    assert nothing.status_code == 400 and "no longer waiting" in message(nothing)
    both = await client.post(
        f"{base}/confirm",
        json={
            "decisions": [
                {"tool_use_id": "toolu_a", "approve": True},
                {"tool_use_id": "toolu_a", "approve": False},
            ]
        },
    )
    assert both.status_code == 400 and "both yes and no" in message(both)
    assert (await client.post(f"{base}/confirm", json={"decisions": []})).status_code == 422
    assert (await client.get(f"{URL}/conversations/not-an-id")).status_code == 422

    assert chat.script.requests == []
    assert (await client.get(base)).json()["state"] == "idle", "a refusal holds nothing"
    async with chat.db() as db:
        assert (await db.execute(select(AuditLog).where(AuditLog.event.like("chat.%")))).all() == []


# --- pictures and files -----------------------------------------------------------


async def test_a_picture_from_a_turn_is_served_to_its_owner_as_the_bytes_claude_saw(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    barn = compact.short_id(chat.ids["barn-wide.jpg"])
    cid = await new_conversation(client)
    chat.script.then(answer(use("toolu_look", "view_media", id=barn)), answer(say("A barn.")))
    turn = await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Look"})
    (shown,) = named(parse(turn.text), "tool_result")
    (ref,) = shown["images"]
    assert ref["media_type"] == "image/jpeg"

    got = await client.get(f"{URL}/conversations/{cid}/blobs/{ref['blob']}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    sent_to_claude = chat.script.requests[1]["messages"][2]["content"][0]["content"][0]
    assert got.content == base64.b64decode(sent_to_claude["source"]["data"])
    assert "immutable" in got.headers["cache-control"] and "private" in got.headers["cache-control"]
    assert "sandbox" in got.headers["content-security-policy"]

    chip = (await client.get(f"{URL}/conversations/{cid}")).json()["messages"][1]["tools"][0]
    assert chip["images"] == [ref], "the stored conversation points at the same picture"

    for wrong in ("0" * 64, "not-a-hash", "..%2F..%2Fsecret"):
        missing = await client.get(f"{URL}/conversations/{cid}/blobs/{wrong}")
        assert missing.status_code == 404, wrong


async def test_the_pictures_switch_keeps_later_previews_from_claude_not_from_the_operator(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    barn = compact.short_id(chat.ids["barn-wide.jpg"])
    cid = await new_conversation(client)
    switched = await client.patch(f"{URL}/conversations/{cid}", json={"pictures": False})
    assert switched.status_code == 200 and switched.json()["pictures"] is False
    assert (await client.patch(f"{URL}/conversations/{cid}", json={})).status_code == 422

    chat.script.then(answer(use("toolu_look", "view_media", id=barn)), answer(say("Described.")))
    turn = parse(
        (await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Look"})).text
    )
    assert turn[0].data["pictures"] is False
    assert "base64" not in json.dumps(chat.script.requests[1]), "no picture left the building"
    (shown,) = named(turn, "tool_result")
    seen = await client.get(f"{URL}/conversations/{cid}/blobs/{shown['images'][0]['blob']}")
    assert seen.status_code == 200, "the operator is still shown it"

    # A conversation can also be started with them off, or switched as a
    # message is sent.
    quiet = (await client.post(f"{URL}/conversations", json={"pictures": False})).json()
    assert quiet["pictures"] is False
    chat.script.then(answer(say("On again.")))
    await client.post(
        f"{URL}/conversations/{cid}/messages", json={"text": "Pictures on", "pictures": True}
    )
    assert (await client.get(f"{URL}/conversations/{cid}")).json()["pictures"] is True


async def test_a_timeline_a_tool_built_is_a_download(chat: Chat) -> None:  # noqa: F811
    client = chat.env["client"]
    video = await _video(chat.env)
    cid = await new_conversation(client)
    chat.script.then(
        answer(
            use("toolu_tags", "list_tags"),
            use(
                "toolu_cut",
                "build_rough_cut",
                title="Ledgerock 60",
                cuts=[{"id": str(video), "in_s": 12, "out_s": 16}],
                profile="Mac Mini",
            ),
        ),
        answer(say("Your timeline is ready.")),
    )
    turn = parse(
        (await client.post(f"{URL}/conversations/{cid}/messages", json={"text": "Cut it"})).text
    )
    plain, built = named(turn, "tool_result")
    assert plain["artifact"] is None and built["artifact"] == {"name": "Ledgerock 60.xml"}

    got = await client.get(f"{URL}/conversations/{cid}/artifacts/toolu_cut")
    assert got.status_code == 200, got.text
    assert got.headers["content-type"].startswith("application/xml")
    assert got.headers["content-disposition"] == (
        "attachment; filename=\"Ledgerock 60.xml\"; filename*=UTF-8''Ledgerock%2060.xml"
    ), "a plain name for old browsers and the exact one for the rest"
    assert got.text.startswith("<?xml") and "<xmeml" in got.text and "Ledgerock 60" in got.text

    chips = (await client.get(f"{URL}/conversations/{cid}")).json()["messages"][1]["tools"]
    assert [chip["artifact"] for chip in chips] == [None, {"name": "Ledgerock 60.xml"}]
    for wrong in ("toolu_tags", "toolu_never"):
        missing = await client.get(f"{URL}/conversations/{cid}/artifacts/{wrong}")
        assert missing.status_code == 404, wrong


# --- the model setting ------------------------------------------------------------


async def test_the_chat_model_is_set_with_the_other_claude_settings(
    chat: Chat,  # noqa: F811
) -> None:
    client = chat.env["client"]
    shown = (await client.get("/api/v1/develop/settings/ai")).json()
    assert shown["chat_model"] == "claude-opus-5-5"
    assert shown["chat_models"] == engine.models()
    assert "claude-haiku-4-5" not in shown["chat_models"], "it takes a different request"
    before = await new_conversation(client)

    changed = await client.put(
        "/api/v1/develop/settings/ai", json={"chat_model": "claude-sonnet-5-5"}
    )
    assert changed.status_code == 200 and changed.json()["chat_model"] == "claude-sonnet-5-5"
    assert (await client.get(f"{URL}/status")).json()["model"] == "claude-sonnet-5-5"
    after = await new_conversation(client)
    assert (await client.get(f"{URL}/conversations/{after}")).json()["model"] == (
        "claude-sonnet-5-5"
    )
    assert (await client.get(f"{URL}/conversations/{before}")).json()["model"] == MODEL, (
        "a conversation keeps the model it began on"
    )
    chat.script.then(answer(say("Hello.")))
    await client.post(f"{URL}/conversations/{after}/messages", json={"text": "Hello"})
    assert chat.script.requests[0]["model"] == "claude-sonnet-5-5"

    # A model with no price would spend outside the budget; one that takes a
    # different request would fail on the first message. Neither is taken,
    # nothing else in the same save is applied, and each is refused for the
    # reason that is true of it: the second has a price, and can be the
    # review model on this same form.
    for unknown, why, not_why in (
        ("claude-made-up-9", "no price for it", "different kind of request"),
        ("claude-haiku-4-5", "different kind of request", "price"),
    ):
        refused = await client.put(
            "/api/v1/develop/settings/ai",
            json={"chat_model": unknown, "monthly_budget_usd": 99},
        )
        assert refused.status_code == 400, unknown
        assert unknown in message(refused) and "claude-opus-5-5" in message(refused)
        assert why in message(refused) and not_why not in message(refused), unknown
    kept = (await client.get("/api/v1/develop/settings/ai")).json()
    assert (kept["chat_model"], kept["monthly_budget_usd"]) == ("claude-sonnet-5-5", 5.0)

    async with signed_in(chat, "editor@example.com", "user") as editor:
        denied = await editor.put(
            "/api/v1/develop/settings/ai", json={"chat_model": "claude-opus-5-5"}
        )
        assert denied.status_code == 403
        assert (await editor.get("/api/v1/develop/settings/ai")).json()["chat_models"]
