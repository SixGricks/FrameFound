"""Determining the real client address behind a reverse proxy.

This is security-critical, not cosmetic. The public-access gate, the login
rate limiter, and the audit log all key off the client address. If
`X-Forwarded-For` were trusted unconditionally, anyone could send
`X-Forwarded-For: 192.168.1.5` and be treated as a LAN client — walking
straight past the kill switch and diluting rate limiting across forged
addresses.

So a forwarded header is honoured only when the *direct peer* is a proxy the
operator configured. Everything else uses the socket address, which cannot be
spoofed on an established TCP connection.
"""

import ipaddress
from functools import lru_cache

from starlette.requests import Request

FORWARDED_FOR = "x-forwarded-for"
REAL_IP = "x-real-ip"


@lru_cache(maxsize=1)
def _trusted_networks(raw: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks = []
    for entry in raw.split(","):
        candidate = entry.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            continue  # a malformed entry must not disable the whole list
    return tuple(networks)


def is_trusted_proxy(peer: str | None, trusted: str) -> bool:
    if not peer or not trusted:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in network for network in _trusted_networks(trusted))


def resolve_client_ip(request: Request, trusted: str) -> str | None:
    """The client address, honouring forwarding headers only from proxies we
    configured. `trusted` is a comma-separated list of CIDRs or addresses."""
    peer = request.client.host if request.client else None
    if not is_trusted_proxy(peer, trusted):
        return peer

    forwarded = request.headers.get(FORWARDED_FOR)
    if forwarded:
        # Walk from the right, past our own proxies. Each entry was appended
        # by the hop to its right, so the first one no trusted proxy added is
        # the nearest address anyone we trust can vouch for. The left end is
        # whatever the client typed: Caddy happens to discard a client's
        # header today, but put any appending proxy in front (a CDN, a
        # tunnel) and a request claiming "127.0.0.1" would have walked past
        # the kill switch and the login limiter if the left end were trusted.
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        for hop in reversed(hops):
            if not is_trusted_proxy(hop, trusted):
                return hop
        if hops:
            return hops[0]  # every hop is one of ours: the request began inside
    real_ip = request.headers.get(REAL_IP)
    return real_ip.strip() if real_ip else peer
