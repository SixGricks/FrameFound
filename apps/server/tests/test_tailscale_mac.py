"""The Security page's Tailscale card on a Mac.

The Mac app (Mac App Store or tailscale.com/download) has no socket at the
Linux path, so a Mac running it read "not installed" and was shown a Linux
installer with a uid-1000 operator. Pinned: the app is found and read
through its own command line, a Homebrew tailscaled is found at its Mac
socket, the answer says which kind of machine this is, and nothing asks a
Mac user to run Linux commands. Nothing here runs the real app.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from framefound.ddns import tailscale_local

RUNNING = {
    "BackendState": "Running",
    "Version": "1.104.5",
    "Self": {
        "HostName": "framefound",
        "DNSName": "framefound.tail1234.ts.net.",
        "TailscaleIPs": ["100.101.102.103"],
    },
    "CurrentTailnet": {"Name": "owner@example.com"},
}


@pytest.fixture()
def mac(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A Mac with neither the app nor a tailscaled socket, until a test adds one."""
    monkeypatch.setattr(tailscale_local, "host_platform", lambda: "macos")
    monkeypatch.setattr(tailscale_local, "SOCKET", tmp_path / "linux" / "tailscaled.sock")
    monkeypatch.setattr(tailscale_local, "MAC_SOCKET", tmp_path / "tailscaled.socket")
    monkeypatch.setattr(tailscale_local, "MAC_APP_CLI", tmp_path / "Tailscale.app" / "Tailscale")
    return tmp_path


def app_answers(
    monkeypatch: pytest.MonkeyPatch, mac: Path, answer: Callable[[Path], str]
) -> list[Path]:
    """Put the app in place, answering `status --json` with `answer`."""
    cli = mac / "Tailscale.app" / "Tailscale"
    cli.parent.mkdir(parents=True)
    cli.touch()
    asked: list[Path] = []

    def says(app: Path) -> str:
        asked.append(app)
        return answer(app)

    monkeypatch.setattr(tailscale_local, "_app_says", says)
    return asked


async def test_a_mac_with_nothing_installed_says_so_and_says_it_is_a_mac(mac: Path) -> None:
    found = await tailscale_local.status()
    assert found.state == tailscale_local.NOT_INSTALLED
    assert found.platform == "macos"


