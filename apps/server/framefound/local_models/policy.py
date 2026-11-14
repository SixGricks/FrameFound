"""Whose models may be installed: three lists of publishers and one rule.

A model on Hugging Face is named `publisher/name`. The owner approves
publishers, not models, so a new model from an approved publisher needs no
release of FrameFound:

- **makers** train models (google, nvidia);
- **converters** only repackage a maker's model for a runtime
  (mlx-community). A conversion must say what it was built from, and that
  must be a maker's;
- **blocked** publishers are refused, and so is anything built from a model
  of theirs, however it is packaged and whoever packaged it. Blocked wins
  over the other two.

`judge` is that rule and the only place it is written down. It fails closed:
what it cannot see in full it refuses, because "was this built from a blocked
publisher's model" can only be answered for a whole chain.

Pure: nothing here reads a clock, a file, the network or the database, and
this module imports only the standard library. The caller reads the chain
(preflight, from Hugging Face; the registry, from what was stored at install)
and hands it in.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

DEFAULT_MAKERS: tuple[str, ...] = ("google", "allenai", "nvidia", "apple")
DEFAULT_CONVERTERS: tuple[str, ...] = ("mlx-community",)

# The built-in blocked publishers: Hugging Face publisher names of the
# well-known Chinese labs (the owner's standing rule). This tuple is the only
# place those names are written in this package, its fixtures (demo_data) and
# its tests (tests/test_local_models*.py), and no model of theirs is named in
# any of them: a test that needs a blocked publisher uses DEFAULT_BLOCKED[0]
# with an invented model name. That much is enforced — a test reads every one
# of those files for each name. The rest of the repository is not read by it.
#
# It is a list of names, not a detector. A name that matches nothing blocks
# nothing, and the real gate is that an unfamiliar publisher is refused until
# somebody adds it to makers or converters. Names are compared without regard
# to case. The operator can add to this list on the page but cannot take a
# built-in name off it, so a later release that adds a lab reaches every
# install.
DEFAULT_BLOCKED: tuple[str, ...] = (
    # Seen on Hugging Face with models under exactly this name.
    "Qwen",
    "Alibaba-NLP",
    "alibaba-pai",
    "Wan-AI",
    "deepseek-ai",
    "zai-org",
    "OpenGVLab",
    "openbmb",
    "internlm",
    "baidu",
    "PaddlePaddle",
    "tencent",
    "TencentARC",
    "Tencent-Hunyuan",
    "ByteDance",
    "ByteDance-Seed",
    "moonshotai",
    "MiniMaxAI",
    "stepfun-ai",
    "01-ai",
    "BAAI",
    "baichuan-inc",
    "XiaomiMiMo",
    "rednote-hilab",
    "huawei-noah",
    "IDEA-CCNL",
    "Skywork",
    # Publisher pages that answered on 2026-11-12.
    "ali-vilab",
    "modelscope",
    "Tongyi-MAI",
    "FunAudioLLM",
    "bytedance-research",
    "Kwai-Kolors",
    "KlingTeam",
    "sensenova",
    "HiDream-ai",
    "infly",
    "ZhipuAI",
    "inclusionAI",
    "OpenMOSS",
    "fnlp",
    # Names their labs have left (no models there today, or the page is
    # gone and its old model names forward to the lab's new name). Kept
    # because an older model's page may still say it was built from one.
    "THUDM",
    "AIDC-AI",
    "KwaiVGI",
)

# One half of a model's name. Both halves become folder names on disk, so
# nothing is accepted that could be anything else: no slash, no leading dot.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")
# The most names one list may hold. Far more than anyone needs; it bounds
# what a request can make the server store and compare.
MAX_LIST = 200

_LONGEST_SHOWN = 200
_ADDRESS = re.compile(r"(?:https?://)?(?:www\.)?(?:huggingface\.co|hf\.co)/", re.IGNORECASE)
_BUILT_IN = frozenset(name.casefold() for name in DEFAULT_BLOCKED)

NOT_A_MODEL_NAME = (
    "That is not a model's name. Paste it as it appears on Hugging Face: the publisher, "
    "a slash, then the model, like mlx-community/gemma-4-e4b-it-4bit."
)
NOT_A_PUBLISHER = "That is not a Hugging Face publisher name."
COULD_NOT_READ = (
    "FrameFound could not read everything this model was built from, so it cannot be "
    "checked. Try again."
)

Status = Literal["blocked", "maker", "converter", "unknown"]


@dataclass(frozen=True)
class PublisherLists:
    """The three lists in force, every name casefolded.

    However one is built, the built-in blocked names are in it: there is no
    way to hold a set of lists that leaves them out.
    """

    makers: frozenset[str]
    converters: frozenset[str]
    blocked: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "makers", _folded(self.makers))
        object.__setattr__(self, "converters", _folded(self.converters))
        object.__setattr__(self, "blocked", _folded(self.blocked) | _BUILT_IN)

    def status(self, publisher: str | None) -> Status:
        """Which list a publisher is on. Blocked is asked first: it wins."""
        key = (publisher or "").strip().casefold()
        if not key:
            return "unknown"
        if key in self.blocked:
            return "blocked"
        if key in self.makers:
            return "maker"
        if key in self.converters:
            return "converter"
        return "unknown"


@dataclass(frozen=True)
class Node:
    """One model in the chain: the candidate, or something it says it was built from."""

    repo_id: str
    publisher: str | None
    # What THIS model says it was built from (a merge names several).
    bases: tuple[str, ...] = ()
    # False: Hugging Face has no such model now.
    found: bool = True


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    via: Literal["maker", "converter"] | None
    # One sentence for the operator, either way.
    reason: str
    # The publisher a refusal is about, when it is about one.
    offender: str | None = None


def clean_repo_id(text: str) -> str:
    """`publisher/name` from what was typed or pasted; ValueError(sentence) otherwise.

    A pasted huggingface.co address is reduced to its first two parts, so a
    link to a file or a branch of the model still names the model. The
    spelling is kept as given: Hugging Face tells case apart.
    """
    name = text.strip()
    address = _ADDRESS.match(name)
    if address:
        path = re.split(r"[?#]", name[address.end() :], maxsplit=1)[0]
        parts = path.split("/")
        if parts[0].casefold() in ("datasets", "spaces"):
            # Otherwise the answer would be "datasets is not one of your
            # approved publishers", which sends the operator the wrong way.
            raise ValueError(
                "That address is a dataset or a Space, not a model. Paste the address of "
                "the model's own page."
            )
        name = "/".join(parts[:2])
    name = name.rstrip("/")
    publisher, slash, model = name.partition("/")
    if not slash or not NAME.fullmatch(publisher) or not NAME.fullmatch(model):
        raise ValueError(NOT_A_MODEL_NAME)
    return name


def publisher_of(repo_id: str) -> str | None:
    """The part before the slash; None for a name that has no publisher part."""
    publisher, slash, model = repo_id.strip().partition("/")
    return publisher if slash and publisher and model else None


def clean_publisher(text: str) -> str:
    """One publisher's name for a list, spelled as given; ValueError(sentence) otherwise."""
    name = text.strip()
    address = _ADDRESS.match(name)
    if address:
        name = name[address.end() :]
    name = name.removeprefix("@").rstrip("/")
    if not NAME.fullmatch(name):
        raise ValueError(NOT_A_PUBLISHER)
    return name


