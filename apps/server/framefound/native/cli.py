"""The `framefound` command (docs/mac.md).

framefound install            set up or repair; safe to run again
framefound install --store <folder>   …and keep everything bulky in that folder
framefound status             what is running
framefound start | stop | restart [service]
framefound open               the web app, in the browser
framefound doctor [--parity]  is everything healthy
framefound logs [service]     follow a service's log
framefound update             back up, pull the latest release and restart
framefound update --skip-backup   …without the backup first
framefound backup             take a catalogue backup now
framefound restore <archive>  replace the catalogue with a backup's
framefound storage            where each bulky kind of file lives
framefound storage move <kind> <folder>   put one kind somewhere else
framefound proxies            where proxies live
framefound proxies move <folder>   put them somewhere else
framefound proxies rebuild    remake any that are missing
framefound migrate --from user@host   bring a container install here
framefound ops <tool> [...]   run a maintenance tool (pair_proxies, shots, …)
framefound uninstall          remove the services (keeps the data)
"""

import argparse
import contextlib
import os
import shlex
import sys
from pathlib import Path

from framefound.native import (
    backup,
    database,
    doctor,
    envfile,
    installer,
    migrate,
    proc,
    proxies,
    services,
    storage,
)
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout
from framefound.storage import moves, stores

# sysexits.h EX_CONFIG: what a service exits with when there is nothing to
# run it from, so the reason is recognisable in `launchctl print`.
EX_CONFIG = 78


def say(message: str) -> None:
    print(message, flush=True)


