# ADR-0018: A scoped mount helper, not a privileged application

- **Status**: Accepted
- **Date**: 2026-07-29
- **Supersedes**: the "read-only storage view" limitation noted in ADR-0011

## Context

Adding a NAS share to FrameFound meant editing `/etc/fstab` on the host over
SSH. That is a poor experience for the product's actual audience — a
photographer or a church AV volunteer who has storage to add and no wish to
learn fstab syntax — and it makes the app's own storage page a bystander to
the thing it is describing.

Mounting a filesystem requires `CAP_SYS_ADMIN`. That capability is close
enough to root that it is routinely described as "the new root": it permits
`mount`, and `mount` permits, among other things, bind-mounting the host root
into a container, mounting `proc` with unusual options, and overlaying the
application's own code.

The threat model forbids privileged containers "unless unavoidable", and the
API container is the least suitable process in the system to hold this: it
terminates untrusted requests from the internet, parses user input, and has
the largest attack surface of anything we ship. An RCE there would be
recoverable today and a host compromise tomorrow.

## Options considered

1. **Keep it manual.** Generate the fstab line, let the operator paste it.
   Safe, and shipped as stage 1 — but it does not answer the request, and an
   operator pasting a generated command as root is not obviously safer than a
   constrained helper doing one narrow thing.
2. **Give the API `CAP_SYS_ADMIN`.** Simple, and unacceptable. Every
   input-handling bug becomes a host compromise.
3. **A host-side agent** installed as a systemd unit. Good isolation, but it
   puts installation outside compose, which is the deployment story for the
   whole product.
4. **A scoped helper container** holding the capability and nothing else.
   Chosen.

## Decision

A separate `mounter` service, from the same image, holds `CAP_SYS_ADMIN` and
`DAC_READ_SEARCH` and drops every other capability. It exposes two operations
— mount and unmount — and no others.

**It is off by default.** It sits behind a compose profile, so an install that
never adds a drive from the UI never runs a privileged container:

```bash
docker compose --profile storage up -d
```

Constraints, each chosen against a specific failure:

| Constraint | Prevents |
|---|---|
| `cifs` and `nfs` only | `bind`/`overlay`/`proc` mounts, the container-escape primitives |
| Targets confined under `/mnt/media` or `/mnt/cache` | mounting over `/etc`, or over the app's own code |
| Mount options constructed, never accepted | `,rw` on a media share; `credentials=` aimed at an arbitrary file |
| argv only, never a shell | command injection through a share name |
| Credentials via a 0600 file | passwords visible in `ps` and `/proc/*/cmdline` |
| Media mounts always `ro` | a compromised container destroying originals |
| Internal network, no published port | reachability from outside the stack |
| Shared secret, constant-time compare | another container on the network driving it |
| Empty token refuses to start | a helper that accepts anything because nobody set a token |
| Admin-only API, audited | a viewer-role account adding storage |

Validation runs **twice** — in the API for a good error message, and again in
the helper because that is the side holding the capability. The helper does
not trust its caller.

Mount propagation is `rshared` on the helper's binds and `rslave` on every
consumer's, so a mount made in the helper appears on the host and in every
other container. Without it the mount would exist only in the helper's
namespace and nothing would be able to read it.

## Consequences

**Good.** The capability is isolated in ~150 lines that do one thing, and it
is absent entirely unless the operator opts in. Adding a drive is now a form.
The same validated `MountSpec` produces both the live mount and the fstab
line, so the "make it permanent" instructions cannot drift from what was
actually mounted.

**Bad.** There is now a privileged container in the compose file, and readers
will reasonably flinch at it. `apparmor:unconfined` is required for mounting
inside a container, which weakens a second layer. Mounts made this way are not
persistent — the UI surfaces the fstab line and says so plainly rather than
pretending otherwise, because writing to the host's `/etc/fstab` from a
container is a larger privilege than mounting and was deliberately not taken.

**Unresolved.** `SYS_ADMIN` remains coarse; Linux offers nothing finer for
mounting. A future option is a tiny setuid binary with a compiled-in
allowlist, or delegating to the host's systemd via a socket — both trade
compose-only deployment for a smaller blast radius. Revisit if the helper ever
needs to do more than these two operations. If it does, that is itself the
signal that this design has stopped being right.
