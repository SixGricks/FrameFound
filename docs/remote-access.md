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

**On the VM that runs FrameFound** (recommended — access survives the stack
being down, which is exactly when you want to reach it):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname framefound
```

Then let FrameFound sign it in for you from the Security page:

```bash
sudo tailscale set --operator=$(id -un 1000)
```

Compose mounts `/var/run/tailscale` read-only into the api container, so the
Security page shows whether the server is on the tailnet and offers a single
**Sign in to Tailscale** button that opens the sign-in page. (The operator
setting is what lets the app — uid 1000 in its container — ask for a sign-in
link; status needs nothing.) Or, without the page: the first `tailscale up`
prints a login link; open it on any device signed in
to your Tailscale account. Then install Tailscale on your phone or laptop,
sign in with the same account, and open `http://framefound:8080` (the name
you gave the host; MagicDNS resolves it anywhere on the tailnet). The
Security page records that address the first time you arrive on it and shows
it with a Copy button from then on — the address shown is one that has
actually worked.

Optional, and worth it: **HTTPS with a real certificate, no port needed.**
In the Tailscale admin console turn on *MagicDNS* and *HTTPS certificates*
(DNS → HTTPS Certificates), then on the VM:

```bash
sudo tailscale serve --bg 8080
```

That publishes FrameFound to the tailnet only, as
`https://framefound.<your-tailnet>.ts.net`, with a certificate browsers
accept. The app still sees your tailnet address (`tailscale serve` forwards
it, and Caddy passes it on), so the Security page keeps saying "you are on
the tailnet". Reversible with `sudo tailscale serve reset`.

**On the Proxmox host instead, as a subnet router**, when you also want the
Proxmox console and the NAS from outside and would rather enrol one machine
than three:

```bash
# on the Proxmox host, as root
curl -fsSL https://tailscale.com/install.sh | sh
echo 'net.ipv4.ip_forward = 1' > /etc/sysctl.d/99-tailscale.conf
sysctl -p /etc/sysctl.d/99-tailscale.conf
tailscale up --hostname proxmox --advertise-routes=192.168.1.0/24   # your LAN
```

Approve the route in the admin console (Machines → proxmox → Edit route
settings). From then on every LAN address — the FrameFound VM, the NAS, the
Proxmox web UI — is reachable from any enrolled device by its ordinary IP.
Two things to know: connections arrive at FrameFound from the router's LAN
address rather than a tailnet one, so the Security page will say "your local
network" and will not learn a tailnet address; and the Proxmox host is a
machine you otherwise keep as plain as possible. Running Tailscale on the VM
is the simpler of the two, and nothing stops you doing both.

**As a sidecar**, if installing on either host is awkward:

```bash
# .env: TAILSCALE_AUTHKEY=tskey-auth-...   (and COMPOSE_PROFILES=tailnet to make it stick)
docker compose --profile tailnet up -d
```

**Claude from outside the office.** The MCP connector
([mcp.md](mcp.md)) is served under the same address: give Claude Code or
Claude Desktop `http://framefound:8080/api/mcp` (or the `https://….ts.net`
one) and a panel token, and it can search and tag from wherever you are. The
Security page's Tailscale card shows that address once the tailnet one is
known.

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
- **Point the tunnel's public hostname at `http://caddy:8081`, not `:80`.**
  8081 is the tunnel's own entrance, reachable only from the cloudflared
  container: it marks each request as having come through the tunnel and
  carries Cloudflare's `Cf-Connecting-Ip` to the API. Through `:80` every
  tunnel visitor looked like a LAN client — Caddy rewrites `X-Forwarded-For`
  to cloudflared's private address — so the *disable public access* switch,
  the docs lock and the per-address login limiter did nothing for them
  (found in the 2026-11 security review). Every other site strips the marker,
  so it cannot be sent from outside.

## Mode D — Local only

- Default out of the box. Nothing listens beyond the LAN; wizard can be re-run
  anytime. A "disable public access" kill switch reverts B/C to D immediately.

## Wizard flow (M7)

1. Pick mode (cards A-D with plain-language pros/cons, A recommended).
2. Collect mode-specific config (domain + token for B; tunnel token for C).
3. Validate (token test call, DNS propagation check, reachability probe).
4. Template Caddyfile + compose profile; reload Caddy; verify; show result on
   System Health with ongoing status (cert expiry, DDNS last update, tunnel state).
