"""The Check button (local_models/preflight.py) and the programs that run models
(local_models/kinds.py, and the demo's stand-in in fake.py).

Every model page here is a fixture from `local_models/demo_data` or built in
the test; nothing reaches a network (a fixture breaks every socket to prove
it) and no file is ever downloaded. Each refusal is asserted in the exact
words the owner will read.

No blocked publisher's model is named: a test that needs one invents a model
under `policy.DEFAULT_BLOCKED[0]`.
"""

import re
import socket
import uuid
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest

from framefound.config import Settings, get_settings
from framefound.local_models import fake, files, hub, kinds, policy, preflight
from framefound.local_models.hub import HubError
from framefound.local_models.kinds import Fit, MlxVlm, RuntimeKind, Support, Variant
from framefound.local_models.preflight import Check, Installed, Preflight
from framefound.local_models.settings_store import LocalModelsConfig

GB = 1024**3
FIRST = "mlx-community/gemma-4-e4b-it-4bit"
BUILD = "475b9088d29754a3379866cf5aeb6b41acd313c2"
TWELVE = "mlx-community/gemma-4-12B-it-OptiQ-4bit"
LARGE = "mlx-community/demo-large-4bit"
TOKEN = "not-a-real-token"
BLOCKED = policy.DEFAULT_BLOCKED[0]
SUM = "ab" * 32

NEEDS_CODE = (
    "This model only runs with program code from its own repository. FrameFound never runs "
    "code that comes with a model, so it has to wait for a build that does not need any."
)
ORIGINAL_BUILD = (
    "This is the maker's original build, not one packaged for the Mac. Look for the same "
    "model from an approved converter, such as mlx-community."
)
NAMES_MLX_VLM = "Its page says it runs on MLX-VLM."
DOES_NOT_NAME_IT = "Its page does not name MLX-VLM. It may still run — Test will tell."
CONVERTER_NOTE = (
    "Packaged by mlx-community, not by the maker. FrameFound can check what a build says it "
    "was made from, not that it is true — which is why converters are a list you control."
)
COULD_NOT_READ = (
    "FrameFound could not read everything this model was built from, so it cannot be "
    "checked. Try again."
)

# What a developer's or a CI machine's own environment might already say.
OURS = ("FRAMEFOUND_RUNTIMES_DIR", "FRAMEFOUND_LOCAL_AI_DEMO", "FRAMEFOUND_DOMAIN")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a test tried to open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture()
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in OURS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def page(
    repo_id: str,
    *,
    siblings: Sequence[tuple[str, int]] = (
        ("README.md", 400),
        ("config.json", 1500),
        ("model.safetensors", 3 * GB),
    ),
    base: str | Sequence[str] | None = None,
    tags: Sequence[str] = ("mlx", "safetensors", "mlx-vlm"),
    library: str = "mlx",
    pipeline: str = "image-text-to-text",
    licence: str | None = "mit",
    gated: Any = False,
    sha: str = "b" * 40,
    **more: Any,
) -> dict[str, Any]:
    """A model page as Hugging Face would answer it, for a model no fixture has."""
    card: dict[str, Any] = {}
    if base is not None:
        card["base_model"] = base
    if licence is not None:
        card["license"] = licence
    listed: list[dict[str, Any]] = []
    for path, size in siblings:
        entry: dict[str, Any] = {"rfilename": path, "size": size}
        if path.endswith((".safetensors", ".gguf", ".bin")):
            entry["lfs"] = {"sha256": SUM, "size": size}
        listed.append(entry)
    return {
        "id": repo_id,
        "author": repo_id.split("/")[0],
        "sha": sha,
        "private": False,
        "disabled": False,
        "gated": gated,
        "library_name": library,
        "pipeline_tag": pipeline,
        "tags": list(tags),
        "cardData": card,
        "config": {"model_type": "demo"},
        "siblings": listed,
        **more,
    }


def fixtures(*extra: dict[str, Any]) -> fake.FakeHub:
    """Every fixture page, plus the pages a test made up."""
    site = fake.FakeHub.from_fixtures()
    for payload in extra:
        site.payloads[payload["id"]] = payload
    return site


def check(
    site: hub.Hub,
    name: str = FIRST,
    *,
    config: LocalModelsConfig | None = None,
    kinds: Mapping[str, RuntimeKind] | None = None,
    installed: Mapping[str, str | None] | None = None,
    free_bytes: int | None = 812 * GB,
    **more: Any,
) -> Preflight:
    """The check as the Mac would run it: MLX-VLM 0.7.12 installed, plenty of room."""
    return preflight.preflight(
        site,
        name,
        config=config or LocalModelsConfig(),
        kinds={"mlx-vlm": MlxVlm()} if kinds is None else kinds,
        installed={"mlx-vlm": "0.7.12"} if installed is None else installed,
        free_bytes=free_bytes,
        min_free_bytes=5 * GB,
        **more,
    )


def row(found: Preflight, key: str) -> Check:
    return next(item for item in found.checks if item.key == key)


def info_of(name: str) -> hub.RepoInfo:
    return hub.parse_info(fake.fixture(name))


def plan_of(info: hub.RepoInfo) -> files.FilePlan:
    return files.plan_files(info.files)


# --- the programs that run models ------------------------------------------------


def test_mlx_models_need_a_mac_with_apple_silicon() -> None:
    kind = MlxVlm()

    assert kind.supported_here("Darwin", "arm64") is None
    for system, machine in (("Darwin", "x86_64"), ("Linux", "arm64"), ("Windows", "AMD64")):
        assert kind.supported_here(system, machine) == "MLX models need a Mac with Apple silicon."


def test_what_the_first_kind_says_about_itself() -> None:
    kind = MlxVlm()

    # Stored on every model's row and in every manifest: it cannot change.
    assert (kind.name, kind.label, kind.demo) == ("mlx-vlm", "MLX-VLM", False)
    assert kind.weights == (".safetensors",)
    assert kind.needs_uv is True
    assert kind.needs_compare is False
    assert kind.summary


@pytest.mark.parametrize(
    ("name", "ok", "note", "takes"),
    [
        ("gemma-4-e4b-it-4bit", True, NAMES_MLX_VLM, ("text", "image")),
        # Packaged for the Mac, but its page files it under plain text and
        # does not name the program: it may run, and the operator is told so.
        ("gemma-4-12B-it-OptiQ-4bit", True, DOES_NOT_NAME_IT, ("text",)),
        ("gemma-4-E4B-it", False, ORIGINAL_BUILD, ("text", "image", "audio")),
        ("needs-code", False, NEEDS_CODE, ("text", "image")),
    ],
)
def test_a_repository_is_judged_from_its_page_and_its_files(
    name: str, ok: bool, note: str, takes: tuple[str, ...]
) -> None:
    info = info_of(name)
    plan = plan_of(info)

    support = MlxVlm().supports(info, plan)

    assert (support.ok, support.note, support.takes) == (ok, note, takes)
    # Only "it may run, Test will tell" is a caveat to acknowledge.
    assert support.caveat is (note == DOES_NOT_NAME_IT)
    # An MLX repository is one build: everything the plan keeps.
    (build,) = support.variants
    assert (build.id, build.files) == ("", plan.keep)
    assert build.weights_bytes == plan.weights_bytes
    assert MlxVlm().prefer([(build, "day")]) == ""


def test_a_task_the_page_does_not_name_reads_as_text_only() -> None:
    info = hub.parse_info(page("mlx-community/unfiled-4bit", pipeline=""))

    assert MlxVlm().supports(info, plan_of(info)).takes == ("text",)


def test_a_page_tagged_mlx_counts_as_packaged_for_the_mac_whatever_library_it_names() -> None:
    tagged = hub.parse_info(page("mlx-community/tagged-only", library="transformers"))
    untagged = hub.parse_info(
        page("mlx-community/not-packaged", library="transformers", tags=("safetensors",))
    )

    assert MlxVlm().supports(tagged, plan_of(tagged)).ok
    assert MlxVlm().supports(untagged, plan_of(untagged)).note == ORIGINAL_BUILD


def test_a_gguf_build_is_for_a_program_framefound_cannot_run_yet() -> None:
    info = hub.parse_info(
        page(
            "mlx-community/demo-gguf",
            siblings=(("README.md", 400), ("demo-Q4_K_M.gguf", 4 * GB)),
            tags=("gguf",),
            library="gguf",
        )
    )

    support = MlxVlm().supports(info, plan_of(info))

    assert not support.ok
    assert support.note == "This build is for llama.cpp, which FrameFound cannot run yet."
    # Nothing of it is a build this program would download.
    assert support.variants == ()


def test_a_repository_with_no_weights_at_all_cannot_be_run() -> None:
    info = hub.parse_info(page("mlx-community/only-a-readme", siblings=(("README.md", 400),)))

    support = MlxVlm().supports(info, plan_of(info))

    assert not support.ok
    assert support.note == "It has no safetensors weights, which is the only kind MLX-VLM runs."


def test_gguf_files_beside_an_mlx_build_are_not_part_of_it() -> None:
    info = hub.parse_info(
        page(
            "mlx-community/both-formats",
            siblings=(
                ("config.json", 1500),
                ("model.safetensors", 3 * GB),
                ("extra-Q8_0.gguf", 9 * GB),
            ),
        )
    )
    plan = plan_of(info)
    assert plan.total_bytes > 12 * GB

    support = MlxVlm().supports(info, plan)

    (build,) = support.variants
    assert [file.path for file in build.files] == ["config.json", "model.safetensors"]
    assert build.weights_bytes == 3 * GB


def test_the_newest_version_is_read_from_the_package_index() -> None:
    asked: list[str] = []

    def index(url: str) -> Mapping[str, Any]:
        asked.append(url)
        return {"info": {"version": " 0.7.12 ", "requires_python": ">=3.10"}}

    assert MlxVlm().latest(index) == "0.7.12"
    assert asked == ["https://pypi.org/pypi/mlx-vlm/json"]


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"info": None},
        {"info": {"version": 7}},
        {"info": {"version": ""}},
        # What is read here is later asked for by name when the program is
        # installed, so it is only ever a version.
        {"info": {"version": "--index-url=http://elsewhere.example"}},
        {"info": {"version": "0.7.12 ; something else"}},
    ],
)
def test_an_index_answer_that_is_not_a_version_is_refused(answer: Mapping[str, Any]) -> None:
    with pytest.raises(HubError) as caught:
        MlxVlm().latest(lambda _url: answer)

    assert caught.value.problem == "bad_answer"
    assert str(caught.value) == (
        "The package index answered in a form FrameFound could not read, so the newest "
        "version of MLX-VLM is not known."
    )


class Stub:
    """A kind that says one fixed thing, to see which one `choose` listens to."""

    summary = "A stand-in for a test."
    demo = False
    needs_uv = False
    needs_compare = False

    def __init__(self, name: str, weights: tuple[str, ...], ok: bool) -> None:
        self.name = name
        self.label = name.upper()
        self.weights = weights
        self.ok = ok
        self.asked = 0

    def supported_here(self, system: str, machine: str) -> str | None:
        return None

    def supports(self, info: hub.RepoInfo, plan: files.FilePlan) -> Support:
        self.asked += 1
        return Support(self.ok, f"{self.name} says {'yes' if self.ok else 'no'}.", ("text",), ())

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        return sized[0][0].id

    def latest(self, fetch: hub.FetchJson) -> str:
        return "1"


