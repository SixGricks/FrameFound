"""Bake-off: FrameFound's auto-edit against the edits that shipped.

    python -m framefound.ops.bakeoff davis-rd \\
        --originals "Intel:2026/11-21 - 130 Davis Rd Coatesville PA/Fotello Batch 1" \\
        --reference "Intel:2026/11-21 - 130 Davis Rd Coatesville PA/MLS - Fotello Edited" \\
        --label Fotello --models claude-sonnet-5

    python -m framefound.ops.bakeoff --combine davis-rd skook cambridge

A side is either `Library:folder` (catalogued photographs) or a directory as
the container sees it (finals copied in from Drive, /data/benchmarks/refs/…).
`--recursive` takes originals from subfolders too.

For every original matched to its final (by name, else by content —
media/compare.py), it renders the variants and measures each against the
final:

- **original**: untouched, the distance there is to cover;
- **preset**: the local listing preset, no API;
- **one per model**: the recipe-picker's sliders, exactly as auto-edit asks;
- **best sliders**: sliders fitted directly to the final — the engine's
  ceiling, i.e. what perfect judgment would get.

Writes `results.json`, `summary.md` and a side-by-side sheet per photograph
to /data/benchmarks/<name>/. Nothing in the catalogue changes: recipes live
in the results, not as edits, so several models can be tried on one shoot
without touching its listing. The only thing that leaves the machine is the
768px preview per photograph per model, to the Anthropic API — the same
request auto-edit makes — and only when --models is given.
"""

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import Asset, Library
from framefound.media import compare, looks
from framefound.media import develop as develop_lib
from framefound.scanner.paths import PathValidationError, safe_join

WORK_EDGE = 1024  # renders and measurements
# The library sky closest to what Fotello puts into overcast exteriors: blue
# with scattered cumulus.
DEFAULT_SKY = "blue-sky-scattered-clouds-302810758.jpg"
PANEL_EDGE = 720  # each picture on a sheet
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

# $ per million tokens: input, output, 5-minute cache write, cache read.
# From platform.claude.com/docs/en/about-claude/pricing, read 2026-09-25 —
# a dated snapshot for the report; the token counts are the measurement.
PRICES = {
    "claude-sonnet-5": (2.0, 10.0, 2.50, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 12.50, 0.25),
    "claude-haiku-4-5": (1.0, 5.0, 1.25, 0.10),
}

_EXTERIOR_WORDS = (
    "exterior", "aerial", "drone", "dji_", "yard", "barn", "elevation", "driveway", "lawn",
    "porch", "field", "pasture", "pond", "outbuilding", "shed", "facade", "deck", "patio",
    "acreage", "parcel", "garden", "landscape", "frontage", "pool", "silo", "front-entry",
    "flagstone", "walkway",
)  # fmt: skip


def price_of(model: str, usage: dict[str, int]) -> float | None:
    key = next((k for k in PRICES if model.startswith(k)), None)
    if key is None:
        return None
    inp, out, write, read = PRICES[key]
    return (
        usage.get("input_tokens", 0) * inp
        + usage.get("output_tokens", 0) * out
        + usage.get("cache_creation_input_tokens", 0) * write
        + usage.get("cache_read_input_tokens", 0) * read
    ) / 1_000_000


def is_exterior(pair: dict[str, Any]) -> bool:
    """Outside or in, from the words that describe the photograph: the SEO
    file name when there is one, else the first clause of a model's caption
    ("Kitchen island, looking toward the deck" is a kitchen). Camera names
    say nothing — except a drone's, which is outside."""
    names = f"{pair['original']} {pair['reference']}".lower()
    if "dji_" in names:
        return True
    if not re.search(r"(^|/)(img_|_mg_|dsc|\d{3}_img)", names):
        return any(word in names for word in _EXTERIOR_WORDS)
    captions = [
        str((detail or {}).get("caption") or "").split(",")[0].lower()
        for detail in pair.get("models", {}).values()
    ]
    votes = [any(word in c for word in _EXTERIOR_WORDS) for c in captions if c]
    return sum(votes) * 2 > len(votes) if votes else False


