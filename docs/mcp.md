# Claude as a client — the MCP connector

FrameFound exposes its catalogue to Claude through the [Model Context
Protocol](https://modelcontextprotocol.io): a small set of tools Claude can
call from Claude Code or Claude Desktop. Ask for "the drone shots tagged barn
from September", have Claude look at a batch of thumbnails and say what they
are, work through the tag review queue in conversation. Everything it does
goes through the same API, the same tags and the same previews the web UI
uses, so Claude and the browser never disagree about a result.

Two rules hold whatever Claude is asked:

- **Nothing writes to an original.** The connector reads the catalogue and
  the derived previews. The library is read-only to it, as to everything.
- **A tag Claude proposes is a suggestion.** It enters the same review queue
  as the learner's guesses, marked as Claude's, and becomes an example only
  when you say yes — on the Tags page, or in chat through `decide_tag`.

---

## Connecting

1. **Make a token.** Security → Panel tokens → *Which application?* →
   **Claude (MCP connector)**, name the machine, keep **Allow tagging**
   ticked if you want Claude to propose and record tags, and create it.
   The token is shown once; copy it.
2. **Point Claude at the server.** The address is wherever you reach
   FrameFound from that machine, plus `/api/mcp`:
   - on the office network: `http://<server>:8080/api/mcp`
   - from anywhere, over Tailscale: `http://framefound:8080/api/mcp` (or the
     `https://framefound.<tailnet>.ts.net/api/mcp` address once `tailscale
     serve` is on — see [remote-access.md](remote-access.md))

   The one-time token card on the Security page prints the Claude Code
   command with the right address and the token already in it:

   ```bash
   claude mcp add --transport http framefound http://framefound:8080/api/mcp --header "Authorization: Bearer ffp_…"
   ```

   **Claude Desktop (and Cowork in it)** has no field for a bearer header,
   so it goes through the small `mcp-remote` bridge. Install Node.js from
   nodejs.org first, then *Settings → Developer → Edit config*, which opens
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows and
   `~/Library/Application Support/Claude/claude_desktop_config.json` on a
   Mac. The same block works on both:

   ```json
   {
     "mcpServers": {
       "framefound": {
         "command": "npx",
         "args": [
           "-y", "mcp-remote", "http://framefound:8080/api/mcp",
           "--allow-http",
           "--header", "Authorization:${AUTH_HEADER}"
         ],
         "env": { "AUTH_HEADER": "Bearer ffp_…" }
       }
     }
   }
   ```

   Three details in it are load-bearing:
   - `--allow-http`: `mcp-remote` refuses plain `http://` to anything but
     localhost without it. Drop it if you use the `https://….ts.net` address.
   - `Authorization:${AUTH_HEADER}` with **no space** after the colon, and
     the space inside the variable: Claude Desktop on Windows does not quote
     arguments, and `Authorization: Bearer …` arrives split in three.
   - On a **Mac**, Claude Desktop does not see your shell's `PATH`. If it
     reports `spawn npx ENOENT`, use the full path from `which npx` (for
     Homebrew, `"/opt/homebrew/bin/npx"`) as `command`.

   Quit and reopen Claude Desktop after saving.

   **claude.ai in a browser** (the "custom connector" in Settings) is not
   supported yet: it needs a server reachable from the public internet that
   speaks OAuth, and FrameFound deliberately has neither. That is a possible
   later step (Tailscale Funnel plus an OAuth layer over panel tokens) and is
   on the roadmap, not in the product.

3. **Say hello.** "What's in FrameFound?" — Claude calls `overview`, which
   names the libraries and says whether this token may tag.

---

## Making a video with Claude (Cowork)

Ask in plain words: "find the drone footage of the Ledgerock barn and the
part where the auctioneer talks about the starting bid, and make me a
sixty-second rough cut I can open in Premiere on this Mac." Claude:

1. calls `path_profiles` (or asks you) to learn which computer it is on;
2. finds clips with `search_media` (`media_type: "video"`), which ranks by
   tag, by what the footage looks like and by what was said, and gives the
   best moment in each (`at_ms`; a photo hit has none, as it has no moment);
3. reads `get_transcript` for the quote's exact cue times and `get_scenes`
   for clean b-roll ranges (one per detected shot), and looks at the shots
   it needs with `view_media(id, ts_ms=…)`, which hands Claude the nearest
   sampled frame as a picture. The signed `frame_url` links are for you:
   Claude Desktop fetches the web from Anthropic's side, which cannot reach
   a server on your network;
4. calls `build_rough_cut` with the pieces and your profile, and saves the
   XML it returns. *File → Import* in Premiere Pro, or *File → Import →
   Timeline* in DaVinci Resolve, opens it as a timeline whose clips point
   at the originals on your share — nothing is copied, and nothing needs
   relinking. Final Cut Pro (10/11) reads only FCPXML, so it opens the file
   only after a converter such as 7toX has turned it into one.

When Claude needs the footage itself (to cut, caption or check it), it
downloads the 1080p proxy from `get_media_files`; the full-quality originals
stay on the NAS and open through the share. Cowork can also open them
directly if you grant it the share's folder (`Y:\` on the PC,
`/Volumes/<share>` on the Mac).

