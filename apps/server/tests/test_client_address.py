"""Spoof resistance for client-address resolution.

The public-access gate, rate limiter, and audit log all key off this value,
so a forged header must never be able to impersonate a LAN or tailnet client.
"""

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from framefound.auth.client_address import is_trusted_proxy, resolve_client_ip

DOCKER_BRIDGE = "172.16.0.0/12"


def make_request(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": Headers(headers or {}).raw,
        "client": (peer, 12345) if peer else None,
    }
    return Request(scope)


def test_direct_client_header_is_ignored() -> None:
    # The attack: a public client claims to be on the LAN.
    request = make_request("203.0.113.9", {"x-forwarded-for": "192.168.1.5"})
    assert resolve_client_ip(request, DOCKER_BRIDGE) == "203.0.113.9"


def test_trusted_proxy_header_is_honoured() -> None:
    request = make_request("172.18.0.5", {"x-forwarded-for": "203.0.113.9"})
    assert resolve_client_ip(request, DOCKER_BRIDGE) == "203.0.113.9"


def test_leftmost_entry_wins_through_a_proxy_chain() -> None:
    request = make_request("172.18.0.5", {"x-forwarded-for": "203.0.113.9, 10.0.0.1, 172.18.0.4"})
    assert resolve_client_ip(request, DOCKER_BRIDGE) == "203.0.113.9"


def test_x_real_ip_used_when_forwarded_for_absent() -> None:
    request = make_request("172.18.0.5", {"x-real-ip": "198.51.100.7"})
    assert resolve_client_ip(request, DOCKER_BRIDGE) == "198.51.100.7"


def test_no_trusted_proxies_configured_means_peer_only() -> None:
    request = make_request("172.18.0.5", {"x-forwarded-for": "192.168.1.5"})
    assert resolve_client_ip(request, "") == "172.18.0.5"


def test_missing_client_is_none() -> None:
    assert resolve_client_ip(make_request(None), DOCKER_BRIDGE) is None


def test_malformed_trusted_entry_does_not_disable_the_list() -> None:
    assert is_trusted_proxy("172.18.0.5", "not-a-cidr, 172.16.0.0/12")


@pytest.mark.parametrize("peer", ["not-an-ip", "", "999.999.999.999"])
def test_unparseable_peer_is_never_trusted(peer: str) -> None:
    assert not is_trusted_proxy(peer, DOCKER_BRIDGE)


def test_single_address_entry_supported() -> None:
    assert is_trusted_proxy("10.1.2.3", "10.1.2.3")
    assert not is_trusted_proxy("10.1.2.4", "10.1.2.3")
