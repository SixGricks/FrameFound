# ADR-0020: Claude as a client — an MCP connector inside the API, and a second opinion under a budget

- Status: Accepted
- Date: 2026-11-05

## Context

The operator wants to talk to the catalogue from Claude: ask for media by
tag, have Claude look at pictures and propose tags so the tagging model keeps
learning, and do it from outside the office. Three forces shape how:

1. **The learner cannot invent vocabulary.** `ai/tagging.py` spreads a tag the
   operator typed; it cannot say "this is a horse barn" about a picture nobody
   has tagged. A model that can look at a thumbnail can, and the operator's
   yes or no on that proposal is exactly the training signal the learner
   wants.
2. **Credentials and exposure.** Every way into FrameFound is a listed,
   revocable thing (sessions, panel tokens), and the app enforces its own
   access gate rather than trusting the network. A connector must not become
   the exception.
3. **Cost.** The recipe-picker already spends money through the Claude API on
   the operator's key. Anything unsupervised that spends more needs a ceiling
   the operator sets and the software enforces.

The MCP Python SDK (2.x) ships an HTTP transport, an OAuth authorization
server and a token-verifier hook. Claude Code and Claude Desktop can send a
bearer header to a remote server; claude.ai's custom connectors cannot (they
expect OAuth on a publicly reachable URL).

## Decision

**The connector lives inside the API process**, mounted at `/api/mcp` in
`framefound.main` — one package, one image, one more entrypoint surface,
per ADR-0001. It rides Caddy's existing `/api/*` route, the public-access
gate and whichever network the web UI is reached on, so Tailscale access to
the UI is Tailscale access to Claude. Stateless, plain-JSON streamable HTTP:
nothing to time out behind a proxy, nothing to lose on a restart.

**Authentication is the panel token**, with a new `mcp` host label and one
new scope, `tag`. The SDK's OAuth machinery is not used: FrameFound already
has a credential that is minted, listed and revoked on the Security page.
`tag` is the first catalogue write any scope grants, and the scope vocabulary
test now says so in words: suggestions and the operator's relayed decisions,
never a file, never an original. A read-only account's token cannot tag.

**Claude's tags are suggestions.** `propose_tags` writes `suggested` rows
marked `origin = "mcp"`; `decide_tag` records the operator's answer and
re-runs the learner. The learner's own rows are now marked `origin = "clip"`
and the server-side reviewer's `origin = "claude"`, so the review queue says
who is asking.

**The server-side reviewer** (`ai/tag_review.py`, `processing/tag_review_tasks.py`)
sends the thumbnail FrameFound already made to the Claude API for untagged
media, by hand from the Tags page, from the connector, or nightly when turned
on. An **admission policy** (`tag_review.admit`) decides which unsupervised
answers reach the queue — left as the operator's decision point, with
conservative defaults.

**One ledger, one cap.** `ai_usage` records every call FrameFound makes to
the API with the token counts the API reported; `ai/spend.py` prices them
from a dated table. The monthly budget on the Security page is checked
*before* each call from that ledger, for the reviewer, auto-edit and naming
alike, and a run that would cross it stops and says so. Zero pauses
everything without removing the key.

The API calls stay on raw `httpx` as the recipe-picker's do (one shared
`post_messages`), rather than adding the Anthropic SDK beside an existing
hand-rolled client.

## Alternatives considered

- **A stdio MCP server on the laptop talking to the REST API.** Simplest
  auth story, but it duplicates the API as tool wrappers, needs its own
  install, and gives Claude nothing the browser does not have. Rejected; the
  bridge for Claude Desktop (`mcp-remote`) is one line of config instead.
- **The SDK's OAuth authorization server.** Would let claude.ai connect —
  only over a public URL, which this install does not have and should not
  need. Deferred: possible later as Tailscale Funnel plus an OAuth layer that
  issues tokens bound to panel tokens.
- **Letting Claude apply tags directly above a confidence.** Faster, and it
  would make the learner's positive examples partly Claude's judgement rather
  than the operator's. The whole feature rests on the examples being human
  decisions. Rejected; the operator can still say "yes" in chat and have it
  recorded.
- **A per-call or per-run cap instead of a monthly one.** Harder to reason
  about from a Security page; the operator thinks in bills. A monthly figure
  with the pre-call check gives the same protection in the units they use.

## Consequences

- Claude Code and Claude Desktop work today over LAN or Tailscale with a
  token. claude.ai's browser connector does not, and the docs say so.
- `mcp` is a new optional extra (`framefound[mcp]`); the API runs without it
  and logs once. The Docker image and CI install it; the VM's `ff-dev` test
  image must be rebuilt for the connector tests to run there rather than skip.
- `asset_tags` gains `origin`; `ai_usage` is new (migration 0025). The
  bake-off tool now prices from `ai/spend.py`, so its dollars and the Security
  page's agree.
- Thumbnails leave the machine only when the operator connects Claude and asks,
  presses Review, or turns the nightly sweep on — the same per-action consent
  the recipe-picker set, now with a ceiling.
