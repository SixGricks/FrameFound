# Remote Access Architecture

Four supported modes, selected in **Settings → Remote Access** (wizard, M7).
All modes share one invariant: the app enforces authentication and signed URLs
itself — the network layer is defense in depth, never the only gate.

## Mode A — Tailscale / private overlay (recommended default)

```
Phone/Laptop ──(WireGuard, Tailscale)──► VM tailnet IP ──► Caddy :80 ──► app
```

- No open router ports, works behind CGNAT, access limited to enrolled devices.
- FrameFound ships **documentation + detection**, not credentials: the host (or a
  `tailscale/tailscale` sidecar the user configures) joins the tailnet; the app
  detects access via a CGNAT-range (100.64/10) client IP and shows the stable
  tailnet URL on the System Health page. Headscale documented for fully
  self-hosted; ZeroTier noted as alternative.
- HTTPS optional here (WireGuard already encrypts); Tailscale cert integration
  documented for browsers that complain.

### Setting it up

**On the host** (recommended — access survives the stack being down, which is
exactly when you want to reach it):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Install Tailscale on your phone or laptop, sign in with the same account, and
open FrameFound at `http://framefound` (or whatever you named the host).

**As a sidecar**, if installing on the host is awkward:

```bash
# .env: TAILSCALE_AUTHKEY=tskey-auth-...
docker compose --profile tailnet up -d
```

Userspace networking, so no `/dev/net/tun` or `NET_ADMIN` on the host. Slightly
lower throughput, which does not matter for a UI.

### How the address is discovered

FrameFound runs in a container and cannot see the host's Tailscale state — no
socket, no interface, no daemon. So it does not try to. Instead, when a request
arrives *from* the CGNAT range Tailscale allocates from (100.64/10, or
`fd7a:115c:a1e0::/48`), the `Host` header on that request is the address the
operator actually used, and it is recorded.

The Host header is attacker-controlled in general. It is trusted here only
because the source address is already inside a range that cannot be reached
from the public internet without the operator having enrolled the device — a
request from 8.8.8.8 claiming any hostname is ignored. `framefound/ddns/tailnet.py`,
and the tests are written as the attacks they prevent.

The pleasant consequence: the address on the Security page cannot be wrong,
because it is only ever shown after a connection to it has succeeded.

## Mode B — Public domain + DDNS + Caddy

```
Browser ──HTTPS 443──► router forward ──► Caddy (Let's Encrypt, HSTS, rate limits) ──► app
                                          ▲
        ddns sidecar ── public IP watch ──┘ (updates Cloudflare DNS record)
```

- Requires: user-owned domain, Cloudflare DNS (first adapter), router port
  forward 80/443 → VM.
- The `ddns` sidecar polls public IPv4/IPv6, updates the record only on
  change, logs updates, surfaces failures in System Health. **Scoped API token
  (Zone:DNS:Edit) only — the UI must refuse a global API key.** Additional
  providers (DuckDNS, No-IP, Dynu, RFC 2136) via the same adapter interface.
- Hardening checklist before wizard marks this mode "ready": HTTPS enforced,
  TOTP offered, rate limits on, audit log on, setup token consumed, no
  default credentials, certificate expiry monitored.

## Mode C — Cloudflare Tunnel

```
Browser ──► Cloudflare edge ──► outbound tunnel (cloudflared container) ──► Caddy ──► app
```

- No port forwarding; works behind CGNAT. Clearly disclosed trade-off: traffic
  transits Cloudflare (TLS terminates at their edge) — not fully local.
- Shipped as an optional compose profile (`--profile tunnel`) + token config.
  Never required.

## Mode D — Local only

- Default out of the box. Nothing listens beyond the LAN; wizard can be re-run
  anytime. A "disable public access" kill switch reverts B/C to D immediately.

## Wizard flow (M7)

1. Pick mode (cards A-D with plain-language pros/cons, A recommended).
2. Collect mode-specific config (domain + token for B; tunnel token for C).
3. Validate (token test call, DNS propagation check, reachability probe).
4. Template Caddyfile + compose profile; reload Caddy; verify; show result on
   System Health with ongoing status (cert expiry, DDNS last update, tunnel state).
