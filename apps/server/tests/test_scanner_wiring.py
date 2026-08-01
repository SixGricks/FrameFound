"""Every maintenance sweep is actually called by the loop.

This exists because the same bug happened five times in one feature: a
function written, unit-tested, deployed, and never invoked. Face clustering
was defined and had sixteen passing tests while `await _cluster_new_faces(db)`
appeared nowhere in the scanner, so the People page stayed empty and nothing
errored.

Unit tests cannot catch that — each unit worked. What catches it is asserting
the *seam*: that the periodic block names every sweep it is supposed to run.

Deliberately parsed from the source rather than executed. Running the loop
would mean standing up a database, a broker and a filesystem watcher to prove
a one-line fact, and the fragile part is the wiring, not the runtime.
"""

import ast
import inspect
from pathlib import Path

import pytest

from framefound.scanner import __main__ as scanner

# Every sweep the maintenance tick is responsible for. Adding one here without
# wiring it up is a failing test rather than a silent no-op.
REQUIRED_SWEEPS = (
    "_requeue_stuck_assets",
    "_requeue_missing_transcripts",
    "_cluster_new_faces",
    "_reap_orphaned_jobs",
    "_refresh_statistics",
)


def _main_source() -> str:
    return inspect.getsource(scanner.main)


def _called_names(source: str) -> set[str]:
    """Every function name called anywhere in the given source."""
    tree = ast.parse(source.strip())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.mark.parametrize("sweep", REQUIRED_SWEEPS)
def test_the_loop_calls_every_maintenance_sweep(sweep: str) -> None:
    assert sweep in _called_names(_main_source()), (
        f"{sweep} is defined but the scanner loop never calls it. "
        "A sweep that is not wired in is dead code that looks alive."
    )


@pytest.mark.parametrize("sweep", REQUIRED_SWEEPS)
def test_every_required_sweep_exists(sweep: str) -> None:
    assert hasattr(scanner, sweep), f"{sweep} is called but not defined"


def test_no_sweep_is_defined_and_forgotten() -> None:
    """The inverse check: a private `_requeue_*`/`_cluster_*` coroutine that
    nothing calls is almost certainly a wiring mistake, not intentional."""
    source = Path(inspect.getfile(scanner)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name.startswith(("_requeue_", "_cluster_"))
    }
    called = _called_names(source)
    orphans = defined - called
    assert not orphans, (
        f"defined but never called: {sorted(orphans)}. "
        "Wire it into the maintenance block or delete it."
    )


def test_the_periodic_block_is_guarded_by_an_interval() -> None:
    """The sweeps are expensive enough that running them every poll would be a
    different bug — a busy loop hammering the database every five seconds."""
    assert "last_requeue" in _main_source()


def test_a_failure_in_the_loop_is_logged_rather_than_fatal() -> None:
    """A scanner that dies on one bad iteration stops watching the library.
    The symptom of a silently blocked loop is no output at all, so the error
    path has to be loud."""
    source = _main_source()
    assert "scanner.loop_error" in source
