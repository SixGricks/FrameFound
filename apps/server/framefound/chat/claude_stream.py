"""One streaming request to the Claude API: the only code that talks to it.

Everything the chat panel sends to Anthropic goes through
`stream_message`, by way of the official `anthropic` SDK. The engine
depends on that one function and nothing else here, so the test suite
replaces it with a scripted generator and never opens a socket.

The SDK is an optional extra (`chat`), imported when first needed. Without
it `problem()` says so and the rest of FrameFound is unaffected.

What comes out is deliberately plain, so the engine and its tests deal in
dictionaries and not in SDK objects:

    {"type": "usage", "usage": {...}}          what the API has counted so far
    {"type": "thinking"}                       Claude began to think
    {"type": "text", "text": "..."}            more of the answer
    {"type": "tool_use", "id": .., "name": ..} Claude began asking for a tool
    {"type": "alive"}                          something else arrived
    {"type": "message", "message": {...}}      last: the whole answer

**Every event the SDK delivers is passed on as something**, even when
there is nothing in it to show: a piece of a tool call's arguments, the
signature of a thinking block, the end of a block. Those come out as
`alive`. Whoever is listening decides from them whether the answer is still
arriving, and an answer two minutes into writing a long tool call is
arriving, though none of it can be shown yet.

`usage` is said twice: when the answer begins (the API has read the prompt
and counted it, which is the moment the request starts to cost money) and
when it ends (the output is counted too). A listener that keeps the last
one knows what a request has cost even if it never finishes.

The final message is `{"id", "model", "stop_reason", "content", "usage"}`:
`content` is the answer's content blocks as the SDK would send them back
(thinking blocks and their signatures included, untouched) and `usage` has
the four token counts the budget ledger prices.

A failure raises `ClaudeUnavailable` with a sentence for the operator. The
API key is passed to the SDK and appears nowhere else: not in a message,
not in a log line.
"""

import importlib
from collections.abc import AsyncIterator
from typing import Any

import structlog

log = structlog.get_logger()

# Seconds allowed for the connection, and for the connection to send
# nothing at all. The second is the check for a dead connection: while
# Claude is working the API keeps sending `ping` events, which the SDK
# reads and drops, so a healthy answer does not come near it however long
# Claude thinks. (The engine's own limit, STREAM_IDLE_S, is a backstop
# behind this one and has to stay the longer of the two.)
CONNECT_TIMEOUT_S = 10.0
TIMEOUT_S = 300.0
# The SDK retries a refused connection, a 429 and a 5xx by itself, before
# any of the answer has arrived. Never mid-answer: a half-streamed answer
# that restarted would be shown twice.
MAX_RETRIES = 2

DAMAGED = "Claude's answer arrived damaged and was discarded. Nothing was changed; ask again."
LOST = (
    "The connection to Anthropic was lost part-way through an answer. Nothing was changed; "
    "ask again."
)
NOT_INSTALLED = (
    "The chat panel needs the Anthropic SDK, which this server was installed without "
    "(the `chat` extra)."
)


class ClaudeUnavailable(RuntimeError):
    """The request could not be made or did not finish. The message is a
    sentence for the operator. `fatal` means trying again will not help
    until something is changed (the key, the model, the account).

    `usage` is what the API had counted when the request stopped: None for
    one that never began to be answered (refused, unreachable: Anthropic
    bills nothing), the token counts for one that failed part-way (the
    prompt was read and is billed, whatever became of the answer). The
    engine fills it in from the `usage` events it heard.
    """

    def __init__(
        self, message: str, *, fatal: bool = False, usage: dict[str, int] | None = None
    ) -> None:
        super().__init__(message)
        self.fatal = fatal
        self.usage = usage


def _sdk() -> Any:
    """The `anthropic` package, as `Any`: the type checker runs with and
    without the extra installed, and must say the same thing both times."""
    try:
        return importlib.import_module("anthropic")
    except ImportError as exc:
        raise ClaudeUnavailable(NOT_INSTALLED, fatal=True) from exc


def problem() -> str | None:
    """Why Claude cannot be called on this install, or None."""
    try:
        _sdk()
    except ClaudeUnavailable as exc:
        return str(exc)
    return None


def _client(sdk: Any, api_key: str) -> Any:
    """A client for one request. Given the key explicitly, the SDK does not
    look in the environment for another credential."""
    return sdk.AsyncAnthropic(
        api_key=api_key,
        max_retries=MAX_RETRIES,
        timeout=sdk.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    )


def _explain(exc: Any) -> ClaudeUnavailable:
    """An API error as a sentence. The API's own message names the problem
    ("credit balance is too low") and never echoes the key or a picture, so
    it is safe to show; it is trimmed, not trusted to be short."""
    status = int(getattr(exc, "status_code", 0) or 0)
    kind = str(getattr(exc, "type", "") or "")
    said = " ".join(str(getattr(exc, "api_message", "") or "").split())[:200]
    lowered = said.lower()
    if status in (401, 403) or kind in ("authentication_error", "permission_error"):
        return ClaudeUnavailable(
            "Anthropic refused the API key. Check the key on the Security page.", fatal=True
        )
    if "credit" in lowered or "billing" in lowered or "balance" in lowered:
        return ClaudeUnavailable(
            f"Anthropic says the account cannot pay for this: {said}", fatal=True
        )
    if status == 404 or kind == "not_found_error":
        return ClaudeUnavailable(
            "Anthropic does not offer this conversation's model to this key"
            + (f": {said}" if said else "."),
            fatal=True,
        )
    if status == 413 or kind == "request_too_large":
        return ClaudeUnavailable(
            "This conversation has grown too large to send. Start a new one.", fatal=True
        )
    if status == 429 or kind == "rate_limit_error":
        return ClaudeUnavailable(
            "Anthropic is limiting how fast this key may ask. Wait a minute and try again."
        )
    if status >= 500 or kind in ("overloaded_error", "api_error", "timeout_error"):
        return ClaudeUnavailable("Anthropic is overloaded or having trouble. Try again shortly.")
    return ClaudeUnavailable(
        "Anthropic rejected the request" + (f": {said}" if said else f" (HTTP {status})."),
        fatal=True,
    )


