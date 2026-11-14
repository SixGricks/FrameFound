"""The chat panel: a conversation with Claude inside FrameFound.

The operator already has the MCP connector, which lets Claude (in Claude
Desktop or Cowork) search the catalogue, look at previews, read
transcripts and build timelines. This package gives the web UI the same
abilities without leaving the page: the server holds the conversation,
calls the Claude API itself, and runs the connector's tools in process,
as the signed-in user.

What leaves the building is what the connector already sends, and nothing
else: what the operator types, the page they are on, what the tools answer
(names, tags, transcript lines) and any preview Claude asks to look at.
Originals never leave, and a switch per conversation keeps pictures back.

Three rules the package keeps whatever Claude asks:

- A tool that changes the catalogue or spends money (`tools.CONFIRM_FIRST`)
  is not run until the operator says yes to that one call.
- The monthly budget on the Security page is checked before every request
  to the API, not once per question: a question can take a dozen requests.
- What Claude was sent is never rewritten. A conversation is replayed to
  the API byte for byte on every request (the API rejects one whose earlier
  turns changed), so its instructions, its tool list, its pictures and
  Claude's own turns are stored as they were first sent or received.

`store` keeps conversations; `tools` is the registry over the connector;
`claude_stream` is the one function that talks to Anthropic; `engine` runs
a turn. docs/chat.md is the operator's description.
"""
