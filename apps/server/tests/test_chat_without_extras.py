"""The chat panel on an install that has neither of its optional packages.

The connector's tools (`mcp`) and the Anthropic SDK (`anthropic`) are
extras. Without them the panel must say so, in a sentence, and everything
else must go on working: the chat routes are part of the one router every
install loads, so a single `import anthropic` at the top of a chat module
would stop the whole API from starting, on exactly the installs the other
chat tests never run on. Those tests skip when an extra is missing, and
where both are installed they cannot see the difference.

So this file needs neither package. It asks a fresh interpreter, with both
names made unimportable, to load the API's router and say what the panel
reports. A fresh one because this one may already hold the real packages
from another test file.
"""

import subprocess
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]

WITHOUT_THE_EXTRAS = """
import sys
from importlib.abc import MetaPathFinder


class NotInstalled(MetaPathFinder):
    # Says the two extras are missing, whatever this machine really has.
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in ("mcp", "anthropic"):
            raise ModuleNotFoundError("No module named " + repr(name), name=name)
        return None


sys.meta_path.insert(0, NotInstalled())

import framefound.api.v1.router  # every route the API has, chat included
from framefound.chat import claude_stream, engine, tools

held = sorted(m for m in sys.modules if m.split(".")[0] in ("mcp", "anthropic"))
print("held=" + ",".join(held))
print("tools=" + str(tools.problem()))
print("sdk=" + str(claude_stream.problem()))
print("panel=" + str(engine.unavailable()))
print("models=" + ",".join(engine.models()))
"""


def test_without_the_extras_the_api_still_loads_and_the_panel_says_what_is_missing() -> None:
    # A fixed program of our own, run with this interpreter.
    done = subprocess.run(  # noqa: S603 - no outside input in the arguments
        [sys.executable, "-c", WITHOUT_THE_EXTRAS],
        cwd=SERVER,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    said = dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    assert said["held"] == "", "nothing reached for either package just to load the routes"
    assert "connector's tools" in said["tools"] and "`mcp` extra" in said["tools"]
    assert "Anthropic SDK" in said["sdk"] and "`chat` extra" in said["sdk"]
    # GET /chat/status shows this sentence with `available: false`.
    assert said["panel"] == said["tools"]
    # The settings page can still list the models: that needs neither.
    assert "claude-opus-5-5" in said["models"].split(",")
