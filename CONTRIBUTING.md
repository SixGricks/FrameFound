# Contributing to FrameFound

Thanks for your interest! This project is in early development (Milestone 0-1);
expect churn. Please open an issue before large PRs.

## Development setup

Prerequisites: Docker + Docker Compose, Python 3.12+, Node 20+.

```bash
git clone https://github.com/SixGricks/FrameFound.git
cd FrameFound
cp .env.example .env

# Backend
cd apps/server
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest

# Frontend
cd ../web
npm install
npm run dev
```

Full stack: `docker compose up -d --build`.

### Looking at the UI with a seeded catalogue

No Postgres, Redis or NAS needed: the API runs on SQLite and the Next dev
server forwards `/api` to it.

```bash
# API on SQLite, in apps/server (media and data dirs can be anywhere)
export FRAMEFOUND_DATABASE_URL=sqlite+aiosqlite:////tmp/ff-ui/ui.db
export FRAMEFOUND_DATA_DIR=/tmp/ff-ui/data FRAMEFOUND_MEDIA_ROOT=/tmp/ff-ui/media
export FRAMEFOUND_MODELS_DIR=/tmp/ff-ui/models FRAMEFOUND_LOCAL_AI_DEMO=1
export FRAMEFOUND_SECRET_KEY=$(openssl rand -hex 32) FRAMEFOUND_SETUP_TOKEN=dev-setup
pip install -e ".[dev,db,media]"
alembic upgrade head
python ../../infrastructure/scripts/seed_dev.py /tmp/ff-ui --compare   # 66 generated assets, faces, tags, 2 listings, demo AI models, colour suggestions, rough cuts, clips ready for a folder review, a comparison
uvicorn framefound.main:app --port 8000

# Web, in apps/web — .env.local is git-ignored
echo FRAMEFOUND_API_PROXY=http://localhost:8000 > .env.local
npm run dev
```

Create the first admin once with `POST /api/v1/auth/setup`
(`{"setup_token": "dev-setup", "email": ..., "password": ...}`), then sign in
at http://localhost:3000. Visual search, auto-edit and Drive need the real
models and keys and stay off; everything else works on the seeded data.

**The AI models page (Manage › AI models) in demo mode.** Local models run
only on the Mac install, so anywhere else the page is one card saying so.
`FRAMEFOUND_LOCAL_AI_DEMO=1` shows it against made-up models instead: Hugging
Face, the software that runs models and the model server are the stand-ins
in `apps/server/framefound/local_models/fake.py` (the pages in
`local_models/demo_data/`), a download writes a few kilobytes per file while
its bar moves against the real size, and a Test's answer is invented. There
is no queue: the jobs run inside the API process. Every demo page carries a
banner saying it is a demonstration. The flag is for review copies only and
is ignored wherever `FRAMEFOUND_RUNTIMES_DIR` or `FRAMEFOUND_DOMAIN` is set, so
it can never make a real install show invented models; it is deliberately not
in `.env.example` or the Mac's settings file. The seed adds four demo
models under `$FRAMEFOUND_MODELS_DIR/library` — the Looker (with a finished
Test of the newest photograph), the Editor (text only, with a Test waiting
for the overnight window), a GGUF build (below) and a download whose
connection dropped — and two versions of the demo software with a newer one
to update to. On a catalogue seeded before these existed,
`seed_dev.py /tmp/ff-ui --local-ai-only` adds just them. The waiting Test is
real to the demo: once its window opens (21:30 on the day it was seeded, or
the next day if seeded after 20:00) and the page is open, it runs and
finishes. Running `seed_dev.py /tmp/ff-ui --local-ai-only` again puts a
waiting one back, and adds the GGUF build to a catalogue seeded before it
existed; it adds nothing else to this page. On a catalogue that already has
models the seed runs no job, because a reviewer's own may be waiting there:
the GGUF build's download is left waiting its turn, and the review server
does it when the AI models page is next opened (restart the API first if it
was started before the code you are seeding from). The build is added once:
remove it on the page and seeding again does not bring it back.

