#!/bin/bash
# FrameFound on a Mac, as a program — no Docker (docs/mac.md).
#
#   git clone https://github.com/SixGricks/FrameFound ~/FrameFound/src
#   bash ~/FrameFound/src/infrastructure/mac/install.sh
#
# Optional:  --store "/Volumes/<share>/Framefound"
#            keep everything bulky on a NAS share: video proxies, slideshow
#            videos, listing exports, basemaps and backup copies, each in a
#            folder of its own inside that one. Connect the share first: a
#            folder that turns out to be on this Mac's own disk (what a
#            share that is not connected looks like) is asked about before
#            it is used. --yes answers that in advance.
#            --proxies "/Volumes/<drive>/FrameFound Proxies"
#            keep video proxies alone somewhere else (with --store as well,
#            proxies go here and the rest to the store).
#
# This script only does what has to exist before Python can take over: a
# Python 3.12 environment with FrameFound in it. Everything else — Homebrew's
# Postgres, the queue, rsync, the web build, the `framefound` command, the
# background services — is `framefound install`, which this hands off to and
# which is safe to run again at any time. Until that has put the command in
# place, the way to run it again is this script.
#
# Runs as you; nothing needs sudo. Written for macOS's own bash 3.2.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(cd "$HERE/../.." && pwd)"
HOME_DIR="${FRAMEFOUND_HOME:-$HOME/FrameFound}"
EXTRAS="db,queue,scanner,media,ai,mcp,chat"

die() {
  printf 'install.sh: %s\n' "$*" >&2
  exit 1
}

[ "$(uname -s)" = "Darwin" ] || die "this installer is for macOS (on Linux: infrastructure/scripts/install.sh)"

# A Terminal opened before Homebrew was set up may not have it on PATH yet.
# The installer is told what this Terminal's own PATH is, so that when it
# says what to type next it does not name a command this Terminal cannot
# find (`framefound` goes in Homebrew's bin).
export FRAMEFOUND_SHELL_PATH="$PATH"
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
