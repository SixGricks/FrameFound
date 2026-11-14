"""What "a folder" means (framefound/media/folders.py).

One definition of "this folder and everything beneath it", answered from the
catalogue alone, the same on SQLite and PostgreSQL. Compare's folder sample
uses it first; the folder level in Browse and the shot filter use it later.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import TEST_SETUP_TOKEN
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from framefound.api.v1 import assets as assets_api
from framefound.config import get_settings
from framefound.db.base import Base
from framefound.db.engine import get_session
from framefound.db.models import Asset, Library
from framefound.media import folders
from framefound.media.folders import FolderError, clean_folder

BASE = datetime(2026, 11, 3, 5, 15, tzinfo=UTC)

PATHS = (
    "2026/Drone/DJI_0001.MP4",
    "2026/Drone/RAW/DJI_0002.DNG",
    "2026/drone/DJI_0003.MP4",  # the same name in another case: another folder
    "2026/Dronework/DJI_0004.MP4",  # shares the prefix, is beside it
    "2026-old/Drone/DJI_0005.MP4",
    "2026/Bay_1/IMG_0006.JPG",
    "2026/Bay-1/IMG_0007.JPG",  # what an unescaped _ would also take
    "2026/BayX1/IMG_0008.JPG",
    "2026/50% off/IMG_0009.JPG",
    "2026/50 percent off/IMG_0010.JPG",  # what an unescaped % would also take
    "2026",  # a file named like the folder, at the top
    "IMG_0011.JPG",
)


@pytest.fixture()
async def catalogue(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    """Two libraries with files at the same relative paths, so a folder
    query that forgot its library would show it."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'f.db').as_posix()}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    libraries: dict[str, uuid.UUID] = {}
    async with factory() as db:
        for name in ("GELCO", "Intel"):
            library = Library(name=name, root_path=str(tmp_path / name))
            db.add(library)
            await db.flush()
            libraries[name] = library.id
            for relative in PATHS:
                db.add(
                    Asset(
                        library_id=library.id,
                        relative_path=relative,
                        filename=relative.rsplit("/", 1)[-1],
                        extension="mp4",
                        media_type="video",
                        size_bytes=1,
                        mtime=BASE,
                    )
                )
        await db.commit()
    yield {"factory": factory, "libraries": libraries}
    await engine.dispose()


async def _in(catalogue: dict[str, Any], folder: str) -> list[str]:
    async with catalogue["factory"]() as db:
        rows = await db.execute(
            select(Asset.relative_path)
            .where(Asset.library_id == catalogue["libraries"]["GELCO"], *folders.under(folder))
            .order_by(Asset.relative_path)
        )
        return [str(path) for path in rows.scalars()]


async def test_a_folder_holds_what_is_beneath_it_and_nothing_beside_it(
    catalogue: dict[str, Any],
) -> None:
    assert await _in(catalogue, "2026/Drone") == [
        "2026/Drone/DJI_0001.MP4",
        "2026/Drone/RAW/DJI_0002.DNG",
    ]
    # "2026" is not "2026-old", and a file called 2026 is not inside itself.
    assert "2026-old/Drone/DJI_0005.MP4" not in await _in(catalogue, "2026")
    assert "2026" not in await _in(catalogue, "2026")
    assert len(await _in(catalogue, "2026")) == 9
    # The whole library is every file in it, and only in it.
    assert len(await _in(catalogue, "")) == len(PATHS)
    assert folders.under("") == []
    assert folders.under("/") == []
    # A folder nothing is in is simply empty.
    assert await _in(catalogue, "2026/Nowhere") == []


async def test_a_folder_name_with_a_percent_sign_or_an_underscore_matches_only_itself(
    catalogue: dict[str, Any],
) -> None:
    assert await _in(catalogue, "2026/Bay_1") == ["2026/Bay_1/IMG_0006.JPG"]
    assert await _in(catalogue, "2026/50% off") == ["2026/50% off/IMG_0009.JPG"]
    # A backslash is a character of a name, and cannot be used to escape
    # the escapes: "2026\%" takes neither "2026" nor "2026-old".
    assert await _in(catalogue, "2026\\%") == []
    assert await _in(catalogue, "2026\\") == []