**The second program (llama.cpp) in demo mode.** The demo has a stand-in for
each program that runs models: "Demo runtime" for MLX builds and "Demo
llama.cpp" (`fake.DemoGgufKind`) for GGUF builds. The second judges a
repository, lists its builds, suggests one and reads a version by
llama.cpp's own rules, and installs nothing: installing it makes a folder,
and its newest version is a made-up 0.10.0, one past the real 0.9.0, so
Install a particular version › 0.9.0, then Update and Roll back all have
something to do; a daily build such as `b12318` can be installed by its
number. The GGUF repositories in `local_models/demo_data/gguf/` can be
checked by name: `google/demo-vision-gguf` (three builds, Q4_K_M suggested,
fits by day); `example-gguf/demo-many-builds-GGUF` (21 builds, a list that
scrolls) and `example-gguf/demo-omni-30b-GGUF` (three builds), both refused
until `example-gguf` is added as a converter under Approved publishers, and
then addable but too big for the default budgets; `nvidia/demo-gated-gguf`
(gated, but the demo's Hugging Face lets anyone read it, so its access reads
as fine); and `google/demo-projector-only` (refused: it holds only the part
that reads pictures). The seed installs `google/demo-vision-gguf`'s suggested
build (Q4_K_M) and leaves Demo llama.cpp uninstalled, so its row says a
model needs it; it gives that model no role, because a model on llama.cpp
that nobody compared is asked about first — giving it one from the page
shows that question.

