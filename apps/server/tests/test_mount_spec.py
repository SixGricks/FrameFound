"""The mount validation boundary.

Everything accepted here becomes arguments to mount(8) inside a container
holding CAP_SYS_ADMIN. These tests are the record of what must never get
through, so each rejection case is written as the attack it prevents.
"""

import pytest

from framefound.storage.spec import MountSpecError, parse_mount_spec


def _spec(**overrides: str):
    args = {
        "protocol": "cifs",
        "server": "192.168.1.157",
        "share": "GELCO",
        "name": "gelco",
        "purpose": "media",
    }
    args.update(overrides)
    return parse_mount_spec(**args)  # type: ignore[arg-type]


def test_a_normal_smb_share_is_accepted() -> None:
    spec = _spec()
    assert spec.source == "//192.168.1.157/GELCO"
    assert str(spec.target) == "/mnt/media/gelco"
    assert spec.read_only


def test_nfs_builds_the_other_source_form() -> None:
    spec = _spec(protocol="nfs", server="nas.local", share="export/media")
    assert spec.source == "nas.local:/export/media"


def test_media_is_always_read_only() -> None:
    """Originals are never written. A writable media mount would let a
    compromised container destroy what the catalogue describes."""
    assert "ro" in _spec(purpose="media").options().split(",")
    assert "rw" not in _spec(purpose="media").options().split(",")


def test_cache_is_writable_and_lands_in_the_cache_root() -> None:
    spec = _spec(purpose="cache", name="previews")
    assert "rw" in spec.options().split(",")
    assert str(spec.target) == "/mnt/cache/previews"


@pytest.mark.parametrize("protocol", ["bind", "overlay", "proc", "tmpfs", "ext4", ""])
def test_only_network_filesystems_are_allowed(protocol: str) -> None:
    """bind and overlay are how a mount becomes a container escape."""
    with pytest.raises(MountSpecError):
        _spec(protocol=protocol)


@pytest.mark.parametrize(
    "name",
    ["../../etc", "..", ".", "a/b", "a\\b", "with,comma", "with\nnewline", "", "   "],
)
def test_folder_names_that_could_escape_or_forge_are_refused(name: str) -> None:
    with pytest.raises(MountSpecError):
        _spec(name=name)


@pytest.mark.parametrize(
    "server",
    ["", "not a host", "1.2.3.4,rw", "host\nname", "host/../x", "-leading-dash.com"],
)
def test_malformed_servers_are_refused(server: str) -> None:
    with pytest.raises(MountSpecError):
        _spec(server=server)


@pytest.mark.parametrize("share", ["", "has,comma", "has\nnewline", "back\\slash"])
def test_malformed_shares_are_refused(share: str) -> None:
    with pytest.raises(MountSpecError):
        _spec(share=share)


def test_shares_with_spaces_are_fine() -> None:
    # Common on a NAS, and argv carries them safely.
    assert _spec(share="Grick Family Storage").source.endswith("/Grick Family Storage")


def test_nested_share_paths_are_allowed() -> None:
    assert _spec(share="media/2026").source == "//192.168.1.157/media/2026"


def test_options_are_never_taken_from_input() -> None:
    """A comma in any field would otherwise inject an option — swapping ro for
    rw, or pointing credentials= at an arbitrary file."""
    spec = _spec(share="GELCO", name="gelco")
    options = spec.options()
    assert options.count("ro") >= 1
    for field in ("GELCO", "gelco", "192.168.1.157"):
        assert field not in options


def test_the_command_is_argv_with_no_shell() -> None:
    argv = _spec().argv()
    assert argv[0] == "mount"
    assert "-t" in argv and "cifs" in argv
    assert not any(";" in part or "|" in part or "&&" in part for part in argv)


def test_credentials_are_referenced_by_path_not_value() -> None:
    argv = _spec().argv("/run/ffcred-abc")
    joined = " ".join(argv)
    assert "credentials=/run/ffcred-abc" in joined
    assert "password" not in joined


def test_guest_is_used_when_no_credentials_file_is_supplied() -> None:
    assert "guest" in _spec().options().split(",")


def test_network_mounts_never_block_boot() -> None:
    """nofail and _netdev: a NAS that is down must not stop the host booting
    or hang the boot sequence waiting for a network that is not up yet."""
    options = _spec().options().split(",")
    assert "nofail" in options
    assert "_netdev" in options


def test_nfs_is_soft_mounted() -> None:
    """A hard NFS mount against a dead server produces processes stuck in
    uninterruptible sleep that cannot even be killed."""
    options = _spec(protocol="nfs").options().split(",")
    assert "soft" in options
    assert any(o.startswith("timeo=") for o in options)


def test_the_fstab_line_round_trips_the_same_options() -> None:
    line = _spec().fstab_line()
    assert line.startswith("//192.168.1.157/GELCO")
    assert "/mnt/media/gelco" in line
    assert line.endswith("0 0")


def test_cache_and_media_cannot_be_confused() -> None:
    media = _spec(purpose="media", name="shared")
    cache = _spec(purpose="cache", name="shared")
    assert media.target != cache.target
