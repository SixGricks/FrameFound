"""Talking to the host's Tailscale, so the Security page can sign it in.

Tailscale runs on the host (or VM), not in the stack, so that access
survives the stack being down. Its daemon answers a small HTTP API on a
unix socket; compose mounts the socket's directory read-only into the api
container (`/var/run/tailscale`). Through it FrameFound can see whether
the host is on the tailnet, under what name, and — the reason this exists —
ask for a fresh sign-in link, so joining the tailnet is one button on the
Security page instead of a terminal session.

Reading status needs nothing. Asking for a sign-in link is a write, which
the daemon allows only to root or to the operator set with
`tailscale set --operator=<user>`; the container runs as uid 1000, so the
operator is the host account with that uid. Without it the button says
exactly that rather than failing vaguely.

Nothing here can take the host *off* the tailnet: there is no logout, no
`down`, no route or ACL change. The worst a caller can do is produce a
sign-in link, which is useless without the owner's Tailscale account.

On a Mac the usual Tailscale is the app (from the Mac App Store or
tailscale.com/download), which has no socket at the Linux path and reported
"not installed" while it was running. The app is read through the command
line inside it; a tailscaled from Homebrew is reached through its own
socket like on Linux.
"""

import asyncio
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx
import structlog

from framefound.native import proc

log = structlog.get_logger()

SOCKET = Path("/var/run/tailscale/tailscaled.sock")
# Where tailscaled puts its socket on a Mac (Homebrew's, or one built from
# source). The Mac app has none.
MAC_SOCKET = Path("/var/run/tailscaled.socket")
# The Tailscale command line that comes inside the Mac app, whichever store
# it came from. It finds the app's own way in by itself.
MAC_APP_CLI = Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale")
# The daemon refuses LocalAPI requests that do not name it as the host — a
# guard against browsers being tricked into talking to it.
LOCALAPI = "http://local-tailscaled.sock/localapi/v0"
TIMEOUT_S = 5.0
# How long to wait for the daemon to hand back a sign-in link.
LOGIN_WAIT_S = 12.0
LOGIN_POLL_S = 0.5

ADMIN_CONSOLE = "https://login.tailscale.com/admin/machines"
LINUX_DOWNLOAD = "https://tailscale.com/download"
MAC_DOWNLOAD = "https://tailscale.com/download/mac"

APP_NOT_ANSWERING = (
    "The Tailscale app is on this Mac but is not answering. Open it from Applications "
    "and make sure it is connected, then look again."
)
APP_SIGNS_IN = (
    "On a Mac, Tailscale is signed in from its own app: click the Tailscale icon in the "
    "menu bar and choose Log in."
)
# Signed in but turned off: the app's own switch, not its Log in.
APP_TURNS_ON = (
    "On a Mac, Tailscale is turned on from its own app: click the Tailscale icon in the "
    "menu bar and choose Connect."
)

# What the page shows, from the daemon's BackendState.
NOT_INSTALLED = "not_installed"
NEEDS_LOGIN = "needs_login"
NEEDS_APPROVAL = "needs_approval"
STOPPED = "stopped"
STARTING = "starting"
CONNECTED = "connected"

_STATES = {
    "NeedsLogin": NEEDS_LOGIN,
    "NeedsMachineAuth": NEEDS_APPROVAL,
    "Stopped": STOPPED,
    "Running": CONNECTED,
    "Starting": STARTING,
    "NoState": STARTING,
}


class TailscaleUnavailable(RuntimeError):
    """The daemon could not be reached, or refused. Message safe to show."""


def host_platform() -> str:
    """Which way of installing Tailscale fits the machine FrameFound runs
    on, "macos" or "linux", so the page can show that one."""
    return "macos" if sys.platform == "darwin" else "linux"


def download_page(platform: str) -> str:
    """Where to get Tailscale for this kind of machine, for installing it on
    the server itself (a phone or laptop wants LINUX_DOWNLOAD, the page for
    every device).

    The Security page's answer carries `platform` too; a server older than
    that field still sent this link, so the card falls back to it when
    `platform` is missing.
    """
    return MAC_DOWNLOAD if platform == "macos" else LINUX_DOWNLOAD


# Read by the Security page's API for every answer; the machine does not
# change while the server runs.
DOWNLOAD = download_page(host_platform())


@dataclass
class TailscaleStatus:
    state: str
    login_url: str = ""
    # framefound.tail1234.ts.net, without the trailing dot.
    dns_name: str = ""
    # The short MagicDNS name: framefound.
    host_name: str = ""
    tailnet: str = ""
    ips: list[str] = field(default_factory=list)
    version: str = ""
    # Looked up when made, so a test can say which kind of machine this is.
    platform: str = field(default_factory=lambda: host_platform())
    # Read through the Mac app's command line, which cannot be asked for a
    # sign-in link: signing in is done in the app.
    via_app: bool = False


def _socket() -> Path | None:
    """The daemon's socket, where this kind of host keeps it."""
    places = [SOCKET, MAC_SOCKET] if host_platform() == "macos" else [SOCKET]
    return next((place for place in places if place.exists()), None)