async def test_the_mac_app_is_read_through_its_own_command_line(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    asked = app_answers(monkeypatch, mac, lambda _app: json.dumps(RUNNING))
    found = await tailscale_local.status()
    assert asked == [mac / "Tailscale.app" / "Tailscale"]
    assert found.state == "connected" and found.via_app
    assert found.dns_name == "framefound.tail1234.ts.net"
    assert found.host_name == "framefound" and found.platform == "macos"


async def test_a_mac_app_that_does_not_answer_is_named_not_called_missing(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    def silent(_app: Path) -> str:
        raise tailscale_local.TailscaleUnavailable(tailscale_local.APP_NOT_ANSWERING)

    app_answers(monkeypatch, mac, silent)
    with pytest.raises(tailscale_local.TailscaleUnavailable, match="Tailscale app is on this Mac"):
        await tailscale_local.status()

    monkeypatch.setattr(tailscale_local, "_app_says", lambda _app: "not json")
    with pytest.raises(tailscale_local.TailscaleUnavailable, match="not answering"):
        await tailscale_local.status()


async def test_the_mac_app_is_signed_in_from_the_app_not_from_the_page(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    waiting = {"BackendState": "NeedsLogin", "Self": {"HostName": "framefound"}}
    app_answers(monkeypatch, mac, lambda _app: json.dumps(waiting))
    with pytest.raises(tailscale_local.TailscaleUnavailable, match="menu bar"):
        await tailscale_local.start_login()

    # A link the app already holds is handed on: it works from any browser.
    waiting["AuthURL"] = "https://login.tailscale.com/a/from-the-app"
    found = await tailscale_local.start_login()
    assert found.login_url.endswith("/from-the-app")


async def test_a_mac_app_that_is_turned_off_is_told_to_connect_not_to_log_in(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    # Signed in but switched off: "Turn Tailscale on" used to answer with the
    # sign-in instruction, under the card's own "installed but turned off".
    app_answers(monkeypatch, mac, lambda _app: json.dumps({**RUNNING, "BackendState": "Stopped"}))
    with pytest.raises(tailscale_local.TailscaleUnavailable) as refused:
        await tailscale_local.start_login()
    assert str(refused.value) == tailscale_local.APP_TURNS_ON
    assert "Connect" in str(refused.value) and "Log in" not in str(refused.value)


async def test_homebrew_tailscaled_is_found_at_its_mac_socket(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    (mac / "tailscaled.socket").touch()
    monkeypatch.setattr(
        tailscale_local,
        "_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=RUNNING))
        ),
    )
    found = await tailscale_local.status()
    assert found.state == "connected" and not found.via_app


async def test_a_refused_sign_in_on_a_mac_names_the_mac_user_not_uid_1000(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    (mac / "tailscaled.socket").touch()

    def daemon(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"BackendState": "NeedsLogin"})
        return httpx.Response(403, text="access denied")

    monkeypatch.setattr(
        tailscale_local,
        "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(daemon)),
    )
    with pytest.raises(tailscale_local.TailscaleUnavailable) as refused:
        await tailscale_local.start_login()
    assert "--operator=$USER" in str(refused.value)
    assert "1000" not in str(refused.value)


async def test_on_linux_the_mac_places_are_never_looked_at(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    monkeypatch.setattr(tailscale_local, "host_platform", lambda: "linux")
    (mac / "tailscaled.socket").touch()
    asked = app_answers(monkeypatch, mac, lambda _app: json.dumps(RUNNING))
    found = await tailscale_local.status()
    assert found.state == tailscale_local.NOT_INSTALLED and found.platform == "linux"
    assert asked == []


def test_the_app_is_asked_with_an_argument_list_and_a_failure_is_a_sentence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran: list[list[str]] = []

    def run(argv: list[Path | str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        ran.append([str(word) for word in argv])
        code = 0 if len(ran) == 1 else 1
        return subprocess.CompletedProcess(argv, code, stdout='{"BackendState": "Running"}')

    monkeypatch.setattr(tailscale_local.proc, "run", run)
    cli = tmp_path / "Tailscale"
    assert json.loads(tailscale_local._app_says(cli))["BackendState"] == "Running"
    assert ran == [[str(cli), "status", "--json"]]
    with pytest.raises(tailscale_local.TailscaleUnavailable, match="not answering"):
        tailscale_local._app_says(cli)


def test_a_mac_server_answers_with_the_mac_download_page_and_linux_keeps_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The download page the Security page's answer carries names the Mac
    too: the card also reads it, so an answer without `platform` still
    reaches the Mac branch."""
    from framefound.api.v1.remote_access import _tailscale_out

    assert tailscale_local.download_page("linux") == "https://tailscale.com/download"
    assert tailscale_local.download_page("macos") == tailscale_local.MAC_DOWNLOAD
    this_machine = tailscale_local.download_page(tailscale_local.host_platform())
    assert this_machine == tailscale_local.DOWNLOAD

    # As the module reads on a Mac, where DOWNLOAD is fixed at import.
    monkeypatch.setattr(tailscale_local, "DOWNLOAD", tailscale_local.download_page("macos"))
    answer = _tailscale_out(tailscale_local.TailscaleStatus(state="not_installed"))
    assert answer.download == "https://tailscale.com/download/mac"
    refused = _tailscale_out(tailscale_local.TailscaleStatus(state="unavailable"), "not answering")
    assert refused.download == "https://tailscale.com/download/mac"


async def test_the_security_page_is_told_it_is_a_mac_and_that_the_app_answered(
    monkeypatch: pytest.MonkeyPatch, mac: Path
) -> None:
    """The answer once dropped `platform`, so a Mac's card showed the Linux
    installer and command. Status read through the app says so too: signing
    in is done in the app, not from the page."""
    from framefound.api.v1.remote_access import tailscale_status

    app_answers(monkeypatch, mac, lambda _app: json.dumps(RUNNING))
    answer = (await tailscale_status(None)).model_dump()  # type: ignore[arg-type]
    assert (answer["state"], answer["platform"], answer["via_app"]) == ("connected", "macos", True)
    assert answer["dns_name"] == "framefound.tail1234.ts.net"

    # An app that does not answer is still a Mac's answer.
    def silent(_app: Path) -> str:
        raise tailscale_local.TailscaleUnavailable(tailscale_local.APP_NOT_ANSWERING)

    monkeypatch.setattr(tailscale_local, "_app_says", silent)
    refused = (await tailscale_status(None)).model_dump()  # type: ignore[arg-type]
    assert (refused["state"], refused["platform"], refused["via_app"]) == (
        "unavailable",
        "macos",
        False,
    )


async def test_a_linux_server_is_told_it_is_linux_and_read_through_its_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from framefound.api.v1.remote_access import tailscale_status

    monkeypatch.setattr(tailscale_local, "host_platform", lambda: "linux")
    monkeypatch.setattr(tailscale_local, "SOCKET", tmp_path / "tailscaled.sock")
    answer = (await tailscale_status(None)).model_dump()  # type: ignore[arg-type]
    assert (answer["state"], answer["platform"], answer["via_app"]) == (
        tailscale_local.NOT_INSTALLED,
        "linux",
        False,
    )