def lists_of(makers: object, converters: object, blocked_added: object) -> PublisherLists:
    """The lists in force for what is stored: the built-in blocked names plus the operator's.

    Lenient on purpose, and never raises. This reads what an earlier save
    stored, on every check and every question put to a model, so it is given
    whatever the row holds: a row from a newer version after a rollback may
    hold a list in a shape this version does not know. A name that should
    not be there can only fail to match, and a value that is not a list of
    names is an empty list. That fails closed for the two lists that
    approve — nobody is a maker or a converter — and the built-in blocked
    names are in force whatever was stored. `check_lists` is where a save is
    refused.
    """
    return PublisherLists(
        makers=_folded(makers),
        converters=_folded(converters),
        blocked=_folded(blocked_added),
    )


def check_lists(
    makers: Sequence[str], converters: Sequence[str], blocked_added: Sequence[str]
) -> tuple[list[str], list[str], list[str]]:
    """What may be stored when the operator saves the lists; ValueError(sentence) otherwise.

    Each list comes back trimmed and without repeats (compared without
    regard to case, keeping the first spelling). `blocked_added` comes back
    without the built-in names: only the operator's additions are stored.
    """
    kept_makers = _tidy(makers)
    kept_converters = _tidy(converters)
    kept_blocked = [name for name in _tidy(blocked_added) if name.casefold() not in _BUILT_IN]

    blocked = {name.casefold() for name in kept_blocked}
    for name in (*kept_makers, *kept_converters):
        if name.casefold() in _BUILT_IN:
            raise ValueError(f"{name} is built in to the blocked list and cannot be approved.")
        if name.casefold() in blocked:
            raise ValueError(
                f"{name} is on the blocked list. Blocked always wins; take it off that list first."
            )
    made = {name.casefold() for name in kept_makers}
    for name in kept_converters:
        if name.casefold() in made:
            # The two are judged differently (a converter must name a
            # maker's model), so a publisher has to be one or the other.
            raise ValueError(f"{name} is already a maker.")
    return kept_makers, kept_converters, kept_blocked