def _client() -> httpx.AsyncClient:
    """A client bound to the daemon's socket. Replaced in tests."""
    socket = _socket() or SOCKET
    return httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=str(socket)), timeout=TIMEOUT_S)


def socket_present() -> bool:
    return _socket() is not None


def mac_app() -> Path | None:
    """The Mac app's command line, when this is a Mac and the app is there."""
    if host_platform() != "macos" or not MAC_APP_CLI.exists():
        return None
    return MAC_APP_CLI


def _app_says(app: Path) -> str:
    """`status --json` from the Mac app's command line: the same answer the
    socket gives. Replaced in tests."""
    try:
        done = proc.run([app, "status", "--json"], capture=True, check=False, timeout=TIMEOUT_S)
    except proc.CommandFailed as err:
        raise TailscaleUnavailable(APP_NOT_ANSWERING) from err
    if done.returncode != 0:
        raise TailscaleUnavailable(APP_NOT_ANSWERING)
    return done.stdout


def parse_status(body: dict[str, Any]) -> TailscaleStatus:
    me = body.get("Self") or {}
    tailnet = body.get("CurrentTailnet") or {}
    dns_name = str(me.get("DNSName") or "").rstrip(".")
    return TailscaleStatus(
        state=_STATES.get(str(body.get("BackendState") or ""), STARTING),
        login_url=str(body.get("AuthURL") or ""),
        dns_name=dns_name,
        host_name=str(me.get("HostName") or dns_name.split(".", 1)[0]),
        tailnet=str(tailnet.get("Name") or ""),
        ips=[str(ip) for ip in (me.get("TailscaleIPs") or body.get("TailscaleIPs") or [])],
        version=str(body.get("Version") or ""),
    )


async def status() -> TailscaleStatus:
    """What the host's Tailscale is doing right now."""
    if not socket_present():
        app = mac_app()
        if app is None:
            return TailscaleStatus(state=NOT_INSTALLED)
        said = await asyncio.to_thread(_app_says, app)
        try:
            body = json.loads(said)
        except ValueError as err:
            raise TailscaleUnavailable(APP_NOT_ANSWERING) from err
        if not isinstance(body, dict):
            raise TailscaleUnavailable(APP_NOT_ANSWERING)
        return replace(parse_status(body), via_app=True)
    try:
        async with _client() as client:
            response = await client.get(f"{LOCALAPI}/status", params={"peers": "false"})
    except httpx.HTTPError as err:
        log.warning("tailscale.unreachable", error=type(err).__name__)
        raise TailscaleUnavailable(
            "Tailscale is installed but not answering — is tailscaled running?"
        ) from err
    if response.status_code != 200:
        raise TailscaleUnavailable(f"Tailscale answered {response.status_code}")
    return parse_status(response.json())


def _refused(response: httpx.Response) -> TailscaleUnavailable:
    if response.status_code == 403:
        if host_platform() == "macos":
            # The native install runs as the Mac's own user, not uid 1000
            # (on a Mac the first account is 501).
            return TailscaleUnavailable(
                "Tailscale won't let FrameFound start a sign-in. In Terminal on this Mac, "
                "run once: sudo tailscale set --operator=$USER"
            )
        return TailscaleUnavailable(
            "Tailscale won't let FrameFound start a sign-in. On the server, run once: "
            "sudo tailscale set --operator=$(id -un 1000)"
        )
    return TailscaleUnavailable(f"Tailscale answered {response.status_code}")


async def start_login() -> TailscaleStatus:
    """Turn Tailscale on and get a sign-in link if it needs one.

    Returns the status afterwards: `needs_login` with `login_url` set (open
    it, sign in), or `connected` when turning it back on was all it needed.
    """
    current = await status()
    if current.state == NOT_INSTALLED:
        raise TailscaleUnavailable("Tailscale is not installed on this server")
    if current.state in (CONNECTED, NEEDS_APPROVAL):
        return current
    if current.state == NEEDS_LOGIN and current.login_url:
        return current  # a link is already waiting
    if current.via_app:
        # The app's command line has no way to ask for a link and wait for
        # it without holding a terminal open. Turned off, it is already
        # signed in and only needs switching on.
        raise TailscaleUnavailable(APP_TURNS_ON if current.state == STOPPED else APP_SIGNS_IN)

    async with _client() as client:
        # Wanting to run is what makes a finished sign-in connect; a host
        # brought up with `tailscale up` already wants to.
        prefs = await client.patch(
            f"{LOCALAPI}/prefs",
            json={"WantRunningSet": True, "WantRunning": True},
        )
        if prefs.status_code >= 400:
            raise _refused(prefs)
        if current.state != STOPPED:
            login = await client.post(f"{LOCALAPI}/login-interactive")
            if login.status_code >= 400:
                raise _refused(login)

    waited = 0.0
    while waited < LOGIN_WAIT_S:
        await asyncio.sleep(LOGIN_POLL_S)
        waited += LOGIN_POLL_S
        current = await status()
        if current.login_url or current.state in (CONNECTED, NEEDS_APPROVAL):
            break
    log.info("tailscale.login_started", state=current.state, has_link=bool(current.login_url))
    return current