async def test_folders_that_differ_only_in_case_are_two_folders(
    catalogue: dict[str, Any],
) -> None:
    """SQLite's LIKE ignores case and PostgreSQL's does not; the exact
    comparison beside the LIKE makes them agree, as the disk does."""
    assert await _in(catalogue, "2026/drone") == ["2026/drone/DJI_0003.MP4"]
    assert await _in(catalogue, "2026/DRONE") == []


def test_the_folder_test_reads_the_same_on_postgresql() -> None:
    """The condition PostgreSQL is sent: the escaped LIKE and the exact
    prefix, character for character."""
    compiled = [
        str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for clause in folders.under("2026/Bay_1")
    ]
    # %% is psycopg's spelling of one %, in a statement with literals bound.
    assert compiled[0] == "assets.relative_path LIKE '2026/Bay\\_1/%%' ESCAPE '\\'"
    assert "substr(assets.relative_path, 1, 11) = '2026/Bay_1/'" in compiled[1]


@pytest.mark.parametrize(
    ("given", "cleaned"),
    [
        ("", ""),
        ("/", ""),
        ("Brookside/2026", "Brookside/2026"),
        ("/Brookside/2026/", "Brookside/2026"),
        ("Brookside Country Club/50% off", "Brookside Country Club/50% off"),
        # Names are kept exactly as the scanner wrote them: a space at
        # either end and a backslash are ordinary characters in a folder's
        # name on the Mac, and trimming or refusing them lost the folder.
        ("Brookside/Rev 2 ", "Brookside/Rev 2 "),
        ("/ Raw/", " Raw"),
        ("Drone\\RAW", "Drone\\RAW"),
    ],
)
def test_a_folder_is_written_the_way_the_catalogue_writes_paths(given: str, cleaned: str) -> None:
    assert clean_folder(given) == cleaned


@pytest.mark.parametrize(
    "given",
    [
        "..",
        "../Intel",
        "Brookside/../../etc",
        "Brookside/..",
        "./Brookside",
        "Brookside//2026",
        "Brookside\x002026",
        "a" * 2049,
        None,
        7,
    ],
)
def test_a_path_with_dot_dot_is_refused(given: Any) -> None:
    """And every other shape that would mean "somewhere else": a folder is
    only ever a path inside its library."""
    with pytest.raises(FolderError) as refused:
        clean_folder(given)
    # The message is the sentence a person reads; it never echoes the path.
    assert str(refused.value).endswith(".")
    assert "etc" not in str(refused.value)


# ------------------------------------------------- one level, through the API
#
# Browse's folder level (GET /assets/folders) and the folder filter on
# GET /assets, against a shoot laid out the way a card dump is.

ADMIN = {"email": "admin@example.com", "password": "a-strong-password"}
TAKEN = datetime(2026, 11, 5, 17, 42, 10, tzinfo=UTC)  # after BASE, the files' mtime

SHOOT: tuple[tuple[str, str], ...] = (
    ("top.JPG", "image"),
    ("Pete Dye/IMG_0001.JPG", "image"),
    ("Pete Dye/IMG_0002.JPG", "image"),
    ("Pete Dye/Drone/DJI_0001.MP4", "video"),
    ("Pete Dye/Drone/DJI_0002.MP4", "video"),
    ("Pete Dye/Drone/RAW/DJI_0003.DNG", "image"),
    ("Pete Dye/drone/DJI_0004.MP4", "video"),  # another folder, by case
    ("Pete Dye/50% off/IMG_0003.JPG", "image"),
    ("Pete Dye/Interview/take1.wav", "audio"),
    ("Other/IMG_0004.JPG", "image"),
)


