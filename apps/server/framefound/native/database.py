"""The install's own Postgres: create, dump, restore.

Homebrew's Postgres binaries, a cluster under `~/FrameFound/pgdata`, on a
loopback port with a generated password. Always addressed over TCP with
`PGPASSWORD` in the child's environment — never on a command line, where
`ps` would show it.
"""

import os
import re
from pathlib import Path

from framefound.native import envfile, proc, services
from framefound.native.layout import Layout

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class DatabaseError(RuntimeError):
    pass


def _identifier(value: str, what: str) -> str:
    # The user and database names reach SQL and argv; they are ours to choose
    # and there is no reason for one to be anything but a plain identifier.
    if not _IDENTIFIER.match(value):
        raise DatabaseError(f"{what} must be a plain lowercase identifier, not {value!r}")
    return value


def user(env: dict[str, str]) -> str:
    return _identifier(env.get("POSTGRES_USER") or "framefound", "POSTGRES_USER")


def name(env: dict[str, str]) -> str:
    return _identifier(env.get("POSTGRES_DB") or "framefound", "POSTGRES_DB")


def tool(env: dict[str, str], program: str) -> str:
    return str(Path(env["FRAMEFOUND_PG_BIN"]) / program)


def connection(env: dict[str, str]) -> list[str]:
    return ["-h", "127.0.0.1", "-p", str(services.port(env, "POSTGRES_PORT")), "-U", user(env)]


def child_env(env: dict[str, str]) -> dict[str, str]:
    return {**os.environ, "PGPASSWORD": env.get("POSTGRES_PASSWORD", "")}


def psql(env: dict[str, str], sql: str, database: str | None = None) -> str:
    done = proc.run(
        [
            tool(env, "psql"),
            *connection(env),
            "-d",
            database or name(env),
            "-tA",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        env=child_env(env),
        capture=True,
    )
    return (done.stdout or "").strip()


def initialise(layout: Layout, env: dict[str, str]) -> bool:
    """Create the cluster if there is none. True when it was created."""
    if (layout.pgdata / "PG_VERSION").exists():
        return False
    layout.run.mkdir(parents=True, exist_ok=True)
    pwfile = layout.run / "initdb-password"
    envfile.write(pwfile, env["POSTGRES_PASSWORD"] + "\n")  # mode 600
    try:
        proc.run(
            [
                tool(env, "initdb"),
                "-D",
                str(layout.pgdata),
                "-U",
                user(env),
                f"--pwfile={pwfile}",
                # Every connection proves the password, local socket included.
                "--auth=scram-sha-256",
                "--encoding=UTF8",
                "--locale=en_US.UTF-8",
            ],
            capture=True,
        )
    finally:
        pwfile.unlink(missing_ok=True)
    return True


def exists(env: dict[str, str]) -> bool:
    # name() is validated as a plain identifier above, so it is safe inline.
    sql = f"SELECT 1 FROM pg_database WHERE datname = '{name(env)}'"  # noqa: S608
    return psql(env, sql, "postgres") == "1"


def create(env: dict[str, str]) -> None:
    proc.run(
        [tool(env, "createdb"), *connection(env), "--encoding=UTF8", name(env)],
        env=child_env(env),
        capture=True,
    )


def drop(env: dict[str, str]) -> None:
    # --force ends any connection still open (a psql left in a terminal).
    proc.run(
        [tool(env, "dropdb"), *connection(env), "--if-exists", "--force", name(env)],
        env=child_env(env),
        capture=True,
    )


def schema_version(env: dict[str, str]) -> str | None:
    try:
        return psql(env, "SELECT version_num FROM alembic_version") or None
    except proc.CommandFailed:
        return None


def count(env: dict[str, str], table: str) -> int:
    """Rows in `table`, 0 when it does not exist yet."""
    try:
        sql = f"SELECT count(*) FROM {_identifier(table, 'table')}"  # noqa: S608 - validated
        return int(psql(env, sql) or 0)
    except (proc.CommandFailed, ValueError):
        return 0


def dump(env: dict[str, str], target: Path) -> None:
    proc.run(
        [
            tool(env, "pg_dump"),
            *connection(env),
            "-d",
            name(env),
            "--format=custom",
            f"--file={target}",
        ],
        env=child_env(env),
        capture=True,
    )


def restore(env: dict[str, str], dump_file: Path) -> None:
    """Replace the database with `dump_file` (pg_dump custom format).

    Drop and create rather than `--clean`: a dump from another major version
    or an older schema restores into an empty database or not at all, never
    into a mixture. `--no-owner` because the role that made the dump (the old
    server's) need not be this install's."""
    drop(env)
    create(env)
    done = proc.run(
        [
            tool(env, "pg_restore"),
            *connection(env),
            "-d",
            name(env),
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            str(dump_file),
        ],
        env=child_env(env),
        capture=True,
        check=False,
    )
    if done.returncode != 0:
        tail = "\n".join((done.stderr or "").strip().splitlines()[-12:])
        raise DatabaseError(f"The dump did not restore cleanly:\n{tail}")


def cluster_major(layout: Layout) -> str | None:
    try:
        return (layout.pgdata / "PG_VERSION").read_text().strip()
    except OSError:
        return None
