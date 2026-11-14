"""What the Check button shows: everything about a model that can be learned
without downloading it.

Given a name from Hugging Face, `preflight` answers the questions an owner
would ask before letting a model onto the machine: who published it and what
it was built from, under which licence, whether an account is needed, which
files would be downloaded and which never are, how large it is, whether it
fits the memory budgets, and which program would run it. Each answer is one
`Check` for the page; anything that rules the model out is a sentence in
`problems`, and anything the owner should knowingly accept is in `warnings`.

The same function runs again when Download is pressed, at the build the check
showed, and nothing else from the browser is trusted. So every rule that
keeps a model out is enforced here, not on the page.

It only reads. It asks Hugging Face for the model's page, the page of each
model it says it was built from, and its small settings files. **It never
downloads a file** (`hub.fetch` is not called; a test counts). It touches no
database and no disk: what is installed, which runtimes exist and how much
room there is are handed in by the caller. It blocks on the network, so call
it with `asyncio.to_thread`.

A refused model still gets every check that could be filled in, so the owner
sees everything that is wrong at once rather than one thing per attempt.
"""

import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from framefound.local_models import gguf, policy, schedule
from framefound.local_models.files import (
    MAX_INSPECTED,
    TEXT_NAMES,
    FilePlan,
    inspect_config,
    plan_files,
    to_inspect,
)
from framefound.local_models.hub import Hub, HubError, RepoFile, RepoInfo, now_points_to
from framefound.local_models.kinds import Fit, RuntimeKind, Support, Variant, choose
from framefound.local_models.settings_store import LocalModelsConfig

# How far "built from" is followed. The first model of the plan is two steps
# from its origin; a chain longer than this is not one FrameFound will vouch
# for, and a card can be written to name models without end.
MAX_DEPTH = 5
MAX_ANCESTORS = 12

Tone = Literal["ok", "warn", "bad"]
# What a file of a build is to the person reading the check.
Part = Literal["model", "pictures", "text"]
# open: anyone may download. ok: it asks for an account and the stored token
# was let in. untested: it asks for an account and the repository has no
# small file to try the token on, so the download will be what finds out.
Access = Literal["open", "ok", "needs_token", "no_access", "untested"]

CAN_BE_ADDED = "This model can be added."
# The first line of the card when the build it would download fits no
# budget: it can be added (after one acknowledgement), and "can be added"
# alone would read as if it would also run.
WONT_RUN_YET = "This model can be added, but it will not run until {until}."
ALREADY_INSTALLED = "This exact build is already installed."
NOT_IN_REPOSITORY = "That build is not in this repository."
PRIVATE = (
    "This model is private on Hugging Face. FrameFound only adds models whose page anyone can read."
)
DISABLED = "Hugging Face has switched this model off, so it cannot be downloaded."
NO_LICENCE = "Its page states no licence. Read the model's page before using it for client work."