**Colour (a clip's Look card, a listing's local edits) in demo mode.** Every
seed run, `--local-ai-only` included, also adds what the colour pages show on
first load ([video-looks.md](docs/video-looks.md),
[ai-editing-models.md](docs/ai-editing-models.md#auto-edits-modes)). The Davis
Rd listing has three local-model proposals (one mild, one large, one with a
sky worth replacing) for the Local edit panel, "What the local model
suggests", above its gallery. The one clip in GELCO's Hudson National folder
has a suggested look, so its page opens on all three looks side by side, and
the first clip by name in GELCO's LedgeRock folder has a look set by hand,
with its LUT to download (they are GELCO's first two clips by folder, then
name; the seed prints each one's path when it adds the look). They are made
up as if by the demo's Looker and marked demo. Each of the three parts is
added once per catalogue and then remembered, so seeding a review catalogue
again never undoes what a reviewer did: proposals applied or dismissed are
not put back, and neither is a suggestion replaced or a choice cleared on
the clip's card. A listing the model has already looked at, and a clip that
already has a look of any kind, are a reviewer's and are left as they are.
Anything that could not be added is printed on a `Colour: not added:` line,
with the reason, and is tried again at the next seed.

**Cuts (Cuts in the top menu) in demo mode.** Every seed run,
`--local-ai-only` included, also adds what the Cuts pages show on first load
([local-models.md](docs/local-models.md#rough-cuts-from-the-local-editor)).
Each of the five GELCO clips gets three shot windows with their stills
(where it has none), so a draft can be asked for from any of GELCO's three
folders. "Pete Dye — 30 second teaser" is a cut in two versions: version 1
a local draft, made up as if by the demo's Editor and marked demo, with a
reason on each of its six shots, the two things the check changed (a shot
number that was not on the list, a cut that ran past the end of its shot)
and the six shots it was chosen from; version 2 a revision kept beside it
as Claude's are, with one shot trimmed, one moved, one added and one left
out, so **Compare** has a shot of every kind to show. "Pete Dye Quick 30" is
an imported timeline standing alone. One draft failed because the Editor
answered in prose: its card has the sentence, **What the Editor wrote**, and
Try again, Ask Claude instead and Clear. The local draft was never
downloaded, so its shots are not marked used; the shots that are used are
the five of the imported timeline. Pressing **Draft** runs for real against
the made-up Editor, which takes shots evenly through the list; the seeded
Editor only fits the overnight budget, so by day the draft waits for 21:30
and says so (give the Editor role to the Looker's model on the AI models
page to see one finish at once). The seed has no playable copies of the
clips, so the viewer a shot's picture opens has nothing to play and says
so. The cut, the timeline and the failed draft are each added once per
catalogue and then remembered: delete a version, clear the draft or delete
the timeline, and seeding again does not bring it back. Nothing here is a
job, so the AI models page's list of jobs is unchanged. What could not be
added is printed on a `Cuts: not added:` line, with the reason, and is tried
again at the next seed.

**A folder review by the local model (Review with AI) in demo mode.** Every
seed run, `--local-ai-only` included, then gives each clip that has shot
windows a ready motion file: a hundred bytes or so of JSON beside the clip's
other derived files, which is how the catalogue knows a clip's shots have been
found ([local-models.md](docs/local-models.md#reviewing-a-folder-on-request)).
That is all it adds, and it is what makes a review of a seeded folder worth
looking at. With it, GELCO's five clips count as ready and are looked at
shot by shot (Pete Dye reads 6 photographs and 2 clips, 6 shots); without
it every clip read as needing a playable copy first, which the seed cannot
make, so each would have been judged from one still. **The seed adds no
looks.** Those are made by reviewing a folder: in demo mode a review runs
for real on the demo's lane, and every caption, score and tag it writes is
made up and marked so (a stock caption, the reason "Made up for the
demonstration: no model looked at this picture.", a score from a hash, one
to three tags drawn from the catalogue's own words, never a new word). The
sweep is made once per catalogue and then remembered, like the parts above;
a clip with no shot windows gets none, and a clip that already has a motion
file of any standing is left as it is. A GELCO clip that reaches the
catalogue after that sweep gets its file from the Cuts part, with its
windows, or neither: windows without the file is a clip a review asks
about again and again. It is no model and no job, so the AI
models page is unchanged. What could not be added is printed on a `Looking:
not added:` line, with the reason, and is tried again at the next seed. A
review is started with **Review with AI**, on a folder in Browse's Folders
view or on a library's card on the Libraries page (or with
`POST /api/v1/looks/reviews`), and followed there or on Processing.

**Compare (AI models › Compare models) in demo mode.** With `--compare` (as in
the commands above; without it the AI models page gets only the four models
described here) the seed also says yes to `exterior` on four clips and no to
`sunset` on three, and installs two more demo models that read pictures (from
`local_models/demo_data/compare/`): `mlx-community/demo-small-4bit`, which
fits by day, and `mlx-community/demo-medium-4bit`, which fits only overnight,
so a column of it in a comparison started by day waits until 21:30 and says
so. It saves one finished comparison of the Looker and demo-small on the
fifteen GELCO shots the Cuts part made, five of them used by the imported
timeline "Pete Dye Quick 30", with answers chosen so its figures are worth
reading. A
comparison started in the browser runs on the demo's lane, and its answers are
made up in the shape of real ones (`looking/demo.py`). Each part is added only
when it is missing — the comparison is recognised even after being renamed, as
the imported timeline is by the Cuts part — so `--local-ai-only --compare` on
an older review catalogue adds just what it lacks and, run again, nothing; a
demo model whose download an earlier run left unfinished is downloaded again.
What could not be added is printed on its own `Compare: not added:` line, with
the reason. Colour, Cuts, Looking and Compare need the catalogue's tables at
the newest migration, so run `alembic upgrade head` against it first (the
seed says so, part by part, if they are older).

## Standards

- **Python**: type hints everywhere, `ruff` for lint+format, `mypy` clean, pytest for tests.
- **TypeScript**: strict mode (`npm run typecheck`), ESLint (`npm run lint`), `npm test` for the few tests the web app has.
- **Commits**: [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:`...).
- **Branches**: `main` is always releasable; work on feature branches; PRs required.
- **Migrations**: every schema change ships as an Alembic migration, committed with the code.
- **Security**: never commit secrets, real paths, domains, or tokens. `.env` is gitignored.
- **Architecture changes**: significant decisions need an ADR in `docs/adr/` (see template).

## Non-negotiables (see docs/architecture.md)

1. Original media is never modified — read-only mounts, derived files only.
2. Local-first — no required cloud services.
3. All paths validated against approved library roots; never trust filenames or metadata.
4. Authorization enforced before serving any proxy, thumbnail, or transcript.

## Pull requests

1. Fork/branch, make the change, add tests.
2. Backend, in `apps/server`: `ruff check && mypy && pytest` pass locally.
3. Frontend, in `apps/web`: `npm run lint && npm run typecheck && npm test`
   pass locally. `npm test` is Node's own test runner over `tests/*.test.mjs`
   — nothing to install, Node 22.18 or newer — and it covers only logic that
   lives outside React, in files under `src/lib` that import nothing (which
   key presses a page shortcut must leave alone, how the server is asked why
   a file would not load). The chat panel's store is the one exception: it
   imports other pure files, and `tests/ts-imports.mjs` lets Node resolve
   those imports. A page's behaviour has no automated test, so
   after those three, look at the change in the browser on the seeded
   catalogue (above). CI runs all three.
   `npm run build` is worth running before a PR that adds a page or reads the
   URL: it is the only step that catches a prerender error (a
   `useSearchParams()` without a `<Suspense>` around it), and CI only meets
   it when it builds the image.
4. Fill in the PR template; link the issue.
5. CI must be green; one maintainer review required.
