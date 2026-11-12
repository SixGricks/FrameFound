#!/bin/bash
# FrameFound on a Mac, as a program — no Docker (docs/mac.md).
#
#   git clone https://github.com/SixGricks/FrameFound ~/FrameFound/src
#   bash ~/FrameFound/src/infrastructure/mac/install.sh
#
# Optional:  --proxies "/Volumes/<share>/FrameFound Proxies"
#            keep video proxies on a NAS share or an external drive.
#
# This script only does what has to exist before Python can take over: a
# Python 3.12 environment with FrameFound in it. Everything else — Homebrew's
# Postgres, the queue, the web build, the background services — is
# `framefound install`, which this hands off to and which is safe to run
# again at any time.
#
# Runs as you; nothing needs sudo. Written for macOS's own bash 3.2.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
HOME_DIR="${FRAMEFOUND_HOME:-$HOME/FrameFound}"
EXTRAS="db,queue,scanner,media,ai,mcp"

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS (on Linux: infrastructure/scripts/install.sh)"

# A Terminal opened before Homebrew was set up may not have it on PATH yet.
for prefix in /opt/homebrew /usr/local; do
  if [ -x "$prefix/bin/brew" ]; then
    PATH="$prefix/bin:$PATH"
    break
  fi
done
command -v brew >/dev/null || die "install Homebrew first (https://brew.sh), then run this again"

case "$SRC" in
  "$HOME/Desktop"* | "$HOME/Documents"* | "$HOME/Downloads"*)
    die "move the checkout out of $SRC (macOS keeps background services out of that folder); ~/FrameFound/src works"
    ;;
esac

command -v uv >/dev/null || brew install uv

mkdir -p "$HOME_DIR"
if [ ! -x "$HOME_DIR/venv/bin/python" ]; then
  uv venv --python 3.12 "$HOME_DIR/venv"
fi
uv pip install --python "$HOME_DIR/venv/bin/python" -e "$SRC/apps/server[$EXTRAS]"

exec "$HOME_DIR/venv/bin/python" -m framefound.native install "$@"