def test_the_first_kind_that_can_run_a_repository_is_chosen() -> None:
    info = info_of("gemma-4-e4b-it-4bit")
    plan = plan_of(info)
    no = Stub("first", (".safetensors",), ok=False)
    yes = Stub("second", (".gguf",), ok=True)
    also = Stub("third", (".safetensors",), ok=True)

    kind, support = kinds.choose(info, plan, {"first": no, "second": yes, "third": also})

    assert kind is yes
    assert support.note == "second says yes."
    # In the order given, and no further than the first that says yes.
    assert (no.asked, yes.asked, also.asked) == (1, 1, 0)


def test_when_nothing_can_run_it_the_program_for_its_format_gives_the_reason() -> None:
    info = info_of("gemma-4-e4b-it-4bit")  # safetensors
    plan = plan_of(info)
    other = Stub("for-gguf", (".gguf",), ok=False)
    matching = Stub("for-safetensors", (".safetensors",), ok=False)

    kind, support = kinds.choose(info, plan, {"for-gguf": other, "for-safetensors": matching})
    assert kind is None
    assert support.note == "for-safetensors says no."

    # No program for its format: the first one's reason.
    kind, support = kinds.choose(info, plan, {"for-gguf": other})
    assert (kind, support.note) == (None, "for-gguf says no.")

    # No program at all (an install that cannot run models).
    kind, support = kinds.choose(info, plan, {})
    assert kind is None
    assert not support.ok
    assert support.note == (
        "This install has no software that runs models, so nothing here can run it."
    )


@pytest.mark.usefixtures("_clean_environment")
def test_a_real_install_has_the_real_kind_and_a_container_has_none(tmp_path: Path) -> None:
    on_the_mac = kinds.kinds(Settings(runtimes_dir=tmp_path))
    # MLX-VLM first: a repository either could run goes to it (L6 3.3).
    assert list(on_the_mac) == ["mlx-vlm", "llama-cpp"]
    assert isinstance(on_the_mac["mlx-vlm"], MlxVlm)
    assert isinstance(on_the_mac["llama-cpp"], kinds.LlamaCpp)

    assert kinds.kinds(Settings()) == {}


@pytest.mark.usefixtures("_clean_environment")
def test_the_stand_in_kind_exists_in_the_demo_and_nowhere_else(tmp_path: Path) -> None:
    in_the_demo = kinds.kinds(Settings(local_ai_demo=True))
    assert list(in_the_demo) == ["demo", "demo-gguf"]
    assert isinstance(in_the_demo["demo"], fake.DemoKind)
    assert isinstance(in_the_demo["demo-gguf"], fake.DemoGgufKind)

    # One stray environment variable must not make a real install show
    # invented models: a runtimes folder or a public address wins.
    real = kinds.kinds(Settings(local_ai_demo=True, runtimes_dir=tmp_path))
    assert list(real) == ["mlx-vlm", "llama-cpp"]
    assert kinds.kinds(Settings(local_ai_demo=True, domain="media.example.com")) == {}
    assert not {"demo", "demo-gguf"} & set(kinds.kinds(Settings()))


def test_the_demo_kind_judges_by_the_real_rules_and_asks_nobody() -> None:
    demo = fake.DemoKind()

    assert (demo.name, demo.demo) == ("demo", True)
    assert demo.label == "Demo runtime (not a real model)"
    assert demo.weights == MlxVlm.weights
    # Nothing is installed in the demo, so no installer is asked for.
    assert demo.needs_uv is False
    assert demo.supported_here("Windows", "AMD64") is None

    for name in fake.fixture_names():
        info = info_of(name)
        plan = plan_of(info)
        assert demo.supports(info, plan) == MlxVlm().supports(info, plan)

    def never(_url: str) -> Mapping[str, Any]:
        raise AssertionError("the demo asked the network for a version")

    assert demo.latest(never) == "2.0"


# --- following what a model was built from ------------------------------------------


def test_the_first_models_chain_is_three_links_long() -> None:
    site = fixtures()

    chain, complete = preflight.walk(site, site.info(FIRST))

    assert complete
    assert chain == (
        policy.Node(FIRST, "mlx-community", ("google/gemma-4-E4B-it",)),
        # A maker says what it built on too; the walk does not stop at the
        # first approved name.
        policy.Node("google/gemma-4-E4B-it", "google", ("google/gemma-4-E4B",)),
        policy.Node("google/gemma-4-E4B", "google", ()),
    )
    assert site.fetches == 0


def test_a_card_that_names_itself_or_goes_in_a_circle_is_asked_about_once() -> None:
    site = fixtures(
        page("google/loop-a", base=["google/loop-a", "google/loop-b"]),
        page("google/loop-b", base=["GOOGLE/LOOP-A", "google/loop-b"]),
    )

    chain, complete = preflight.walk(site, site.info("google/loop-a"))

    assert complete
    assert [node.repo_id for node in chain] == ["google/loop-a", "google/loop-b"]
    asked = [call[1] for call in site.calls if call[0] == "info"]
    assert asked == ["google/loop-a", "google/loop-b"]


def test_a_base_hugging_face_no_longer_has_is_in_the_chain_marked_missing() -> None:
    site = fixtures(page("mlx-community/orphaned-4bit", base="google/withdrawn"))

    chain, complete = preflight.walk(site, site.info("mlx-community/orphaned-4bit"))

    assert not complete
    assert chain[1] == policy.Node("google/withdrawn", "google", (), found=False)


def links(count: int) -> list[dict[str, Any]]:
    """`google/link-0` built from `google/link-1` built from … `google/link-<count - 1>`."""
    return [
        page(f"google/link-{n}", base=f"google/link-{n + 1}" if n + 1 < count else None)
        for n in range(count)
    ]


def test_five_steps_back_are_followed_and_a_sixth_is_not() -> None:
    assert preflight.MAX_DEPTH == 5
    whole = fixtures(*links(6))  # the candidate and five ancestors

    chain, complete = preflight.walk(whole, whole.info("google/link-0"))

    assert complete
    assert len(chain) == 6

    longer = fixtures(*links(7))
    chain, complete = preflight.walk(longer, longer.info("google/link-0"))

    assert not complete
    assert [node.repo_id for node in chain][-1] == "google/link-5"
    assert ("info", "google/link-6", None) not in longer.calls
    # A chain that was not read to its end is refused, and says so: a maker's
    # own model included, because the blocked rule is about the whole chain.
    found = check(longer, "google/link-0")
    assert found.verdict.reason == COULD_NOT_READ
    assert COULD_NOT_READ in found.problems
    built_from = row(found, "built_from")
    assert built_from.tone == "bad"
    assert built_from.detail is not None
    assert built_from.detail.endswith(
        "FrameFound follows what a model was built from for 5 steps and 12 models, and "
        "this goes further."
    )


def test_an_unread_chain_shows_on_its_own_row_even_when_the_publisher_is_refused_too() -> None:
    site = fixtures(page("example-lab/long-history", base="google/link-0"), *links(6))

    found = check(site, "example-lab/long-history")

    assert found.problems == (COULD_NOT_READ,)
    assert row(found, "publisher").tone == "bad"
    assert row(found, "built_from").tone == "bad"


def test_twelve_models_are_asked_about_and_a_thirteenth_is_not() -> None:
    assert preflight.MAX_ANCESTORS == 12

    def merged(count: int) -> fake.FakeHub:
        parts = [page(f"google/part-{n}") for n in range(count)]
        return fixtures(page("google/merge", base=[part["id"] for part in parts]), *parts)

    twelve = merged(12)
    chain, complete = preflight.walk(twelve, twelve.info("google/merge"))
    assert complete
    assert len(chain) == 13

    thirteen = merged(13)
    chain, complete = preflight.walk(thirteen, thirteen.info("google/merge"))
    assert not complete
    assert len(chain) == 13
    assert ("info", "google/part-12", None) not in thirteen.calls
    assert check(thirteen, "google/merge").verdict.reason == COULD_NOT_READ


@pytest.mark.parametrize("problem", ["unreachable", "rate_limited", "bad_answer"])
def test_the_network_failing_part_way_leaves_the_chain_unread(problem: hub.Problem) -> None:
    site = fixtures(page("google/second-base"))
    site.payloads[FIRST]["cardData"]["base_model"] = ["google/gemma-4-E4B-it", "google/second-base"]
    site.fail_info("google/gemma-4-E4B-it", problem)

    chain, complete = preflight.walk(site, site.info(FIRST))

    assert not complete
    assert [node.repo_id for node in chain] == [FIRST]
    # Nothing more is asked once Hugging Face stops answering.
    assert ("info", "google/second-base", None) not in site.calls

    found = check(site)
    assert not found.ok
    assert found.problems == (COULD_NOT_READ,)
    assert row(found, "built_from").tone == "bad"
    assert row(found, "built_from").value == (
        "google/gemma-4-E4B-it — could not be read; google/second-base — could not be read"
    )


def test_a_base_whose_name_now_points_elsewhere_is_not_judged_as_what_it_points_to() -> None:
    site = fixtures(page("mlx-community/renamed-base-4bit", base="google/old-gemma"))
    # Asked for by its old name, Hugging Face answers with the model it became.
    site.payloads["google/old-gemma"] = fake.fixture("gemma-4-E4B")

    found = check(site, "mlx-community/renamed-base-4bit")

    assert not found.complete
    assert found.chain[1] == policy.Node(
        "google/old-gemma", "google", ("google/gemma-4-E4B",), found=False
    )
    assert found.problems == (
        "It says it was built from google/old-gemma, but Hugging Face has no such model now, "
        "so who made it cannot be checked.",
    )
    built_from = row(found, "built_from")
    assert built_from.tone == "bad"
    assert built_from.value == "google/old-gemma now points to google/gemma-4-E4B on Hugging Face"


def test_a_base_that_now_points_to_a_blocked_publisher_is_refused_as_that() -> None:
    site = fixtures(page("mlx-community/forwarded-4bit", base="google/moved-away"))
    site.payloads["google/moved-away"] = page(f"{BLOCKED}/demo-base", library="transformers")

    found = check(site, "mlx-community/forwarded-4bit")

    assert found.problems == (
        f"Refused. It was built from {BLOCKED}/demo-base, which comes from {BLOCKED} — a "
        "blocked publisher. A model built from a blocked publisher's model is refused "
        "however it is packaged.",
    )
    assert found.verdict.offender == BLOCKED


def test_a_base_the_real_client_says_has_moved_is_marked_the_same_way() -> None:
    class Forwarding(fake.FakeHub):
        def info(self, repo_id: str, revision: str | None = None) -> hub.RepoInfo:
            if repo_id == "google/old-gemma":
                raise HubError("moved", hub.MOVED, 307, moved_to="google/gemma-4-E4B")
            return super().info(repo_id, revision)

    site = Forwarding({"mlx-community/renamed-4bit": page("mlx-community/renamed-4bit")})
    site.payloads["mlx-community/renamed-4bit"]["cardData"]["base_model"] = "google/old-gemma"

    chain, complete = preflight.walk(site, site.info("mlx-community/renamed-4bit"))

    assert not complete
    assert chain[1] == policy.Node(
        "google/old-gemma", "google", ("google/gemma-4-E4B",), found=False
    )