@pytest.fixture()
async def browse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    url = f"sqlite+aiosqlite:///{(tmp_path / 'browse.db').as_posix()}"
    monkeypatch.setenv("FRAMEFOUND_SETUP_TOKEN", TEST_SETUP_TOKEN)
    monkeypatch.setenv("FRAMEFOUND_SECRET_KEY", "folders-secret")
    monkeypatch.setenv("FRAMEFOUND_DATABASE_URL", url)
    monkeypatch.setenv("FRAMEFOUND_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override() -> AsyncIterator[AsyncSession]:
        async with factory() as session:
            yield session

    from framefound.main import create_app

    app = create_app()
    app.dependency_overrides[get_session] = override
    ids: dict[str, uuid.UUID] = {}
    async with factory() as db:
        gelco = Library(name="GELCO", root_path=str(tmp_path / "GELCO"))
        intel = Library(name="Intel", root_path=str(tmp_path / "Intel"))
        db.add_all([gelco, intel])
        await db.flush()
        for relative, media_type in SHOOT:
            asset = _asset(gelco.id, relative, media_type)
            if relative == "Pete Dye/Drone/DJI_0001.MP4":
                asset.captured_at = TAKEN
            db.add(asset)
            await db.flush()
            ids[relative] = asset.id
        # Premiere's proxy of a clip, paired with it: out of the catalogue.
        db.add(
            _asset(
                gelco.id,
                "Pete Dye/Drone/Proxies/DJI_0001_Proxy.MP4",
                "video",
                proxy_of_id=ids["Pete Dye/Drone/DJI_0001.MP4"],
            )
        )
        # The same folder in another library: never part of GELCO's answer.
        db.add(_asset(intel.id, "Pete Dye/Drone/DJI_9999.MP4", "video"))
        db.add(_asset(intel.id, "Pete Dye/Elsewhere/IMG_9999.JPG", "image"))
        await db.commit()
        libraries = {"GELCO": gelco.id, "Intel": intel.id}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        assert (
            await http.post("/api/v1/auth/setup", json={"setup_token": TEST_SETUP_TOKEN, **ADMIN})
        ).status_code == 201
        yield {"http": http, "app": app, "libraries": libraries, "ids": ids, "factory": factory}
    await engine.dispose()
    get_settings.cache_clear()


def _asset(library_id: uuid.UUID, relative: str, media_type: str, **extra: Any) -> Asset:
    return Asset(
        library_id=library_id,
        relative_path=relative,
        filename=relative.rsplit("/", 1)[-1],
        extension=relative.rsplit(".", 1)[-1].lower(),
        media_type=media_type,
        size_bytes=1,
        mtime=BASE,
        **extra,
    )


async def _level(browse: dict[str, Any], path: str = "", library: str = "GELCO") -> Any:
    response = await browse["http"].get(
        "/api/v1/assets/folders",
        params={"library_id": str(browse["libraries"][library]), "path": path},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _files(browse: dict[str, Any], **params: Any) -> list[str]:
    params.setdefault("library_id", str(browse["libraries"]["GELCO"]))
    response = await browse["http"].get(
        "/api/v1/assets", params={"sort": "name", "page_size": 200, **params}
    )
    assert response.status_code == 200, response.text
    return sorted(item["relative_path"] for item in response.json()["items"])


async def test_one_level_lists_the_folders_inside_with_what_each_holds(
    browse: dict[str, Any],
) -> None:
    level = await _level(browse, "/Pete Dye/")
    assert level["library_name"] == "GELCO"
    assert level["path"] == "Pete Dye"
    assert level["crumbs"] == [{"name": "Pete Dye", "path": "Pete Dye"}]
    by_name = {row["name"]: row for row in level["folders"]}
    # Every folder directly inside, by name; "drone" beside "Drone", and a
    # % in a name is only a character.
    assert [row["name"] for row in level["folders"]] == sorted(by_name)
    assert set(by_name) == {"50% off", "Drone", "Interview", "drone"}
    drone = by_name["Drone"]
    # A folder's counts are for everything beneath it, RAW/ included.
    assert (drone["items"], drone["photos"], drone["clips"]) == (3, 1, 2)
    assert drone["path"] == "Pete Dye/Drone"
    assert drone["newest"].startswith("2026-11-05T17:42:10")
    assert drone["looked"] == 0
    # Audio is an item, neither a photograph nor a clip.
    interview = by_name["Interview"]
    assert (interview["items"], interview["photos"], interview["clips"]) == (1, 0, 0)
    # A folder whose files have no capture time is as new as its newest file.
    assert interview["newest"].startswith("2026-11-03T05:15")
    # Here: the two photographs directly in Pete Dye. Inside: all of it.
    assert level["here"] == {"items": 2, "photos": 2, "clips": 0}
    assert level["inside"] == {"items": 8, "photos": 4, "clips": 3, "looked": 0}
    assert level["truncated"] is False


async def test_the_top_of_a_library_lists_its_folders_and_its_own_files(
    browse: dict[str, Any],
) -> None:
    level = await _level(browse)
    assert level["path"] == "" and level["crumbs"] == []
    assert [row["name"] for row in level["folders"]] == ["Other", "Pete Dye"]
    assert level["here"] == {"items": 1, "photos": 1, "clips": 0}
    assert level["inside"]["items"] == len(SHOOT)
    # Deeper down, the crumbs are the way back up.
    deep = await _level(browse, "Pete Dye/Drone/RAW")
    assert deep["crumbs"] == [
        {"name": "Pete Dye", "path": "Pete Dye"},
        {"name": "Drone", "path": "Pete Dye/Drone"},
        {"name": "RAW", "path": "Pete Dye/Drone/RAW"},
    ]
    assert deep["folders"] == []
    assert deep["here"]["items"] == 1


async def test_a_paired_proxy_is_not_counted(browse: dict[str, Any]) -> None:
    """Premiere's Proxies/ folder holds only paired proxies: it is not a
    folder of the catalogue, and its files are in no count or list."""
    drone = await _level(browse, "Pete Dye/Drone")
    assert [row["name"] for row in drone["folders"]] == ["RAW"]
    assert drone["here"] == {"items": 2, "photos": 0, "clips": 2}
    assert await _files(browse, folder="Pete Dye/Drone", deep="true") == [
        "Pete Dye/Drone/DJI_0001.MP4",
        "Pete Dye/Drone/DJI_0002.MP4",
        "Pete Dye/Drone/RAW/DJI_0003.DNG",
    ]


async def test_a_level_belongs_to_its_library(browse: dict[str, Any]) -> None:
    intel = await _level(browse, "Pete Dye", library="Intel")
    assert intel["library_name"] == "Intel"
    assert [row["name"] for row in intel["folders"]] == ["Drone", "Elsewhere"]
    assert intel["inside"]["items"] == 2
    assert await _files(
        browse, library_id=str(browse["libraries"]["Intel"]), folder="Pete Dye/Drone"
    ) == ["Pete Dye/Drone/DJI_9999.MP4"]


async def test_a_folder_nothing_is_under_is_an_empty_level_not_an_error(
    browse: dict[str, Any],
) -> None:
    level = await _level(browse, "Pete Dye/Renamed since")
    assert level["folders"] == []
    assert level["here"] == {"items": 0, "photos": 0, "clips": 0}
    assert level["inside"] == {"items": 0, "photos": 0, "clips": 0, "looked": 0}
    assert level["crumbs"][-1] == {"name": "Renamed since", "path": "Pete Dye/Renamed since"}


async def test_a_level_with_too_many_folders_says_so(
    browse: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assets_api, "MAX_FOLDERS", 3)
    level = await _level(browse, "Pete Dye")
    assert [row["name"] for row in level["folders"]] == ["50% off", "Drone", "Interview"]
    assert level["truncated"] is True
    # The totals are still the whole folder's, not the shown rows'.
    assert level["inside"]["items"] == 8
    assert level["here"]["items"] == 2
    monkeypatch.setattr(assets_api, "MAX_FOLDERS", 4)
    assert (await _level(browse, "Pete Dye"))["truncated"] is False


async def test_files_directly_in_a_folder_are_not_the_files_beneath_it(
    browse: dict[str, Any],
) -> None:
    assert await _files(browse, folder="Pete Dye") == [
        "Pete Dye/IMG_0001.JPG",
        "Pete Dye/IMG_0002.JPG",
    ]
    beneath = await _files(browse, folder="Pete Dye", deep="true")
    assert len(beneath) == 8 and "Other/IMG_0004.JPG" not in beneath
    # The top of the library is its own files; deep, it is the library.
    assert await _files(browse, folder="") == ["top.JPG"]
    assert len(await _files(browse, folder="", deep="true")) == len(SHOOT)
    # Case is a different folder here too.
    assert await _files(browse, folder="Pete Dye/drone") == ["Pete Dye/drone/DJI_0004.MP4"]
    # The other filters still apply inside a folder, and the total is the
    # folder's.
    response = await browse["http"].get(
        "/api/v1/assets",
        params={
            "library_id": str(browse["libraries"]["GELCO"]),
            "folder": "Pete Dye",
            "deep": "true",
            "media_type": "video",
        },
    )
    assert response.json()["total"] == 3


async def test_without_a_folder_the_asset_list_is_what_it_was(browse: dict[str, Any]) -> None:
    everything = await browse["http"].get("/api/v1/assets", params={"page_size": 200})
    body = everything.json()
    # Both libraries, every depth, no paired proxy, and the same envelope.
    assert body["total"] == len(SHOOT) + 2
    assert set(body) == {"items", "total", "page", "page_size"}
    assert not any("Proxies/" in item["relative_path"] for item in body["items"])
    assert await _files(browse) == sorted(path for path, _ in SHOOT)


async def test_a_folder_needs_a_library(browse: dict[str, Any]) -> None:
    for url, params in (
        ("/api/v1/assets", {"folder": "Pete Dye"}),
        ("/api/v1/assets", {"folder": ""}),
        ("/api/v1/assets/folders", {"path": "Pete Dye"}),
    ):
        response = await browse["http"].get(url, params=params)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "Choose a library to browse a folder."
    unknown = await browse["http"].get(
        "/api/v1/assets/folders", params={"library_id": str(uuid.uuid4())}
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "No such library."


async def test_every_folder_a_level_lists_opens_from_the_path_it_gives(
    browse: dict[str, Any],
) -> None:
    """Finder lets a folder's name start or end with a space ("Rev 2 " after
    a careless rename) or hold a backslash, and the scanner writes the name
    as it is. Each is listed, and following the path the listing gives
    opens it with its files: not an empty level, not a refusal."""
    gelco = browse["libraries"]["GELCO"]
    odd = (" Raw/IMG_0101.JPG", "Pete Dye/Rev 2 /IMG_0102.JPG", "Pete Dye/Drone\\RAW/IMG_0103.JPG")
    async with browse["factory"]() as db:
        db.add_all([_asset(gelco, relative, "image") for relative in odd])
        await db.commit()

    top = {row["name"]: row["path"] for row in (await _level(browse))["folders"]}
    inside = {row["name"]: row["path"] for row in (await _level(browse, "Pete Dye"))["folders"]}
    assert top[" Raw"] == " Raw"
    assert inside["Rev 2 "] == "Pete Dye/Rev 2 "
    assert inside["Drone\\RAW"] == "Pete Dye/Drone\\RAW"
    for name, path, relative in (
        (" Raw", top[" Raw"], odd[0]),
        ("Rev 2 ", inside["Rev 2 "], odd[1]),
        ("Drone\\RAW", inside["Drone\\RAW"], odd[2]),
    ):
        level = await _level(browse, path)
        assert level["path"] == path
        assert level["crumbs"][-1] == {"name": name, "path": path}
        assert level["here"] == {"items": 1, "photos": 1, "clips": 0}
        assert await _files(browse, folder=path) == [relative]
    # The name without its space is another folder, and the backslash folder
    # holds nothing of "Drone" beside it.
    assert await _files(browse, folder="Pete Dye/Rev 2", deep="true") == []
    assert await _files(browse, folder="Pete Dye/Drone\\RAW", deep="true") == [odd[2]]


@pytest.mark.parametrize("path", ["../Intel", "Pete Dye/..", "Pete Dye/./Drone", "a//b"])
async def test_a_folder_path_that_points_elsewhere_is_refused_by_both_routes(
    browse: dict[str, Any], path: str
) -> None:
    library = str(browse["libraries"]["GELCO"])
    for url, params in (
        ("/api/v1/assets/folders", {"library_id": library, "path": path}),
        ("/api/v1/assets", {"library_id": library, "folder": path}),
        ("/api/v1/assets", {"library_id": library, "folder": path, "deep": "true"}),
    ):
        response = await browse["http"].get(url, params=params)
        assert response.status_code == 400, (url, params)
        message = response.json()["error"]["message"]
        assert message.endswith(".") and "Intel" not in message


async def test_folders_is_not_read_as_an_asset_id(browse: dict[str, Any]) -> None:
    """`/assets/folders` must reach its handler, not be parsed as a UUID by
    `/assets/{asset_id}` (the route-order trap `/near` once fell into)."""
    response = await browse["http"].get("/api/v1/assets/folders")
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Choose a library to browse a folder."


async def test_a_level_is_for_people_who_are_signed_in(browse: dict[str, Any]) -> None:
    async with AsyncClient(transport=ASGITransport(app=browse["app"]), base_url="http://t") as anon:
        response = await anon.get(
            "/api/v1/assets/folders",
            params={"library_id": str(browse["libraries"]["GELCO"])},
        )
    assert response.status_code == 401


# ------------------------------------------------------- the helpers themselves


def test_crumbs_are_the_way_down_to_a_folder() -> None:
    assert folders.crumbs("") == []
    assert folders.crumbs("/Pete Dye/Drone/") == [
        ("Pete Dye", "Pete Dye"),
        ("Drone", "Pete Dye/Drone"),
    ]
    with pytest.raises(FolderError):
        folders.crumbs("Pete Dye/../Intel")


async def test_directly_in_keeps_a_wildcard_in_a_name_literal(
    catalogue: dict[str, Any],
) -> None:
    async def directly(folder: str) -> list[str]:
        async with catalogue["factory"]() as db:
            rows = await db.execute(
                select(Asset.relative_path)
                .where(
                    Asset.library_id == catalogue["libraries"]["GELCO"],
                    *folders.directly_in(folder),
                )
                .order_by(Asset.relative_path)
            )
            return [str(path) for path in rows.scalars()]

    assert await directly("2026/Bay_1") == ["2026/Bay_1/IMG_0006.JPG"]
    assert await directly("2026/50% off") == ["2026/50% off/IMG_0009.JPG"]
    assert await directly("2026/Drone") == ["2026/Drone/DJI_0001.MP4"]
    assert await directly("") == ["2026", "IMG_0011.JPG"]


def test_the_grouped_level_reads_the_same_on_postgresql() -> None:
    """PostgreSQL has no instr, SQLite no strpos; the level query is
    compiled for PostgreSQL and read: strpos, the escaped LIKE, the exact
    prefix, and grouping on the subquery's column rather than a second copy
    of the expression (which PostgreSQL would refuse)."""
    library_id = uuid.UUID("9c1e0000-0000-0000-0000-000000000001")
    sql = str(
        assets_api.folder_level_query(library_id, "Pete Dye/Bay_1", "postgresql").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "strpos(substr(assets.relative_path, 16), '/')" in sql
    assert "instr(" not in sql
    assert "LIKE 'Pete Dye/Bay\\_1/%%' ESCAPE '\\'" in sql
    assert "substr(assets.relative_path, 1, 15) = 'Pete Dye/Bay_1/'" in sql
    assert "assets.proxy_of_id IS NULL" in sql
    assert "GROUP BY anon_1.name" in sql
    # SQLite gets instr, and the top of a library skips nothing.
    sqlite_sql = str(
        assets_api.folder_level_query(library_id, "", "sqlite").compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    assert "instr(substr(assets.relative_path, 1), '/')" in sqlite_sql
