"""Tailnet awareness.

FrameFound runs in a container. When Tailscale runs on the host — the normal
arrangement, and the one that survives a container restart — the app cannot
see the host's Tailscale state at all: no socket, no interface, no daemon.

So it learns instead. When a request arrives from the CGNAT range Tailscale
hands out, the `Host` header on that request *is* the tailnet address the
operator used to get here. Recording it once gives the System page a real URL
to show, with no privilege, no daemon socket, and no configuration.

That has a pleasant property: the address is only ever shown after it has been
demonstrated to work, because it was learned from a request that worked. A URL
assembled from configuration can be wrong; this one cannot.
"""

import ipaddress
import re
from dataclasses import dataclass
from datetime import UTC, datetime

# Tailscale and Headscale both allocate from the CGNAT range.
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
# Tailscale's own IPv6 ULA prefix.
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")

# MagicDNS names, plus the bare short name inside a tailnet.
HOSTNAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9.-]{0,251}[a-zA-Z0-9])?$")


def is_tailnet_address(ip: str | None) -> bool:
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return address in TAILNET_V4 or address in TAILNET_V6


@dataclass(frozen=True)
class TailnetSighting:
    """A confirmed arrival over the tailnet."""

    host: str
    seen_at: str


def sighting_from_request(client_ip: str | None, host_header: str | None) -> TailnetSighting | None:
    """Record how someone reached us, when they came in over the tailnet.

    The Host header is attacker-controlled in general, which is why this is
    only trusted when the *source address* is already inside the tailnet range
    — an address that cannot be reached from the public internet without the
    operator having enrolled the device.
    """
    if not is_tailnet_address(client_ip) or not host_header:
        return None

    host = host_header.strip().lower()
    # Drop the port; keep the name. A bare IP is useless to display, and a
    # tailnet IP alone tells the operator nothing they did not already know.
    if host.startswith("["):  # bracketed IPv6 literal
        return None
    host = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if not host or not HOSTNAME_RE.match(host):
        return None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return None  # an address, not a name — nothing worth showing

    return TailnetSighting(host=host, seen_at=datetime.now(UTC).isoformat())


def tailnet_url(host: str, https: bool) -> str:
    """Tailscale issues real certificates for *.ts.net, so prefer https where
    the name suggests one is available."""
    scheme = "https" if https or host.endswith(".ts.net") else "http"
    return f"{scheme}://{host}"