def _on_the_wire(exc: BaseException) -> bool:
    """Whether an error is the connection's rather than the answer's: a
    read that timed out, a peer that hung up. The SDK turns these into its
    own errors only while a request is being made; once an answer is
    streaming, the HTTP client's exception comes through as it is, and the
    SDK does not say which client that is, so it is known by where its
    class lives."""
    home = type(exc).__module__.partition(".")[0]
    return home.startswith(("httpx", "httpcore")) or isinstance(exc, TimeoutError | OSError)


def _count(value: Any) -> int:
    return int(value or 0)


def _counts(usage: Any) -> dict[str, int]:
    """The four token counts the ledger prices, from the SDK's usage object."""
    return {
        "input_tokens": _count(usage.input_tokens),
        "output_tokens": _count(usage.output_tokens),
        "cache_creation_input_tokens": _count(usage.cache_creation_input_tokens),
        "cache_read_input_tokens": _count(usage.cache_read_input_tokens),
    }


def _plain(final: Any) -> dict[str, Any]:
    """The finished answer as a dictionary. `to_dict` leaves out what the
    API did not send, so each block goes back as it came."""
    return {
        "id": str(final.id),
        "model": str(final.model),
        "stop_reason": final.stop_reason,
        "content": [block.to_dict(mode="json") for block in final.content],
        "usage": _counts(final.usage),
    }


async def stream_message(request: dict[str, Any], api_key: str) -> AsyncIterator[dict[str, Any]]:
    """Send one request and yield what comes back as it arrives; see the
    module docstring for the events. `request` is the keyword arguments of
    `client.messages.stream` (model, max_tokens, system, tools, messages,
    thinking, output_config), built by the engine.

    Raises `ClaudeUnavailable`. Nothing is retried once an answer has begun.
    If a `usage` event was yielded before the failure, the request had been
    accepted and is billed.
    """
    sdk = _sdk()
    final: Any = None
    try:
        async with _client(sdk, api_key) as client, client.messages.stream(**request) as stream:
            async for event in stream:
                kind = event.type
                heard: dict[str, Any] | None = None
                if kind == "text":
                    heard = {"type": "text", "text": event.text}
                elif kind in ("message_start", "message_delta"):
                    # The SDK keeps the running total: the prompt's counts
                    # from the first of these, the output's from the last.
                    counted = _counts(stream.current_message_snapshot.usage)
                    heard = {"type": "usage", "usage": counted}
                elif kind == "content_block_start":
                    block = event.content_block
                    if block.type in ("thinking", "redacted_thinking"):
                        heard = {"type": "thinking"}
                    elif block.type == "tool_use":
                        heard = {"type": "tool_use", "id": block.id, "name": block.name}
                # Never swallowed. A listener told nothing while Claude
                # writes a long tool call would take a healthy answer for a
                # dead one, and give up on it.
                yield heard or {"type": "alive"}
            final = await stream.get_final_message()
    except sdk.APIStatusError as exc:
        # Also an `error` event part-way through an answer (overloaded):
        # the SDK raises it as a status error with the stream's 200.
        log.warning(
            "chat.api_error",
            status=getattr(exc, "status_code", None),
            kind=getattr(exc, "type", None),
            request_id=getattr(exc, "request_id", None),
        )
        raise _explain(exc) from None
    except sdk.APIConnectionError as exc:
        log.warning("chat.api_unreachable", error=type(exc).__name__)
        raise ClaudeUnavailable(
            "FrameFound could not reach Anthropic. Check this server's internet connection "
            "and try again."
        ) from None
    except sdk.APIError as exc:
        log.warning("chat.api_error", error=type(exc).__name__)
        raise ClaudeUnavailable("Anthropic's answer could not be read. Try again.") from None
    except ValueError as exc:
        # The SDK could not parse the arguments Claude wrote for a tool.
        # No tool call was completed, so there is nothing to answer; the
        # operator asks again.
        log.warning("chat.unreadable_tool_input", error=str(exc)[:200])
        raise ClaudeUnavailable(DAMAGED) from None
    except Exception as exc:
        if _on_the_wire(exc):
            # Nothing came down the connection for TIMEOUT_S, or it was
            # dropped. This is the check for a dead stream (see TIMEOUT_S),
            # so it gets its own sentence and not "arrived damaged".
            log.warning("chat.stream_lost", error=type(exc).__name__)
            raise ClaudeUnavailable(LOST) from None
        # Events out of order, a block the SDK cannot place: whatever it
        # was, the answer is not one to act on, and the turn must be told
        # so in the same way as any other failed request.
        log.exception("chat.stream_unreadable")
        raise ClaudeUnavailable(DAMAGED) from None
    yield {"type": "message", "message": _plain(final)}
