"""What "a folder" means, in one place.

A folder is a library plus a path relative to that library's root:
`/`-separated, with no slash at either end; `""` is the whole library. The
catalogue already knows every file's `relative_path`, so "what is in this
folder" is a question for the database and never for the disk: nothing here
opens, lists or stats anything, and a folder path is only ever compared with
paths the scanner wrote. That is also why a path that came from a person can
never reach outside a library through this module — it is never joined to a
root — and `clean_folder` still refuses the shapes that would mean "go
somewhere else" anywhere else they are used.

"Folder" means two other things in older code (a substring in the
connector's `find_shots`, the files directly in a folder in
`/listings/folders/assets`); those are left as they are. New code asks here.
"""

from typing import Any

from sqlalchemy import case, func
from sqlalchemy.sql.elements import ColumnElement

from framefound.db.models import Asset

# Longer than any path the scanner has written (relative_path is 2,048).
MAX_FOLDER_CHARS = 2048


class FolderError(ValueError):
    """Not a folder path; the message is the sentence."""


def clean_folder(text: Any) -> str:
    """A folder path as the catalogue writes it, or FolderError.

    Only slashes at either end are dropped ("/Brookside/2026/" is
    "Brookside/2026"; "/" and "" are the whole library). Everything else is
    kept exactly, because every folder the catalogue lists must open again
    from its own path: a folder renamed in Finder to "Rev 2 " (a trailing
    space) or named "Drone\\RAW" (a backslash is an ordinary character in a
    name on the Mac and on Linux) is a real folder, and trimming the space
    or refusing the backslash made Browse list it and then show it empty or
    refuse it. Neither is a risk here: a folder is only ever compared with
    the catalogue's paths (`_like_literal` escapes a backslash), never
    joined to a root.

    Refused, with a sentence that can be shown as it is: anything that is
    not text; a NUL; a part that is `..` or `.`; an empty part ("a//b");
    more than 2,048 characters. None of those can be a folder the scanner
    wrote.
    """
    if not isinstance(text, str):
        raise FolderError("A folder is written as text, such as Brookside/2026.")
    if "\x00" in text:
        raise FolderError("A folder's name cannot contain a NUL character.")
    folder = text.strip("/")
    if len(folder) > MAX_FOLDER_CHARS:
        raise FolderError(f"That folder path is longer than {MAX_FOLDER_CHARS:,} characters.")
    if not folder:
        return ""
    parts = folder.split("/")
    if any(part in ("..", ".") for part in parts):
        raise FolderError(
            "A folder is a path inside the library; it cannot use . or .. to point elsewhere."
        )
    if any(part == "" for part in parts):
        raise FolderError("That folder path has an empty part (two slashes in a row).")
    return folder


def _like_literal(value: str) -> str:
    """`value` for a LIKE pattern, its own wildcards escaped with a backslash."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def under(folder: str) -> list[ColumnElement[bool]]:
    """The conditions for "in this folder or anywhere beneath it", on
    `Asset.relative_path`; `[]` for the whole library. Apply them with the
    library's own condition: a path means nothing without its library.

    The folder is cleaned first (`clean_folder`, so FolderError can come out
    of here too). Two conditions, and both are needed:

    - a LIKE on the folder and a slash, with `%` and `_` in the name escaped,
      so "2026" does not take "2026-old" and "Bay_1" does not take "Bay-1".
      The LIKE is what an index can use.
    - the same prefix compared exactly with `substr`, because LIKE ignores
      case on SQLite and not on PostgreSQL. With it, "Drone" and "drone" are
      two folders on both, as they are on the Mac's disk listing.
    """
    folder = clean_folder(folder)
    if not folder:
        return []
    prefix = folder + "/"
    return [
        Asset.relative_path.like(_like_literal(prefix) + "%", escape="\\"),
        # substr counts characters on both databases, as len() does here.
        func.substr(Asset.relative_path, 1, len(prefix)) == prefix,
    ]


def crumbs(folder: str) -> list[tuple[str, str]]:
    """Each folder on the way down, as (name, path): "Pete Dye/Drone" is
    [("Pete Dye", "Pete Dye"), ("Drone", "Pete Dye/Drone")]; [] for the
    whole library. Cleaned first, so FolderError can come out of here."""
    folder = clean_folder(folder)
    if not folder:
        return []
    parts = folder.split("/")
    return [(part, "/".join(parts[: i + 1])) for i, part in enumerate(parts)]


def directly_in(folder: str) -> list[ColumnElement[bool]]:
    """The conditions for "a file in this folder itself, not in a folder
    inside it"; for the whole library, the files at its top. Apply them with
    the library's own condition, as `under`'s.

    The NOT LIKE only has to look past the prefix: `under` has already made
    the prefix exact, so SQLite's case-blind LIKE cannot let a file beneath
    another folder through.
    """
    folder = clean_folder(folder)
    if not folder:
        return [Asset.relative_path.not_like("%/%")]
    return [
        *under(folder),
        Asset.relative_path.not_like(_like_literal(folder + "/") + "%/%", escape="\\"),
    ]


def child_name(folder: str, dialect: str) -> ColumnElement[str]:
    """For a file beneath `folder`, the name of the folder directly inside
    it that holds the file; "" for a file directly in `folder`. Grouping by
    this gives a folder's children and what each holds in one query, with no
    path read into Python.

    Only meaningful on rows `under(folder)` has already chosen. `dialect` is
    the session's (`db.get_bind().dialect.name`): PostgreSQL finds a slash
    with `strpos`, SQLite with `instr`, and neither has the other's.

    Group by it through a subquery, not directly: PostgreSQL compares a
    GROUP BY expression with the select list's by text, and the two copies
    of the same expression carry different bound parameters, so the direct
    form is refused there while passing on SQLite.
    """
    folder = clean_folder(folder)
    skip = len(folder) + 1 if folder else 0
    rest = func.substr(Asset.relative_path, skip + 1)
    slash = func.strpos(rest, "/") if dialect == "postgresql" else func.instr(rest, "/")
    return case((slash > 0, func.substr(rest, 1, slash - 1)), else_="")
