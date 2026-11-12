"""The `framefound` command (docs/mac.md).

framefound install            set up or repair; safe to run again
framefound status             what is running
framefound start | stop | restart [service]
framefound open               the web app, in the browser
framefound doctor [--parity]  is everything healthy
framefound logs [service]     follow a service's log
framefound update             pull the latest release and restart
framefound backup             take a catalogue backup now
framefound restore <archive>  replace the catalogue with a backup's
framefound proxies            where proxies live
framefound proxies move <folder>   put them somewhere else
framefound proxies rebuild    remake any that are missing
framefound migrate --from user@host   bring a container install here
framefound ops <tool> [...]   run a maintenance tool (pair_proxies, shots, …)
framefound uninstall          remove the services (keeps the data)
"""

import argparse
import os
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
)
from framefound.native.launchd import Launchd
from framefound.native.layout import Layout

EXTRAS = "db,queue,scanner,media,ai,mcp"
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
    if args.proxies:
        cmd_proxies_move(layout, launchd, Path(args.proxies).expanduser(), empty=False)
    http = services.port(env, "FRAMEFOUND_HTTP_PORT")
    say("")
    say(f"FrameFound is running:  http://localhost:{http}")
    say("From other machines:    http://<this Mac's name or address>:" + str(http))
    if database.count(env, "users") == 0 and env.get("FRAMEFOUND_SETUP_TOKEN"):
        say(f"First-time setup token: {env['FRAMEFOUND_SETUP_TOKEN']}")
        say("Or bring an existing install over:  framefound migrate --from user@host")
    say("Check it any time with:  framefound doctor")
    return 0


def cmd_status(layout: Layout, launchd: Launchd) -> int:
    env = _installed_env(layout)
    for service in services.SERVICES:
        state = launchd.state(service.name)
        shown = state.state if state.loaded else "stopped"
        pid = f"pid {state.pid}" if state.pid else ""
        say(f"{service.name:<14} {shown:<12} {pid:<10} {service.summary}")
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


def cmd_update(layout: Layout) -> int:
    proc.run(["git", "-C", str(layout.source), "pull", "--ff-only"])
    proc.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(layout.python),
            "-e",
            f"{layout.server_source}[{EXTRAS}]",
        ]
    )
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
    backup.restore(layout, env, archive)
    installer.start_all(layout, env, launchd, say)
    say("Restored. The API brings the schema up to date as it starts.")
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
    from framefound.storage import stores

    env = _installed_env(layout)
    _adopt(layout, env)
    settings = get_settings()
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
    install.add_argument("--proxies", metavar="FOLDER", help="keep video proxies in this folder")
    sub.add_parser("status", help="what is running")
    for verb, text in (("start", "start"), ("stop", "stop"), ("restart", "restart")):
        one = sub.add_parser(verb, help=f"{text} everything, or one service")
        one.add_argument("service", nargs="?", choices=names)
    sub.add_parser("open", help="open the web app")
    check = sub.add_parser("doctor", help="is everything healthy")
    check.add_argument("--parity", action="store_true", help="also compare CoreML with the CPU")
    logs = sub.add_parser("logs", help="follow a service's log")
    logs.add_argument("service", nargs="?", default="api", choices=names)
    sub.add_parser("update", help="pull the latest release and restart")
    back = sub.add_parser("backup", help="take a catalogue backup now")
    back.add_argument("--if-due", action="store_true", help=argparse.SUPPRESS)
    restore = sub.add_parser("restore", help="replace the catalogue with a backup's")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--yes", action="store_true", help="do not ask")

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
        return cmd_update(layout)
    if command == "backup":
        return cmd_backup(layout, args.if_due)
    if command == "restore":
        return cmd_restore(layout, launchd, args.archive, args.yes)
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
)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    layout = Layout.default()
    os.environ["PATH"] = installer.with_homebrew_on_path(os.environ.get("PATH", ""))
    try:
        return dispatch(args, layout, Launchd(layout))
    except EXPECTED as err:
        print(f"framefound: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nframefound: interrupted", file=sys.stderr)
        return 130
