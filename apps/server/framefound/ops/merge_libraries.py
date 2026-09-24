"""Fold libraries into one rooted at their common parent folder.

    python -m framefound.ops.merge_libraries /media/intel --name Intel          # plan
    python -m framefound.ops.merge_libraries /media/intel --name Intel --apply  # do it

Written when the operator wanted the whole Intel share as one library instead
of three sub-folder libraries (2026, Breeze Video, PROMO VIDEO).

Both obvious ways lose. Deleting the old libraries cascades away every
embedding, face, transcript, tag, listing and edit their assets carry. Adding
a library at the parent while they exist catalogues every file twice — the
scanner's move detection only re-binds a file whose old path is gone. So this
re-parents: each asset keeps its id, and with it everything attached, and only
its library and relative path change — `IMG_1.jpg` under `/media/intel/2026`
becomes `2026/IMG_1.jpg` under `/media/intel`. Derived files are keyed by
asset id and never move.

The library with the most assets becomes the merged one (one already rooted
at the parent wins outright), so its id, settings and history survive and
links to it keep working. The others fold in and are removed; their pending
scans are cancelled and one fresh scan of the parent is queued.

One transaction: every row moves or none does. Stop the scanner and workers
first — a task that read an asset's old path and the library's new root would
find nothing there and flag the file missing.
"""

import argparse
import asyncio
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from sqlalchemy import delete, func, literal, select, union_all, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.config import get_settings
from framefound.db.models import Asset, AuditLog, Library, PathMapping, Scan

ACTIVE_SCANS = ("pending", "running", "paused")


class MergeError(RuntimeError):
    """The merge cannot be done as asked; nothing was changed."""


@dataclass
class Member:
    id: uuid.UUID
    name: str
    root_path: str
    subfolder: str  # relative to the new root; "" for a library already there
    assets: int


@dataclass
class MergePlan:
    root: str
    name: str
    target: Member
    members: list[Member]  # including the target
    exclude_globs: list[str]
    include_extensions: list[str] | None
    scan_interval_minutes: int | None
    path_mappings: dict[str, tuple[str, str]]  # profile -> (platform, prefix)
    notes: list[str] = field(default_factory=list)

    @property
    def folded(self) -> list[Member]:
        return [m for m in self.members if m.id != self.target.id]


def _prefix(subfolder: str) -> str:
    return f"{subfolder}/" if subfolder else ""


def _parent_prefix(prefix: str, subfolder: str) -> str | None:
    """A workstation mapping for a sub-folder, lifted to the parent: `Z:\\2026`
    for `2026` becomes `Z:\\`. None when the prefix does not end with the
    sub-folder, which means it cannot be translated safely."""
    if not subfolder:
        return prefix
    separator = "\\" if "\\" in prefix else "/"
    trimmed = prefix.rstrip("/\\")
    tail = subfolder.replace("/", separator)
    head = trimmed[: len(trimmed) - len(tail)]
    # A whole trailing segment, not a suffix of one: `Z:\X2026` is not `2026`.
    if not trimmed.lower().endswith(tail.lower()) or (head and head[-1] not in "/\\"):
        return None
    parent = head.rstrip("/\\")
    if parent.endswith(":"):  # a bare drive letter needs its separator back
        parent += separator
    return parent or separator