Each computer needs a **path profile** for every library (Libraries →
the library → path profiles): `Y:\` or `\\nas\share` for Windows,
`/Volumes/<share>` for a Mac. FrameFound refuses a profile whose path does
not fit its platform.

---

## What Claude can do

| Tool | Does | Needs |
|---|---|---|
| `overview` | Libraries, tag and queue counts, what this token may do | read |
| `list_tags` | The vocabulary, with confirmed and pending counts | read |
| `find_media` | Media by tags (`all` or `any`), type, library, capture dates; newest first | read |
| `search_media` | Words → tags, visual search, transcripts, file names, merged and ranked | read |
| `get_media` | Everything the catalogue knows about one item, with signed preview links | read |
| `view_media` | The thumbnail (or a video's poster) itself, so Claude can look; with `ts_ms`, the video's sampled frame nearest that moment | read |
| `review_queue` | Suggestions awaiting your judgement, strongest first, by tag or by who suggested | read |
| `propose_tags` | Queue tag suggestions for one item, marked as Claude's | tag |
| `decide_tag` | Record *your* yes or no on one tag for one item; the learner re-runs | tag |
| `api_review_status` | The server-side reviewer: key, budget, what is left, last run | read |
| `start_api_review` | Have the server review a batch with the Claude API, under the budget (admin accounts only) | tag |
| `path_profiles` | Each computer's path profile (Windows PC, Mac) and where it sees each library | read |
| `get_media_files` | One item's original path on each computer, and signed links to its proxy, poster and captions | read |
| `get_transcript` | Every timed cue of what was said, optionally between two times | read |
| `get_scenes` | A video's shots as start/end ranges, one per detected cut, with the moments to pass to `view_media` (and a signed link for you) | read |
| `build_rough_cut` | Chosen pieces laid end to end as an FCP7 XML timeline for Premiere Pro or DaVinci Resolve (Final Cut Pro only via a converter such as 7toX) | read |

`find_media` and `search_media` also take `profile` (adds `local_path`, the
original as that computer opens it) and `search_media` takes `media_type`
(`image`, `video`, `audio`).

Every result carries a signed link to the preview, valid for twelve hours,
built from the address you connected on — so a link Claude hands you opens
from the same place, without signing in, exactly as the panels' links do.

### Two ways to get a second opinion

**In conversation** — "look at the last twenty untagged pictures and tell me
what they are." Claude calls `view_media` on each, proposes tags with
`propose_tags`, and you answer on the Tags page or by telling Claude, which
records it with `decide_tag`. This costs nothing beyond your Claude
subscription; the pictures go to Claude as small JPEG thumbnails, one per
call, only while you are asking.

**On the server** — "Review with Claude" on the Tags page, `start_api_review`
here, or the nightly sweep. FrameFound sends thumbnails to the Claude API with
the key on the Security page, and an admission policy decides which answers
are worth your time. Capped by the monthly budget on the same page, enforced
from a ledger of what every call actually cost, *before* the call that would
cross it. [tagging.md](tagging.md) has the details.

---

## Security

- **Authentication is a panel token**, the same credential the Premiere panel
  and the Lightroom plugin use: shown once, stored as a hash, listed with its
  prefix and last use, revocable from the Security page. The MCP SDK's own
  OAuth machinery is not used — a second kind of credential would be a second
  thing to leak.
- **Scopes.** `read` is search, metadata and previews. `tag` adds queuing
  suggestions and recording your decisions. There is no scope that reaches
  a file. A token made by a **read-only** account cannot tag even with the
  scope, because the account could not in a browser either.
- **The same gates apply.** The connector sits behind Caddy's `/api/*`
  route, the public-access kill switch, and whatever network you reach the
  web UI on. Turn off public access and the connector is off from the
  internet too, on the next request.
- **Every write is logged** with the token's name: proposals, decisions and
  reviews started.

---

## When it does not work

| Symptom | Cause |
|---|---|
| `401` with *A FrameFound panel token is required* | No `Authorization: Bearer ffp_…` header, a revoked or expired token, or a typo. Make a new one; the old one is listed as revoked. |
| `403` *Remote access is turned off* | You reached the server from the internet with public access off. Use Tailscale, or turn public access on (not recommended). |
| `propose_tags` says the token cannot write tags | Create a token with **Allow tagging** ticked, from an account that is not read-only. |
| `view_media`: *No thumbnail has been generated* | The picture has not been through processing yet; the Processing page shows the backlog. |
| `view_media` with `ts_ms`: *No frames have been sampled* | The video's frames have not been extracted yet (same backlog); its poster still works without `ts_ms`. |
| The API log says `mcp.unavailable` | The server was installed without the `mcp` extra: `pip install 'framefound[mcp]'` (the Docker image includes it). |

---

## Reference

| | |
|---|---|
| Tools | `framefound/mcp/server.py` |
| Token gate | `framefound/mcp/auth.py` |
| Mount | `framefound/main.py` (`/api/mcp`, stateless streamable HTTP) |
| Decision | [ADR-0020](adr/0020-mcp-connector.md) |
| Tests | `tests/test_mcp.py` — a real MCP client over the real transport, in process |
