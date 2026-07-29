"""Validated mount requests.

This module is the security boundary for mounting. Everything it accepts ends
up as arguments to `mount(8)` inside a container holding CAP_SYS_ADMIN, so a
value that slips through here is a root-level filesystem operation chosen by
whoever sent the request.

The rules, and why each one exists:

- **Two filesystem types, no more.** `cifs` and `nfs` are what a NAS speaks.
  Allowing arbitrary types would open `bind`, `overlay`, `proc` and friends,
  which are how a mount becomes a container escape.
- **Targets confined to known roots.** A mount at `/etc` or over the app's own
  code directory is game over. Paths are resolved and must sit *under* an
  allowed root, so `..` cannot climb out.
- **Options are constructed here, never accepted.** A free-text options string
  is the whole attack surface: `,bind`, `,ro` swapped for `,rw`, or an
  arbitrary `credentials=` path pointing at a secret. The caller chooses a
  *purpose*, and the purpose decides the options.
- **No shell, ever.** The result is an argv list. Nothing is interpolated into
  a command line.
- **Credentials never appear in argv.** `ps` is world-readable inside a
  container; a password on a command line is a password in every process
  listing. They go in a 0600 file that is deleted after the mount.

Media mounts are read-only without exception. FrameFound never writes to
originals, and mounting them writable would mean a compromised container
could destroy the thing the catalogue exists to describe.
"""

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# Where mounts are allowed to land. Both are bind-mounted into the containers
# with shared propagation, so a mount here becomes visible everywhere.
MEDIA_ROOT = PurePosixPath("/mnt/media")
CACHE_ROOT = PurePosixPath("/mnt/cache")
ALLOWED_ROOTS = (MEDIA_ROOT, CACHE_ROOT)

PROTOCOLS = ("cifs", "nfs")
PURPOSES = ("media", "cache")

# Hostname (RFC 1123) or IPv4. Deliberately no IPv6 literals: they contain
# colons, which are separators in both NFS sources and mount options, and
# nobody has asked for one.
HOST_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)
# Share and directory names. Spaces are common on NAS shares and are fine —
# argv carries them safely. Commas and newlines are not: a comma ends an
# option, and a newline could forge an fstab entry.
SHARE_RE = re.compile(r"^[^,\n\r\t\x00/\\]{1,255}(/[^,\n\r\t\x00/\\]{1,255})*$")
NAME_RE = re.compile(r"^[^,\n\r\t\x00/\\]{1,120}$")

# uid/gid the containers run as; the cache must be writable by that user.
RUN_AS_UID = 1000
RUN_AS_GID = 1000


class MountSpecError(ValueError):
    """A mount request that will not be attempted."""


@dataclass(frozen=True)
class MountSpec:
    protocol: str
    server: str
    share: str
    name: str
    purpose: str

    @property
    def read_only(self) -> bool:
        # Not a preference. Originals are never written by this application.
        return self.purpose == "media"

    @property
    def target(self) -> PurePosixPath:
        root = MEDIA_ROOT if self.purpose == "media" else CACHE_ROOT
        return root / self.name

    @property
    def source(self) -> str:
        return (
            f"//{self.server}/{self.share}"
            if self.protocol == "cifs"
            else f"{self.server}:/{self.share}"
        )

    def options(self, credentials_file: str | None = None) -> str:
        """Build the option string. Never accepts one from a caller."""
        access = "ro" if self.read_only else "rw"
        # nofail so a NAS that is down at boot cannot stop the host booting;
        # _netdev so the mount waits for networking.
        common = [access, "_netdev", "nofail"]
        if self.protocol == "cifs":
            opts = [
                *common,
                f"uid={RUN_AS_UID}",
                f"gid={RUN_AS_GID}",
                "iocharset=utf8",
                "vers=3.0",
            ]
            if credentials_file:
                opts.insert(0, f"credentials={credentials_file}")
            else:
                opts.insert(0, "guest")
            return ",".join(opts)
        # soft+timeo so a dead NFS server produces an error rather than an
        # unkillable process wedged in uninterruptible sleep.
        return ",".join([*common, "soft", "timeo=100", "retrans=2"])

    def argv(self, credentials_file: str | None = None) -> list[str]:
        return [
            "mount",
            "-t",
            self.protocol,
            "-o",
            self.options(credentials_file),
            self.source,
            str(self.target),
        ]

    def fstab_line(self, credentials_file: str = "/etc/framefound-smb.cred") -> str:
        opts = self.options(credentials_file if self.protocol == "cifs" else None)
        return f"{self.source}  {self.target}  {self.protocol}  {opts}  0 0"


def parse_mount_spec(
    *, protocol: str, server: str, share: str, name: str, purpose: str
) -> MountSpec:
    """Validate a request, or refuse it. There is no partial acceptance."""
    protocol = (protocol or "").strip().lower()
    if protocol not in PROTOCOLS:
        raise MountSpecError(f"Protocol must be one of {', '.join(PROTOCOLS)}")

    purpose = (purpose or "").strip().lower()
    if purpose not in PURPOSES:
        raise MountSpecError(f"Purpose must be one of {', '.join(PURPOSES)}")

    server = (server or "").strip()
    if not HOST_RE.match(server):
        raise MountSpecError("Server must be a hostname or IPv4 address")

    share = (share or "").strip().strip("/")
    if not SHARE_RE.match(share):
        raise MountSpecError("Share name contains characters that are not allowed")

    name = (name or "").strip().strip("/")
    if not NAME_RE.match(name) or name in (".", ".."):
        raise MountSpecError("Folder name contains characters that are not allowed")

    spec = MountSpec(protocol=protocol, server=server, share=share, name=name, purpose=purpose)
    # Belt and braces: the name regex already forbids separators, but the
    # target is what actually matters, so it is checked directly.
    assert_within_allowed_root(spec.target)
    return spec


def assert_within_allowed_root(target: PurePosixPath) -> None:
    """The resolved target must sit strictly under an allowed root."""
    resolved = PurePosixPath(Path(str(target)).as_posix())
    if ".." in resolved.parts or not resolved.is_absolute():
        raise MountSpecError("Mount point must be an absolute path with no '..'")
    for root in ALLOWED_ROOTS:
        if resolved.is_relative_to(root) and resolved != root:
            return
    allowed = " or ".join(str(r) for r in ALLOWED_ROOTS)
    raise MountSpecError(f"Mount point must be inside {allowed}")
