"""launchd, for the logged-in user's session.

Agents (not daemons): they start at login and run as the user, which is
where the NAS shares are mounted and where Adobe lives. A Mac that is logged
in and never sleeps is the deployment; running before login is deliberately
not attempted.

`launchctl` is driven through `proc.run`-shaped callables so tests can
record what would have been run.
"""

import os
import plistlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from framefound.native import proc, services
from framefound.native.layout import Layout

Runner = Callable[..., Any]


@dataclass(frozen=True)
class AgentState:
    loaded: bool
    state: str = "not loaded"  # running | waiting | not loaded | …
    pid: int | None = None
    last_exit: int | None = None

    @property
    def running(self) -> bool:
        return self.loaded and self.state == "running"


def parse_print(text: str) -> AgentState:
    """Read `launchctl print gui/<uid>/<label>` (top-level fields only)."""

    def top(key: str) -> str | None:
        found = re.search(rf"^\t{key} = (.+)$", text, re.MULTILINE)
        return found.group(1).strip() if found else None

    pid, last = top("pid"), top("last exit code")
    return AgentState(
        loaded=True,
        state=top("state") or "unknown",
        pid=int(pid) if pid and pid.isdigit() else None,
        last_exit=int(last) if last and last.lstrip("-").isdigit() else None,
    )


class Launchd:
    def __init__(
        self,
        layout: Layout,
        runner: Runner = proc.run,
        agents_dir: Path | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.layout = layout
        self._run = runner
        self._sleep = sleep
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"

    @property
    def domain(self) -> str:
        return f"gui/{os.getuid()}"  # type: ignore[attr-defined, unused-ignore]

    def plist_path(self, name: str) -> Path:
        return self.agents_dir / f"{services.label(name)}.plist"

    def write(self, service: services.Service) -> Path:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.plist_path(service.name)
        path.write_bytes(plistlib.dumps(services.agent(service, self.layout)))
        return path

    def state(self, name: str) -> AgentState:
        done = self._run(
            ["launchctl", "print", f"{self.domain}/{services.label(name)}"],
            capture=True,
            check=False,
        )
        if done.returncode != 0:
            return AgentState(loaded=False)
        return parse_print(done.stdout or "")

    def start(self, name: str) -> None:
        """Load the agent (RunAtLoad starts it). A no-op if already loaded."""
        if self.state(name).loaded:
            return
        self._run(["launchctl", "bootstrap", self.domain, str(self.plist_path(name))])

    def stop(self, name: str) -> None:
        """Unload the agent and wait until it is really gone: `bootout`
        can return while the process is still shutting down, and a
        `bootstrap` in that window fails with an unhelpful I/O error."""
        if not self.state(name).loaded:
            return
        self._run(["launchctl", "bootout", f"{self.domain}/{services.label(name)}"], check=False)
        deadline = time.monotonic() + services.BY_NAME[name].exit_timeout_s + 10
        while self.state(name).loaded and time.monotonic() < deadline:
            self._sleep(0.5)

    def restart(self, name: str) -> None:
        """Stop and start, re-reading the plist (kickstart would not)."""
        self.stop(name)
        self.start(name)

    def remove(self, name: str) -> None:
        self.stop(name)
        self.plist_path(name).unlink(missing_ok=True)