def judge(chain: Sequence[Node], lists: PublisherLists, *, complete: bool = True) -> Verdict:
    """May this model be installed and run, under these lists?

    `chain[0]` is the candidate; the rest is every model read while following
    what it says it was built from, nearest first. `complete` is False when
    the caller could not follow all of it (the network failed part-way, or
    the chain was longer than it was willing to walk).

    In this order, first match decides:

    1. no publisher: refused;
    2. the publisher is blocked: refused;
    3. anything in the ancestry comes from a blocked publisher: refused, and
       this beats a maker entry. A model that is only *named* as a base
       counts, read or not: its publisher is in its name;
    4. the ancestry could not be read in full: refused;
    5. the publisher is a maker: allowed;
    6. the publisher is a converter: it must say what it was built from, and
       every model it names must be a maker's;
    7. anyone else: refused, naming the list they would have to be added to.
    """
    if not chain:
        return Verdict(False, None, "There is no model to check.")
    candidate = chain[0]
    publisher = _publisher(candidate)
    if publisher is None:
        return Verdict(
            False,
            None,
            f"{_shown(candidate.repo_id)} has no publisher in its name, so who published it "
            "cannot be checked. Use the full name, like mlx-community/gemma-4-e4b-it-4bit.",
        )
    for owner in _owners(candidate):
        if lists.status(owner) == "blocked":
            return Verdict(
                False,
                None,
                f"Refused. {_shown(owner)} is on the blocked list, and blocked always wins.",
                owner,
            )

    own = candidate.repo_id.casefold()
    for node in chain:
        named = [Node(base, publisher_of(base)) for base in node.bases]
        for ancestor in ([] if node is candidate else [node]) + named:
            if ancestor.repo_id.casefold() == own:
                continue  # a page may name itself; its publisher was asked above
            for owner in _owners(ancestor):
                if lists.status(owner) == "blocked":
                    return Verdict(
                        False,
                        None,
                        f"Refused. It was built from {_shown(ancestor.repo_id)}, which comes "
                        f"from {_shown(owner)} — a blocked publisher. A model built from a "
                        "blocked publisher's model is refused however it is packaged.",
                        owner,
                    )

    if not candidate.found:
        return Verdict(
            False,
            None,
            f"Hugging Face has no model called {_shown(candidate.repo_id)} now, so it cannot be "
            "checked.",
        )
    for node in chain[1:]:
        if not node.found:
            return Verdict(
                False,
                None,
                f"It says it was built from {_shown(node.repo_id)}, but Hugging Face has no "
                "such model now, so who made it cannot be checked.",
            )
    read = {node.repo_id.casefold(): node for node in chain}
    # Not taking the caller's word alone: a base that is named but was never
    # read is an unread chain, whatever `complete` says.
    if not complete or any(base.casefold() not in read for node in chain for base in node.bases):
        return Verdict(False, None, COULD_NOT_READ)

    status = lists.status(publisher)
    if status == "maker":
        return Verdict(True, "maker", f"{_shown(publisher)} is on your makers list.")
    if status == "converter":
        if not candidate.bases:
            return Verdict(
                False,
                None,
                f"This was packaged by {_shown(publisher)}, and its page does not say which "
                "model it was made from. Without that FrameFound cannot tell who made it, so "
                "it is refused.",
                publisher,
            )
        makers: list[str] = []
        for base in candidate.bases:
            maker = _publisher(read[base.casefold()])
            if maker is None:
                return Verdict(
                    False,
                    None,
                    f"It was built from {_shown(base)}, which has no publisher in its name, "
                    "so who made it cannot be checked.",
                )
            standing = lists.status(maker)
            if standing == "converter":
                # "Add them as a maker" would be the wrong advice about a
                # publisher the operator already listed as a repackager.
                return Verdict(
                    False,
                    None,
                    f"It was built from {_shown(base)}, which is itself a repackaged build "
                    f"(by {_shown(maker)}). FrameFound only accepts a build made straight from "
                    "a maker's own model.",
                    maker,
                )
            if standing != "maker":
                return Verdict(
                    False,
                    None,
                    f"It was built from {_shown(base)}, published by {_shown(maker)}, who is "
                    f"not on your makers list. Add {_shown(maker)} as a maker if you trust "
                    "them, then check again.",
                    maker,
                )
            if maker not in makers:
                makers.append(maker)
        return Verdict(
            True,
            "converter",
            f"Packaged by {_shown(publisher)} from {_and(candidate.bases)}. "
            f"{_and(makers)} {'is' if len(makers) == 1 else 'are'} on your makers list.",
        )
    return Verdict(
        False,
        None,
        f"{_shown(publisher)} is not one of your approved publishers. If you know who they "
        "are and want their models, add them under Approved publishers — as a maker if they "
        "trained it, as a converter if they only packaged it. Then check again.",
        publisher,
    )