def test_a_base_written_in_other_capitals_is_the_same_model_however_the_site_answers() -> None:
    # Whoever converted a model typed its base by hand, and the operator
    # cannot correct somebody else's page. Whether Hugging Face answers such
    # a name outright or forwards it to its own spelling has not been seen:
    # the check has to come out the same either way.
    def site_for(forwarding: bool) -> fake.FakeHub:
        site = fixtures()
        site.payloads[FIRST]["cardData"]["base_model"] = "GOOGLE/Gemma-4-e4b-IT"
        site.payloads[FIRST]["tags"] = [
            tag for tag in site.payloads[FIRST]["tags"] if not tag.startswith("base_model:")
        ]
        if forwarding:
            site.forward_capitals()
        return site

    outright, forwarded = check(site_for(False)), check(site_for(True))

    for found in (outright, forwarded):
        assert found.ok and found.complete
        assert [node.repo_id for node in found.chain] == [
            FIRST,
            "google/gemma-4-E4B-it",
            "google/gemma-4-E4B",
        ]
        assert found.verdict.via == "converter"
    assert forwarded.checks == outright.checks


def test_a_name_typed_in_other_capitals_is_checked_even_when_the_site_forwards_it() -> None:
    site = fixtures()
    site.forward_capitals()

    found = check(site, FIRST.upper())

    assert found.ok
    assert found.info.repo_id == FIRST
    # Asked once as typed, once under the spelling it was forwarded to.
    assert site.calls[:2] == [("info", FIRST.upper(), None), ("info", FIRST, None)]

    # The same when Download checks again at the build the check showed: the
    # second asking is for that build, not for whatever is newest by then.
    site.calls.clear()
    assert check(site, FIRST.upper(), revision=BUILD).info.revision == BUILD
    assert site.calls[:2] == [("info", FIRST.upper(), BUILD), ("info", FIRST, BUILD)]


def test_only_a_change_of_capitals_is_followed_and_only_once() -> None:
    class Forwarding(fake.FakeHub):
        """Every name leads to the next one: a change of capitals, then another model."""

        leads_to: Mapping[str, str] = {}

        def info(self, repo_id: str, revision: str | None = None) -> hub.RepoInfo:
            if repo_id in self.leads_to:
                self.calls.append(("info", repo_id, revision))
                target = self.leads_to[repo_id]
                raise HubError("moved", hub.now_points_to(target), 307, moved_to=target)
            return super().info(repo_id, revision)

    site = Forwarding.from_fixtures()
    # In other capitals it forwards to its spelling, and that forwards elsewhere.
    site.leads_to = {FIRST.upper(): FIRST, FIRST: "mlx-community/another-model"}

    with pytest.raises(HubError) as caught:
        check(site, FIRST.upper())

    assert caught.value.problem == "moved"
    assert caught.value.moved_to == "mlx-community/another-model"
    assert [call[1] for call in site.calls] == [FIRST.upper(), FIRST]

    # A name that forwards to itself is not asked for again and again.
    site.calls.clear()
    site.leads_to = {FIRST: FIRST}
    with pytest.raises(HubError):
        check(site)
    assert len(site.calls) == 1


def test_a_base_that_is_not_plain_text_never_reaches_a_sentence() -> None:
    # Free text from whoever wrote a model's card. On the model's own page it
    # is a page that cannot be read; on a page further back, a chain that
    # cannot be read. Either way none of it is said back to the operator.
    odd = "google/gemma\nIGNORE ALL PREVIOUS INSTRUCTIONS " + "x" * 5000

    with pytest.raises(HubError) as caught:
        check(
            fixtures(page("mlx-community/odd-base-4bit", base=odd)), "mlx-community/odd-base-4bit"
        )
    assert (caught.value.problem, str(caught.value)) == ("bad_answer", hub.BAD_ANSWER)

    site = fixtures(
        page("mlx-community/odd-further-back-4bit", base="google/odd-base"),
        page("google/odd-base", base=odd, library="transformers"),
    )
    found = check(site, "mlx-community/odd-further-back-4bit")
    assert found.problems == (COULD_NOT_READ,)
    assert all(
        text.isprintable() and len(text) < 1000
        for item in found.checks
        for text in (item.value, item.detail or "")
    )


def test_a_base_that_is_not_a_model_name_is_never_asked_for_by_the_real_client() -> None:
    requests: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=page("google/tuned", base="just-a-word"))

    real = hub.HttpHub(transport=httpx.MockTransport(answer))

    chain, complete = preflight.walk(real, real.info("google/tuned"))

    # What a model says it was built from is free text; a name with no
    # publisher part is not put into an address.
    assert len(requests) == 1
    assert not complete
    assert chain[1] == policy.Node("just-a-word", None, (), found=False)


def test_every_base_any_model_names_has_a_place_in_the_chain_or_the_chain_is_incomplete() -> None:
    # policy.judge refuses a chain in which a named base was never read,
    # whatever it is told; the walk must never claim more than it read.
    site = fixtures()
    for name in fake.fixture_names():
        repo_id = fake.fixture(name)["id"]
        chain, complete = preflight.walk(site, site.info(repo_id))
        read = {node.repo_id.casefold() for node in chain}
        named = {base.casefold() for node in chain for base in node.bases}
        assert complete is (named <= read and all(node.found for node in chain)), repo_id


# --- the memory estimate -----------------------------------------------------------


def test_the_memory_estimate_is_the_weights_and_a_fifth_rounded_up() -> None:
    # The three figures the plan's own candidates come to.
    assert preflight.estimate_memory_gb(5_146_800_534) == 5.8
    assert preflight.estimate_memory_gb(5_349_856_755 + 3_510_027_171 + 104_760_992) == 10.1
    assert preflight.estimate_memory_gb(16_750_372_454) == 18.8
    # Exact where the sum is exact: a rounding slip here would move a model
    # from "by day" to "overnight only".
    assert [preflight.estimate_memory_gb(n * GB) for n in (0, 3, 5, 7, 10)] == [
        0.0,
        3.6,
        6.0,
        8.4,
        12.0,
    ]
    assert preflight.estimate_memory_gb(5 * GB + 1) == 6.1


# --- the check, on every fixture ------------------------------------------------------


def test_the_first_model_can_be_added_and_every_row_says_why() -> None:
    site = fixtures()

    found = check(site)

    assert found.ok
    assert found.summary == "This model can be added."
    assert (found.problems, found.warnings, found.notes) == ((), (), ())
    assert found.checks == (
        Check(
            "publisher",
            "Published by",
            "ok",
            "mlx-community — an approved converter",
            CONVERTER_NOTE,
        ),
        Check(
            "built_from",
            "Built from",
            "ok",
            "google/gemma-4-E4B-it, by google — an approved maker",
            "…which google built from google/gemma-4-E4B.",
        ),
        Check(
            "licence",
            "Licence",
            "ok",
            "gemma",
            "As stated on this build's own page. The maker's page may state a different one.",
        ),
        Check("access", "Access", "ok", "Open — no account needed"),
        Check(
            "files",
            "Files",
            "ok",
            "9 files to download, 1 left out",
            "Weights are safetensors. Left out: .gitattributes.",
        ),
        Check("size", "Size", "ok", "4.8 GB", "The model library has 812.0 GB free."),
        Check(
            "memory", "Memory", "ok", "About 5.8 GB — fits by day", "The daytime budget is 8 GB."
        ),
        Check("runtime", "Runs on", "ok", "MLX-VLM 0.7.12", NAMES_MLX_VLM),
    )
    # The build that was shown is the build that will be downloaded.
    assert (found.info.repo_id, found.info.revision) == (FIRST, BUILD)
    assert found.verdict.allowed
    assert found.verdict.via == "converter"
    assert found.complete
    assert len(found.chain) == 3
    assert (found.runtime, found.runtime_installed) == ("mlx-vlm", True)
    assert found.variant is not None
    assert found.variant.files == found.plan.keep
    # The size is the files kept, not the repository's history.
    assert (found.size_bytes, found.memory_gb, found.fits) == (5_179_239_942, 5.8, "day")
    assert (found.access, found.takes) == ("open", ("text", "image"))
    assert (found.existing, found.same_build) == (None, False)
    assert found.free_bytes == 812 * GB


EXPECTED: dict[str, tuple[str, ...]] = {
    "gemma-4-e4b-it-4bit": (),
    "gemma-4-12B-it-OptiQ-4bit": (),
    "demo-large-4bit": (),
    # A maker's own page: approved, and not a build this Mac can run.
    "gemma-4-E4B-it": (ORIGINAL_BUILD,),
    "gemma-4-E4B": (ORIGINAL_BUILD,),
    "gemma-4-12B-it": (ORIGINAL_BUILD,),
    "demo-large": (ORIGINAL_BUILD,),
    "needs-code": (NEEDS_CODE,),
    "model-file": (NEEDS_CODE,),
    "gated-blocked-base": (
        f"Refused. It was built from {BLOCKED}/demo-base, which comes from {BLOCKED} — a "
        "blocked publisher. A model built from a blocked publisher's model is refused "
        "however it is packaged.",
        ORIGINAL_BUILD,
    ),
    "unknown-publisher": (
        "example-lab is not one of your approved publishers. If you know who they are and "
        "want their models, add them under Approved publishers — as a maker if they trained "
        "it, as a converter if they only packaged it. Then check again.",
    ),
    "no-base-conversion": (
        "This was packaged by mlx-community, and its page does not say which model it was "
        "made from. Without that FrameFound cannot tell who made it, so it is refused.",
    ),
}


def test_every_fixture_has_an_expected_answer() -> None:
    assert sorted(EXPECTED) == fake.fixture_names()


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_fixture_is_allowed_or_refused_in_these_exact_words(name: str) -> None:
    site = fixtures()

    found = check(site, fake.fixture(name)["id"])

    assert found.problems == EXPECTED[name]
    assert found.ok is (EXPECTED[name] == ())
    assert found.summary == (EXPECTED[name][0] if EXPECTED[name] else "This model can be added.")
    assert [item.key for item in found.checks] == [
        "publisher",
        "built_from",
        "licence",
        "access",
        "files",
        "size",
        "memory",
        "runtime",
    ]
    # A refusal always shows on a row, and an allowed model has no bad row.
    assert any(item.tone == "bad" for item in found.checks) is (not found.ok)
    # A refused model still has its later rows filled in.
    assert all(item.value for item in found.checks)
    assert site.fetches == 0


def test_a_check_never_downloads_a_file() -> None:
    site = fixtures()
    site.need_token("nvidia/demo-gated")

    for name in fake.fixture_names():
        check(site, fake.fixture(name)["id"])

    assert site.fetches == 0
    # What it does ask for: pages, and the small settings files a runtime reads.
    assert {call[0] for call in site.calls} == {"info", "read_text"}
    read = {call[3] for call in site.calls if call[0] == "read_text"}
    assert read <= set(files.INSPECTED)


