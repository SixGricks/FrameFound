"""Learning the tailnet address from a request.

The Host header is attacker-controlled in general. The whole design rests on
only trusting it when the *source address* is already inside the CGNAT range
Tailscale allocates from — a range that cannot be reached from the public
internet without the operator having enrolled the device. These tests pin that
condition, because losing it would let a public visitor plant whatever address
they liked on the operator's System page.
"""

import pytest

from framefound.ddns.tailnet import is_tailnet_address, sighting_from_request, tailnet_url


@pytest.mark.parametrize("ip", ["100.64.0.1", "100.101.102.103", "100.127.255.254"])
def test_cgnat_addresses_are_tailnet(ip: str) -> None:
    assert is_tailnet_address(ip)


def test_tailscale_ipv6_range_is_recognised() -> None:
    assert is_tailnet_address("fd7a:115c:a1e0::1")


@pytest.mark.parametrize(
    "ip", ["192.168.1.5", "10.0.0.1", "172.16.0.1", "8.8.8.8", "127.0.0.1", "", "nonsense"]
)
def test_everything_else_is_not_tailnet(ip: str) -> None:
    assert not is_tailnet_address(ip)


def test_an_address_is_learned_from_a_tailnet_request() -> None:
    sighting = sighting_from_request("100.101.102.103", "framefound.tailnet-abc.ts.net")
    assert sighting is not None
    assert sighting.host == "framefound.tailnet-abc.ts.net"
    assert sighting.seen_at


def test_a_public_client_cannot_plant_an_address() -> None:
    """The attack this exists to prevent: a visitor from the internet sending
    a Host header and having it displayed as the operator's private URL."""
    assert sighting_from_request("8.8.8.8", "evil.example.com") is None


def test_a_lan_client_cannot_plant_an_address() -> None:
    assert sighting_from_request("192.168.1.20", "framefound.local") is None


def test_a_port_is_stripped_from_the_host() -> None:
    sighting = sighting_from_request("100.64.1.1", "framefound.tailnet.ts.net:8080")
    assert sighting is not None
    assert sighting.host == "framefound.tailnet.ts.net"


def test_a_bare_tailnet_ip_is_not_worth_recording() -> None:
    # Showing someone the IP they just typed in tells them nothing.
    assert sighting_from_request("100.64.1.1", "100.64.1.1") is None


def test_a_bracketed_ipv6_host_is_ignored() -> None:
    assert sighting_from_request("fd7a:115c:a1e0::1", "[fd7a:115c:a1e0::1]:443") is None


@pytest.mark.parametrize(
    "host",
    ["", "   ", "has space.ts.net", "-leading.ts.net", "with/slash", "with\nnewline"],
)
def test_malformed_hosts_are_refused(host: str) -> None:
    assert sighting_from_request("100.64.1.1", host) is None


def test_a_missing_host_header_is_handled() -> None:
    assert sighting_from_request("100.64.1.1", None) is None


def test_ts_net_names_get_https_because_tailscale_issues_real_certs() -> None:
    assert tailnet_url("framefound.tailnet.ts.net", https=False).startswith("https://")


def test_a_plain_name_over_http_stays_http() -> None:
    # Headscale or a custom name with no certificate: claiming https would
    # send the operator to a URL that fails.
    assert tailnet_url("framefound", https=False) == "http://framefound"


def test_https_is_honoured_when_the_request_was_already_secure() -> None:
    assert tailnet_url("framefound", https=True) == "https://framefound"
