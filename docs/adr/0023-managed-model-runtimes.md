# ADR-0023: Local AI models and the software that runs them are managed from the UI

- Status: Accepted
- Date: 2026-11-12

## Context

The owner wants open-weight models on the Mac mini to tag, score, colour and
draft cuts at no cost per use ([local-models.md](../local-models.md)). Two
of his rules shape everything else: **no console and no release** to add a
model, switch one or update what runs it; and **no model from a Chinese
lab**, however it is packaged.

The program that runs a model changes faster than FrameFound does. MLX-VLM,
the one that runs today's Apple-silicon builds, ships often, and support for
a given model arrives in a release of its own; llama.cpp, the second, runs
the GGUF builds that some candidates exist only as. A model released next
year may need a version of either that does not exist yet.

Running models also means running other people's code on the machine that
holds the catalogue: MLX-VLM will execute a Python file from a model's
folder if that model's settings name one, will download a model when given a
name where a folder was meant, binds every network interface unless told
otherwise, and inherits whatever environment it is started with — which on
the Mac includes the database password and the key that seals stored
secrets.

## Decision

### The software is managed like the models

Each runtime lives in `~/FrameFound/runtimes/<name>/<version>/`, with an
environment of its own (`FRAMEFOUND_RUNTIMES_DIR`; `local_models/runtimes.py`).
A version is whole only once its `installed.json` is written, after the
runtime's own check (import the server, print the version) has passed. Which
version is in use, and which is kept to go back to, is one `state.json`
replaced in one write, so an update is install-then-switch, a failed install
changes nothing, and roll back is a button. Nothing about runtimes is in the
database. A new *version* of a runtime is never a FrameFound release; a new
*kind* of runtime is one class (`kinds.RuntimeKind`) and one line in
`kinds()`.

### One model server; the runtime is its child

A `model-server` service (127.0.0.1:54805) is the only way to a model. It
has no database and no policy: it checks a model's folder against its
manifest before every load, starts the right runtime on it as a child
process (127.0.0.1:54806), forwards one OpenAI-shaped chat at a time, and
stops the child when it has been idle. The child is not a launchd service,
because "stopped while idle" must be a normal state; it stays in the
server's process group, so launchd stopping the server takes it too. Its
environment is an allow-list — no FrameFound setting, no Hugging Face
access (`HF_HUB_OFFLINE=1`, an empty `HF_HOME`), its key in the environment,
never in argv — and `--host 127.0.0.1` and `--port` are always passed.

### Whether a model may run is decided in one place

`local_models.ask()` is the only way any phase asks a model anything. It
checks, every time: paused, still approved by the publisher lists (re-judged
on use, so blocking a publisher stops a model installed last month), the
memory budget for the hour (8 GB by day, 20 GB 21:30–05:30 by default), the
overnight window by the Mac's own clock, and that a role's model is on this
Mac. Requests to the model server carry a token derived from the server's
secret key, so only FrameFound's own code can get past `ask()`. A
once-a-minute tick unloads a model that may no longer run.

### What reaches the disk is the control

Only allow-listed weight and settings files are downloaded, pinned to the
40-character commit the check showed, each verified by SHA-256 or git blob
id. A repository tagged `custom_code`, or whose settings name code to run,
is refused. `--trust-remote-code` is never passed. The model server refuses a
folder holding anything its manifest does not list. These are the controls;
the absence of a trust flag is not.

### The publisher rule fails closed

Makers, converters and a blocked list, all edited on the page; built-in
blocked names cannot be removed from it. A model is allowed when its
publisher is a maker, or a converter whose declared base is a maker's, and
nothing anywhere in its declared ancestry is blocked. An ancestry that
cannot be read in full is refused.

### One table of work, one lane

Downloads, copies onto the Mac, runtime installs and Tests are rows in
`local_ai_jobs` (migration 0032). The Celery message on the `local_ai` queue
is only a wake-up; one worker (`worker-local-ai`, nice 15) runs one job at a
time. Progress, cancel, resume, "waiting until 21:30" and "the worker was
restarted" are state in the row, so they outlive a tab and a process, and a
redelivered message does nothing.

### Where models live

The library — every model added — is a bulk kind (`models`, under
`library/`), on the NAS with the rest and movable from the Storage page. A
model that holds a role is copied onto the Mac first, and the old holder
keeps the role until the copy is whole, so a share that drops half way
leaves the role where it was.

### Containers and demo

The containers have no runtime: there the page says so in one sentence and
every write is refused with it. `FRAMEFOUND_LOCAL_AI_DEMO=1` shows the page
against stand-ins for review on a machine that cannot run models; it is
ignored wherever a runtimes folder or a public domain is configured.

## Alternatives considered