def test_no_fixture_that_passes_the_check_has_a_blocked_publisher_anywhere_in_it() -> None:
    # The owner's rule, on the models the demo and the tests show as allowed.
    site = fixtures()
    lists = LocalModelsConfig().lists()
    allowed = 0
    for name in fake.fixture_names():
        found = check(site, fake.fixture(name)["id"])
        if not found.ok:
            continue
        allowed += 1
        everyone = {node.publisher for node in found.chain}
        everyone |= {policy.publisher_of(node.repo_id) for node in found.chain}
        everyone |= {policy.publisher_of(base) for node in found.chain for base in node.bases}
        assert all(lists.status(publisher) != "blocked" for publisher in everyone), name
        assert found.complete
    assert allowed == 3


def test_no_blocked_publisher_is_written_anywhere_but_the_blocked_list() -> None:
    # The owner's rule does not depend on which file a name lands in. Read
    # here: every module of the package (and anything a later step puts in a
    # folder under it), every fixture, and every test file of local models,
    # whichever step wrote it. Not read: the rest of the repository.
    package = Path(preflight.__file__).parent
    blocked_list = package / "policy.py"
    modules = [path for path in package.rglob("*.py") if path != blocked_list]
    fixtures_ = sorted(package.rglob("*.json"))
    tests = sorted(Path(__file__).parent.glob("test_local_models*.py"))
    assert len(modules) >= 8 and len(fixtures_) >= 12 and len(tests) >= 6
    assert Path(__file__) in tests

    for path in (*modules, *fixtures_, *tests):
        name = blocked_name_in(path.read_text(encoding="utf-8"))
        assert name is None, f"{path.name} names the blocked publisher {name}"

    # And the reading itself catches a name however it is written in: as a
    # model's publisher, an author, a tag, in any capitals. A longer word
    # that only contains the letters is not it.
    one = BLOCKED
    for text in (f'"{one}/demo-base"', f'"author": "{one.upper()}"', f"base_model:{one}/demo"):
        assert blocked_name_in(text) == one, text
    for text in (f"{one}s", f"un{one}", f"{one}-like", f"{one}_x", "google/gemma"):
        assert blocked_name_in(text) is None, text


def blocked_name_in(text: str) -> str | None:
    """The first built-in blocked publisher written out in `text`, as a whole name."""
    folded = text.casefold()
    for name in policy.DEFAULT_BLOCKED:
        if re.search(rf"(?<![a-z0-9_.-]){re.escape(name.casefold())}(?![a-z0-9_.-])", folded):
            return name
    return None


# --- who published it, and what it was built from ------------------------------------


def test_blocking_a_publisher_refuses_a_model_that_was_allowed() -> None:
    site = fixtures()
    config = LocalModelsConfig(makers=["nvidia"], blocked_added=["google"])

    found = check(site, config=config)

    assert found.problems == (
        "Refused. It was built from google/gemma-4-E4B-it, which comes from google — a "
        "blocked publisher. A model built from a blocked publisher's model is refused "
        "however it is packaged.",
    )
    assert row(found, "publisher").tone == "ok"
    built_from = row(found, "built_from")
    assert built_from.tone == "bad"
    assert built_from.value == "google/gemma-4-E4B-it, by google — a blocked publisher"


def test_a_blocked_publishers_own_model_is_refused_on_its_first_row() -> None:
    site = fixtures(page(f"{BLOCKED}/demo-model"))

    found = check(site, f"{BLOCKED}/demo-model")

    assert found.problems == (
        f"Refused. {BLOCKED} is on the blocked list, and blocked always wins.",
    )
    assert row(found, "publisher") == Check(
        "publisher", "Published by", "bad", f"{BLOCKED} — on the blocked list"
    )


def test_a_model_is_blocked_when_either_name_it_is_published_under_is() -> None:
    # The publisher Hugging Face reports and the one in the model's name are
    # the same on every page seen. Should they ever differ, neither may be
    # used to walk a blocked publisher's model past the rule.
    site = fixtures(
        page(f"{BLOCKED}/demo-reported-as-a-maker", author="google"),
        page("google/demo-reported-as-blocked", author=BLOCKED),
        page("mlx-community/demo-built-on-it", base="google/demo-reported-as-blocked"),
    )

    for name in (f"{BLOCKED}/demo-reported-as-a-maker", "google/demo-reported-as-blocked"):
        found = check(site, name)
        assert found.problems == (
            f"Refused. {BLOCKED} is on the blocked list, and blocked always wins.",
        )
        assert row(found, "publisher").value == f"{BLOCKED} — on the blocked list"

    built_on_it = check(site, "mlx-community/demo-built-on-it")
    assert not built_on_it.ok
    assert built_on_it.verdict.offender == BLOCKED
    assert row(built_on_it, "built_from").value == (
        f"google/demo-reported-as-blocked, by {BLOCKED} — a blocked publisher"
    )


def test_blocked_far_up_the_chain_is_named_under_built_from() -> None:
    site = fixtures(
        page("mlx-community/deep-4bit", base="google/tuned"),
        page("google/tuned", base="nvidia/middle", library="transformers"),
        page("nvidia/middle", base=f"{BLOCKED}/demo-base", library="transformers"),
    )

    found = check(site, "mlx-community/deep-4bit")

    assert found.problems == (
        f"Refused. It was built from {BLOCKED}/demo-base, which comes from {BLOCKED} — a "
        "blocked publisher. A model built from a blocked publisher's model is refused "
        "however it is packaged.",
    )
    built_from = row(found, "built_from")
    assert built_from.tone == "bad"
    assert built_from.value == "google/tuned, by google — an approved maker"
    assert built_from.detail == (
        "…which google built from nvidia/middle. "
        f"nvidia built nvidia/middle from {BLOCKED}/demo-base. "
        f"{BLOCKED}/demo-base comes from {BLOCKED}, a blocked publisher."
    )


def test_a_model_gone_from_further_up_the_chain_is_named_too() -> None:
    site = fixtures()
    del site.payloads["google/gemma-4-E4B"]

    found = check(site)

    assert found.problems == (
        "It says it was built from google/gemma-4-E4B, but Hugging Face has no such model "
        "now, so who made it cannot be checked.",
    )
    assert row(found, "built_from") == Check(
        "built_from",
        "Built from",
        "bad",
        "google/gemma-4-E4B-it, by google — an approved maker",
        "…which google built from google/gemma-4-E4B. "
        "google/gemma-4-E4B is not on Hugging Face now.",
    )


def test_a_conversion_of_a_model_that_is_not_a_makers_is_refused_by_name() -> None:
    site = fixtures()
    config = LocalModelsConfig(makers=["nvidia"])

    found = check(site, config=config)

    assert found.problems == (
        "It was built from google/gemma-4-E4B-it, published by google, who is not on your "
        "makers list. Add google as a maker if you trust them, then check again.",
    )
    built_from = row(found, "built_from")
    assert built_from.tone == "bad"
    assert built_from.value == "google/gemma-4-E4B-it, by google — not on your makers list"


def test_a_base_that_has_gone_from_hugging_face_is_refused_and_named() -> None:
    site = fixtures()
    del site.payloads["google/gemma-4-E4B-it"]

    found = check(site)

    assert found.problems == (
        "It says it was built from google/gemma-4-E4B-it, but Hugging Face has no such model "
        "now, so who made it cannot be checked.",
    )
    assert row(found, "built_from") == Check(
        "built_from", "Built from", "bad", "google/gemma-4-E4B-it is not on Hugging Face now"
    )


def test_a_makers_own_model_names_nothing_and_that_is_fine() -> None:
    site = fixtures(page("google/original-mlx"))

    found = check(site, "google/original-mlx")

    assert found.ok
    assert row(found, "publisher") == Check(
        "publisher", "Published by", "ok", "google — an approved maker"
    )
    assert row(found, "built_from") == Check(
        "built_from", "Built from", "ok", "Nothing — its page names no other model"
    )
    assert row(found, "licence").detail == "As stated on the model's page."


def test_a_conversion_that_names_nothing_says_so_on_its_row() -> None:
    found = check(fixtures(), "mlx-community/orphan-4bit")

    assert row(found, "built_from") == Check(
        "built_from", "Built from", "bad", "Its page does not say"
    )


# --- the name itself --------------------------------------------------------------


def test_a_pasted_address_is_checked_as_the_model_it_names() -> None:
    found = check(fixtures(), f"https://huggingface.co/{FIRST}/tree/main")

    assert found.info.repo_id == FIRST


def test_a_name_in_other_capitals_is_checked_under_hugging_faces_spelling() -> None:
    found = check(fixtures(), FIRST.upper())

    assert found.ok
    assert found.info.repo_id == FIRST
    assert found.chain[0].repo_id == FIRST


def test_something_that_is_not_a_model_name_is_refused_before_anything_is_asked() -> None:
    site = fixtures()

    with pytest.raises(ValueError, match="That is not a model's name") as caught:
        check(site, "gemma")

    assert str(caught.value) == policy.NOT_A_MODEL_NAME
    assert site.calls == []


def test_a_model_hugging_face_does_not_have_is_an_error_not_a_check() -> None:
    with pytest.raises(HubError) as caught:
        check(fixtures(), "google/no-such-model")

    assert caught.value.problem == "not_found"
    assert (
        str(caught.value)
        == "Hugging Face has no model called google/no-such-model. Check the spelling."
    )


def test_a_name_that_now_points_elsewhere_is_not_checked_under_the_old_name() -> None:
    site = fixtures()
    site.payloads["mlx-community/old-name"] = fake.fixture("gemma-4-e4b-it-4bit")

    with pytest.raises(HubError) as caught:
        check(site, "mlx-community/old-name")

    assert caught.value.problem == "moved"
    assert caught.value.moved_to == FIRST
    assert str(caught.value) == (
        f"This name now points to {FIRST} on Hugging Face. Check that name instead."
    )
    # Nothing was judged, read or sized for it.
    assert [call[0] for call in site.calls] == ["info"]


def test_hugging_face_not_answering_about_the_model_itself_is_an_error() -> None:
    site = fixtures()
    site.fail_info(FIRST)

    with pytest.raises(HubError) as caught:
        check(site)

    assert caught.value.problem == "unreachable"


def test_the_check_is_run_at_the_build_it_was_given() -> None:
    site = fixtures()

    found = check(site, revision=BUILD)
    assert found.info.revision == BUILD
    assert site.calls[0] == ("info", FIRST, BUILD)

    # Hugging Face has moved on since the check was shown: nothing is
    # downloaded under the old check's name.
    with pytest.raises(HubError) as caught:
        check(site, revision="0" * 40)
    assert caught.value.problem == "not_found"


@pytest.mark.parametrize(
    ("flag", "sentence"),
    [
        (
            "private",
            "This model is private on Hugging Face. FrameFound only adds models whose page "
            "anyone can read.",
        ),
        ("disabled", "Hugging Face has switched this model off, so it cannot be downloaded."),
    ],
)
def test_a_private_or_switched_off_model_cannot_be_added(flag: str, sentence: str) -> None:
    site = fixtures()
    site.payloads[FIRST][flag] = True

    found = check(site)

    assert found.problems == (sentence,)
    assert found.summary == sentence


# --- code from the repository ------------------------------------------------------


