# ADR-0019: Premiere integration via UXP, with a browser fallback that ships first

- **Status**: Accepted (M9 direction)
- **Date**: 2026-07-30

## Context

The catalogue answers "where is the shot of the power broom at Hackman Rd" in
about three milliseconds. The editor then alt-tabs to Premiere and hunts for
the same file by hand. That gap is where the value leaks out — a search tool
that cannot get its answer into the timeline has done most of the work and
stopped short of the point.

Adobe offers two extension technologies, and the choice is not close on the
technical merits but is complicated by reach.

**CEP** (Common Extensibility Platform) is the old one: a bundled Chromium
running Node.js with effectively full filesystem and shell access, driving the
host through ExtendScript. It is deprecated. Adobe has stated CEP is on its way
out and UXP is the future, but CEP still works in shipping versions of Premiere
and a great many production panels are still CEP.

**UXP** (Unified Extensibility Platform) is the replacement: a modern
JavaScript engine with a React-like layout model, a deliberately restricted
capability set, and a proper permission manifest. It is what Adobe is investing
in. In Premiere specifically, UXP arrived considerably later than in Photoshop
and InDesign, and its API surface for sequence and project manipulation is
still narrower than what ExtendScript exposes.

## Options considered

1. **CEP panel.** Most capable today, most examples to copy from, works in
   older Premiere versions the target users may still be running. Building new
   on a deprecated platform means writing something with a known expiry date,
   and its "full Node.js in the host process" model is exactly the kind of
   ambient capability this project has avoided everywhere else.
2. **UXP panel.** The supported path, aligned with where Adobe is going, and a
   permission model that matches how this project thinks about privilege.
   Narrower API, less prior art, and requires a recent Premiere.
3. **No panel — browser-driven handoff.** Have the catalogue write files the
   editor already understands, and let the operator drag them in.
4. **Both CEP and UXP.** Twice the surface to maintain for one feature.

## Decision

**Ship option 3 first, then build option 2. Do not build CEP.**

### Why the fallback ships first

The handoff can be built entirely on this side of the fence, with no Adobe SDK,
no code signing, no panel distribution, and no dependency on which Premiere
version anyone has installed:

- **A Premiere-readable project or bin.** A `.prproj` is undocumented, but an
  **FCP7 XML** or **EDL** export is well understood, and Premiere imports both.
  A search result set becomes a bin with the clips already in it.
- **Timecode-accurate markers** from the transcript, so a search hit lands on
  the frame rather than the file.
- **A local "reveal" link** that opens the containing folder, which covers the
  common case of "I just want that file".

That is genuinely useful on its own, works for every NLE rather than only
Premiere — DaVinci and Final Cut both read XML — and is testable in CI without
a copy of Premiere. It also does not become dead work when the panel arrives:
the panel will call the same export path.

### Why UXP and not CEP

Writing new code against a platform Adobe has announced the end of is a
liability the moment it ships. The narrower API is a real cost, but the
operations this needs are modest: import a set of clips, create a bin, place a
marker, reveal a file. Those are within reach; the ambitious end of panel
development is not what this feature wants.

CEP's capability model is also wrong for this project. The threat model
argues against ambient authority everywhere else — the mount helper holds one
capability and nothing more, media is mounted read-only, the API cannot mount
anything. A CEP panel is a Node.js process with the user's full filesystem
access, talking to a self-hosted service over HTTP. UXP's manifest, where the
panel declares the network host it may reach and gets nothing it did not ask
for, is the model that matches.

### Authentication

The panel will not hold a password. It will use a **scoped, revocable panel
token**, created in the UI, listed alongside authenticated sessions on the Security page,
and revocable there — the same treatment sessions already get. A token that
cannot be revoked from the machine it grants access to is a credential leak
waiting to happen.

## Consequences

**Good.** Something useful ships without an Adobe dependency, and it serves
DaVinci and Final Cut too. The panel work that follows is additive rather than
a prerequisite. No new deprecated technology enters the codebase.

**Bad.** The fallback is a worse experience than a panel: it is an export and
an import rather than a click. UXP's Premiere API may turn out to be too
narrow for the panel to be worth having, in which case the fallback is what
this feature is, permanently — and that outcome is acceptable, which is part
of why the fallback goes first.

**Deliberately not decided yet.** Whether the panel searches inside Premiere or
only receives results from the browser. Searching in-panel is the better
experience and much more work; receiving is nearly free once the export path
exists. Left open until the export path is in real use and it is clear which
one the work is actually asking for.

## First steps

1. FCP7 XML export for a result set (no Adobe dependency, testable in CI).
2. Marker export from transcript hits.
3. Panel tokens with revocation, reusing the session machinery.
4. A UXP hello-world panel against a current Premiere, purely to establish what
   its project API can and cannot do, before committing to more.

Step 4 is a spike, and its result may well change steps that follow it. That is
the point of doing it fourth rather than first.