_GB = 1024**3
_MB = 1024**2
# How much of a text file is read when it is only there to try the token on.
_PROBE_BYTES = 1024
_MOST_NAMES = 5
# Below this many bits per weight a build is "very small" (gguf.SMALL_BUILD).
_FEW_BITS = 4
_NUMBERS = ("two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


@dataclass(frozen=True)
class Check:
    """One row of the check: a question, its answer, and how it stands."""

    # publisher | built_from | licence | access | build | files | size | memory | runtime
    # (`build` only for a repository of more than one build).
    key: str
    label: str
    tone: Tone
    value: str
    detail: str | None = None


class Installed(NamedTuple):
    """A build of a repository that is already installed."""

    id: uuid.UUID
    revision: str
    variant: str = ""


@dataclass(frozen=True)
class Sized:
    """One build of the repository, with what it would cost to have."""

    variant: Variant
    size_bytes: int
    memory_gb: float
    fits: Fit


@dataclass(frozen=True)
class Preflight:
    """Everything one check found. Plain values: safe to hand from the thread
    that ran it to the request that asked."""

    info: RepoInfo
    # The candidate first, then every model read while following what it
    # says it was built from. Stored with the model: approval is judged
    # again from it each time the model is used.
    chain: tuple[policy.Node, ...]
    complete: bool
    verdict: policy.Verdict
    plan: FilePlan
    # The kind that would run it; None when nothing here can.
    runtime: str | None
    runtime_installed: bool
    support: Support
    # The build everything below describes; None when nothing here can run
    # the repository, and then nothing is sized.
    variant: Variant | None
    size_bytes: int
    memory_gb: float
    fits: Fit
    access: Access
    free_bytes: int | None
    # A build of the same repository that is already installed: this very
    # build when `same_build`, otherwise another one.
    existing: uuid.UUID | None
    same_build: bool
    checks: tuple[Check, ...]
    # Any one of these: it cannot be added.
    problems: tuple[str, ...]
    # To be accepted once, knowingly, before the download starts.
    warnings: tuple[str, ...]
    # Worth saying beside the result; nothing to accept.
    notes: tuple[str, ...] = ()
    # Every build of the repository, sized, in the kind's order.
    sized: tuple[Sized, ...] = ()
    # The build the kind suggests among `sized` — worked out whichever build
    # was asked for, so the list can mark it. None when nothing is sized.
    recommended: str | None = None
    # When the chosen build fits no budget: what has to change before it
    # runs ("the overnight budget is raised"). "" when it fits one.
    until: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def summary(self) -> str:
        """The first line of the card: that it can be added, or the first reason it cannot."""
        if self.problems:
            return self.problems[0]
        if self.until:
            return WONT_RUN_YET.format(until=self.until)
        return CAN_BE_ADDED

    @property
    def takes(self) -> tuple[str, ...]:
        """What the build takes: text, and pictures if it reads them."""
        if self.variant is not None and self.variant.takes:
            return self.variant.takes
        return self.support.takes


def _none_installed(_repo_id: str) -> Sequence[Installed]:
    return ()


def estimate_memory_gb(weights_bytes: int) -> float:
    """What a model is assumed to need until a real answer has been measured.

    The size of its weights and a fifth more, rounded up to a tenth of a GB.
    The plan's own figure for the first model (a 4.8 GB file, about 6 GB in
    memory) is where the fifth comes from. Whole-number arithmetic, so a
    model of exactly 5 GB is 6.0 on every machine and never 6.1.
    """
    return -(-max(weights_bytes, 0) * 12 // _GB) / 10


def bits_of(variant: Variant) -> int | None:
    """Bits per weight the build's own quantisation names (4 for "Q4_K_M"); None when unknown."""
    return gguf.nominal_bits(variant.quant) if variant.quant else None


def part_of(path: str) -> Part:
    """What one file of a build is: the model, the part that reads pictures, or a text."""
    if path in TEXT_NAMES:
        return "text"
    if gguf.is_projector(path):
        return "pictures"
    return "model"


def size_text(size: int) -> str:
    """A size as the check writes it: GB with one decimal, whole MB below that. 1 GB = 1024³."""
    if size >= _GB:
        return f"{size / _GB:.1f} GB"
    return f"{max(1, round(size / _MB))} MB"


@dataclass(frozen=True)
class _Walked:
    chain: tuple[policy.Node, ...]
    complete: bool
    # A base whose name now points somewhere else -> where, when that was said.
    moved: Mapping[str, str | None]
    # Stopped at a limit, rather than by something that could not be read.
    cut_short: bool


def walk(hub: Hub, info: RepoInfo) -> tuple[tuple[policy.Node, ...], bool]:
    """Follow what a model says it was built from: `(chain, complete)`.

    The chain starts with the candidate and holds one node for every model
    that was asked about, nearest first. `complete` is False when not every
    named model could be read: one Hugging Face no longer has (it is in the
    chain, marked not found), the network failing part-way, or a chain longer
    than `MAX_DEPTH` steps or `MAX_ANCESTORS` models. `policy.judge` refuses
    an incomplete chain, because "built from a blocked publisher's model" can
    only be answered for a whole one.
    """
    walked = _walk(hub, info)
    return walked.chain, walked.complete


def _ask(hub: Hub, name: str, revision: str | None = None) -> RepoInfo:
    """A model's page, asked for again if Hugging Face only corrects the name's capitals.

    A name that has moved is never followed: that would judge one
    publisher's model under another's name. A name that "moved" to the same
    letters in other capitals is the same model of the same publisher, and
    is asked for once more under that spelling. Whether Hugging Face answers
    such a name outright or forwards it has not been seen; without this, the
    second way would refuse for good every build whose page writes its base
    in other capitals, a thing its operator cannot correct.
    """
    try:
        return hub.info(name, revision)
    except HubError as err:
        respelled = err.moved_to if err.problem == "moved" else None
        if respelled is None or respelled == name or respelled.casefold() != name.casefold():
            raise
    return hub.info(respelled, revision)


def _walk(hub: Hub, info: RepoInfo) -> _Walked:
    candidate = _node(info)
    chain = [candidate]
    # Names already dealt with, so a card that names itself, or two models
    # that name each other, are each asked about once.
    seen = {info.repo_id.casefold()}
    waiting = deque((base, 1) for base in candidate.bases)
    moved: dict[str, str | None] = {}
    complete = True
    cut_short = False
    asked = 0

    while waiting:
        name, depth = waiting.popleft()
        if name.casefold() in seen:
            continue
        if depth > MAX_DEPTH or asked >= MAX_ANCESTORS:
            complete, cut_short = False, True
            break
        seen.add(name.casefold())
        asked += 1
        try:
            page = _ask(hub, name)
        except HubError as err:
            complete = False
            if err.problem == "not_found":
                chain.append(policy.Node(name, policy.publisher_of(name), found=False))
                continue
            if err.problem == "moved":
                moved[name] = err.moved_to
                chain.append(_forwarded(name, err.moved_to))
                continue
            # Hugging Face is not answering. Asking for the rest would only
            # wait out one timeout per name to learn the same thing.
            break
        if page.repo_id.casefold() != name.casefold():
            # The answer is about another model: the name was forwarded.
            complete = False
            moved[name] = page.repo_id
            chain.append(_forwarded(name, page.repo_id))
            continue
        node = _node(page)
        chain.append(node)
        waiting.extend((base, depth + 1) for base in node.bases)

    return _Walked(tuple(chain), complete, moved, cut_short)


def _forwarded(name: str, leads_to: str | None) -> policy.Node:
    """A named model whose name now leads to another one.

    Under this name Hugging Face has nothing, and what a forwarded name
    leads to is not judged in its place: that would be judging one
    publisher's model as another's. Where it leads is kept as what this node
    names, so a name that forwards to a blocked publisher is refused as that.
    """
    return policy.Node(
        name,
        policy.publisher_of(name),
        bases=(leads_to,) if leads_to else (),
        found=False,
    )


def preflight(
    hub: Hub,
    name: str,
    *,
    config: LocalModelsConfig,
    kinds: Mapping[str, RuntimeKind],
    installed: Mapping[str, str | None],
    free_bytes: int | None,
    min_free_bytes: int,
    existing: Callable[[str], Sequence[Installed]] = _none_installed,
    revision: str | None = None,
    variant: str | None = None,
    scratch_free_bytes: int | None = None,
) -> Preflight:
    """Check one model. Raises `ValueError` (the name, or a build that is not there)
    and `HubError` (the model's own page could not be read); both carry a sentence.

    - `hub` already holds the token, if one is stored.
    - `kinds` is what this install can run (`kinds.kinds()`); `installed` is
      the version of each that is installed, by kind name (missing or None:
      not installed).
    - `free_bytes` is the room in the model library and `min_free_bytes` what
      must stay free on any disk FrameFound writes to. `scratch_free_bytes`
      is the room on this machine's own disk, where a download lands first:
      give it only when that is a different disk from the library's.
    - `existing(repo_id)` is every installed build of that repository,
      whatever the capitals of its name, the newest first.
    - `revision` pins the build (Download re-checks the one the check
      showed); `variant` names the build of a repository that holds several.
    """
    asked_for = policy.clean_repo_id(name)
    info = _ask(hub, asked_for, revision)
    if info.repo_id.casefold() != asked_for.casefold():
        # The answer is about another model: the name was forwarded. It is
        # not judged under the name that was typed.
        raise HubError("moved", now_points_to(info.repo_id), moved_to=info.repo_id)

    lists = config.lists()
    budgets = config.budgets()
    walked = _walk(hub, info)
    verdict = policy.judge(walked.chain, lists, complete=walked.complete)
    plan = plan_files(info.files)
    access, refusal, config_problems = _read_settings(hub, info, plan)
    kind, support = choose(info, plan, kinds)

    # Sizes are a build's, never the repository's: a repository can hold
    # twenty builds, and what matters is the one that would be downloaded.
    sized = tuple(_sized(build, budgets) for build in support.variants)
    # The suggestion is worked out even when a build was asked for: the page
    # marks it in the list whichever one the operator is looking at.
    suggested: Sized | None = None
    if sized:
        preferred = (
            kind.prefer([(build.variant, build.fits) for build in sized])
            if kind is not None
            else sized[0].variant.id
        )
        suggested = next((build for build in sized if build.variant.id == preferred), sized[0])
    wanted = variant or None
    chosen: Sized | None = suggested
    if wanted is not None:
        chosen = next((build for build in sized if build.variant.id == wanted), None)
        if chosen is None:
            raise ValueError(NOT_IN_REPOSITORY)

    size = chosen.size_bytes if chosen else 0
    version = installed.get(kind.name) if kind is not None else None
    room = [
        sentence
        for sentence in (
            _no_room(size, free_bytes, min_free_bytes, "the model library"),
            _no_room(size, scratch_free_bytes, min_free_bytes, "this machine's own disk"),
        )
        if chosen is not None and sentence
    ]

    builds = list(existing(info.repo_id))
    build_id = chosen.variant.id if chosen else ""
    same = next(
        (
            build
            for build in builds
            if build.revision.lower() == info.revision and build.variant == build_id
        ),
        None,
    )
    other = same or (builds[0] if builds else None)

    problems: list[str] = []
    if info.private:
        problems.append(PRIVATE)
    if info.disabled:
        problems.append(DISABLED)
    if not verdict.allowed:
        problems.append(verdict.reason)
    problems.extend(plan.problems)
    problems.extend(config_problems)
    if kind is None:
        problems.append(support.note)
    if refusal:
        problems.append(refusal)
    problems.extend(room)
    if same is not None:
        problems.append(ALREADY_INSTALLED)

    # A warning is something to accept before a download starts. A model
    # that cannot be added has no download, and "it can be downloaded, but…"
    # under a refusal would contradict it; the rows still say all of this.
    warnings: list[str] = []
    budget_warning = _budget_warning(chosen, budgets) if chosen is not None else None
    if not problems and kind is not None:
        if budget_warning:
            warnings.append(budget_warning)
        if not info.licence:
            warnings.append(NO_LICENCE)
        if not version:
            warnings.append(
                f"{kind.label} is not installed yet. The model can be downloaded now and will "
                "work once it is."
            )
        if support.caveat:
            warnings.append(support.note)

    notes: list[str] = []
    if other is not None and same is None:
        notes.append(_other_build_note(other, info, build_id, pinned=revision is not None))

    publisher_check = _publisher_check(walked.chain[0], lists)
    build_check = _build_check(sized, chosen, suggested, budgets)
    checks = (
        publisher_check,
        _built_from_check(walked, verdict, lists, publisher_fine=publisher_check.tone == "ok"),
        _licence_check(info, lists),
        _access_check(access),
        *((build_check,) if build_check is not None else ()),
        _files_check(plan, chosen, sized, bool(plan.problems or config_problems)),
        _size_check(chosen, free_bytes, bool(room)),
        _memory_check(chosen, budgets),
        _runtime_check(kind, support, version),
    )
    return Preflight(
        info=info,
        chain=walked.chain,
        complete=walked.complete,
        verdict=verdict,
        plan=plan,
        runtime=kind.name if kind is not None else None,
        runtime_installed=bool(version),
        support=support,
        variant=chosen.variant if chosen else None,
        size_bytes=size,
        memory_gb=chosen.memory_gb if chosen else 0.0,
        fits=chosen.fits if chosen else schedule.fits(0.0, budgets),
        access=access,
        free_bytes=free_bytes,
        existing=other.id if other is not None else None,
        same_build=same is not None,
        checks=checks,
        problems=tuple(dict.fromkeys(problems)),
        warnings=tuple(dict.fromkeys(warnings)),
        notes=tuple(notes),
        sized=sized,
        recommended=suggested.variant.id if suggested is not None else None,
        until=_until(chosen, budgets)
        if chosen is not None and kind is not None and chosen.fits == "never"
        else "",
    )


# --- reading the model's own settings ---------------------------------------------


def _read_settings(hub: Hub, info: RepoInfo, plan: FilePlan) -> tuple[Access, str, list[str]]:
    """Read the settings files a runtime would load the model by.

    Two things are learned from the same requests: whether any of them asks
    for program code from the repository (the model's page carries only some
    of a settings file's keys, so the file itself is read), and whether a
    model that asks for an account lets this token in. Returns the access,
    the sentence when access was refused, and the settings files' problems.

    Each file is one request, and how many a repository lists is its
    publisher's choice. Past `MAX_INSPECTED` none is read: the plan has
    already refused the model for it (`plan_files`), in its own sentence.
    """
    problems: list[str] = []
    tried = False
    inspected = to_inspect(plan.keep)
    if len(inspected) > MAX_INSPECTED:
        inspected = ()
    for file in inspected:
        try:
            text = hub.read_text(info.repo_id, info.revision, file.path)
        except HubError as err:
            refused = _refused(err)
            if refused is not None:
                # The rest would be refused the same way. They stay unread;
                # the model is refused for the access, and is checked again
                # once a token is there.
                return refused, str(err), problems
            if err.problem != "not_found":
                raise
            # Listed, and not there: what cannot be read cannot be cleared.
            problems.extend(inspect_config(file.path, ""))
            continue
        tried = True
        problems.extend(inspect_config(file.path, text))

    if not info.gated:
        return "open", "", problems
    if tried:
        return "ok", "", problems
    # It asks for an account and has no settings file to learn from: try the
    # token on the start of a small text file instead.
    for file in _texts(plan.keep):
        try:
            hub.read_text(info.repo_id, info.revision, file.path, _PROBE_BYTES)
        except HubError as err:
            refused = _refused(err)
            if refused is not None:
                return refused, str(err), problems
            if err.problem != "not_found":
                raise
            continue
        return "ok", "", problems
    return "untested", "", problems


def _refused(err: HubError) -> Access | None:
    """The access a refusal means, when it is a refusal of access at all."""
    if err.problem == "needs_token":
        return "needs_token"
    if err.problem == "no_access":
        return "no_access"
    return None


def _texts(files: Sequence[RepoFile]) -> list[RepoFile]:
    """The readme and licence texts among `files`, the readme first."""
    texts = [file for file in files if file.path in TEXT_NAMES]
    return sorted(texts, key=lambda file: TEXT_NAMES.index(file.path))


# --- sizes and budgets ------------------------------------------------------------


def _sized(variant: Variant, budgets: schedule.Budgets) -> Sized:
    memory = estimate_memory_gb(variant.weights_bytes)
    return Sized(
        variant=variant,
        size_bytes=sum(file.size for file in variant.files),
        memory_gb=memory,
        fits=schedule.fits(memory, budgets),
    )


def _no_room(needed: int, free: int | None, floor: int, where: str) -> str | None:
    """Why the download will not fit on a disk; None when it fits or the room is not known."""
    if free is None or free - needed >= floor:
        return None
    sentence = (
        f"There is not enough room: it needs {size_text(needed)} and {where} has "
        f"{size_text(free)} free."
    )
    if needed <= free:
        # The two figures alone would read as if it fitted.
        sentence += f" FrameFound leaves the last {size_text(floor)} of a disk free."
    return sentence


def _window(budgets: schedule.Budgets) -> str:
    return f"{schedule.clock_text(budgets.night_start)}–{schedule.clock_text(budgets.night_end)}"


def _larger(budgets: schedule.Budgets) -> tuple[float, str]:
    """The larger budget and its name: the overnight one, the usual way round."""
    if budgets.night_gb >= budgets.day_gb:
        return budgets.night_gb, "overnight"
    return budgets.day_gb, "daytime"


def _budget_warning(build: Sized, budgets: schedule.Budgets) -> str | None:
    needs = schedule.gb_text(build.memory_gb)
    day = schedule.gb_text(budgets.day_gb)
    if build.fits == "day":
        return None
    if build.fits == "night":
        return (
            f"It needs about {needs} GB of memory, more than the daytime budget ({day} GB). "
            f"It would only run overnight, {_window(budgets)}."
        )
    if schedule.lacks_window(build.memory_gb, budgets):
        # A window that starts and ends at the same minute never opens.
        return (
            f"It needs about {needs} GB of memory, more than the daytime budget ({day} GB), "
            "and there is no overnight window. It can be downloaded, but it will not run "
            "until one of them is changed."
        )
    larger, which = _larger(budgets)
    return (
        f"It needs about {needs} GB, more than the {which} budget "
        f"({schedule.gb_text(larger)} GB). It can be downloaded, but it will not run until "
        "the budget is raised."
    )


def _until(build: Sized, budgets: schedule.Budgets) -> str:
    """What has to change before a build that fits no budget runs (the card's first line)."""
    if schedule.lacks_window(build.memory_gb, budgets):
        return "the daytime budget or the overnight window is changed"
    _larger_gb, which = _larger(budgets)
    return f"the {which} budget is raised"


def _other_build_note(other: Installed, info: RepoInfo, build_id: str, *, pinned: bool) -> str:
    short = other.revision[:7]
    if other.revision.lower() == info.revision:
        # The same files as Hugging Face has them today, another build of them.
        if other.variant and build_id:
            # A repository of several builds: each has a name to say.
            return (
                f"You already have the {other.variant} build of this model. This would "
                f"download {build_id} beside it."
            )
        return (
            "You already have another build of this model. This would download this one beside it."
        )
    if pinned:
        # A build chosen by its commit: which of the two is newer is not known.
        return (
            f"You have another build of this model ({short}). This would download this one "
            "beside it; remove the one you do not want when you are happy."
        )
    return (
        f"You have an older build of this model ({short}). This would download the current "
        "build beside it; remove the old one when you are happy."
    )


# --- the rows of the check --------------------------------------------------------


def _node(info: RepoInfo) -> policy.Node:
    return policy.Node(info.repo_id, info.publisher, info.bases, True)


def _standing(
    repo_id: str, reported: str | None, lists: policy.PublisherLists
) -> tuple[policy.Status, str | None]:
    """Which list a model's publisher is on, and the publisher's name.

    The publisher Hugging Face reports and the one in the model's name are
    the same on every page seen. If they ever differ the model is blocked
    when either is — the same reading as `policy.judge`.
    """
    named = policy.publisher_of(repo_id)
    owners = tuple(dict.fromkeys(owner for owner in (reported, named) if owner))
    for owner in owners:
        if lists.status(owner) == "blocked":
            return "blocked", owner
    if not owners:
        return "unknown", None
    return lists.status(owners[0]), owners[0]


def _publisher_check(candidate: policy.Node, lists: policy.PublisherLists) -> Check:
    status, owner = _standing(candidate.repo_id, candidate.publisher, lists)
    if owner is None:
        return Check("publisher", "Published by", "bad", "No publisher in its name")
    name = _shown(owner)
    if status == "maker":
        return Check("publisher", "Published by", "ok", f"{name} — an approved maker")
    if status == "converter":
        return Check(
            "publisher",
            "Published by",
            "ok",
            f"{name} — an approved converter",
            f"Packaged by {name}, not by the maker. FrameFound can check what a build says "
            "it was made from, not that it is true — which is why converters are a list you "
            "control.",
        )
    if status == "blocked":
        return Check("publisher", "Published by", "bad", f"{name} — on the blocked list")
    return Check(
        "publisher", "Published by", "bad", f"{name} — not one of your approved publishers"
    )


def _built_from_check(
    walked: _Walked, verdict: policy.Verdict, lists: policy.PublisherLists, *, publisher_fine: bool
) -> Check:
    candidate = walked.chain[0]
    read = {node.repo_id.casefold(): node for node in walked.chain}
    own = candidate.repo_id.casefold()
    bases = [base for base in candidate.bases if base.casefold() != own]
    # With an approved publisher, a refusal can only be about the ancestry.
    # The rule itself is policy.judge's; this row never decides anything.
    wrong = (publisher_fine and not verdict.allowed) or not walked.complete

    if not bases:
        if wrong:
            return Check("built_from", "Built from", "bad", "Its page does not say")
        return Check("built_from", "Built from", "ok", "Nothing — its page names no other model")

    parts: list[str] = []
    for base in bases:
        text, flawed = _described(base, read.get(base.casefold()), walked, lists)
        parts.append(text)
        wrong = wrong or flawed

    further: list[str] = []
    nearest = {base.casefold() for base in bases}
    for node in walked.chain[1:]:
        named = [base for base in node.bases if base.casefold() != node.repo_id.casefold()]
        direct = node.repo_id.casefold() in nearest
        if not direct:
            # The nearest ones are described in the row's value already.
            status, owner = _standing(node.repo_id, node.publisher, lists)
            if status == "blocked":
                wrong = True
                further.append(
                    f"{_shown(node.repo_id)} comes from {_shown(owner or '')}, a blocked publisher."
                )
            elif not node.found:
                further.append(_gone(node.repo_id, walked))
        if not node.found:
            continue
        if named:
            maker = _shown(node.publisher or "its publisher")
            if direct and len(bases) == 1:
                further.append(f"…which {maker} built from {_and(named)}.")
            else:
                further.append(f"{maker} built {_shown(node.repo_id)} from {_and(named)}.")
    if walked.cut_short:
        further.append(
            f"FrameFound follows what a model was built from for {MAX_DEPTH} steps and "
            f"{MAX_ANCESTORS} models, and this goes further."
        )
    return Check(
        "built_from",
        "Built from",
        "bad" if wrong else "ok",
        "; ".join(parts),
        " ".join(further) or None,
    )


def _described(
    base: str, node: policy.Node | None, walked: _Walked, lists: policy.PublisherLists
) -> tuple[str, bool]:
    """One model the candidate names, in a few words, and whether that alone rules it out."""
    name = _shown(node.repo_id if node is not None and node.found else base)
    status, owner = _standing(base, node.publisher if node is not None else None, lists)
    by = _shown(owner or "")
    if status == "blocked":
        return f"{name}, by {by} — a blocked publisher", True
    if node is None:
        return f"{name} — could not be read", True
    if not node.found:
        return _gone(base, walked).removesuffix("."), True
    if owner is None:
        return f"{name} — no publisher in its name", False
    if status == "maker":
        return f"{name}, by {by} — an approved maker", False
    if status == "converter":
        return f"{name}, by {by} — a converter, not a maker", False
    return f"{name}, by {by} — not on your makers list", False


def _gone(name: str, walked: _Walked) -> str:
    if name in walked.moved:
        leads_to = walked.moved[name]
        if leads_to:
            return f"{_shown(name)} now points to {_shown(leads_to)} on Hugging Face."
        return f"{_shown(name)} now points somewhere else on Hugging Face."
    return f"{_shown(name)} is not on Hugging Face now."


def _licence_check(info: RepoInfo, lists: policy.PublisherLists) -> Check:
    if not info.licence:
        return Check("licence", "Licence", "warn", "None stated", NO_LICENCE)
    value = _shown(info.licence)
    if info.licence == "other" and info.licence_name:
        value = f"other — {_shown(info.licence_name)}"
    status, _owner = _standing(info.repo_id, info.publisher, lists)
    if status == "converter":
        detail = "As stated on this build's own page. The maker's page may state a different one."
    else:
        detail = "As stated on the model's page."
    return Check("licence", "Licence", "ok", value, detail)


def _access_check(access: Access) -> Check:
    if access == "open":
        return Check("access", "Access", "ok", "Open — no account needed")
    if access == "ok":
        return Check(
            "access",
            "Access",
            "ok",
            "Needs an account — your Hugging Face token can read it",
        )
    if access == "needs_token":
        return Check("access", "Access", "bad", "Needs an account, and no token is stored")
    if access == "no_access":
        return Check("access", "Access", "bad", "Your Hugging Face token cannot read it")
    return Check(
        "access",
        "Access",
        "warn",
        "Could not be tested before downloading",
        "This repository has no small file to try. If the token cannot read it, the download "
        "will stop and say so.",
    )


def _build_check(
    sized: Sequence[Sized],
    chosen: Sized | None,
    suggested: Sized | None,
    budgets: schedule.Budgets,
) -> Check | None:
    """Which of several builds this is, and why: only for a repository of more than one.

    `warn` when the build fits no budget or is very small, else `ok`; never
    `bad`, because choosing among builds is the operator's to do.
    """
    if len(sized) <= 1 or chosen is None or suggested is None:
        return None
    count = len(sized)
    value = f"{chosen.variant.id} — one of {count} in this repository"
    _larger_gb, which = _larger(budgets)
    nothing_fits = all(build.fits == "never" for build in sized)
    # A build between the two budgets is `never` when the overnight window never
    # opens: what is in the way is then the window, not the overnight budget.
    fits = {
        "day": "it fits the daytime budget.",
        "night": "it fits the overnight budget only.",
        "never": (
            "it fits the overnight budget, but there is no overnight window."
            if schedule.lacks_window(chosen.memory_gb, budgets)
            else "it fits no budget."
        ),
    }[chosen.fits]
    if nothing_fits:
        if any(schedule.lacks_window(build.memory_gb, budgets) for build in sized):
            said = (
                f"None of the {_number(count)} fits the daytime budget, and there is no "
                "overnight window for those that fit the overnight one; "
            )
        else:
            said = f"None of the {_number(count)} fits the {which} budget; "
        said += (
            "this is the smallest."
            if chosen is suggested
            else f"the smallest is {suggested.variant.id}."
        )
    elif chosen is suggested:
        said = f"The suggested build; {fits}"
    else:
        said = f"Chosen instead of the suggested {suggested.variant.id}; {fits}"
    bits = bits_of(chosen.variant)
    small = bits is not None and bits < _FEW_BITS
    if small and chosen.variant.note:
        said += f" {chosen.variant.note}"
    said += " Choose another under Which build."
    tone: Tone = "warn" if chosen.fits == "never" or small else "ok"
    return Check("build", "Build", tone, value, said)


def _files_check(
    plan: FilePlan, chosen: Sized | None, sized: Sequence[Sized], wrong: bool
) -> Check:
    wanted = chosen.variant.files if chosen else plan.keep
    if chosen is not None and any(_is_gguf(file.path) for file in wanted):
        return _gguf_files_check(plan, chosen, sized, wrong)
    taken = {file.path for file in wanted}
    left_out = [path for path, _why in plan.left_out]
    # Kept by the plan but not part of this build: another build's files.
    left_out.extend(file.path for file in plan.keep if file.path not in taken)

    value = f"{_count(len(wanted), 'file')} to download"
    if left_out:
        value += f", {len(left_out)} left out"
    formats = [
        _FORMATS[suffix]
        for suffix in plan.suffixes
        if any(file.path.lower().endswith(suffix) for file in wanted)
    ]
    detail = f"Weights are {_and(formats)}." if formats else "No weights FrameFound can use."
    if left_out:
        names = [_shown(path) for path in left_out[:_MOST_NAMES]]
        more = len(left_out) - len(names)
        listed = ", ".join(names) + (f" and {more} more" if more else "")
        detail += f" Left out: {listed}."
    return Check("files", "Files", "bad" if wrong else "ok", value, detail)


def _gguf_files_check(plan: FilePlan, chosen: Sized, sized: Sequence[Sized], wrong: bool) -> Check:
    """The files row for a GGUF build: what each file is, and which builds stay behind.

    A GGUF repository can hold twenty builds and three projectors; a count
    alone ("4 files, 23 left out") would not tell the operator that the
    other builds are what is left, nor which picture reader was chosen.
    """
    wanted = chosen.variant.files
    taken = {file.path for file in wanted}
    models = [file for file in wanted if part_of(file.path) == "model"]
    projector = next((file for file in wanted if part_of(file.path) == "pictures"), None)
    texts = [file.path for file in wanted if part_of(file.path) == "text"]

    what = ["the model" if len(models) <= 1 else f"the model in {len(models)} parts"]
    if projector is not None:
        what.append("the part that reads pictures")
    if texts:
        names = dict.fromkeys(path.rsplit(".", 1)[0] if "." in path else path for path in texts)
        what.append(f"its {_and(list(names))}")
    value = f"{_count(len(wanted), 'file')} to download: {_listed(what)}"

    detail = "Weights are GGUF."
    if projector is not None:
        detail += (
            f" The part that reads pictures is {_shown(projector.path)} "
            f"({size_text(projector.size)})."
        )
    else:
        detail += " This build has no part that reads pictures."

    others = [build for build in sized if build is not chosen]
    # Another build's model files are said as that build, not one by one.
    theirs = {
        file.path
        for build in others
        for file in build.variant.files
        if part_of(file.path) == "model" and file.path not in taken
    }
    left: list[str] = []
    if others:
        ids = [build.variant.id for build in others]
        shown = ", ".join(ids[:_MOST_NAMES])
        if len(ids) > _MOST_NAMES:
            shown += f" and {len(ids) - _MOST_NAMES} more"
        if len(ids) == 1:
            left.append(f"the other build ({shown})")
        elif len(ids) > _MOST_NAMES:
            left.append(f"the other {len(ids)} builds ({shown})")
        else:
            left.append(f"the other builds ({shown})")
    rest = [file.path for file in plan.keep if file.path not in taken and file.path not in theirs]
    rest.extend(path for path, _why in plan.left_out)
    left.extend(_shown(path) for path in rest[:_MOST_NAMES])
    if len(rest) > _MOST_NAMES:
        left.append(f"{len(rest) - _MOST_NAMES} more")
    if left:
        detail += f" Left out: {_joined(left)}."
    return Check("files", "Files", "bad" if wrong else "ok", value, detail)


def _size_check(chosen: Sized | None, free: int | None, short: bool) -> Check:
    if chosen is None:
        return Check(
            "size",
            "Size",
            "warn",
            "Not worked out",
            "Nothing here can run this model, so there is no build to size.",
        )
    if free is None:
        detail = "How much room the model library has could not be read."
    else:
        detail = f"The model library has {size_text(free)} free."
    return Check("size", "Size", "bad" if short else "ok", size_text(chosen.size_bytes), detail)


def _memory_check(chosen: Sized | None, budgets: schedule.Budgets) -> Check:
    if chosen is None:
        return Check("memory", "Memory", "warn", "Not worked out")
    about = f"About {schedule.gb_text(chosen.memory_gb)} GB"
    day = f"The daytime budget is {schedule.gb_text(budgets.day_gb)} GB"
    night = f"the overnight budget is {schedule.gb_text(budgets.night_gb)} GB"
    if chosen.fits == "day":
        return Check("memory", "Memory", "ok", f"{about} — fits by day", f"{day}.")
    if chosen.fits == "night":
        return Check(
            "memory",
            "Memory",
            "warn",
            f"{about} — overnight only",
            f"{day}; {night}, {_window(budgets)}.",
        )
    if schedule.lacks_window(chosen.memory_gb, budgets):
        return Check(
            "memory",
            "Memory",
            "warn",
            f"{about} — more than the daytime budget",
            f"{day}, and there is no overnight window.",
        )
    larger, which = _larger(budgets)
    return Check(
        "memory",
        "Memory",
        "warn",
        f"{about} — more than the {which} budget",
        f"The {which} budget is {schedule.gb_text(larger)} GB.",
    )


def _runtime_check(kind: RuntimeKind | None, support: Support, version: str | None) -> Check:
    if kind is None:
        return Check("runtime", "Runs on", "bad", "Nothing here can run it", support.note)
    if not version:
        return Check(
            "runtime", "Runs on", "warn", f"{kind.label} — not installed yet", support.note
        )
    tone: Tone = "warn" if support.caveat else "ok"
    return Check("runtime", "Runs on", tone, f"{kind.label} {version}", support.note)


_FORMATS = {".safetensors": "safetensors", ".gguf": "GGUF"}


def _count(number: int, thing: str) -> str:
    return f"{number} {thing}" if number == 1 else f"{number} {thing}s"


def _and(names: Sequence[str]) -> str:
    return _joined([_shown(name) for name in names])


def _joined(shown: Sequence[str]) -> str:
    """Phrases already fit to show, as "a, b and c"."""
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def _listed(parts: Sequence[str]) -> str:
    """The parts of one build, as "a, b, and c": each part is a phrase with its own "and"."""
    if len(parts) <= 2:
        return " and ".join(parts)
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _number(count: int) -> str:
    """A small count in words ("three"), a larger one in figures."""
    return _NUMBERS[count - 2] if 2 <= count < len(_NUMBERS) + 2 else str(count)


def _is_gguf(path: str) -> bool:
    return path.lower().endswith(".gguf")


def _shown(text: str) -> str:
    """Somebody else's text made fit to put in a sentence."""
    return "".join(letter for letter in text if letter.isprintable())[:120]