def test_a_settings_file_that_names_a_model_file_refuses_a_model_whose_page_says_nothing() -> None:
    site = fixtures()
    info = site.info("mlx-community/demo-model-file")
    # Its page gives no sign: not tagged, and the page's own copy of the
    # settings does not carry the key. Only the file itself does.
    assert not info.custom_code
    assert MlxVlm().supports(info, plan_of(info)).ok

    found = check(site, "mlx-community/demo-model-file")

    assert found.problems == (NEEDS_CODE,)
    assert row(found, "files").tone == "bad"
    assert ("read_text", "mlx-community/demo-model-file", "5" * 40, "config.json") in site.calls


def test_every_settings_file_a_runtime_reads_is_inspected_wherever_it_sits() -> None:
    site = fixtures(
        page(
            "mlx-community/nested-4bit",
            base="google/gemma-4-E4B",
            siblings=(
                ("config.json", 1500),
                ("tokenizer_config.json", 900),
                ("vision/preprocessor_config.json", 700),
                ("generation_config.json", 200),
                ("model.safetensors", 3 * GB),
            ),
        )
    )
    site.contents[("mlx-community/nested-4bit", "vision/preprocessor_config.json")] = (
        b'{"processor": {"auto_map": {"AutoProcessor": "processing_demo.DemoProcessor"}}}'
    )

    found = check(site, "mlx-community/nested-4bit")

    assert found.problems == (NEEDS_CODE,)
    read = [call[3] for call in site.calls if call[0] == "read_text"]
    assert sorted(read) == [
        "config.json",
        "tokenizer_config.json",
        "vision/preprocessor_config.json",
    ]


@pytest.mark.parametrize(
    ("name", "asks_for_code"),
    [
        ("Config.json", b'{"model_file": "notes.txt"}'),
        ("CONFIG.JSON", b'{"auto_map": {"AutoModel": "modeling_demo.DemoModel"}}'),
        ("vision/Preprocessor_Config.JSON", b'{"auto_map": {"AutoProcessor": "p.Processor"}}'),
    ],
)
def test_a_settings_file_in_other_capitals_is_read_and_refuses_the_model(
    name: str, asks_for_code: bytes
) -> None:
    # Hugging Face tells Config.json from config.json, and builds its page
    # from the lower-case one only: this page gives no sign. The Mac's disk
    # does not tell them apart, so this is the file the runtime would open.
    repo_id = "mlx-community/capitals-4bit"
    site = fixtures(
        page(
            repo_id,
            base="google/gemma-4-E4B",
            siblings=((name, 1500), ("model.safetensors", 3 * GB)),
        )
    )
    site.contents[(repo_id, name)] = asks_for_code
    info = site.info(repo_id)
    assert not info.custom_code and MlxVlm().supports(info, plan_of(info)).ok

    found = check(site, repo_id)

    assert found.problems == (NEEDS_CODE,)
    assert row(found, "files").tone == "bad"
    assert ("read_text", repo_id, "b" * 40, name) in site.calls


def test_a_repository_with_hundreds_of_settings_files_is_refused_without_reading_them() -> None:
    # One request each, and how many there are is the publisher's choice: a
    # repository that keeps every training checkpoint, or one padded on
    # purpose, would hold a check for minutes and use up this machine's
    # welcome at Hugging Face.
    def kept(count: int) -> dict[str, Any]:
        configs = tuple((f"checkpoint-{n}/config.json", 1500) for n in range(count))
        return page(
            f"google/checkpoints-{count}", siblings=(*configs, ("model.safetensors", 3 * GB))
        )

    site = fixtures(kept(500), kept(files.MAX_INSPECTED))

    found = check(site, "google/checkpoints-500")

    assert found.problems == (
        "It has 500 settings files (config.json and the like), far more than one model has. "
        "FrameFound reads every one of them before it accepts a model, and will not read "
        "that many.",
    )
    assert row(found, "files").tone == "bad"
    assert [call for call in site.calls if call[0] == "read_text"] == []

    # As many as a model made of several parts might have are all read.
    site.calls.clear()
    assert check(site, f"google/checkpoints-{files.MAX_INSPECTED}").ok
    assert len([call for call in site.calls if call[0] == "read_text"]) == files.MAX_INSPECTED

    # Asking for an account does not open the door again: one small read to
    # try the token, not five hundred.
    site.payloads["google/checkpoints-500"]["gated"] = "auto"
    site.calls.clear()
    gated = check(site, "google/checkpoints-500")
    assert not gated.ok
    assert len([call for call in site.calls if call[0] == "read_text"]) <= 1


def test_code_named_by_the_page_and_by_the_settings_is_one_refusal_not_two() -> None:
    found = check(fixtures(), "allenai/demo-needs-code")

    assert found.problems == (NEEDS_CODE,)
    # Its code files are never among what would be downloaded.
    assert found.variant is not None
    assert not any(file.path.endswith(".py") for file in found.variant.files)
    assert row(found, "files").detail == (
        "Weights are safetensors. Left out: modeling_demo.py, processing_demo.py."
    )
    assert row(found, "runtime") == Check(
        "runtime", "Runs on", "bad", "Nothing here can run it", NEEDS_CODE
    )


class Unreadable(fake.FakeHub):
    """A hub whose settings files fail in one chosen way."""

    problem: hub.Problem = "not_found"

    def read_text(self, repo_id: str, revision: str, path: str, max_bytes: int = 4_000_000) -> str:
        self.calls.append(("read_text", repo_id, revision, path))
        raise HubError(self.problem, f"{path} could not be had.")


def test_a_settings_file_that_cannot_be_read_cannot_be_cleared() -> None:
    site = Unreadable({FIRST: fake.fixture("gemma-4-e4b-it-4bit")})
    for name in ("gemma-4-E4B-it", "gemma-4-E4B"):
        site.payloads[fake.fixture(name)["id"]] = fake.fixture(name)

    found = check(site)

    assert not found.ok
    assert (
        "config.json could not be read, so FrameFound cannot tell whether this model needs "
        "program code from its repository."
    ) in found.problems
    assert len(found.problems) == 3  # one for each settings file it has
    assert row(found, "files").tone == "bad"

    # Hugging Face not answering is not a verdict on the model.
    site.problem = "unreachable"
    with pytest.raises(HubError) as caught:
        check(site)
    assert caught.value.problem == "unreachable"


def test_weights_in_an_older_format_are_refused_in_these_words() -> None:
    site = fixtures(
        page(
            "google/older-format",
            siblings=(("config.json", 1500), ("pytorch_model.bin", 3 * GB)),
        )
    )

    found = check(site, "google/older-format")

    assert found.problems == (
        "Its weights are in an older file format (.bin) that can carry program code. "
        "FrameFound only accepts safetensors and GGUF files.",
        "It has no safetensors weights, which is the only kind MLX-VLM runs.",
    )
    assert row(found, "files").tone == "bad"
    assert row(found, "files").detail == (
        "No weights FrameFound can use. Left out: pytorch_model.bin."
    )
    # Nothing can run it, so there is no build to size.
    assert found.variant is None
    assert (found.size_bytes, found.memory_gb) == (0, 0.0)
    assert row(found, "size").value == "Not worked out"
    assert row(found, "memory").value == "Not worked out"


def test_a_gguf_only_repository_is_refused_until_a_program_for_it_exists() -> None:
    site = fixtures(
        page(
            "mlx-community/demo-gguf",
            base="google/gemma-4-E4B",
            siblings=(("README.md", 400), ("demo-Q4_K_M.gguf", 4 * GB)),
            tags=("gguf",),
            library="gguf",
        )
    )

    found = check(site, "mlx-community/demo-gguf")

    assert found.problems == ("This build is for llama.cpp, which FrameFound cannot run yet.",)
    assert (found.runtime, found.variant) == (None, None)


# --- models that ask for an account -----------------------------------------------


def gated_first() -> fake.FakeHub:
    """The first model, as it would be if its maker asked for an account."""
    site = fixtures()
    site.payloads[FIRST]["gated"] = "auto"
    site.need_token(FIRST, accepts=[TOKEN])
    return site


def test_a_gated_model_says_whether_the_token_is_missing_or_has_no_access() -> None:
    site = gated_first()

    missing = check(site)
    assert missing.access == "needs_token"
    assert missing.problems == (
        "mlx-community asks you to accept their terms before downloading. Do that on the "
        "model's page at Hugging Face, then paste a read token under Hugging Face account, "
        "below, and check again.",
    )
    assert row(missing, "access") == Check(
        "access", "Access", "bad", "Needs an account, and no token is stored"
    )

    refused = check(site.with_token("another-token-that-is-not-real"))
    assert refused.access == "no_access"
    assert refused.problems == (
        "Hugging Face would not let this token read the model. Check that you accepted the "
        "terms on the model's page with the same account.",
    )
    assert row(refused, "access") == Check(
        "access", "Access", "bad", "Your Hugging Face token cannot read it"
    )

    let_in = check(site.with_token(TOKEN))
    assert let_in.ok
    assert let_in.access == "ok"
    assert row(let_in, "access") == Check(
        "access", "Access", "ok", "Needs an account — your Hugging Face token can read it"
    )
    assert site.fetches == 0


def test_a_gated_model_that_is_refused_for_another_reason_says_both() -> None:
    site = fixtures()
    site.need_token("nvidia/demo-gated")

    found = check(site, "nvidia/demo-gated")

    assert found.access == "needs_token"
    assert found.problems == (
        *EXPECTED["gated-blocked-base"],
        "nvidia asks you to accept their terms before downloading. Do that on the model's "
        "page at Hugging Face, then paste a read token under Hugging Face account, below, "
        "and check again.",
    )
    assert row(found, "licence").value == "other — nvidia-open-model-license"


def test_a_gated_model_with_no_settings_file_has_the_token_tried_on_its_readme() -> None:
    bare = page(
        "google/gated-no-config",
        siblings=(("LICENSE", 300), ("README.md", 400), ("model.safetensors", 3 * GB)),
        gated="manual",
    )
    site = fixtures(bare)
    site.need_token("google/gated-no-config", accepts=[TOKEN])

    assert check(site, "google/gated-no-config").access == "needs_token"
    assert (
        check(site.with_token("some-other-token"), "google/gated-no-config").access == "no_access"
    )
    assert check(site.with_token(TOKEN), "google/gated-no-config").access == "ok"

    # The readme, not the smaller licence text listed before it, and never the weights.
    read = {call[3] for call in site.calls if call[0] == "read_text"}
    assert read == {"README.md"}
    assert site.fetches == 0


def test_a_gated_model_with_nothing_small_to_read_says_the_download_will_tell() -> None:
    site = fixtures(
        page("google/gated-weights-only", siblings=(("model.safetensors", 3 * GB),), gated="auto")
    )
    site.need_token("google/gated-weights-only")

    found = check(site, "google/gated-weights-only")

    assert found.access == "untested"
    # Not a refusal: nothing is known to be wrong.
    assert found.ok
    assert row(found, "access") == Check(
        "access",
        "Access",
        "warn",
        "Could not be tested before downloading",
        "This repository has no small file to try. If the token cannot read it, the download "
        "will stop and say so.",
    )
    assert [call[0] for call in site.calls] == ["info"]


# --- size, room and the memory budgets ----------------------------------------------