def confirm(question: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _installed_env(layout: Layout) -> dict[str, str]:
    env = envfile.read(layout.env_file)
    if not env:
        raise installer.InstallError(
            f"FrameFound is not installed here ({layout.env_file} is missing). "
            "Run: bash infrastructure/mac/install.sh  (from the checkout)"
        )
    return env


def _adopt(layout: Layout, env: dict[str, str]) -> None:
    """Make this process see the install's settings, for commands that use
    the application's own code (doctor, proxies)."""
    from framefound.config import get_settings

    os.environ.update(env)
    os.environ.update(services.process_environment(layout, env))
    get_settings.cache_clear()


def run_service(layout: Layout, name: str) -> int:
    """What launchd runs. Reads the settings, waits for what the service
    needs, then becomes the service — launchd supervises the real program."""
    env = envfile.read(layout.env_file)
    if not env or name not in services.BY_NAME:
        print(f"framefound: cannot run {name!r}: not installed or unknown", file=sys.stderr)
        return EX_CONFIG
    service = services.BY_NAME[name]
    start = services.launch(name, layout, env)
    process_env = {
        **os.environ,
        **env,
        **services.process_environment(layout, env),
        **start.env,
    }
    for key in service.waits_for:
        # At login everything starts at once. Waiting here turns "the
        # database was not up yet" from a crash-and-retry into a short pause.
        proc.wait_for_port(services.port(env, key), 90)
    for command in start.before:
        proc.run(command, env=process_env, cwd=start.cwd)
    os.chdir(start.cwd)
    os.execvpe(start.argv[0], start.argv, process_env)  # noqa: S606 - argv list, no shell
    return 0  # pragma: no cover - exec does not return


def cmd_install(layout: Layout, launchd: Launchd, args: argparse.Namespace) -> int:
    env = installer.install(layout, launchd, say)
    if args.store:
        try:
            cmd_storage_use(layout, Path(args.store).expanduser(), yes=args.yes)
        except (moves.MoveError, stores.StoresFileError) as err:
            # Several minutes of install have just worked. Ending on the
            # folder's problem alone would read as "the install failed".
            raise installer.InstallError(
                f"FrameFound is installed and running, but its bulk folder was not set: {err}\n"
                "Everything stays on this Mac until it is. Fix that, then set the "
                "folder on the Storage page or run the same command again."
            ) from err
    # After the bulk folder, so that given both, this is the newer
    # instruction for proxies and the one that stands.
    if args.proxies:
        cmd_proxies_move(layout, launchd, Path(args.proxies).expanduser(), empty=False)
    http = services.port(env, "FRAMEFOUND_HTTP_PORT")
    # The command as it can be typed in this Terminal, which is not always
    # the bare name (installer.typed_command).
    framefound = installer.typed_command(layout)
    say("")
    say(f"FrameFound is running:  http://localhost:{http}")
    say("From other machines:    http://<this Mac's name or address>:" + str(http))
    if database.count(env, "users") == 0 and env.get("FRAMEFOUND_SETUP_TOKEN"):
        say(f"First-time setup token: {env['FRAMEFOUND_SETUP_TOKEN']}")
        say(f"Or bring an existing install over:  {framefound} migrate --from user@host")
    say(f"Check it any time with:  {framefound} doctor")
    return 0


def cmd_status(layout: Layout, launchd: Launchd) -> int:
    env = _installed_env(layout)
    # Wide enough for the longest name (worker-local-ai, 15), so the columns
    # line up.
    width = max(16, *(len(service.name) + 1 for service in services.SERVICES))
    for service in services.SERVICES:
        state = launchd.state(service.name)
        shown = state.state if state.loaded else "stopped"
        pid = f"pid {state.pid}" if state.pid else ""
        say(f"{service.name:<{width}} {shown:<12} {pid:<10} {service.summary}")
    say(f"\nhttp://localhost:{services.port(env, 'FRAMEFOUND_HTTP_PORT')}   logs: {layout.logs}")
    return 0


def cmd_start(layout: Layout, launchd: Launchd, name: str | None) -> int:
    env = _installed_env(layout)
    if name:
        launchd.write(services.BY_NAME[name])
        launchd.start(name)
    else:
        installer.start_all(layout, env, launchd, say)
    return 0


def cmd_stop(launchd: Launchd, name: str | None) -> int:
    installer.stop_all(launchd, (name,) if name else None)
    return 0


def cmd_restart(layout: Layout, launchd: Launchd, name: str | None) -> int:
    cmd_stop(launchd, name)
    return cmd_start(layout, launchd, name)


def cmd_doctor(layout: Layout, launchd: Launchd, parity: bool) -> int:
    from framefound.config import get_settings
    from framefound.ops.worker_check import report

    env = _installed_env(layout)
    _adopt(layout, env)
    checks = doctor.run(layout, env, get_settings(), launchd, parity=parity)
    return report(checks, "see the FAIL lines above")


def _git_lines(source: Path, *words: str) -> list[str] | None:
    """What `git <words>` lists about the checkout, or None when git cannot
    say. Only ever used with commands that read."""
    try:
        done = proc.run(["git", "-C", str(source), *words], capture=True, check=False, timeout=30)
    except proc.CommandFailed:
        return None
    if done.returncode != 0:
        return None
    return [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]


def _commits(source: Path, span: str) -> int:
    """How many commits `span` (git's `a..b`) holds; 0 when git cannot say."""
    counted = _git_lines(source, "rev-list", "--count", span)
    return int(counted[0]) if counted and counted[0].isdigit() else 0


def why_the_pull_stopped(layout: Layout) -> str:
    """The message for a `git pull` that refused: what is in the way, in
    words, and how to clear it. Reads the checkout and changes nothing.

    The case that matters is a file edited on this Mac (a fix made by hand
    while waiting for a release) that the release changes too. git refuses
    rather than overwrite it, and so does this: the edit is the operator's
    to set aside or drop, never something an update discards for them."""
    source = shlex.quote(str(layout.source))
    framefound = installer.typed_command(layout)
    edited = _git_lines(layout.source, "diff", "--name-only", "HEAD")
    # What moving forward to the release would change: everything on the
    # release's side since the two parted. The refused pull has already
    # fetched it.
    incoming = _git_lines(layout.source, "diff", "--name-only", "HEAD...@{upstream}")
    in_the_way = sorted(set(edited or ()) & set(incoming or ()))
    if in_the_way:
        one = len(in_the_way) == 1
        files = "\n".join(f"  {name}" for name in in_the_way)
        return (
            "The update stopped before anything was changed. "
            f"{'A file' if one else 'Files'} edited on this Mac "
            f"{'is' if one else 'are'} changed by the new release too, and an update "
            "never overwrites an edit:\n"
            f"{files}\n"
            f"To see what was edited:  git -C {source} diff\n"
            "To put the edits to one side and carry on:\n"
            f"  git -C {source} stash\n"
            f"  {framefound} update\n"
            "Nothing is thrown away: the edits wait in git's stash, and this shows them "
            "again at any time:\n"
            f"  git -C {source} stash show -p\n"
            "Afterwards, if the new release already does what they did, discard them with\n"
            f"  git -C {source} stash drop\n"
            # Not `stash pop`. The files named above are ones the release
            # changed too, so a pop is likely to clash, and a clash leaves
            # git's conflict markers in code that every service and this
            # command import: nothing fails until the next restart, and then
            # everything does. The way back is `checkout HEAD --`; a plain
            # `checkout --` is refused for a file left in that state.
            "If it does not, make the change again by hand in the release's own file. Do "
            "not put the edits back with `git stash pop`: where an edit and the release "
            "changed the same lines, git writes both versions into the file, and FrameFound "
            "cannot start from a file like that. Should that have happened, this puts the "
            "release's file back (the edits stay in the stash):\n"
            f"  git -C {source} checkout HEAD -- {shlex.join(in_the_way)}"
        )
    # Commits made here are in the way only when the release also has
    # commits this checkout lacks. Ahead and not behind, a pull has nothing
    # to do and succeeds, so one that failed in that state failed for another
    # reason (no network, usually) and the commits are not to blame for it.
    count = _commits(layout.source, "@{upstream}..HEAD")
    if count and _commits(layout.source, "HEAD..@{upstream}"):
        return (
            "The update stopped before anything was changed. This checkout has "
            f"{count} change{'' if count == 1 else 's'} of its own, committed on this Mac, "
            "that the release does not have, so it cannot simply move forward to the new "
            "release.\n"
            f"To see them:  git -C {source} log --oneline @{{upstream}}..HEAD\n"
            f"To put them on top of the new release:  git -C {source} pull --rebase\n"
            f"then:  {framefound} update"
        )
    return (
        "The update stopped: git could not bring the new release in (its own message is "
        "above). FrameFound was not rebuilt or restarted and is running as it was. Put right "
        f"what git reports, then:  {framefound} update"
    )


def backup_stopped_the_update(layout: Layout, why: object) -> str:
    """The message for a backup that failed before an update: why, and how
    to update without one."""
    framefound = installer.typed_command(layout)
    reason = str(why).strip().rstrip(".")
    return (
        f"The update stopped before anything was changed: the backup it takes first failed "
        f"({reason}). To update without a backup, run:  {framefound} update --skip-backup"
    )


def _backup_first(layout: Layout) -> Path | None:
    """A catalogue backup, as `framefound backup` takes, before an update:
    the release an update hands over to may migrate the catalogue, and a
    migration that goes wrong on real data is undone only from an archive
    taken just before it. A backup that fails stops the update
    (`InstallError` with the sentence); an install that is not there says
    so in its own words.

    Only the archive on this Mac decides. `backup.run` writes it first and
    copies it off the machine after, and that copy can still fail part way
    (a NAS that fills up or drops mid-copy raises out of `shutil.copyfile`).
    Stopping then would tell the operator the backup failed, hide the good
    archive just written, and point them at `--skip-backup`. So an archive
    that appeared during the run means the backup was made: the failed
    copy is said, its leftover `.partial` removed, and the update goes on."""
    env = _installed_env(layout)
    say("Backing up the catalogue before updating…")
    here = backup.folder(env, layout)
    before = set(backup.archives(here))
    try:
        archive = backup.run(layout, env)
    except (backup.BackupError, proc.CommandFailed, OSError) as err:
        written = [found for found in backup.archives(here) if found not in before]
        if not written:
            raise installer.InstallError(backup_stopped_the_update(layout, err)) from err
        archive = written[0]  # newest first
        say(f"Backup written: {archive}")
        _off_machine_copy_failed(layout, env, archive, err)
        return archive
    if archive is not None:
        say(f"Backup written: {archive}")
    return archive


def _off_machine_copy_failed(
    layout: Layout, env: dict[str, str], archive: Path, why: object
) -> None:
    """Say that the copy of `archive` off this Mac failed, and clear what
    it left on the share, when the share still answers."""
    elsewhere = backup.copy_dir(env, layout)
    reason = str(why).strip().rstrip(".")
    where = f" to {elsewhere}" if elsewhere is not None else ""
    say(
        f"The copy of this backup off this Mac{where} failed ({reason}), so it is on this "
        "Mac only. The update goes on."
    )
    if elsewhere is not None:
        # A share that is gone keeps its leftover; the next copy replaces it.
        with contextlib.suppress(OSError):
            (elsewhere / (archive.name + ".partial")).unlink(missing_ok=True)


def cmd_update(layout: Layout, *, skip_backup: bool = False) -> int:
    if not skip_backup:
        _backup_first(layout)
    pulled = proc.run(["git", "-C", str(layout.source), "pull", "--ff-only"], check=False)
    if pulled.returncode != 0:
        raise installer.InstallError(why_the_pull_stopped(layout))
    # Enough for the new release's code to start. Its install installs again
    # with its own extras (installer.install_packages): the list here is
    # this release's, and the release pulled may have added one.
    proc.run(installer.package_command(layout))
    # The code that was just replaced is this process's own: start a fresh
    # interpreter to run the new release's install (rebuild, migrate, restart).
    python = str(layout.python)
    os.execv(python, [python, "-m", "framefound.native", "install"])  # noqa: S606
    return 0  # pragma: no cover


def cmd_backup(layout: Layout, if_due: bool) -> int:
    env = _installed_env(layout)
    archive = backup.run(layout, env, if_due=if_due)
    if archive is not None:
        say(f"Backup written: {archive}")
    return 0


def cmd_restore(layout: Layout, launchd: Launchd, archive: Path, yes: bool) -> int:
    env = _installed_env(layout)
    if not archive.is_file():
        raise backup.BackupError(f"{archive} is not a file")
    if not (yes or confirm(f"Replace the whole catalogue with {archive.name}?")):
        say("Nothing was changed.")
        return 1
    app = tuple(n for n in services.BY_NAME if n not in ("postgres", "valkey"))
    installer.stop_all(launchd, app)
    backup.restore(layout, env, archive, say)
    installer.start_all(layout, env, launchd, say)
    say("Restored. The API brings the schema up to date as it starts.")
    return 0


def cmd_storage_use(layout: Layout, root: Path, *, yes: bool = False) -> int:
    from framefound.config import get_settings

    _adopt(layout, _installed_env(layout))
    storage.use_bulk_folder(
        layout, get_settings(), root, say, sure=(lambda _question: True) if yes else confirm
    )
    return 0


def cmd_storage_show(layout: Layout) -> int:
    from framefound.config import get_settings

    _adopt(layout, _installed_env(layout))
    for line in storage.describe(get_settings()):
        say(line)
    return 0


def cmd_storage_move(layout: Layout, kind: str, target: Path, *, empty: bool, yes: bool) -> int:
    from framefound.config import get_settings

    _adopt(layout, _installed_env(layout))
    storage.move(
        get_settings(),
        kind,
        target,
        say,
        empty=empty,
        sure=(lambda _question: True) if yes else confirm,
    )
    return 0


def cmd_proxies_show(layout: Layout) -> int:
    from framefound.config import get_settings

    _adopt(layout, _installed_env(layout))
    for line in proxies.describe(get_settings()):
        say(line)
    return 0


def cmd_proxies_move(layout: Layout, launchd: Launchd, target: Path, *, empty: bool) -> int:
    from framefound.config import get_settings

    _adopt(layout, _installed_env(layout))
    if empty:
        proxies.start_empty(layout, get_settings(), target, launchd, say)
    else:
        proxies.move(layout, get_settings(), target, launchd, say, rsync=installer.rsync())
    return 0


def cmd_proxies_rebuild(layout: Layout) -> int:
    from framefound.ops import proxy_store

    _adopt(layout, _installed_env(layout))
    return proxy_store.main(["--requeue"])


def cmd_migrate(layout: Layout, launchd: Launchd, args: argparse.Namespace) -> int:
    from framefound.config import get_settings

    env = _installed_env(layout)
    _adopt(layout, env)
    settings = get_settings()
    # The bulky kinds that have folders of their own here go straight into
    # them, not through this Mac's disk.
    elsewhere = [kind for kind in migrate.KIND_EXCLUDES if stores.is_separate(kind, settings)]
    options = migrate.Options(
        source=args.source,
        directory=args.source_dir,
        maps=";".join(args.map or []),
        proxies="skip" if args.skip_proxies else "copy",
        replace=args.replace,
        leave_source_running=args.leave_source_running,
        rsync=installer.rsync(),
    )
    migrate.migrate(
        layout,
        env,
        launchd,
        options,
        proxy_root=stores.proxy_root(settings.data_dir, settings),
        proxy_problem=stores.proxy_store_problem(settings),
        say=say,
        confirm=(lambda _question: True) if args.yes else confirm,
        kind_roots={kind: stores.kind_folder(kind, settings) for kind in elsewhere},
        kind_problems={kind: stores.kind_problem(kind, settings) for kind in elsewhere},
    )
    http = services.port(env, "FRAMEFOUND_HTTP_PORT")
    say("")
    say(f"Migrated. Sign in at http://localhost:{http} with the same account as before.")
    say("Next:")
    say("  1. framefound doctor")
    say(
        f"  2. Point the Premiere panel, the Lightroom plugin and Claude at this Mac (port {http})."
    )
    if args.skip_proxies:
        say("  3. framefound proxies rebuild")
    say(
        "The old server's app is stopped, not removed. Keep it until you are sure, "
        f"then on {args.source}:  cd {args.source_dir} && docker compose down"
    )
    return 0


def cmd_ops(layout: Layout, tool: str, rest: list[str]) -> int:
    """Run `python -m framefound.ops.<tool>` with the install's settings —
    the maintenance tools the docs give as `docker compose exec …` commands
    for the containers."""
    if not tool.isidentifier():
        raise installer.InstallError(f"{tool!r} is not the name of a maintenance tool")
    env = _installed_env(layout)
    process_env = {**os.environ, **env, **services.process_environment(layout, env)}
    os.chdir(layout.server_source)
    python = str(layout.python)
    os.execve(python, [python, "-m", f"framefound.ops.{tool}", *rest], process_env)  # noqa: S606
    return 0  # pragma: no cover


def cmd_uninstall(layout: Layout, launchd: Launchd) -> int:
    for service in reversed(services.SERVICES):
        launchd.remove(service.name)
    say(f"The services are removed. Everything else is still in {layout.home}")
    say("(the catalogue, settings, derived files). Delete that folder to remove it all.")
    return 0


def cmd_logs(layout: Layout, name: str) -> int:
    log = layout.logs / f"{name}.log"
    os.execvp("tail", ["tail", "-n", "60", "-f", str(log)])  # noqa: S606, S607
    return 0  # pragma: no cover


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="framefound", description="FrameFound on this Mac (docs/mac.md)"
    )
    sub = top.add_subparsers(dest="command", required=True)
    names = sorted(services.BY_NAME)

    install = sub.add_parser("install", help="set up or repair; safe to run again")
    install.add_argument(
        "--store",
        metavar="FOLDER",
        help="keep everything bulky in this folder: proxies, slideshow videos, "
        "listing exports, basemaps and backup copies",
    )
    install.add_argument("--proxies", metavar="FOLDER", help="keep video proxies in this folder")
    install.add_argument(
        "--yes",
        action="store_true",
        help="do not ask before using a --store folder that looks like a drive "
        "that is not connected (one on this Mac's own disk)",
    )
    sub.add_parser("status", help="what is running")
    for verb, text in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
        one = sub.add_parser(verb, help=f"{text} everything, or one service")
        one.add_argument("service", nargs="?", choices=names)
    sub.add_parser("open", help="open the web app")
    check = sub.add_parser("doctor", help="is everything healthy")
    check.add_argument("--parity", action="store_true", help="also compare CoreML with the CPU")
    logs = sub.add_parser("logs", help="follow a service's log")
    logs.add_argument("service", nargs="?", default="api", choices=names)
    update = sub.add_parser("update", help="back up, pull the latest release and restart")
    update.add_argument(
        "--skip-backup",
        action="store_true",
        help="do not take a catalogue backup first (only when one cannot be taken)",
    )
    back = sub.add_parser("backup", help="take a catalogue backup now")
    back.add_argument("--if-due", action="store_true", help=argparse.SUPPRESS)
    restore = sub.add_parser("restore", help="replace the catalogue with a backup's")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--yes", action="store_true", help="do not ask")

    store = sub.add_parser("storage", help="where each bulky kind of file lives; move one")
    store_sub = store.add_subparsers(dest="storage_command")
    store_move = store_sub.add_parser("move", help="put one kind in another folder")
    store_move.add_argument("kind", choices=moves.MOVABLE)
    store_move.add_argument("folder", type=Path)
    store_move.add_argument(
        "--empty",
        action="store_true",
        help="do not copy the existing files (the old folder is gone)",
    )
    store_move.add_argument("--yes", action="store_true", help="do not ask")

    prox = sub.add_parser("proxies", help="where proxies live; move or rebuild them")
    prox_sub = prox.add_subparsers(dest="proxies_command")
    move = prox_sub.add_parser("move", help="put proxies in another folder")
    move.add_argument("folder", type=Path)
    move.add_argument(
        "--empty",
        action="store_true",
        help="do not copy the existing proxies (the old store is gone)",
    )
    prox_sub.add_parser("rebuild", help="remake proxies that are missing")

    mig = sub.add_parser("migrate", help="bring a container install here")
    mig.add_argument("--from", dest="source", required=True, metavar="USER@HOST")
    mig.add_argument("--source-dir", default="framefound", help="compose folder on the old server")
    mig.add_argument(
        "--map", action="append", metavar="OLD=NEW", help="where a library root is here"
    )
    mig.add_argument(
        "--skip-proxies", action="store_true", help="leave proxies behind; rebuild later"
    )
    mig.add_argument("--replace", action="store_true", help="overwrite a catalogue already here")
    mig.add_argument("--leave-source-running", action="store_true", help="do not stop the old app")
    mig.add_argument("--yes", action="store_true", help="do not ask")

    ops = sub.add_parser("ops", help="run a maintenance tool with this install's settings")
    ops.add_argument("tool", help="e.g. pair_proxies, shots, proxy_store")
    ops.add_argument("rest", nargs=argparse.REMAINDER, help="the tool's own arguments")

    sub.add_parser("uninstall", help="remove the services (keeps the data)")
    run = sub.add_parser("run", help=argparse.SUPPRESS)
    run.add_argument("service")
    return top


