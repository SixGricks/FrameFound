"""Workers on another machine: path mapping, the data-store guard, the
encoder choice and the readiness check (docs/remote-workers.md).

The task shell's behaviour with these settings is in test_jobs.py."""

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from framefound.config import Settings, get_settings
from framefound.ops import worker_check
from framefound.processing import ffmpeg
from framefound.storage import worker_roots
from framefound.storage.worker_roots import (
    DATA_STORE_MARKER,
    UnmappedRoot,
    data_store_problem,
    local_root,
    parse_root_map,
    write_marker,
)


def settings(**values: str) -> Settings:
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- parse_root_map --------------------------------------------------------


def test_root_map_parses_pairs_longest_server_prefix_first(tmp_path: Path) -> None:
    raw = f"/media/intel={tmp_path / 'intel'}; /media/intel/2026={tmp_path / 'fast'}\n"
    mappings = parse_root_map(raw)
    assert [m.server for m in mappings] == [
        PurePosixPath("/media/intel/2026"),
        PurePosixPath("/media/intel"),
    ]
    assert mappings[1].local == tmp_path / "intel"


def test_an_empty_root_map_is_no_mapping() -> None:
    assert parse_root_map("") == []
    assert parse_root_map(" ; \n ") == []


@pytest.mark.parametrize(
    "raw",
    [
        "/media/intel",  # no "="
        "=/Volumes/intel",  # no server side
        "/media/intel=",  # no local side
        "media/intel=/Volumes/intel",  # relative server path
        "/media/intel=Volumes/intel",  # relative local path
    ],
)
def test_a_malformed_root_map_entry_is_refused(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_root_map(raw)


# --- local_root ----------------------------------------------------------------


def test_on_the_server_a_root_is_its_own_path() -> None:
    assert local_root("/media/intel/2026", settings()) == Path("/media/intel/2026")


def test_a_root_under_a_mapped_share_is_translated(tmp_path: Path) -> None:
    mapped = settings(root_map=f"/media/intel={tmp_path / 'intel'}")
    assert local_root("/media/intel", mapped) == tmp_path / "intel"
    assert local_root("/media/intel/2026/GELCO", mapped) == tmp_path / "intel" / "2026" / "GELCO"


def test_mapping_matches_whole_folders_not_name_prefixes(tmp_path: Path) -> None:
    mapped = settings(root_map=f"/media/intel={tmp_path / 'intel'}")
    with pytest.raises(UnmappedRoot):
        local_root("/media/intel-archive/2019", mapped)


def test_the_most_specific_mapping_wins(tmp_path: Path) -> None:
    mapped = settings(
        root_map=f"/media/intel={tmp_path / 'slow'};/media/intel/2026={tmp_path / 'fast'}"
    )
    assert local_root("/media/intel/2026/x", mapped) == tmp_path / "fast" / "x"
    assert local_root("/media/intel/2025", mapped) == tmp_path / "slow" / "2025"


def test_a_remote_worker_never_falls_back_to_the_server_path(tmp_path: Path) -> None:
    mapped = settings(root_map=f"/media/intel={tmp_path}")
    with pytest.raises(UnmappedRoot, match="root_map"):
        local_root("/media/breeze", mapped)


# --- the data-store guard ---------------------------------------------------


def test_the_server_never_checks_for_the_marker(tmp_path: Path) -> None:
    assert data_store_problem(tmp_path, settings()) is None


def test_a_remote_worker_needs_the_marker(tmp_path: Path) -> None:
    remote = settings(root_map=f"/media/intel={tmp_path}")
    problem = data_store_problem(tmp_path, remote)
    assert problem is not None and "not mounted" in problem
    write_marker(tmp_path)
    assert data_store_problem(tmp_path, remote) is None


def test_writing_the_marker_twice_keeps_one_file(tmp_path: Path) -> None:
    write_marker(tmp_path)
    write_marker(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == [DATA_STORE_MARKER]


def test_a_missing_store_does_not_stop_the_api(tmp_path: Path) -> None:
    write_marker(tmp_path / "not-mounted")  # must not raise


def test_the_api_writes_the_marker_on_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from fastapi.testclient import TestClient

    from framefound.main import create_app

    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    with TestClient(create_app()):
        pass
    assert (tmp_path / DATA_STORE_MARKER).is_file()


# --- proxy encoder choice -----------------------------------------------------


def _capture_proxy_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(ffmpeg, "_run", lambda argv, timeout: calls.append(argv))
    return calls


def test_proxies_stay_on_x264_until_media_engine_settings_are_chosen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_proxy_argv(monkeypatch)
    monkeypatch.setattr(ffmpeg, "nvenc_available", lambda: False)
    monkeypatch.setattr(ffmpeg, "videotoolbox_available", lambda: True)
    monkeypatch.setattr(ffmpeg, "videotoolbox_proxy_args", lambda height: None)

    assert ffmpeg.transcode_proxy(tmp_path / "a.mov", tmp_path / "a.mp4") == "libx264"
    assert "libx264" in calls[0]


def test_proxies_use_the_media_engine_when_it_is_set_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _capture_proxy_argv(monkeypatch)
    monkeypatch.setattr(ffmpeg, "nvenc_available", lambda: False)
    monkeypatch.setattr(ffmpeg, "videotoolbox_available", lambda: True)
    monkeypatch.setattr(
        ffmpeg, "videotoolbox_proxy_args", lambda height: ["-c:v", "h264_videotoolbox"]
    )

    assert ffmpeg.transcode_proxy(tmp_path / "a.mov", tmp_path / "a.mp4") == "h264_videotoolbox"
    assert "h264_videotoolbox" in calls[0]
    assert "libx264" not in calls[0]


def test_videotoolbox_is_never_probed_off_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ffmpeg.sys, "platform", "linux")
    monkeypatch.setattr(ffmpeg, "_encoder_works", lambda codec: pytest.fail("probed"))
    ffmpeg.videotoolbox_available.cache_clear()
    try:
        assert ffmpeg.videotoolbox_available() is False
    finally:
        ffmpeg.videotoolbox_available.cache_clear()


# --- the readiness check ------------------------------------------------------


def test_the_check_refuses_an_empty_root_map() -> None:
    checks = worker_check.check_settings(settings())
    assert any(c.name == "root map" and not c.ok and c.fatal for c in checks)


def test_the_check_names_a_malformed_root_map() -> None:
    (check,) = worker_check.check_settings(settings(root_map="/media/intel"))
    assert not check.ok and "server path=local path" in check.detail


def test_the_check_reports_each_library_through_the_map(tmp_path: Path) -> None:
    (tmp_path / "intel").mkdir()
    (tmp_path / "intel" / "clip.mov").write_bytes(b"x")
    mapped = settings(root_map=f"/media/intel={tmp_path / 'intel'}")

    reachable = worker_check._library_check("Intel", "/media/intel", mapped)
    assert reachable.ok
    unmapped = worker_check._library_check("Breeze", "/media/breeze", mapped)
    assert not unmapped.ok and "root_map" in unmapped.detail
    (tmp_path / "empty").mkdir()
    absent = worker_check._library_check(
        "Empty", "/media/empty", settings(root_map=f"/media/empty={tmp_path / 'gone'}")
    )
    assert not absent.ok and "absent" in absent.detail


def test_the_check_wants_a_writable_marked_store(tmp_path: Path) -> None:
    remote = settings(root_map=f"/media/intel={tmp_path}", data_dir=str(tmp_path))
    (check,) = worker_check.check_data_store(remote)
    assert not check.ok
    write_marker(tmp_path)
    (check,) = worker_check.check_data_store(remote)
    assert check.ok
    # The probe file is gone again: only the marker remains.
    assert [p.name for p in tmp_path.iterdir()] == [DATA_STORE_MARKER]


def test_worker_roots_reads_the_live_settings_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FRAMEFOUND_ROOT_MAP", f"/media/intel={tmp_path}")
    get_settings.cache_clear()
    assert worker_roots.is_remote_worker()
    assert local_root("/media/intel/a") == tmp_path / "a"


# --- the task layer: hand back, never retry or fail ------------------------


def _probe_task(name: str, body):  # type: ignore[no-untyped-def]
    from framefound.celery_app import celery_app

    # Registered like the real tasks: autoretry on anything, a few retries.
    return celery_app.task(name=name, autoretry_for=(Exception,), max_retries=3)(body)


def _remote(monkeypatch: pytest.MonkeyPatch, data: Path) -> None:
    monkeypatch.setenv("FRAMEFOUND_ROOT_MAP", f"/media/intel={data}")
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(data))
    monkeypatch.setattr(worker_roots, "HAND_BACK_PAUSE_S", 0.0)
    get_settings.cache_clear()


def test_a_task_is_handed_back_when_the_data_store_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remote(monkeypatch, tmp_path)  # no marker written
    ran: list[int] = []

    def body() -> None:
        ran.append(1)

    result = _probe_task("test.handback_store", body).apply()

    assert ran == []
    assert result.state == "REJECTED"


def test_not_here_is_a_hand_back_not_a_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _remote(monkeypatch, tmp_path)
    write_marker(tmp_path)
    calls: list[int] = []

    def body() -> None:
        calls.append(1)
        local_root("/media/breeze/clip")  # no mapping on this machine

    result = _probe_task("test.handback_unmapped", body).apply()

    assert calls == [1]  # once: autoretry left it alone
    assert result.state == "REJECTED"


def test_on_the_server_the_task_layer_is_a_pass_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))  # no marker, no map
    get_settings.cache_clear()
    ran: list[int] = []

    def body() -> None:
        ran.append(1)

    result = _probe_task("test.passthrough", body).apply()

    assert ran == [1]
    assert result.state == "SUCCESS"
