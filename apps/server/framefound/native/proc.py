"""Running other programs: argv lists only, never a shell.

One place, so the rest of the package states *what* to run as data that
tests can read, and so nothing here can be turned into shell injection by a
path with a space or a password with a quote.
"""

import socket
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any


class CommandFailed(RuntimeError):
    """A command exited non-zero. The message is safe to show the operator:
    it carries the program's name and its last lines, not its environment."""


def run(
    argv: Sequence[str | Path],
    *,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
    stdout: IO[Any] | int | None = None,
    stdin_text: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `argv` and wait. With `capture`, stdout and stderr are returned;
    otherwise they go to the terminal (progress bars, prompts)."""
    words = [str(word) for word in argv]
    try:
        done = subprocess.run(  # noqa: S603 - argv list, no shell
            words,
            env=dict(env) if env is not None else None,
            cwd=cwd,
            stdout=subprocess.PIPE if capture else stdout,
            stderr=subprocess.PIPE if capture else None,
            input=stdin_text,
            text=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as err:
        raise CommandFailed(f"{words[0]} is not installed or not on PATH") from err
    except subprocess.TimeoutExpired as err:
        raise CommandFailed(f"{words[0]} did not finish within {timeout:.0f} s") from err
    if check and done.returncode != 0:
        tail = "\n".join((done.stderr or "").strip().splitlines()[-8:]) if capture else ""
        detail = f":\n{tail}" if tail else ""
        raise CommandFailed(f"{words[0]} failed (exit {done.returncode}){detail}")
    return done


def spawn(
    argv: Sequence[str | Path],
    *,
    env: Mapping[str, str],
    cwd: Path | None = None,
    stdout: IO[Any] | int | None = None,
    stderr: IO[Any] | int | None = None,
) -> subprocess.Popen[bytes]:
    """Start `argv` and return at once; the caller waits, stops and reads it.

    `env` is required and is the child's WHOLE environment: nothing of this
    process's own is added. The one caller is the model server, which runs
    with framefound.env loaded (database and queue passwords, the sealing
    key); a default of "inherit" would hand all of it to a program running a
    model somebody else made. Build `env` with `kinds.child_env`.

    The child reads nothing from a terminal, and stays in this process's
    group, so launchd stopping the service takes the child with it.
    """
    words = [str(word) for word in argv]
    if cwd is not None and not cwd.is_dir():
        raise CommandFailed(f"{cwd} is not a folder, so {words[0]} could not be started")
    try:
        return subprocess.Popen(  # noqa: S603 - argv list, no shell
            words,
            env=dict(env),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
        )
    except FileNotFoundError as err:
        raise CommandFailed(f"{words[0]} is not installed or not on PATH") from err


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_port(port: int, seconds: float, host: str = "127.0.0.1") -> bool:
    """True once something accepts connections on `port`, False on timeout."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_open(port, host):
            return True
        time.sleep(0.5)
    return port_open(port, host)