def test_the_twelve_b_build_only_fits_overnight_at_eight_gigabytes() -> None:
    site = fixtures()

    found = check(site, TWELVE)

    assert found.ok
    assert (found.memory_gb, found.fits) == (10.1, "night")
    assert found.warnings == (
        "It needs about 10.1 GB of memory, more than the daytime budget (8 GB). It would only "
        "run overnight, 21:30–05:30.",
        DOES_NOT_NAME_IT,
    )
    assert row(found, "memory") == Check(
        "memory",
        "Memory",
        "warn",
        "About 10.1 GB — overnight only",
        "The daytime budget is 8 GB; the overnight budget is 20 GB, 21:30–05:30.",
    )
    assert row(found, "runtime") == Check(
        "runtime", "Runs on", "warn", "MLX-VLM 0.7.12", DOES_NOT_NAME_IT
    )
    assert found.takes == ("text",)

    # The budget is the owner's to change, and the answer follows it.
    by_day = check(site, TWELVE, config=LocalModelsConfig(day_budget_gb=12.0))
    assert by_day.fits == "day"
    assert by_day.warnings == (DOES_NOT_NAME_IT,)
    assert row(by_day, "memory").value == "About 10.1 GB — fits by day"
    assert row(by_day, "memory").detail == "The daytime budget is 12 GB."


def test_a_model_larger_than_the_overnight_budget_can_be_added_but_says_it_will_not_run() -> None:
    found = check(fixtures(), LARGE, config=LocalModelsConfig(night_budget_gb=16.0))

    assert found.ok
    assert (found.memory_gb, found.fits) == (18.8, "never")
    assert found.warnings == (
        "It needs about 18.8 GB, more than the overnight budget (16 GB). It can be "
        "downloaded, but it will not run until the budget is raised.",
    )
    assert row(found, "memory") == Check(
        "memory",
        "Memory",
        "warn",
        "About 18.8 GB — more than the overnight budget",
        "The overnight budget is 16 GB.",
    )

    # Budgets set the unusual way round: the one named is the one to raise.
    reversed_ = check(
        fixtures(), LARGE, config=LocalModelsConfig(day_budget_gb=12.0, night_budget_gb=8.0)
    )
    assert reversed_.warnings == (
        "It needs about 18.8 GB, more than the daytime budget (12 GB). It can be downloaded, "
        "but it will not run until the budget is raised.",
    )
    assert row(reversed_, "memory").detail == "The daytime budget is 12 GB."


def test_an_overnight_window_that_never_opens_is_not_promised() -> None:
    window = {"night_start": "21:30", "night_end": "21:30"}
    config = LocalModelsConfig(**window)

    found = check(fixtures(), TWELVE, config=config)

    assert found.warnings[0] == (
        "It needs about 10.1 GB of memory, more than the daytime budget (8 GB), and there is "
        "no overnight window. It can be downloaded, but it will not run until one of them "
        "is changed."
    )
    assert row(found, "memory").value == "About 10.1 GB — more than the daytime budget"
    assert row(found, "memory").detail == (
        "The daytime budget is 8 GB, and there is no overnight window."
    )
    # The one word a page would badge it with says the same as the sentences:
    # not "overnight only" beside "it will not run".
    assert found.fits == "never"
    assert [build.fits for build in found.sized] == ["never"]

    # Too big for either budget, the budget is what is in the way, window or no window.
    too_big = check(fixtures(), LARGE, config=LocalModelsConfig(night_budget_gb=16.0, **window))
    assert too_big.fits == "never"
    assert row(too_big, "memory").detail == "The overnight budget is 16 GB."


def test_not_enough_room_is_said_with_both_figures() -> None:
    site = fixtures()

    short = check(site, free_bytes=int(3.1 * GB))
    assert short.problems == (
        "There is not enough room: it needs 4.8 GB and the model library has 3.1 GB free.",
    )
    assert row(short, "size") == Check(
        "size", "Size", "bad", "4.8 GB", "The model library has 3.1 GB free."
    )

    # It would fit, but only by filling the disk: the two figures alone
    # would read as if there were room.
    tight = check(site, free_bytes=6 * GB)
    assert tight.problems == (
        "There is not enough room: it needs 4.8 GB and the model library has 6.0 GB free. "
        "FrameFound leaves the last 5.0 GB of a disk free.",
    )

    exactly = check(site, free_bytes=5_179_239_942 + 5 * GB)
    assert exactly.ok


def test_room_is_needed_where_a_download_lands_as_well_as_in_the_library() -> None:
    found = check(fixtures(), scratch_free_bytes=2 * GB)

    assert found.problems == (
        "There is not enough room: it needs 4.8 GB and this machine's own disk has 2.0 GB free.",
    )
    assert row(found, "size").tone == "bad"


def test_room_that_cannot_be_read_is_not_a_refusal() -> None:
    found = check(fixtures(), free_bytes=None)

    assert found.ok
    assert found.free_bytes is None
    assert row(found, "size") == Check(
        "size", "Size", "ok", "4.8 GB", "How much room the model library has could not be read."
    )


class TwoBuilds:
    """A kind whose repositories hold two builds, as a GGUF repository does."""

    name = "two-builds"
    label = "Two Builds"
    summary = "A stand-in for a test."
    demo = False
    weights: tuple[str, ...] = (".gguf",)
    needs_uv = False
    needs_compare = True

    def __init__(self) -> None:
        self.offered: list[list[tuple[str, Fit]]] = []

    def supported_here(self, system: str, machine: str) -> str | None:
        return None

    def supports(self, info: hub.RepoInfo, plan: files.FilePlan) -> Support:
        texts = tuple(file for file in plan.keep if not file.path.endswith(".gguf"))
        builds = tuple(
            Variant(
                id=file.path.removesuffix(".gguf"),
                label=file.path,
                files=(*texts, file),
                weights_bytes=file.size,
                takes=("text",) if file.path.startswith("small") else (),
            )
            for file in sorted(plan.weights, key=lambda file: -file.size)
        )
        return Support(bool(builds), "It is a GGUF build.", ("text", "image"), builds)

    def prefer(self, sized: Sequence[tuple[Variant, Fit]]) -> str:
        self.offered.append([(build.id, fits) for build, fits in sized])
        return next(build.id for build, fits in sized if fits == "day")

    def latest(self, fetch: hub.FetchJson) -> str:
        return "1"


def two_builds() -> fake.FakeHub:
    return fixtures(
        page(
            "mlx-community/two-builds",
            base="google/gemma-4-E4B",
            siblings=(("README.md", 400), ("small.gguf", 2 * GB), ("large.gguf", 30 * GB)),
            tags=("gguf",),
            library="gguf",
        )
    )


def test_sizes_are_the_chosen_builds_not_the_repositorys() -> None:
    site = two_builds()
    kind = TwoBuilds()
    using = {"kinds": {"two-builds": kind}, "installed": {"two-builds": "1"}}

    found = check(site, "mlx-community/two-builds", **using)

    # The repository holds 32 GB. The build that would be downloaded is 2.
    assert found.plan.total_bytes == 32 * GB + 400
    assert found.ok
    assert found.variant is not None
    assert found.variant.id == "small"
    assert found.size_bytes == 2 * GB + 400
    assert (found.memory_gb, found.fits) == (2.4, "day")
    assert found.warnings == ()
    assert row(found, "size").value == "2.0 GB"
    # A GGUF build's files are said for what they are, and the other build
    # by its name rather than its file.
    assert row(found, "files") == Check(
        "files",
        "Files",
        "ok",
        "2 files to download: the model and its README",
        "Weights are GGUF. This build has no part that reads pictures. Left out: the other "
        "build (large).",
    )
    assert row(found, "runtime").value == "Two Builds 1"
    # The kind was shown every build with the budget it fits, and chose.
    assert kind.offered == [[("large", "never"), ("small", "day")]]
    assert [(build.variant.id, build.size_bytes, build.memory_gb) for build in found.sized] == [
        ("large", 30 * GB + 400, 36.0),
        ("small", 2 * GB + 400, 2.4),
    ]
    # What this build takes, where that differs from the repository's.
    assert found.takes == ("text",)
    # There is room for the build though not for the repository.
    assert check(site, "mlx-community/two-builds", free_bytes=10 * GB, **using).ok

    asked = check(site, "mlx-community/two-builds", variant="large", **using)
    assert asked.variant is not None
    assert asked.variant.id == "large"
    assert (asked.size_bytes, asked.memory_gb, asked.fits) == (30 * GB + 400, 36.0, "never")
    assert asked.warnings == (
        "It needs about 36 GB, more than the overnight budget (20 GB). It can be downloaded, "
        "but it will not run until the budget is raised.",
    )
    assert asked.takes == ("text", "image")
    # The suggestion is still worked out, so the list can mark it, but the
    # build that was asked for is the one checked.
    assert len(kind.offered) == 3
    assert asked.recommended == "small"

    with pytest.raises(ValueError, match="not in this repository") as caught:
        check(site, "mlx-community/two-builds", variant="medium", **using)
    assert str(caught.value) == "That build is not in this repository."

    # An empty name is "whichever you would choose", as no name is.
    whichever = check(site, "mlx-community/two-builds", variant="", **using)
    assert whichever.variant is not None
    assert whichever.variant.id == "small"


def test_a_build_asked_for_in_a_repository_that_is_one_build_is_not_there() -> None:
    with pytest.raises(ValueError, match="not in this repository"):
        check(fixtures(), variant="Q4_K_M")

    # An empty name means "whichever", as no name does.
    assert check(fixtures(), variant="").variant is not None


# --- what is already installed, and what is not yet --------------------------------


def test_this_exact_build_already_installed_is_refused() -> None:
    mine = uuid.uuid4()
    asked: list[str] = []

    def installed(repo_id: str) -> list[Installed]:
        asked.append(repo_id)
        return [Installed(uuid.uuid4(), "0" * 40), Installed(mine, BUILD.upper())]

    found = check(fixtures(), FIRST.upper(), existing=installed)

    assert asked == [FIRST]  # Hugging Face's spelling, not what was typed
    assert found.problems == ("This exact build is already installed.",)
    assert (found.existing, found.same_build) == (mine, True)
    assert found.notes == ()


def test_an_older_build_already_installed_is_a_note_not_a_refusal() -> None:
    older = uuid.uuid4()

    found = check(fixtures(), existing=lambda _repo: [Installed(older, "3f1c2aa" + "0" * 33)])

    assert found.ok
    assert (found.existing, found.same_build) == (older, False)
    assert found.notes == (
        "You have an older build of this model (3f1c2aa). This would download the current "
        "build beside it; remove the old one when you are happy.",
    )

    # A build asked for by its commit: which of the two is newer is not known.
    pinned = check(
        fixtures(), revision=BUILD, existing=lambda _repo: [Installed(older, "3f1c2aa" + "0" * 33)]
    )
    assert pinned.notes == (
        "You have another build of this model (3f1c2aa). This would download this one beside "
        "it; remove the one you do not want when you are happy.",
    )


def test_another_build_of_the_same_files_is_not_the_same_build() -> None:
    site = two_builds()
    large = uuid.uuid4()
    using: dict[str, Any] = {
        "kinds": {"two-builds": TwoBuilds()},
        "installed": {"two-builds": "1"},
        "existing": lambda _repo: [Installed(large, "b" * 40, "large")],
    }

    small = check(site, "mlx-community/two-builds", **using)
    assert small.ok
    assert (small.existing, small.same_build) == (large, False)
    assert small.notes == (
        "You already have the large build of this model. This would download small beside it.",
    )

    again = check(site, "mlx-community/two-builds", variant="large", **using)
    assert again.problems == ("This exact build is already installed.",)
    assert again.same_build