# ------------------------------------------------------------- resolving


async def _resolve(spec: str, recursive: bool) -> dict[str, Path]:
    """File name (relative to the side's folder) -> path, for one side."""
    if spec.startswith("/"):
        root = Path(spec)
        pattern = "**/*" if recursive else "*"
        return {
            str(p.relative_to(root)): p
            for p in sorted(root.glob(pattern))
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
        }
    library_name, _, folder = spec.partition(":")
    folder = folder.strip("/")
    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            library = (
                await db.execute(select(Library).where(Library.name == library_name))
            ).scalar_one_or_none()
            if library is None:
                raise SystemExit(f"No library named {library_name!r}")
            rows = (
                await db.execute(
                    select(Asset.relative_path).where(
                        Asset.library_id == library.id,
                        Asset.media_type == "image",
                        Asset.relative_path.like(f"{folder}/%"),
                    )
                )
            ).scalars()
            found: dict[str, Path] = {}
            for relpath in rows:
                inside = relpath[len(folder) + 1 :]
                if not recursive and "/" in inside:
                    continue
                try:
                    found[inside] = safe_join(Path(library.root_path), relpath)
                except PathValidationError:
                    continue
            return dict(sorted(found.items()))
    finally:
        await engine.dispose()


async def _current_recipes(spec: str, recursive: bool) -> dict[str, dict[str, Any]]:
    """The recipe each catalogued original carries right now — what an
    export would render. Empty for a directory side (nothing catalogued)."""
    from framefound.db.models import AssetEdit

    if spec.startswith("/"):
        return {}
    library_name, _, folder = spec.partition(":")
    folder = folder.strip("/")
    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            rows = (
                await db.execute(
                    select(Asset.relative_path, AssetEdit.recipe)
                    .join(Library, Library.id == Asset.library_id)
                    .join(AssetEdit, AssetEdit.asset_id == Asset.id)
                    .where(Library.name == library_name, Asset.relative_path.like(f"{folder}/%"))
                    .order_by(Asset.relative_path, AssetEdit.version)
                )
            ).all()
            current: dict[str, dict[str, Any]] = {}
            for relpath, recipe in rows:  # ascending versions: the last wins
                inside = relpath[len(folder) + 1 :]
                if recursive or "/" not in inside:
                    current[inside] = develop_lib.clean_recipe(recipe)
            return current
    finally:
        await engine.dispose()


def _installed_look(exclude_run: str) -> compare.LearnedLook | None:
    """The installed learned look, minus any examples from this very shoot —
    a run must never be scored by a look that has seen its answers."""
    import numpy as np

    path = get_settings().data_dir / looks.LOOK_PATH
    if not path.is_file():
        return None
    saved = json.loads(path.read_text())
    stem = exclude_run.removesuffix("-2")
    kept = [
        e
        for e in saved["examples"]
        if e["source"].split("/", 1)[0].removesuffix("-2") not in (exclude_run, stem)
    ]
    if len(kept) < 10:
        return None
    return compare.LearnedLook.fit(
        [np.asarray(e["features"]) for e in kept], [e["recipe"] for e in kept]
    )


async def _api_settings() -> tuple[str, str]:
    from framefound.media.maps_store import load_ai_edit_config

    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            config = await load_ai_edit_config(db)
            if not config.ready:
                raise SystemExit("No Anthropic key is configured (Security page)")
            return config.api_key(), config.model
    finally:
        await engine.dispose()


# --------------------------------------------------------------- loading


def _quick(path: Path) -> Any:
    """Small and fast, for matching: JPEG draft decoding, upright."""
    from PIL import Image, ImageOps

    with Image.open(path) as img:
        img.draft("RGB", (256, 256))
        upright = ImageOps.exif_transpose(img) or img
        small = upright.convert("RGB")
    small.thumbnail((256, 256))
    return small


