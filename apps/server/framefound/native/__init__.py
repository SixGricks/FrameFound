"""FrameFound installed as a program on one Mac, without Docker (docs/mac.md).

The containers in docker-compose.yml become launchd agents in the logged-in
user's session — Postgres, Valkey, the API, the web app, Caddy, the scanner,
the scheduler and the workers — started at login and restarted if they stop.
Everything lives under one folder (`~/FrameFound`), and one command drives
it: `framefound install | start | stop | status | doctor | update | backup |
proxies | migrate`.

Why native rather than Docker Desktop: containers on macOS run in a Linux VM
that cannot reach the GPU, the Neural Engine or the media engine, which are
the reason to run on Apple silicon at all.

The modules here are plain Python with every outside effect (launchctl,
brew's binaries, ssh) behind a function that takes argv lists, so the logic
is tested on any platform; only the commands themselves need a Mac.
"""