def test_a_runtime_that_is_not_installed_yet_is_a_warning_not_a_refusal() -> None:
    for installed in ({}, {"mlx-vlm": None}, {"mlx-vlm": ""}):
        found = check(fixtures(), installed=installed)

        assert found.ok
        assert (found.runtime, found.runtime_installed) == ("mlx-vlm", False)
        assert found.warnings == (
            "MLX-VLM is not installed yet. The model can be downloaded now and will work once "
            "it is.",
        )
        assert row(found, "runtime") == Check(
            "runtime", "Runs on", "warn", "MLX-VLM — not installed yet", NAMES_MLX_VLM
        )


def test_sentences_about_the_program_use_the_name_of_the_kind_that_would_run_it() -> None:
    found = check(fixtures(), kinds={"demo": fake.DemoKind()}, installed={})

    assert found.runtime == "demo"
    assert found.warnings == (
        "Demo runtime (not a real model) is not installed yet. The model can be downloaded "
        "now and will work once it is.",
    )
    installed = check(fixtures(), kinds={"demo": fake.DemoKind()}, installed={"demo": "2.0"})
    assert row(installed, "runtime").value == "Demo runtime (not a real model) 2.0"


def test_a_model_whose_page_states_no_licence_can_be_added_after_a_warning() -> None:
    site = fixtures(page("google/unlicensed-mlx", licence=None))

    found = check(site, "google/unlicensed-mlx")

    assert found.ok
    assert found.warnings == (
        "Its page states no licence. Read the model's page before using it for client work.",
    )
    assert row(found, "licence") == Check(
        "licence",
        "Licence",
        "warn",
        "None stated",
        "Its page states no licence. Read the model's page before using it for client work.",
    )


def test_a_refused_model_has_nothing_to_accept() -> None:
    # Unlicensed, not runnable by day, its runtime missing — and refused.
    # "It can be downloaded, but…" under a refusal would contradict it.
    site = fixtures(
        page(
            "example-lab/everything-wrong",
            siblings=(("config.json", 1500), ("model.safetensors", 12 * GB)),
            tags=("mlx",),
            licence=None,
        )
    )

    found = check(site, "example-lab/everything-wrong", installed={})

    assert not found.ok
    assert found.warnings == ()
    # The rows still say all of it.
    assert row(found, "licence").tone == "warn"
    assert row(found, "memory").value == "About 14.4 GB — overnight only"
    assert row(found, "runtime").value == "MLX-VLM — not installed yet"


def test_an_install_that_cannot_run_models_refuses_every_model() -> None:
    found = check(fixtures(), kinds={}, installed={})

    assert found.problems == (
        "This install has no software that runs models, so nothing here can run it.",
    )
    assert (found.runtime, found.variant) == (None, None)


def test_text_from_a_models_page_is_made_fit_to_show() -> None:
    site = fixtures(page("google/odd-licence", licence="mit\x1b[31m\n" + "x" * 300))

    value = row(check(site, "google/odd-licence"), "licence").value

    assert value.isprintable()
    assert len(value) <= 120


# --- GGUF repositories and their builds (L6 6.1, 9.2) -------------------------------

OMNI = "example-gguf/demo-omni-30b-GGUF"
VISION = "google/demo-vision-gguf"
MANY = "example-gguf/demo-many-builds-GGUF"
GATED_GGUF = "nvidia/demo-gated-gguf"
PROJECTOR_ONLY = "google/demo-projector-only"
# The GGUF publisher of the fixtures is nobody's default: the operator adds it.
WITH_GGUF = LocalModelsConfig(converters=["mlx-community", "example-gguf"])
ONLY_PICTURES = "This repository holds only the part that reads pictures, not a model."
CHOOSE = "Choose another under Which build."


def both_kinds() -> dict[str, RuntimeKind]:
    """What a Mac has: MLX-VLM first, then llama.cpp."""
    return {"mlx-vlm": MlxVlm(), "llama-cpp": kinds.LlamaCpp()}


def gguf_check(site: hub.Hub, name: str, **more: Any) -> Preflight:
    """The check on a Mac with both programs installed and example-gguf approved."""
    using: dict[str, Any] = {
        "config": WITH_GGUF,
        "kinds": both_kinds(),
        "installed": {"mlx-vlm": "0.7.12", "llama-cpp": "0.9.0"},
    }
    using.update(more)
    return check(site, name, **using)


GGUF_EXPECTED: dict[str, tuple[str, ...]] = {
    "gguf/demo-omni-30b-GGUF": (),
    "gguf/demo-vision-gguf": (),
    "gguf/demo-many-builds-GGUF": (),
    "gguf/demo-gated-gguf": (),
    # The maker's own original: the end of the GGUF builds' chain, not a build itself.
    "gguf/demo-omni-30b": (ORIGINAL_BUILD,),
    "gguf/demo-projector-only": (ONLY_PICTURES,),
}


def test_every_gguf_fixture_has_an_expected_answer() -> None:
    assert sorted(GGUF_EXPECTED) == fake.gguf_fixture_names()
    # The hub of every fixture knows them too, so the demo and the API can be asked.
    assert {fake.fixture(name)["id"] for name in GGUF_EXPECTED} <= set(fixtures().payloads)


@pytest.mark.parametrize("name", sorted(GGUF_EXPECTED))
def test_each_gguf_fixture_is_allowed_or_refused_in_these_exact_words(name: str) -> None:
    site = fixtures()

    found = gguf_check(site, fake.fixture(name)["id"])

    assert found.problems == GGUF_EXPECTED[name]
    keys = ["publisher", "built_from", "licence", "access", "files", "size", "memory", "runtime"]
    if len(found.sized) > 1:
        keys.insert(4, "build")
    assert [item.key for item in found.checks] == keys
    assert any(item.tone == "bad" for item in found.checks) is (not found.ok)
    # The build row is the operator's choice to make, never a refusal.
    assert all(item.tone != "bad" for item in found.checks if item.key == "build")
    assert site.fetches == 0


def test_no_gguf_fixture_that_passes_has_a_blocked_publisher_anywhere_in_it() -> None:
    site = fixtures()
    lists = WITH_GGUF.lists()
    allowed = 0
    for name in fake.gguf_fixture_names():
        found = gguf_check(site, fake.fixture(name)["id"])
        if not found.ok:
            continue
        allowed += 1
        everyone = {node.publisher for node in found.chain}
        everyone |= {policy.publisher_of(node.repo_id) for node in found.chain}
        everyone |= {policy.publisher_of(base) for node in found.chain for base in node.bases}
        assert all(lists.status(publisher) != "blocked" for publisher in everyone), name
        assert found.complete
    assert allowed == 4


def test_a_gguf_converter_nobody_approved_is_refused_until_it_is_added() -> None:
    found = gguf_check(fixtures(), OMNI, config=LocalModelsConfig())

    assert found.problems == (
        "example-gguf is not one of your approved publishers. If you know who they are and "
        "want their models, add them under Approved publishers — as a maker if they trained "
        "it, as a converter if they only packaged it. Then check again.",
    )
    # Every build is still listed and sized, so the operator sees what it would cost.
    assert [build.variant.id for build in found.sized] == ["Q4_K_M", "Q6_K", "Q8_0"]


def test_the_design_example_is_the_check_of_the_demo_omni_repository() -> None:
    # L6 6.1, row for row. Two places differ from the design's text on
    # purpose (L1's choices): the publisher row carries the converter
    # sentence as its detail, and the library's room is written "812.0 GB".
    found = gguf_check(fixtures(), OMNI)

    assert found.ok
    assert found.summary == (
        "This model can be added, but it will not run until the overnight budget is raised."
    )
    assert found.checks == (
        Check(
            "publisher",
            "Published by",
            "ok",
            "example-gguf — an approved converter",
            "Packaged by example-gguf, not by the maker. FrameFound can check what a build "
            "says it was made from, not that it is true — which is why converters are a list "
            "you control.",
        ),
        Check(
            "built_from", "Built from", "ok", "nvidia/demo-omni-30b, by nvidia — an approved maker"
        ),
        Check(
            "licence",
            "Licence",
            "ok",
            "other — nvidia-open-model-agreement",
            "As stated on this build's own page. The maker's page may state a different one.",
        ),
        Check("access", "Access", "ok", "Open — no account needed"),
        Check(
            "build",
            "Build",
            "warn",
            "Q4_K_M — one of 3 in this repository",
            "None of the three fits the overnight budget; this is the smallest. "
            "Choose another under Which build.",
        ),
        Check(
            "files",
            "Files",
            "ok",
            "3 files to download: the model, the part that reads pictures, and its README",
            "Weights are GGUF. The part that reads pictures is mmproj-demo-omni-30b-BF16.gguf "
            "(1.5 GB). Left out: the other builds (Q6_K, Q8_0) and .gitattributes.",
        ),
        Check("size", "Size", "ok", "24.3 GB", "The model library has 812.0 GB free."),
        Check(
            "memory",
            "Memory",
            "warn",
            "About 29.2 GB — more than the overnight budget",
            "The overnight budget is 20 GB.",
        ),
        Check(
            "runtime",
            "Runs on",
            "ok",
            "llama.cpp 0.9.0",
            "It is a GGUF build; llama.cpp runs those. It will be given room for 16,384 "
            "tokens; its page says it can take 262,144.",
        ),
    )
    assert found.warnings == (
        "It needs about 29.2 GB, more than the overnight budget (20 GB). It can be "
        "downloaded, but it will not run until the budget is raised.",
    )
    assert (found.access, found.runtime, found.recommended) == ("open", "llama-cpp", "Q4_K_M")
    assert (found.size_bytes, found.memory_gb, found.fits) == (26_104_638_036, 29.2, "never")
    assert found.takes == ("text", "image")
    assert [
        (build.variant.id, build.variant.label, build.size_bytes, build.memory_gb, build.fits)
        for build in found.sized
    ] == [
        ("Q4_K_M", "Q4_K_M — 24.3 GB", 26_104_638_036, 29.2, "never"),
        ("Q6_K", "Q6_K — 32.7 GB", 35_097_675_348, 39.3, "never"),
        ("Q8_0", "Q8_0 — 32.8 GB", 35_175_003_732, 39.4, "never"),
    ]
    assert found.variant is not None
    assert [(file.path, preflight.part_of(file.path)) for file in found.variant.files] == [
        ("demo-omni-30b-Q4_K_M.gguf", "model"),
        ("mmproj-demo-omni-30b-BF16.gguf", "pictures"),
        ("README.md", "text"),
    ]


def test_sizes_and_memory_are_the_chosen_builds_not_the_repositorys() -> None:
    found = gguf_check(fixtures(), VISION)

    # The repository holds 15 GB; the suggested build is three.
    assert found.plan.total_bytes > 14 * GB
    assert found.variant is not None and found.variant.id == "Q4_K_M"
    # Model and picture reader in memory (D13); readme and licence on disk only.
    assert found.variant.weights_bytes == 2_489_757_856 + 851_251_104
    assert (found.size_bytes, found.memory_gb, found.fits) == (3_341_026_438, 3.8, "day")
    assert row(found, "build") == Check(
        "build",
        "Build",
        "ok",
        "Q4_K_M — one of 3 in this repository",
        f"The suggested build; it fits the daytime budget. {CHOOSE}",
    )
    assert row(found, "size").value == "3.1 GB"
    assert row(found, "memory").value == "About 3.8 GB — fits by day"
    assert found.summary == "This model can be added."


