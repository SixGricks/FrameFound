"""The one place the outside world is handed to the model manager.

Hugging Face, the package index, the model server, the programs that run
models, the installer, the clock: everything local AI reaches outside
FrameFound comes in through a `Deps`. The API's `get_deps` dependency and the
lane's tick each build one with `deps_for()`; a test swaps the whole set for
fakes in one place (`dependency_overrides[get_deps]`), so no test can reach a
network, a model or a child process by a path somebody forgot to replace.
"""

import platform
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from framefound.config import Settings, get_settings
from framefound.local_models import availability
from framefound.local_models.ask import ModelServer, server_for
from framefound.local_models.hub import FetchJson, HttpHub, Hub, fetch_json
from framefound.local_models.kinds import RuntimeKind, kinds
from framefound.local_models.runtimes import Runner
from framefound.native import proc


@dataclass(frozen=True)
class Deps:
    settings: Settings
    hub_for: Callable[[str | None], Hub]  # given the token
    fetch_json: FetchJson
    server: ModelServer
    kinds: Mapping[str, RuntimeKind]
    run: Runner = proc.run
    uv: str = "uv"
    sleep: Callable[[float], None] = time.sleep
    # The Mac's own clock, naive (D24): "overnight" is the wall clock's.
    now: Callable[[], datetime] = datetime.now
    system: str = ""  # platform.system()
    machine: str = ""  # platform.machine()


def _hub(token: str | None) -> Hub:
    return HttpHub(token)


def deps_for(settings: Settings | None = None) -> Deps:
    """The real outside world; in demo mode, the stand-ins (built by demo mode itself).

    On an install that cannot run models this still answers — with no kinds —
    so the page can draw itself and say why; every write is refused before
    anything here is used.
    """
    settings = settings or get_settings()
    if availability(settings).mode == "demo":
        # The demo's hub, runner and model server are put together in one
        # place (demo.py), imported here and nowhere else, so a real install
        # can never be handed them.
        from framefound.local_models import demo

        return demo.deps(settings)
    return Deps(
        settings=settings,
        hub_for=_hub,
        fetch_json=fetch_json,
        server=server_for(settings),
        kinds=kinds(settings),
        system=platform.system(),
        machine=platform.machine(),
    )