- **Install runtimes with Homebrew or into FrameFound's own environment.**
  An update would then be a console step, or could break FrameFound's own
  dependencies; and rollback would mean reinstalling an old version.
- **A desktop model runner with its own model store.** A second catalogue
  of models, its own update cycle, and no say over which files are fetched
  or which publishers are allowed.
- **The runtime as a launchd service of its own.** "Idle, nothing loaded"
  would read as a stopped service, and doctor and launchd's KeepAlive would
  restart it all day.
- **A database and the policy inside the model server.** A second copy of
  the settings to go stale in a long-lived process, and a server that could
  not answer with the database down. The checks live in `ask()` instead,
  enforced by the token and the tick.
- **A second lane on the `exports` queue.** A 9 GB download would hold one
  of the operator's export slots; the promise is that local AI never delays
  proxies or exports.
- **Trusting a runtime flag to keep model code out.** MLX-VLM runs a model's
  code file without any flag; only the file allow-list prevents it.

## Consequences

- A model, a runtime version, a role and a publisher list change from the
  page with no release; a new kind of runtime still needs one.
- The Mac gains two services, a folder and three settings lines; an install
  that has not been updated, and the containers, behave exactly as before,
  and `framefound doctor` prints exactly what it did.
- The budgets are an admission rule, not a cap: nothing stops a model that
  grows past its figure once loaded. Memory measured on one Test is not the
  memory of a long prompt; the highest figure ever seen is kept.
- A Test waits behind a download on the one lane, and a model swap costs a
  load of tens of seconds; later phases batch by model.
- None of it has run on Apple silicon yet. Every outside fact (the
  runtime's flags, its health answer, its memory figure, a real download)
  lives in one function each, so the first run on the mini corrects each in
  one place ([mac.md](../mac.md#local-ai-models)).

## Second kind: a pinned release archive, not Homebrew (2026-11-13)

llama.cpp, for GGUF builds, is the second kind (`kinds.LlamaCpp`, name
`llama-cpp`). Its program is not installed with Homebrew. Each version is the
release archive GitHub publishes for Apple-silicon Macs
(`llama-<build>-bin-macos-arm64.tar.gz`), fetched by FrameFound's own Python
(`python -m framefound.local_models.llama_release fetch`, the kind's one
install step), checked against the sha256 GitHub lists for it — an archive
with no listed checksum is refused — unpacked with Python's `data` filter
into `runtimes/llama-cpp/<version>/dist/`, and recorded in `llama.json`
beside it (version, daily build, archive name, sha256, the program's path).
Everything else is this ADR's mechanism unchanged: `installed.json`,
`state.json`, update as install-then-switch, roll back as a button.

Why not Homebrew: its formula keeps one version and has no versioned
formulae, so there is nothing to roll back to and no way to install a
particular version; it depends on libraries that FrameFound's own Homebrew
programs (ffmpeg, PostgreSQL, Caddy) are linked against, so an upgrade of it
could change them; and anyone typing `brew upgrade` would change the version
under every result's recorded `runtime_version` without the page knowing. Its
one real advantage — it is known to run on Macs — is the archive's open
question: whether macOS runs a program FrameFound unpacked has not been
tried on the mini. If it will not, the fallback is a third kind that uses
Homebrew, a new class rather than a redesign; it is not built.

llama.cpp publishes a numbered version every few weeks (0.9.0) and a daily
build most days (b12318); the Mac archive hangs on the daily builds only,
and a numbered version names the daily build it was cut from in a small
`nightly-tag.txt`. The page offers numbered versions as updates and installs
a daily build only when asked for one by number; with a daily build in use
no update is offered (`llama_release.is_newer`). GitHub is asked only when
the operator presses Check now or Install, never on a timer, because it
limits callers without a token.

The program is started closed against every way it can fetch or wander:
always one absolute model file (`-m`; without it llama.cpp loads whatever a
request names), loopback only, a fixed 16,384-token context, `--offline`,
no web page, no flag that downloads (`-hf`, `--mmproj-url`, …) or reads
other folders, the key only in its environment (`LLAMA_API_KEY`), and that
environment the same allow-list as MLX-VLM's, because llama.cpp reads a
`LLAMA_ARG_*` variable for almost every flag.

A model that runs on llama.cpp is asked about once before it is given a
role when nobody has compared it on this footage (`needs_compare`; L2's
`comparisons.last_finished_for`). It is a question, not a lock: one click
gives the role anyway, and the audit row says it was skipped. MLX models are
never asked.

The demo gains a stand-in for this kind too (`fake.DemoGgufKind`, name
`demo-gguf`), built only in demo mode, which judges repositories, builds
and versions by llama.cpp's rules and installs and starts nothing.