def _work(path: Path) -> Any:
    """The measuring copy: loaded the way auto-edit and export load a
    photograph (upright, sRGB), then reduced."""
    from PIL import Image

    image = develop_lib.open_for_render(path)
    image.thumbnail((WORK_EDGE, WORK_EDGE), Image.Resampling.LANCZOS)
    return image


def _describe_all(paths: dict[str, Path]) -> tuple[dict[str, Any], dict[str, bool]]:
    descriptors: dict[str, Any] = {}
    portrait: dict[str, bool] = {}
    for name, path in paths.items():
        try:
            small = _quick(path)
        except Exception as exc:  # unreadable here is simply not a candidate
            print(f"  skip {name}: {exc}", file=sys.stderr)
            continue
        descriptors[name] = compare.descriptor(small)
        portrait[name] = small.height > small.width
    return descriptors, portrait


# ----------------------------------------------------------------- models


def _pick(preview: bytes, key: str, model: str) -> dict[str, Any]:
    from framefound.ai import recipe_picker

    for attempt in range(4):
        try:
            return recipe_picker.pick_recipe(preview, key, model)
        except recipe_picker.RecipePickUnavailable as exc:
            if attempt == 3:
                return {"error": str(exc)}
            time.sleep(3 * (attempt + 1))  # 429/529: back off and retry
    return {"error": "unreachable"}


# ----------------------------------------------------------------- sheets