def _folded(names: object) -> frozenset[str]:
    # Tolerant of what a stored row may hold: anything that is not a name
    # simply never matches, and anything that is not a list holds no names.
    # A bare string is not a list either: read letter by letter, "google"
    # would approve publishers called "g", "o", "l" and "e".
    if not isinstance(names, list | tuple | set | frozenset):
        return frozenset()
    return frozenset(
        name.strip().casefold() for name in names if isinstance(name, str) and name.strip()
    )


def _shown(text: str) -> str:
    """A name from somebody else's page, made fit to put in a sentence.

    What a model says it was built from is free text. Only what prints is
    kept, and no more of it than the longest real name (two halves of 96
    and a slash), so a sentence of FrameFound's own never carries a line
    break, a character that reverses the line, or a page of somebody's text.
    """
    return "".join(letter for letter in text if letter.isprintable())[:_LONGEST_SHOWN]


def _tidy(names: Sequence[str]) -> list[str]:
    if len(names) > MAX_LIST:
        raise ValueError(f"A list can hold at most {MAX_LIST} publishers.")
    kept: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean = clean_publisher(name)
        if clean.casefold() not in seen:
            seen.add(clean.casefold())
            kept.append(clean)
    return kept


def _publisher(node: Node) -> str | None:
    return node.publisher or publisher_of(node.repo_id)


def _owners(node: Node) -> tuple[str, ...]:
    """Every publisher a model can be laid at the door of.

    The publisher Hugging Face reports and the one in the model's name are
    the same thing on every page seen. If they ever differ, a model is
    blocked when either is.
    """
    named = publisher_of(node.repo_id)
    return tuple(dict.fromkeys(owner for owner in (node.publisher, named) if owner))


def _and(names: Sequence[str]) -> str:
    shown = [_shown(name) for name in names]
    if len(shown) <= 1:
        return "".join(shown)
    return f"{', '.join(shown[:-1])} and {shown[-1]}"
