"""The Premiere panel as a .ccx Creative Cloud installs (native/panel.py)."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest

from framefound.native import cli, panel
from framefound.native.layout import Layout

REPO = Path(__file__).resolve().parents[3]
PANEL = REPO / "apps" / "panel-premiere"


@pytest.fixture()
def layout(tmp_path: Path) -> Layout:
    return Layout(home=tmp_path / "FrameFound", source=REPO)


@pytest.fixture()
def own_copy(tmp_path: Path) -> Layout:
    """A checkout of just the panel, to be changed by a test."""
    source = tmp_path / "src"
    shutil.copytree(PANEL, source / "apps" / "panel-premiere")
    return Layout(home=tmp_path / "FrameFound", source=source)


def _manifest(layout: Layout) -> Path:
    return panel.source(layout) / "manifest.json"


def test_the_shipped_manifest_names_its_one_host_as_an_object() -> None:
    """With "host" as a list Creative Cloud refused the .ccx: "Couldn't
    install plugin… Error code -4". As an object the same three files
    installed, and the panel appeared in Premiere 26."""
    manifest = json.loads((PANEL / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["host"] == {"app": "premierepro", "minVersion": "25.0.0"}


def test_the_panel_says_the_build_its_manifest_says() -> None:
    version = json.loads((PANEL / "manifest.json").read_text(encoding="utf-8"))["version"]
    assert f'const BUILD = "{version}";' in (PANEL / "main.js").read_text(encoding="utf-8")


def test_the_ccx_holds_the_panels_files_at_its_root_and_nothing_else(layout: Layout) -> None:
    built = panel.build(layout)

    version = panel.read_manifest(PANEL)["version"]
    assert built == layout.home / "premiere-panel" / f"FrameFound-{version}.ccx"
    with zipfile.ZipFile(built) as ccx:
        assert sorted(ccx.namelist()) == ["index.html", "main.js", "manifest.json"]
        for name in ccx.namelist():
            assert ccx.read(name) == (PANEL / name).read_bytes()
    assert not list(built.parent.glob("*.partial"))


def test_the_same_panel_makes_the_same_ccx(layout: Layout) -> None:
    first = panel.build(layout).read_bytes()
    assert panel.build(layout).read_bytes() == first


def test_icons_the_manifest_names_go_in_and_a_missing_one_stops_it(own_copy: Layout) -> None:
    manifest = json.loads(_manifest(own_copy).read_text())
    manifest["icons"] = [{"path": "icons/icon.png", "width": 24, "height": 24}]
    _manifest(own_copy).write_text(json.dumps(manifest))
    with pytest.raises(panel.PanelError, match="icons/icon.png"):
        panel.build(own_copy)

    (panel.source(own_copy) / "icons").mkdir()
    (panel.source(own_copy) / "icons" / "icon.png").write_bytes(b"\x89PNG")
    with zipfile.ZipFile(panel.build(own_copy)) as ccx:
        assert "icons/icon.png" in ccx.namelist()


def test_a_host_written_as_a_list_is_refused_before_creative_cloud_sees_it(
    own_copy: Layout,
) -> None:
    manifest = json.loads(_manifest(own_copy).read_text())
    manifest["host"] = [manifest["host"]]
    _manifest(own_copy).write_text(json.dumps(manifest))
    with pytest.raises(panel.PanelError, match="error -4"):
        panel.build(own_copy)
    assert not panel.folder(own_copy).exists()


def test_install_makes_the_ccx_once_per_panel_version(own_copy: Layout) -> None:
    said: list[str] = []

    first = panel.ensure(own_copy, said.append)
    assert first is not None and first.is_file()
    assert "Double-click it to install it" in said[-1]

    said.clear()
    assert panel.ensure(own_copy, said.append) is None  # the same version: nothing to say
    assert said == []

    manifest = json.loads(_manifest(own_copy).read_text())
    manifest["version"] = "9.9.9"
    _manifest(own_copy).write_text(json.dumps(manifest))
    newer = panel.ensure(own_copy, said.append)
    assert newer is not None and newer.name == "FrameFound-9.9.9.ccx"
    assert said == [f"Premiere panel 9.9.9 is ready: {newer}. Double-click it to update the panel."]
    assert first.is_file()  # an earlier one, or one made by hand, is left where it is


def test_a_panel_that_cannot_be_packaged_does_not_stop_the_install(own_copy: Layout) -> None:
    (panel.source(own_copy) / "main.js").unlink()
    said: list[str] = []
    assert panel.ensure(own_copy, said.append) is None
    assert said and "not packaged" in said[0] and "main.js" in said[0]


def test_framefound_panel_makes_it_afresh_and_says_where(
    monkeypatch: pytest.MonkeyPatch, layout: Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "Layout", type("L", (), {"default": staticmethod(lambda: layout)}))
    assert cli.main(["panel"]) == 0
    out = capsys.readouterr().out
    [built] = (layout.home / "premiere-panel").glob("*.ccx")
    assert str(built) in out and "Double-click" in out