def _sheet(panels: list[tuple[str, Any]], title: str) -> Any:
    from PIL import Image, ImageDraw

    from framefound.media.contact_sheet import _font

    columns = 3
    tiles = []
    for label, image in panels:
        tile = image.copy()
        tile.thumbnail((PANEL_EDGE, PANEL_EDGE), Image.Resampling.LANCZOS)
        tiles.append((label, tile))
    cell_w = max(t.width for _l, t in tiles)
    cell_h = max(t.height for _l, t in tiles) + 34
    rows = -(-len(tiles) // columns)
    sheet = Image.new("RGB", (columns * (cell_w + 10) + 10, 48 + rows * (cell_h + 10)), "white")
    draw = ImageDraw.Draw(sheet)
    draw.text((10, 12), title, fill=(20, 20, 20), font=_font(22))
    for index, (label, tile) in enumerate(tiles):
        x = 10 + (index % columns) * (cell_w + 10)
        y = 48 + (index // columns) * (cell_h + 10)
        sheet.paste(tile, (x, y))
        draw.text((x + 2, y + tile.height + 6), label, fill=(20, 20, 20), font=_font(17))
    return sheet


# -------------------------------------------------------------------- run


def run(args: argparse.Namespace) -> int:
    from framefound.ai import recipe_picker

    settings = get_settings()
    out_dir = settings.data_dir / "benchmarks" / args.name
    (out_dir / "sheets").mkdir(parents=True, exist_ok=True)
    models = [m for m in (args.models or "").split(",") if m]

    # --reuse: the recipes an earlier run already paid for, re-rendered and
    # re-measured with the engine as it is now.
    prior: dict[str, dict[str, dict[str, Any]]] = {}
    if args.reuse:
        earlier = json.loads(
            (settings.data_dir / "benchmarks" / args.reuse / "results.json").read_text()
        )
        for old in earlier["pairs"]:
            for model in earlier["models"]:
                variant = old["variants"].get(model)
                if variant is not None:
                    prior.setdefault(model, {})[old["original"]] = {
                        "recipe": variant["recipe"],
                        **(old["models"].get(model) or {}),
                    }
        models = list(dict.fromkeys([*earlier["models"], *models]))

    originals = asyncio.run(_resolve(args.originals, args.recursive))
    references = asyncio.run(_resolve(args.reference, False))
    print(f"{len(originals)} originals, {len(references)} references")
    key = ""
    if any(m not in prior for m in models):
        key, configured = asyncio.run(_api_settings())
        models = [configured if m == "configured" else m for m in models]

    sky_image = None
    if args.sky and args.sky != "none":
        from PIL import Image

        sky_image = Image.open(settings.data_dir / "skies" / args.sky)
        sky_image.load()

    started = time.time()
    orig_desc, orig_portrait = _describe_all(originals)
    ref_desc, ref_portrait = _describe_all(references)
    pairs = compare.match(orig_desc, ref_desc, {**orig_portrait, **ref_portrait})
    unmatched = sorted(set(references) - {p.reference for p in pairs})
    if args.limit:
        pairs = pairs[: args.limit]
    print(f"{len(pairs)} pairs ({time.time() - started:.0f}s); {len(unmatched)} unmatched")

    bases: dict[str, Any] = {}
    for pair in pairs:
        try:
            bases[pair.original] = _work(originals[pair.original])
        except Exception as exc:
            print(f"  cannot render {pair.original}: {exc}", file=sys.stderr)
    pairs = [p for p in pairs if p.original in bases]

    # The API calls, all up front and a few at a time: they are the slow,
    # network-bound part, and nothing about them depends on the renders.
    picks: dict[str, dict[str, dict[str, Any]]] = {m: {} for m in models}
    for model in models:
        if model in prior:
            picks[model] = prior[model]
            print(f"  {model}: reusing {len(prior[model])} recipes from {args.reuse}")
            continue
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                p.original: pool.submit(
                    _pick, recipe_picker.preview_bytes(bases[p.original]), key, model
                )
                for p in pairs
            }
            picks[model] = {name: future.result() for name, future in futures.items()}
        failed = sum(1 for r in picks[model].values() if "error" in r)
        print(f"  {model}: {len(pairs) - failed} recipes, {failed} failed, {time.time() - t0:.0f}s")

    # What FrameFound would ship today (the saved recipes), and what the
    # installed learned look would do — both free, no API.
    current = asyncio.run(_current_recipes(args.originals, args.recursive))
    look = _installed_look(args.name)
    if current:
        print(f"  {len(current)} originals carry a saved edit")
    if look is not None:
        print(f"  learned look: {len(look.recipes)} examples from other shoots")

    def render_full(image: Any, recipe: dict[str, Any]) -> Any:
        """A saved recipe as export renders it, sky included."""
        if "sky" not in recipe:
            return develop_lib.render(image, recipe)
        from PIL import Image

        from framefound.ai import skyseg

        def load_sky(name: str) -> Any:
            path = settings.data_dir / "skies" / name
            return Image.open(path) if path.is_file() else None

        return develop_lib.render(image, recipe, load_sky=load_sky, mask_for=skyseg.sky_mask)

    results = []
    for number, pair in enumerate(pairs, start=1):
        base = bases[pair.original]
        reference = _work(references[pair.reference])
        # Every variant is measured through the frame the final used, so a
        # crop or lens correction in the final is not charged to colour.
        framing = compare.align(base, reference)
        portrait = reference.height > reference.width
        ref_grid = compare.grid_lab(reference, portrait)

        # (label, recipe as recorded, rendered image)
        variants: list[tuple[str, dict[str, Any], Any]] = [
            ("original", {}, base),
            ("preset", dict(develop_lib.LISTING_PRESET), None),
        ]
        if look is not None:
            variants.append(("learned look", looks.predict(look, base), None))
        if pair.original in current:
            saved = current[pair.original]
            variants.append(("FrameFound (as edited)", saved, render_full(base, saved)))
        extra: dict[str, Any] = {}
        wants_sky = False
        for model in models:
            picked = picks[model].get(pair.original, {})
            extra[model] = {
                k: picked.get(k)
                for k in ("caption", "slug", "needs_sky_replacement", "usage", "error")
                if k in picked
            }
            if "recipe" in picked:
                variants.append((model, picked["recipe"], None))
                wants_sky = wants_sky or bool(picked.get("needs_sky_replacement"))

        # The sky swap, where a model asked for one: FrameFound's own
        # compositor with the operator's library sky — the step a
        # slider-only comparison leaves out, and the one Fotello's exteriors
        # lean on hardest.
        sky_base = None
        if sky_image is not None and wants_sky:
            from framefound.ai import skyseg
            from framefound.media.sky import composite_sky

            try:
                sky_base = composite_sky(base, skyseg.sky_mask(base), sky_image)
            except Exception as exc:
                print(f"  no sky for {pair.original}: {exc}", file=sys.stderr)
            if sky_base is not None:
                for model in models:
                    picked = picks[model].get(pair.original, {})
                    if "recipe" in picked and picked.get("needs_sky_replacement"):
                        recipe = picked["recipe"]
                        variants.append(
                            (
                                f"{model} + sky",
                                {**recipe, "sky": {"name": args.sky}},
                                develop_lib.render(sky_base, recipe),
                            )
                        )

        if not args.no_fit:
            for label, start_image in (("best sliders", base), ("best sliders + sky", sky_base)):
                if start_image is None:
                    continue
                small = framing.crop(start_image)
                small.thumbnail((compare.FIT_EDGE, compare.FIT_EDGE))
                fitted, _fit_de = compare.fit_recipe(
                    small,
                    ref_grid,
                    dict(develop_lib.LISTING_PRESET),
                    develop_lib.apply_recipe,
                    develop_lib.RECIPE_FIELDS,
                )
                variants.append((label, fitted, develop_lib.render(start_image, fitted)))

        measured: dict[str, Any] = {}
        panels: list[tuple[str, Any]] = []
        for label, recipe, image in variants:
            rendered = image if image is not None else develop_lib.render(base, recipe)
            framed = framing.crop(rendered)
            dist = compare.distance_lab(compare.grid_lab(framed, portrait), ref_grid)
            measured[label] = {"recipe": recipe, **dist.as_dict()}
            # Plain ASCII: the sheet font has no glyph for a delta or an arrow.
            panels.append((f"{label}   dE {dist.delta_e:.1f}", framed))
        panels.append((f"{args.label} (what shipped)", reference))

        ref_stem = Path(pair.reference).stem
        title = f"{number:02d}  {pair.reference}  <-  {pair.original}  ({pair.method})"
        _sheet(panels, title).save(
            out_dir / "sheets" / f"{number:02d}-{ref_stem[:60]}.jpg", "JPEG", quality=85
        )
        entry = {
            "original": pair.original,
            "reference": pair.reference,
            "match": pair.method,
            "match_score": pair.score,
            "framing": {"scale": framing.scale, "score": framing.score, "box": framing.box},
            "variants": measured,
            "models": extra,
        }
        results.append({**entry, "exterior": is_exterior(entry)})
        print(
            f"  {number:02d}/{len(pairs)} {ref_stem[:50]}: "
            + ", ".join(f"{k} {v['delta_e']:.1f}" for k, v in measured.items())
        )

    payload = {
        "name": args.name,
        "label": args.label,
        "originals": args.originals,
        "reference": args.reference,
        "recursive": args.recursive,
        "sky": args.sky,
        "models": models,
        "unmatched_references": unmatched,
        "pairs": results,
        "seconds": round(time.time() - started),
    }
    (out_dir / "results.json").write_text(json.dumps(payload, indent=1))
    (out_dir / "summary.md").write_text(summarize([payload]))
    print(f"\nWrote {out_dir}")
    print(summarize([payload]))
    return 0


# ---------------------------------------------------------------- summary


def _mean(values: list[float]) -> float:
    return statistics.fmean(values) if values else float("nan")


def summarize(runs: list[dict[str, Any]]) -> str:
    """One Markdown report over one or more bake-off runs."""
    pairs = [pair for run in runs for pair in run["pairs"]]
    labels: list[str] = []
    for pair in pairs:
        for label in pair["variants"]:
            if label not in labels:
                labels.append(label)
    lines = [
        "# Bake-off — "
        + ", ".join(f"{r['name']} ({len(r['pairs'])} vs {r['label']})" for r in runs),
        "",
        f"{len(pairs)} photographs. ΔE is the mean colour difference from what shipped, region "
        "by region (lower is closer; under ~2 is hard to see side by side, over ~10 is a "
        "different-looking photograph). ΔL* is brightness (+ = brighter than what shipped), "
        "Δb* warmth (+ = yellower), ΔC chroma (+ = more saturated).",
        "",
    ]

    def table(subset: list[dict[str, Any]], heading: str) -> None:
        if not subset:
            return
        lines.extend(
            [
                f"## {heading} ({len(subset)})",
                "",
                "| Variant | mean ΔE | median ΔE | ΔL* | Δb* | ΔC | contrast | closest |",
                "|---|---|---|---|---|---|---|---|",
            ]
        )
        # The fitted variants are ceilings (they peeked at the answer), so
        # they are reported but never "closest".
        contenders = [label for label in labels if not label.startswith("best sliders")]
        wins: dict[str, int] = dict.fromkeys(contenders, 0)
        for pair in subset:
            present = [c for c in contenders if c in pair["variants"]]
            if present:
                best = min(present, key=lambda c: pair["variants"][c]["delta_e"])
                wins[best] += 1
        for label in labels:
            rows = [p["variants"][label] for p in subset if label in p["variants"]]
            if not rows:
                continue
            des = [r["delta_e"] for r in rows]
            lines.append(
                f"| {label} | {_mean(des):.2f} | {statistics.median(des):.2f} "
                f"| {_mean([r['d_lightness'] for r in rows]):+.1f} "
                f"| {_mean([r['d_b'] for r in rows]):+.1f} "
                f"| {_mean([r['d_chroma'] for r in rows]):+.1f} "
                f"| {_mean([r['contrast'] for r in rows]):.2f} "
                f"| {wins.get(label, '—')} |"
            )
        # How much of the reachable distance each contender covers: 0% is
        # the untouched original, 100% is the engine's best.
        gaps = []
        for label in contenders:
            if label == "original":
                continue
            covered = []
            for p in subset:
                v = p["variants"]
                if label in v and "best sliders" in v:
                    reach = v["original"]["delta_e"] - v["best sliders"]["delta_e"]
                    if reach > 0.5:
                        covered.append((v["original"]["delta_e"] - v[label]["delta_e"]) / reach)
            if covered:
                gaps.append(f"{label} {statistics.median(covered) * 100:.0f}%")
        if gaps:
            lines.append("")
            lines.append("Share of the reachable improvement covered (median): " + ", ".join(gaps))
        lines.append("")

    table(pairs, "All photographs")
    # Re-classified here rather than read from the file, so earlier runs
    # benefit when the rule improves.
    outside = [is_exterior(p) for p in pairs]
    table([p for p, out in zip(pairs, outside, strict=True) if out], "Exteriors and aerials")
    table([p for p, out in zip(pairs, outside, strict=True) if not out], "Interiors")

    models = sorted({m for run in runs for m in run["models"]})
    if models:
        lines += [
            "## API cost, measured",
            "",
            "| Model | photos | input | output | cache read | $ total | $ per photo |",
            "|---|---|---|---|---|---|---|",
        ]
        for model in models:
            usage: dict[str, int] = {}
            photos = 0
            for pair in pairs:
                used = (pair["models"].get(model) or {}).get("usage")
                if used:
                    photos += 1
                    for k, v in used.items():
                        usage[k] = usage.get(k, 0) + v
            cost = price_of(model, usage)
            per = f"{cost / photos:.4f}" if cost is not None and photos else "—"
            total = f"{cost:.3f}" if cost is not None else "—"
            read = usage.get("cache_read_input_tokens", 0)
            lines.append(
                f"| {model} | {photos} | {usage.get('input_tokens', 0):,} "
                f"| {usage.get('output_tokens', 0):,} | {read:,} | {total} | {per} |"
            )
        lines.append("")

    scales = [p["framing"]["scale"] for p in pairs if "framing" in p]
    if scales:
        reframed = [s for s in scales if s >= 1.03]
        lines += [
            "## Framing",
            "",
            f"{len(reframed)} of {len(scales)} finals were re-framed tighter than the original "
            f"(lens correction, straightening or a crop); median {statistics.median(scales):.2f}x. "
            "Colour above is measured through each final's own frame, so this is not counted "
            "against any variant.",
            "",
        ]

    worst_label = models[0] if models else "preset"
    ranked = sorted(
        (p for p in pairs if worst_label in p["variants"]),
        key=lambda p: -p["variants"][worst_label]["delta_e"],
    )[:8]
    if ranked:
        lines += [f"## Furthest from what shipped ({worst_label})", ""]
        for p in ranked:
            v = p["variants"][worst_label]
            best = p["variants"].get("best sliders", {}).get("delta_e")
            lines.append(
                f"- {p['reference']}: ΔE {v['delta_e']:.1f} (best sliders "
                f"{best if best is not None else '—'}), ΔL* {v['d_lightness']:+.1f}, "
                f"Δb* {v['d_b']:+.1f}"
            )
        lines.append("")
    unmatched = [u for run in runs for u in run.get("unmatched_references", [])]
    if unmatched:
        lines.append(f"Unmatched finals ({len(unmatched)}): " + ", ".join(unmatched[:12]))
    return "\n".join(lines) + "\n"


def learn(names: list[str]) -> int:
    """Can FrameFound learn the look from what already shipped?

    Every bake-off pair carries the sliders fitted to its final. Indexed by
    what each original looked like, they predict sliders for a new
    photograph (compare.LearnedLook) — locally, free, and in the operator's
    own look. Scored honestly: each shoot is predicted by a look learned
    from the *other* shoots only. Also scores a half-and-half blend of the
    learned look with each model's recipe.

    Writes learned-look.json (every example, for production use) and
    learned-<names>.md beside the runs.
    """
    base_dir = get_settings().data_dir / "benchmarks"
    runs = [json.loads((base_dir / n / "results.json").read_text()) for n in names]
    samples: list[dict[str, Any]] = []
    for run in runs:
        recursive = any("/" in p["original"] for p in run["pairs"])
        originals = asyncio.run(_resolve(run["originals"], recursive))
        references = asyncio.run(_resolve(run["reference"], False))
        for pair in run["pairs"]:
            best = pair["variants"].get("best sliders")
            if not best or pair["original"] not in originals:
                continue
            base = _work(originals[pair["original"]])
            reference = _work(references[pair["reference"]])
            portrait = reference.height > reference.width
            recorded = pair.get("framing")
            framing = (
                compare.Framing(tuple(recorded["box"]), recorded["scale"], recorded["score"])
                if recorded
                else compare.align(base, reference)
            )
            small = framing.crop(base)
            small.thumbnail((compare.FIT_EDGE, compare.FIT_EDGE))
            samples.append(
                {
                    "run": run["name"],
                    "pair": pair,
                    "feat": compare.features(base),
                    "small": small,
                    "portrait": portrait,
                    "ref": compare.grid_lab(reference, portrait),
                    "best": best["recipe"],
                }
            )
        print(f"  {run['name']}: {sum(1 for s in samples if s['run'] == run['name'])} examples")

    def score(sample: dict[str, Any], recipe: dict[str, float]) -> dict[str, Any]:
        rendered = develop_lib.apply_recipe(sample["small"], recipe)
        dist = compare.distance_lab(compare.grid_lab(rendered, sample["portrait"]), sample["ref"])
        return {"recipe": recipe, **dist.as_dict()}

    drift: list[float] = []
    for run in runs:
        train = [s for s in samples if s["run"] != run["name"]]
        if len(train) < 10:
            continue
        look = compare.LearnedLook.fit([s["feat"] for s in train], [s["best"] for s in train])
        for sample in (s for s in samples if s["run"] == run["name"]):
            variants = sample["pair"]["variants"]
            predicted = look.predict(sample["feat"])
            variants["learned look"] = score(sample, predicted)
            for model in run["models"]:
                recipe = (variants.get(model) or {}).get("recipe")
                if recipe is None:
                    continue
                keys = set(recipe) | set(predicted)
                blend = {
                    k: round((recipe.get(k, 0.0) + predicted.get(k, 0.0)) / 2, 3)
                    for k in keys
                    if k in develop_lib.RECIPE_FIELDS
                }
                variants[f"learned + {model}"] = score(sample, blend)
            # The runs measured at 1024px, this at 256: check they agree.
            if "preset" in variants:
                small_preset = score(sample, dict(develop_lib.LISTING_PRESET))["delta_e"]
                drift.append(abs(small_preset - variants["preset"]["delta_e"]))

    everything = compare.LearnedLook.fit([s["feat"] for s in samples], [s["best"] for s in samples])
    look_json = json.dumps(
        {
            "features": list(compare.FEATURE_NAMES),
            "mean": everything.mean.tolist(),
            "scale": everything.scale.tolist(),
            "examples": [
                {
                    "source": f"{s['run']}/{s['pair']['reference']}",
                    "features": [round(float(x), 3) for x in s["feat"]],
                    "recipe": s["best"],
                }
                for s in samples
            ],
        },
        indent=1,
    )
    (base_dir / "learned-look.json").write_text(look_json)
    # Installed where auto-edit reads it (media/looks.py): from the next run
    # on, auto-edit takes its tone from these examples.
    installed = get_settings().data_dir / looks.LOOK_PATH
    installed.parent.mkdir(parents=True, exist_ok=True)
    installed.write_text(look_json)
    print(f"Installed the learned look ({len(samples)} examples) at {installed}")
    report = summarize(runs)
    if drift:
        report += (
            f"\nLearned-look scores are measured on 256px renders; the preset measured both "
            f"ways differs by {statistics.fmean(drift):.2f} ΔE on average.\n"
        )
    (base_dir / f"learned-{'-'.join(names)[:80]}.md").write_text(report)
    print(report)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("name", nargs="?", help="output folder under /data/benchmarks")
    parser.add_argument("--originals", help="Library:folder or /path")
    parser.add_argument("--reference", help="Library:folder or /path")
    parser.add_argument("--label", default="reference", help="who made the finals")
    parser.add_argument("--models", default="", help="comma-separated; 'configured' = settings")
    parser.add_argument("--recursive", action="store_true", help="originals from subfolders too")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4, help="API calls in flight")
    parser.add_argument("--no-fit", action="store_true", help="skip the best-sliders fit")
    parser.add_argument("--reuse", help="take model recipes from this earlier run (no API)")
    parser.add_argument(
        "--sky",
        default=DEFAULT_SKY,
        help="library sky to composite where a model asked for one ('none' to skip)",
    )
    parser.add_argument("--combine", nargs="+", help="summarise earlier runs together")
    parser.add_argument("--learn", nargs="+", help="learn the look from earlier runs")
    args = parser.parse_args(argv)

    if args.learn:
        return learn(args.learn)
    if args.combine:
        base = get_settings().data_dir / "benchmarks"
        runs = [json.loads((base / n / "results.json").read_text()) for n in args.combine]
        report = summarize(runs)
        (base / f"combined-{uuid.uuid4().hex[:6]}.md").write_text(report)
        print(report)
        return 0
    if not (args.name and args.originals and args.reference):
        parser.error("name, --originals and --reference are required")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
