# ADR-0021: Editing proxies are paired, not catalogued; selects go to Premiere as markers and a string-out

- Status: Accepted
- Date: 2026-11-06

## Context

The GELCO videos (Quick 30s, project stories, year-end pieces) are edited in
Premiere Pro 26, now on the Mac mini, with the Surface on `Y:\` as the travel
machine. Two problems sat between the catalogue and that edit.

**Premiere's proxies were catalogued as footage.** Premiere's ingest writes
`<clip folder>/Proxies/<stem>_Proxy.<ext>`. The scanner indexed every one as
an asset: `Pete Dye/2025 10 28/MVI_4513.MP4` and
`…/Proxies/MVI_4513_Proxy.mp4` both came back from every search, the proxy
dated 2026-02-12 (the day it was rendered) for a 2025-10-28 shoot, and each
cost a second transcription, embedding and tag pass. 33 such rows in GELCO.

**A search could become a bin, or an edit, but not selects.** `build_bin`
hands over whole clips; `build_rough_cut` lays out cuts somebody already
chose. The step an editor actually does after a search — find where each
soundbite starts and ends, find the shot, string them out to review — was
done by hand.

## Decision

### Pairing

A file matching the library's **proxy pattern** (default
`Proxies/{stem}_Proxy`: a `Proxies` folder beside the original, the
original's stem plus `_Proxy`, any video extension, any case) whose original
is catalogued and online **keeps its row** with `proxy_of_id` set to the
original. That row is out of the catalogue: search, Browse, the tag learner,
the reviewer, the review queues, People, Places and the processing sweeps all
filter it out, and the task shell skips it. A proxy whose original is *not*
there stays an ordinary asset — it is the only copy — and a paired proxy is
released the scan after its original goes.

Rows rather than a column on the original, because the old duplicates are
rows already: one representation covers new files, existing rows and a
released proxy, and nothing is deleted. Tag decisions on a proxy move to the
original (a human decision replaces a mere suggestion there; the original's
own decision stands). Existing rows are linked by
`python -m framefound.ops.pair_proxies`, which reports first and writes only
with `--apply`.

**The pairing is used, not only hidden.** Frame sampling and scene detection
decode the paired proxy instead of the 4K original when FrameFound has no
proxy of its own: a fraction of the bytes over a 5 MB/s share, and the same
timeline frame for frame. It is deliberately *not* registered as a
derivative: derivatives live on the data volume and are deleted and rebuilt
at will, Premiere's presets include ProRes a browser cannot play, and the
file is the editor's. FrameFound still only reads the share.

### Selects

One FCP7 XML, built on the existing writer (`nle/fcp7.py`, now with a
`<project>` holding a bin and a sequence that share file ids):

1. a bin of the source clips with a marker at each selected moment, its
   comment the reason (the sentence said, `tag: …`, `visual: <query>`);
2. a `<title> selects` sequence of the moments end to end, each with
   handles (1 s, clamped to the clip) and the same marker on the words.

Moments come from the transcript (a hit widened to whole sentences, at most
20 s), from detected scenes (the shot holding the frame, at most 8 s centred
on it; ±3 s with no scene data), or a still held for 4 s. Overlapping
moments in one clip merge. Order is search rank or capture time. Every path
goes through the workstation's path profile. Surfaces, in order: the MCP tool
`build_selects`, *Send to Premiere* on the web search page, and
`POST /panel/export/selects` (`export` scope) for the UXP panel.

**Subclips are not offered.** Whether Premiere 26 imports a bin `<clip>` with
real `in`/`out` as a subclip could not be confirmed without Premiere, and the
one first-hand report found says marked FCP7 subclips do not come over.
`fixtures/premiere/subclip-probe.xml` settles it in one import.

## Alternatives considered

- **Skip proxy files in the scanner.** Simplest, but loses the pairing the
  frame sampler uses, cannot represent the 33 rows already there without a
  second mechanism, and drops footage whose original is gone.
- **Exclude `Proxies/` with a library glob.** Same losses, and silently
  hides an orphaned proxy that is the only copy.
- **Render a selects reel.** Rejected outright: the editor wants editable
  media at full quality, not a file to re-import and lose the source behind.
- **Subclips instead of markers.** Better if Premiere honours them; unproven,
  so markers (which it does import) plus the string-out ship first.

## Consequences

- Migration 0028: `libraries.proxy_pattern`, `assets.proxy_of_id`. A
  pattern change takes effect at the library's next scan.
- Every new catalogue listing must apply `catalogued()`; a listing that
  forgets shows proxies again. The places that list assets were updated, and
  the tests cover search; the rest is review discipline.
- A linked proxy's old transcript, frames and vectors stay attached to its
  row, unread, so releasing the pair loses nothing.
- Acceptance in Premiere on the Mac mini (clips online, markers on the
  words, the sequence in order) is by hand; the XML is covered by golden
  files.
