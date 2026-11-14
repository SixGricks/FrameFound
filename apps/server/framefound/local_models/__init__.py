"""Local AI models: the manager, the runtimes and the one way to ask a model.

The plan is docs/local-models.md. Everything that runs a model lives in this
package, and this package imports nothing outward: later phases (looking,
colour, assembly) call into it, never the other way round.

This file answers one question for all of them: can this install run models
at all? Models run on the Mac install only. The containers have no program
that could run one, so there every local-AI page says so in one sentence and
every write is refused with it, rather than queueing work nothing will do.
"""

from dataclasses import dataclass
from typing import Literal

from framefound.config import Settings, get_settings

# The two jobs a model can be given. Each is held by at most one model.
ROLES: tuple[str, ...] = ("looker", "editor")
# What a model must take to hold the role: the Looker is shown pictures, the
# Editor is given a shot list as text.
ROLE_NEEDS: dict[str, str] = {"looker": "image", "editor": "text"}
ROLE_TITLES: dict[str, str] = {"looker": "Looker", "editor": "Editor"}

UNAVAILABLE = (
    "Local AI models run on the Mac install of FrameFound. This server runs in "
    "containers, where the software that runs them does not exist. Everything else "
    "works as before."
)


@dataclass(frozen=True)
class Availability:
    """Whether this install can run models, and what to say when it cannot.

    `ready`: a real install with a runtimes folder. `demo`: a developer's
    review copy showing made-up models. `unavailable`: neither; `reason` is
    then the sentence to show, and nothing may be queued.
    """

    mode: Literal["ready", "demo", "unavailable"]
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.mode != "unavailable"


def availability(settings: Settings | None = None) -> Availability:
    """Decided by the settings alone, so every page and every job agree.

    Whether the machine is Apple silicon is deliberately not asked here: that
    is a runtime's own question, and the page should still load on an Intel
    Mac and say why its runtime cannot be installed.
    """
    settings = settings or get_settings()
    if settings.runtimes_dir is not None:
        return Availability("ready")
    # The demo shows invented models and answers. A machine that names a
    # runtimes folder or a public address is somebody's real install, and
    # must never be made to show them by one stray environment variable.
    if settings.local_ai_demo and not settings.domain:
        return Availability("demo")
    return Availability("unavailable", UNAVAILABLE)
