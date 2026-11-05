#!/usr/bin/env bash
# Install the FrameFound panel for developer loading — macOS.
#
# The Mac twin of install-dev.ps1. The UXP Developer Tool is still the
# supported route; this puts the plugin where it can be pointed at. On a
# first run, one thing is easy to miss:
#
#   Premiere Pro > Settings > Plugins > "Enable developer mode"
#
# Without it, UDT reports "No applications are connected to the service"
# and Premiere never appears as a target. It needs a Premiere restart.
#
# Usage:  bash apps/panel-premiere/install-dev.sh

set -euo pipefail

source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dest="$HOME/Library/Application Support/Adobe/UXP/Plugins/External/com.sixgricks.framefound"

echo "Source     : $source_dir"
echo "Destination: $dest"
echo

if [ -d "$dest" ]; then
  echo "Replacing the existing copy."
  rm -rf "$dest"
fi
mkdir -p "$dest"

# Only the files the panel needs: copying this script into the plugin folder
# would make UXP parse it looking for a manifest entry.
for name in manifest.json index.html main.js; do
  if [ -f "$source_dir/$name" ]; then
    cp "$source_dir/$name" "$dest/"
    echo "  copied $name"
  else
    echo "  WARNING: missing $name" >&2
  fi
done

# Every asset the manifest names must exist — UXP reports a dangling
# reference as a permission error somewhere unrelated (see install-dev.ps1).
missing=$(/usr/bin/python3 - "$source_dir" <<'PY' 2>/dev/null || true
import json, os, sys
root = sys.argv[1]
manifest = json.load(open(os.path.join(root, "manifest.json")))
wanted = [icon.get("path") for icon in manifest.get("icons", []) if icon.get("path")]
if manifest.get("main"):
    wanted.append(manifest["main"])
print("\n".join(p for p in wanted if not os.path.exists(os.path.join(root, p))))
PY
)
if [ -n "$missing" ]; then
  echo
  echo "WARNING: the manifest names files that do not exist:" >&2
  echo "$missing" | sed 's/^/  /' >&2
fi

echo
echo "Done. Next:"
echo "  1. Premiere Pro menu > Settings > Plugins > tick Enable developer mode"
echo "  2. Restart Premiere Pro"
echo "  3. UXP Developer Tool: Add Plugin, choose"
echo "     $dest/manifest.json"
echo "  4. Press Load. The panel appears under Window > Extensions > FrameFound."