def dispatch(args: argparse.Namespace, layout: Layout, launchd: Launchd) -> int:
    command = args.command
    if command == "run":
        return run_service(layout, args.service)
    if command == "install":
        return cmd_install(layout, launchd, args)
    if command == "status":
        return cmd_status(layout, launchd)
    if command == "start":
        return cmd_start(layout, launchd, args.service)
    if command == "stop":
        return cmd_stop(launchd, args.service)
    if command == "restart":
        return cmd_restart(layout, launchd, args.service)
    if command == "open":
        env = _installed_env(layout)
        proc.run(["open", f"http://localhost:{services.port(env, 'FRAMEFOUND_HTTP_PORT')}"])
        return 0
    if command == "doctor":
        return cmd_doctor(layout, launchd, args.parity)
    if command == "logs":
        return cmd_logs(layout, args.service)
    if command == "update":
        return cmd_update(layout, skip_backup=args.skip_backup)
    if command == "backup":
        return cmd_backup(layout, args.if_due)
    if command == "restore":
        return cmd_restore(layout, launchd, args.archive, args.yes)
    if command == "storage":
        if args.storage_command == "move":
            return cmd_storage_move(
                layout, args.kind, args.folder.expanduser(), empty=args.empty, yes=args.yes
            )
        return cmd_storage_show(layout)
    if command == "proxies":
        if args.proxies_command == "move":
            return cmd_proxies_move(layout, launchd, args.folder.expanduser(), empty=args.empty)
        if args.proxies_command == "rebuild":
            return cmd_proxies_rebuild(layout)
        return cmd_proxies_show(layout)
    if command == "migrate":
        return cmd_migrate(layout, launchd, args)
    if command == "ops":
        return cmd_ops(layout, args.tool, args.rest)
    if command == "uninstall":
        return cmd_uninstall(layout, launchd)
    raise AssertionError(command)  # argparse has already rejected anything else


# Failures the operator can act on: shown as one message, no traceback.
EXPECTED = (
    installer.InstallError,
    proc.CommandFailed,
    database.DatabaseError,
    backup.BackupError,
    proxies.ProxyMoveError,
    migrate.MigrationError,
    moves.MoveError,
    stores.StoresFileError,
)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    layout = Layout.default()
    if args.command != "run":
        # The PATH of the Terminal this was typed in, kept before Homebrew is
        # put in front of it on the next line: it decides whether a message
        # may say `framefound` or has to give the full path
        # (installer.typed_command). Without it the answer was always the
        # bare name, since by then Homebrew's bin is always on PATH. The
        # bootstrap script's own record wins, and `update` hands this one on
        # to the install it becomes. Not for a service: launchd is not a
        # Terminal, and a service's environment stays as it was.
        os.environ.setdefault(installer.SHELL_PATH_ENV, os.environ.get("PATH", ""))
    os.environ["PATH"] = installer.with_homebrew_on_path(os.environ.get("PATH", ""))
    try:
        return dispatch(args, layout, Launchd(layout))
    except EXPECTED as err:
        print(f"framefound: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nframefound: interrupted", file=sys.stderr)
        return 130