def test_asking_for_another_build_changes_the_figures() -> None:
    found = gguf_check(fixtures(), VISION, variant="Q8_0")

    assert found.variant is not None and found.variant.id == "Q8_0"
    # The suggestion is kept, so the list still marks it.
    assert found.recommended == "Q4_K_M"
    weights = 4_130_226_336 + 851_251_104
    assert found.variant.weights_bytes == weights
    assert found.size_bytes == weights + 11_358 + 6_120
    assert (found.memory_gb, found.fits) == (preflight.estimate_memory_gb(weights), "day")
    assert row(found, "build") == Check(
        "build",
        "Build",
        "ok",
        "Q8_0 — one of 3 in this repository",
        f"Chosen instead of the suggested Q4_K_M; it fits the daytime budget. {CHOOSE}",
    )
    assert row(found, "files").detail == (
        "Weights are GGUF. The part that reads pictures is mmproj-demo-vision-F16.gguf (812 MB). "
        "Left out: the other builds (Q4_K_M, F16), mmproj-demo-vision-BF16.gguf, .gitattributes "
        "and imatrix.gguf_file."
    )


def test_a_build_that_is_not_there_is_refused() -> None:
    with pytest.raises(ValueError) as caught:
        gguf_check(fixtures(), VISION, variant="Q5_K_M")
    assert str(caught.value) == "That build is not in this repository."


def test_only_weights_and_licence_text_are_planned_for_a_gguf_build() -> None:
    found = gguf_check(fixtures(), VISION)

    assert found.variant is not None
    # No settings or tokenizer file, no .gitattributes, and the importance
    # matrix (a `.gguf_file`, not a `.gguf`) stays behind (D9, P11). The
    # picture reader is the F16 one, not BF16 (D8).
    assert sorted(file.path for file in found.variant.files) == [
        "LICENSE",
        "README.md",
        "demo-vision-Q4_K_M.gguf",
        "mmproj-demo-vision-F16.gguf",
    ]
    assert row(found, "files").value == (
        "4 files to download: the model, the part that reads pictures, and its README and LICENSE"
    )


def test_the_suggested_build_is_the_smallest_when_none_fits_and_a_small_one_says_so() -> None:
    found = gguf_check(fixtures(), MANY)

    assert len(found.sized) == 21
    assert found.variant is not None and found.variant.id == "UD-IQ2_M"
    assert found.variant.note == "A very small build: it answers noticeably worse than a 4-bit one."
    assert row(found, "build") == Check(
        "build",
        "Build",
        "warn",
        "UD-IQ2_M — one of 21 in this repository",
        "None of the 21 fits the overnight budget; this is the smallest. A very small build: "
        f"it answers noticeably worse than a 4-bit one. {CHOOSE}",
    )
    # Twenty other builds are said as builds, five by name, not as twenty files.
    detail = row(found, "files").detail
    assert detail is not None
    assert "Left out: the other 20 builds (" in detail and " and 15 more)" in detail
    assert "mmproj-BF16.gguf, mmproj-F32.gguf" in detail
    assert detail.endswith(".gitattributes and imatrix_unsloth.gguf_file.")


def test_q4_k_m_is_suggested_once_a_budget_lets_it_fit() -> None:
    raised = LocalModelsConfig(converters=["mlx-community", "example-gguf"], night_budget_gb=30.0)

    found = gguf_check(fixtures(), MANY, config=raised)

    assert found.variant is not None and found.variant.id == "UD-Q4_K_M"
    assert found.fits == "night"
    assert row(found, "build").tone == "ok"
    assert row(found, "build").detail == (
        f"The suggested build; it fits the overnight budget only. {CHOOSE}"
    )
    assert found.summary == "This model can be added."


def test_with_no_overnight_window_the_build_row_blames_the_window_not_the_budget() -> None:
    # Builds of 22.5 to 41 GB; the overnight budget holds most, and the window never opens.
    shut = LocalModelsConfig(
        converters=["mlx-community", "example-gguf"],
        night_budget_gb=30.0,
        night_start="21:30",
        night_end="21:30",
    )

    found = gguf_check(fixtures(), MANY, config=shut)

    assert all(build.fits == "never" for build in found.sized)
    assert found.summary.endswith("until the daytime budget or the overnight window is changed.")
    assert row(found, "build").detail == (
        "None of the 21 fits the daytime budget, and there is no overnight window for those "
        "that fit the overnight one; this is the smallest. A very small build: it answers "
        f"noticeably worse than a 4-bit one. {CHOOSE}"
    )
    # Where some build fits the day, one chosen from between the budgets says the same of itself.
    some_by_day = LocalModelsConfig(
        converters=["mlx-community", "example-gguf"],
        day_budget_gb=23.0,
        night_budget_gb=30.0,
        night_start="21:30",
        night_end="21:30",
    )
    suggested = gguf_check(fixtures(), MANY, config=some_by_day).variant
    assert suggested is not None
    chosen = gguf_check(fixtures(), MANY, config=some_by_day, variant="UD-Q4_K_M")
    assert row(chosen, "build").tone == "warn"
    assert row(chosen, "build").detail == (
        f"Chosen instead of the suggested {suggested.id}; it fits the overnight budget, but there "
        f"is no overnight window. {CHOOSE}"
    )
    # With no window and a build too big even overnight, the budget is what is named.
    too_big = gguf_check(fixtures(), MANY, config=some_by_day, variant="UD-Q8_K_XL")
    assert row(too_big, "build").detail == (
        f"Chosen instead of the suggested {suggested.id}; it fits no budget. {CHOOSE}"
    )


def test_a_build_chosen_under_four_bits_is_a_warning_on_its_row() -> None:
    raised = LocalModelsConfig(converters=["mlx-community", "example-gguf"], night_budget_gb=30.0)

    found = gguf_check(fixtures(), MANY, config=raised, variant="UD-Q2_K_XL")

    assert row(found, "build").tone == "warn"
    assert row(found, "build").detail == (
        "Chosen instead of the suggested UD-Q4_K_M; it fits the overnight budget only. A very "
        f"small build: it answers noticeably worse than a 4-bit one. {CHOOSE}"
    )
    assert found.variant is not None and preflight.bits_of(found.variant) == 2


def test_the_same_repository_in_another_build_is_a_note_not_a_refusal() -> None:
    q8 = uuid.uuid4()
    revision = fake.fixture("gguf/demo-vision-gguf")["sha"]
    have_q8 = {"existing": lambda _repo: [Installed(q8, revision, "Q8_0")]}

    other = gguf_check(fixtures(), VISION, **have_q8)
    assert other.ok
    assert (other.existing, other.same_build) == (q8, False)
    assert other.notes == (
        "You already have the Q8_0 build of this model. This would download Q4_K_M beside it.",
    )

    same = gguf_check(fixtures(), VISION, variant="Q8_0", **have_q8)
    assert same.problems == ("This exact build is already installed.",)
    assert (same.existing, same.same_build) == (q8, True)


def test_a_gated_gguf_repository_is_tried_through_its_readme() -> None:
    site = fixtures()
    site.need_token(GATED_GGUF, accepts=[TOKEN])

    assert gguf_check(site, GATED_GGUF).access == "needs_token"
    assert gguf_check(site.with_token("some-other-token"), GATED_GGUF).access == "no_access"
    let_in = gguf_check(site.with_token(TOKEN), GATED_GGUF)
    assert let_in.ok and let_in.access == "ok"

    # It has no settings file: the start of its readme is what the token is tried on.
    read = {call[3] for call in site.calls if call[0] == "read_text"}
    assert read == {"README.md"}
    assert site.fetches == 0


def test_a_check_still_never_downloads_a_file() -> None:
    site = fixtures()
    site.need_token(GATED_GGUF)

    for name in fake.gguf_fixture_names():
        gguf_check(site, fake.fixture(name)["id"])

    assert site.fetches == 0
    assert {call[0] for call in site.calls} == {"info", "read_text"}
    read = {call[3] for call in site.calls if call[0] == "read_text"}
    # The settings files a runtime reads, and the readme a gated repository is tried on.
    assert read <= {*files.INSPECTED, "README.md"}
    assert "README.md" in read


def test_a_gguf_repository_tagged_as_needing_code_is_refused() -> None:
    site = fixtures()
    site.payloads[VISION]["tags"].append("custom_code")

    found = gguf_check(site, VISION)

    assert found.problems == (NEEDS_CODE,)
    # Refused, and still sized: what it would have cost is on the rows.
    assert [build.variant.id for build in found.sized] == ["Q4_K_M", "Q8_0", "F16"]


def test_a_repository_whose_model_files_are_all_left_out_has_no_model() -> None:
    site = fixtures(
        page(
            "google/two-of-one-name",
            siblings=(("README.md", 400), ("demo.gguf", 2 * GB), ("sub/DEMO.gguf", 2 * GB)),
            tags=("gguf",),
            library="gguf",
        )
    )

    found = gguf_check(site, "google/two-of-one-name")

    assert found.problems == ("No GGUF model file was found in it.",)
    assert (found.runtime, found.variant) == (None, None)
    assert gguf_check(fixtures(), PROJECTOR_ONLY).problems == (ONLY_PICTURES,)


def test_a_build_with_no_picture_reader_says_so_on_its_files_row() -> None:
    found = gguf_check(fixtures(), GATED_GGUF)

    assert row(found, "files") == Check(
        "files",
        "Files",
        "ok",
        "2 files to download: the model and its README",
        "Weights are GGUF. This build has no part that reads pictures. Left out: .gitattributes.",
    )
    assert found.takes == ("text",)
    # One build: no build row, nothing to choose.
    assert [item.key for item in found.checks].count("build") == 0


def test_a_build_that_fits_no_budget_says_so_in_the_first_line_whatever_runs_it() -> None:
    larger = check(fixtures(), LARGE, config=LocalModelsConfig(night_budget_gb=16.0))
    assert larger.summary == (
        "This model can be added, but it will not run until the overnight budget is raised."
    )
    no_window = check(
        fixtures(), LARGE, config=LocalModelsConfig(night_start="21:30", night_end="21:30")
    )
    assert no_window.summary == (
        "This model can be added, but it will not run until the daytime budget or the "
        "overnight window is changed."
    )
    # A refused model's first line is still its first problem.
    assert gguf_check(fixtures(), OMNI, config=LocalModelsConfig()).summary.startswith(
        "example-gguf is not one of your approved publishers."
    )


def test_each_file_of_a_build_is_named_for_what_it_is() -> None:
    assert preflight.part_of("demo-omni-30b-Q4_K_M.gguf") == "model"
    assert preflight.part_of("mmproj-demo-omni-30b-BF16.gguf") == "pictures"
    assert preflight.part_of("MMPROJ-F16.GGUF") == "pictures"
    assert preflight.part_of("README.md") == "text"
    assert preflight.part_of("LICENSE") == "text"