async def plan_merge(db: AsyncSession, root: str, name: str) -> MergePlan:
    new_root = PurePosixPath(root.rstrip("/") or "/")
    name = name.strip()
    if not name:
        raise MergeError("The merged library needs a name")
    libraries = (await db.execute(select(Library))).scalars().all()

    for library in libraries:
        if PurePosixPath(library.root_path) in new_root.parents:
            raise MergeError(
                f"{new_root} is inside the library {library.name!r} ({library.root_path}); "
                "a library cannot sit inside another"
            )
    inside = [
        lib
        for lib in libraries
        if PurePosixPath(lib.root_path) == new_root
        or new_root in PurePosixPath(lib.root_path).parents
    ]
    if not inside:
        raise MergeError(f"No library lives under {new_root}")
    clash = [lib for lib in libraries if lib.name == name and lib not in inside]
    if clash:
        raise MergeError(f"Another library is already called {name!r}")

    counts: dict[uuid.UUID, int] = {
        library_id: count
        for library_id, count in (
            await db.execute(
                select(Asset.library_id, func.count())
                .where(Asset.library_id.in_([lib.id for lib in inside]))
                .group_by(Asset.library_id)
            )
        ).all()
    }
    members = [
        Member(
            id=lib.id,
            name=lib.name,
            root_path=lib.root_path,
            subfolder=str(PurePosixPath(lib.root_path).relative_to(new_root)).strip("."),
            assets=int(counts.get(lib.id, 0)),
        )
        for lib in sorted(inside, key=lambda lib: lib.name)
    ]
    by_id = {lib.id: lib for lib in inside}
    at_root = [m for m in members if not m.subfolder]
    target = at_root[0] if at_root else max(members, key=lambda m: m.assets)

    # Two member libraries that overlap would each hold the same file; after
    # the move both rows would claim one path.
    paths = union_all(
        *(
            select((literal(_prefix(m.subfolder)) + Asset.relative_path).label("p")).where(
                Asset.library_id == m.id
            )
            for m in members
        )
    ).subquery()
    collisions = (
        (await db.execute(select(paths.c.p).group_by(paths.c.p).having(func.count() > 1).limit(5)))
        .scalars()
        .all()
    )
    if collisions:
        raise MergeError(
            "These libraries overlap — the same file is catalogued in more than one: "
            + ", ".join(collisions)
        )

    target_lib = by_id[target.id]
    notes: list[str] = []
    # The kept library's excludes first, in its order, then whatever the
    # folded ones add; the union, because the parent now covers all of them.
    ordered = [target, *[m for m in members if m.id != target.id]]
    globs = list(dict.fromkeys(g for m in ordered for g in by_id[m.id].exclude_globs))
    if any(by_id[m.id].include_extensions is None for m in members):
        extensions = None
    else:
        extensions = sorted({e for m in members for e in by_id[m.id].include_extensions or []})
    intervals: list[int] = [
        interval
        for interval in (by_id[m.id].scan_interval_minutes for m in members)
        if interval is not None
    ]
    for setting in ("generate_proxies", "transcribe_enabled", "proxy_resolution"):
        kept = getattr(target_lib, setting)
        differ = [m.name for m in members if getattr(by_id[m.id], setting) != kept]
        if differ:
            notes.append(
                f"{setting} = {kept!r} (from {target.name}); differed in {', '.join(differ)}"
            )

    mappings: dict[str, tuple[str, str]] = {}
    for member in [target, *[m for m in members if m.id != target.id]]:
        rows = (
            (await db.execute(select(PathMapping).where(PathMapping.library_id == member.id)))
            .scalars()
            .all()
        )
        for mapping in rows:
            lifted = _parent_prefix(mapping.mapped_prefix, member.subfolder)
            if lifted is None:
                notes.append(
                    f"path profile {mapping.profile_name!r} ({mapping.mapped_prefix}) on "
                    f"{member.name} does not end with {member.subfolder!r}; dropped — "
                    "set it again on the Libraries page"
                )
                continue
            if mapping.profile_name in mappings and mappings[mapping.profile_name][1] != lifted:
                notes.append(
                    f"path profile {mapping.profile_name!r} disagreed between libraries; "
                    f"kept {mappings[mapping.profile_name][1]}"
                )
                continue
            mappings.setdefault(mapping.profile_name, (mapping.platform, lifted))

    return MergePlan(
        root=str(new_root),
        name=name,
        target=target,
        members=members,
        exclude_globs=globs,
        include_extensions=extensions,
        scan_interval_minutes=min(intervals) if intervals else None,
        path_mappings=mappings,
        notes=notes,
    )


