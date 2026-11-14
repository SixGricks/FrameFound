"""The seed's own process knows every kind of job before it runs the lane.

`infrastructure/scripts/seed_dev.py` runs the local-AI lane itself (the
demo's models on an empty catalogue, Compare's two with `--compare`). The
lane ends a queued job whose kind its process has not loaded as failed,
"This version of FrameFound does not know how to do this.", and a review
catalogue holds a reviewer's queued work: a draft waiting for the night, a
look, a review of a folder. The seed loaded only the model manager's own
handlers, so every one of those was unknown to it.

What a process has loaded cannot be seen from inside the test run, where
every module is loaded already; so this asks a fresh interpreter.
"""

import subprocess
import sys
from pathlib import Path

from framefound.local_models import jobs

SERVER = Path(__file__).resolve().parents[1]
SEED = SERVER.parents[1] / "infrastructure" / "scripts" / "seed_dev.py"

# As the seed starts: the demo module, and nothing that loads a handler.
KINDS_KNOWN = """
from framefound.local_models import demo, jobs

print("before=" + ",".join(sorted(jobs.registered())))
demo.know_every_kind()
print("after=" + ",".join(sorted(jobs.registered())))
"""

# What a reviewer can have waiting on a review catalogue, beside the model
# manager's own five.
A_REVIEWERS = {
    "clip_look",
    "listing_look",
    "draft_cut",
    "look_folder",
    "look_ingest",
    "look_prepare",
}
THE_MANAGERS = {"download", "place", "runtime_install", "test", "compare_model"}


def test_a_process_about_to_run_the_lane_knows_every_kind_the_worker_does() -> None:
    # A fixed program of our own, run with this interpreter.
    done = subprocess.run(  # noqa: S603 - no outside input in the arguments
        [sys.executable, "-c", KINDS_KNOWN],
        cwd=SERVER,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    said = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    # Loading the demo alone registers none of a reviewer's kinds: the call is what does.
    assert not A_REVIEWERS & set(said["before"].split(","))
    known = set(said["after"].split(","))
    assert known >= A_REVIEWERS | THE_MANAGERS
    # And it is the worker's and the API's own list, not one kept beside it:
    # a kind a later phase adds there is known to the seed too.
    from framefound.processing import local_ai_tasks  # noqa: F401 - loads what the worker loads

    assert known <= set(jobs.registered())


def test_both_places_the_seed_runs_the_lane_ask_for_every_kind_first() -> None:
    """The demo's seed and the script's Compare part each run `jobs.run_next`;
    each must have called `know_every_kind` in the same function first."""
    from framefound.local_models import demo

    for path, function in (
        (Path(demo.__file__), "async def seed("),
        (SEED, "async def _install("),
    ):
        text = path.read_text(encoding="utf-8")
        body = text[text.index(function) :]
        asked, ran = body.find("know_every_kind()"), body.find("jobs.run_next(")
        assert 0 <= asked < ran, f"{path.name} {function} runs the lane before it knows every kind"