async def apply_merge(db: AsyncSession, plan: MergePlan) -> None:
    target_id = plan.target.id
    # The kept library first: its own assets get their prefix while they are
    # still the only ones in it. Moved in first, a folded library's assets
    # would be caught by that update too and prefixed twice.
    for member in [plan.target, *plan.folded]:
        await db.execute(
            update(Asset)
            .where(Asset.library_id == member.id)
            .values(
                library_id=target_id,
                relative_path=literal(_prefix(member.subfolder)) + Asset.relative_path,
            )
        )
    member_ids = [m.id for m in plan.members]
    # Scans queued against the old roots would walk folders that are no
    # longer a library's root; one fresh scan of the parent replaces them.
    await db.execute(
        update(Scan)
        .where(Scan.library_id.in_(member_ids), Scan.status.in_(ACTIVE_SCANS))
        .values(status="cancelled")
    )
    await db.execute(
        update(Scan).where(Scan.library_id.in_(member_ids)).values(library_id=target_id)
    )
    await db.execute(delete(PathMapping).where(PathMapping.library_id.in_(member_ids)))
    for profile, (platform, prefix) in sorted(plan.path_mappings.items()):
        db.add(
            PathMapping(
                library_id=target_id, profile_name=profile, platform=platform, mapped_prefix=prefix
            )
        )

    target = await db.get(Library, target_id)
    assert target is not None
    target.name = plan.name
    target.root_path = plan.root
    target.exclude_globs = plan.exclude_globs
    target.include_extensions = plan.include_extensions
    target.scan_interval_minutes = plan.scan_interval_minutes
    for member in plan.folded:
        folded = await db.get(Library, member.id)
        if folded is not None:
            await db.delete(folded)
    db.add(Scan(library_id=target_id, status="pending"))
    db.add(
        AuditLog(
            event="library.merged",
            detail={
                "into": plan.name,
                "root": plan.root,
                "kept_id": str(target_id),
                "folded": [
                    {"name": m.name, "root": m.root_path, "assets": m.assets} for m in plan.folded
                ],
            },
        )
    )
    await db.commit()


def describe(plan: MergePlan) -> str:
    lines = [f"Merge into {plan.name!r} at {plan.root}:"]
    for member in plan.members:
        role = "kept (id, settings, history)" if member.id == plan.target.id else "folded in"
        lines.append(
            f"  {member.name:<24} {member.assets:>6} assets  {member.root_path}  "
            f"-> {_prefix(member.subfolder) or '(root)'}   [{role}]"
        )
    lines.append(f"  exclude: {', '.join(plan.exclude_globs) or '(none)'}")
    lines.append(
        "  rescan: "
        + (f"every {plan.scan_interval_minutes} min" if plan.scan_interval_minutes else "manual")
    )
    for profile, (platform, prefix) in sorted(plan.path_mappings.items()):
        lines.append(f"  path profile {profile!r} ({platform}): {prefix}")
    lines.extend(f"  note: {note}" for note in plan.notes)
    return "\n".join(lines)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    parser.add_argument("root", help="the parent folder, as the containers see it (/media/...)")
    parser.add_argument("--name", required=True, help="name of the merged library")
    parser.add_argument("--apply", action="store_true", help="make the change (default: plan)")
    args = parser.parse_args(argv)

    engine = create_async_engine(get_settings().db_url)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            try:
                plan = await plan_merge(db, args.root, args.name)
            except MergeError as err:
                print(f"Cannot merge: {err}", file=sys.stderr)
                return 1
            print(describe(plan))
            if not args.apply:
                print("\nNothing changed. Stop the scanner and workers, then run with --apply.")
                return 0
            await apply_merge(db, plan)
            moved = sum(m.assets for m in plan.members)
            print(f"\nMerged. {moved} assets now live in {plan.name!r}.")
            return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
