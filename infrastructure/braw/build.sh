#!/usr/bin/env bash
# Build the braw-decode CLI against the Blackmagic RAW SDK (Linux).
#
# The SDK is EULA-licensed by Blackmagic Design: download it yourself from
# https://www.blackmagicdesign.com/products/blackmagicraw and NEVER commit it
# or bake it into published images. This script builds locally and installs
# to an output directory that docker-compose.braw.yml mounts read-only.
#
# Usage: ./build.sh /path/to/Blackmagic_RAW_*.tar.gz [output_dir=/opt/braw]
set -euo pipefail

SDK_TAR=${1:?"Usage: build.sh <Blackmagic_RAW_*.tar.gz> [output_dir]"}
OUT=${2:-/opt/braw}
BRAW_DECODE_REPO="https://github.com/AkBKukU/braw-decode"

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }

say "Installing build dependencies"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential rpm2cpio cpio git >/dev/null

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

say "Extracting SDK"
tar -xf "$SDK_TAR" -C "$WORK"
RPM=$(find "$WORK" -name '*.rpm' | head -1)
if [ -n "$RPM" ]; then
  mkdir -p "$WORK/rpmroot"
  (cd "$WORK/rpmroot" && rpm2cpio "$RPM" | cpio -idm --quiet)
fi
SDKDIR=$(find "$WORK" -type d -path '*BlackmagicRAWSDK/Linux' | head -1)
[ -n "$SDKDIR" ] || { echo "Could not locate BlackmagicRAWSDK/Linux in the archive" >&2; exit 1; }
say "SDK at: $SDKDIR"

say "Building braw-decode"
git clone -q --depth 1 "$BRAW_DECODE_REPO" "$WORK/braw-decode"
cp -r "$SDKDIR/Include" "$WORK/braw-decode/"
cp -r "$SDKDIR/Libraries" "$WORK/braw-decode/"
make -C "$WORK/braw-decode"

say "Installing to $OUT"
sudo mkdir -p "$OUT"
sudo cp "$WORK/braw-decode/braw-decode" "$OUT/"
sudo rm -rf "$OUT/Libraries"
sudo cp -r "$WORK/braw-decode/Libraries" "$OUT/Libraries"
sudo chmod -R a+rX "$OUT"

say "Smoke test"
(cd "$OUT" && LD_LIBRARY_PATH="$OUT/Libraries" ./braw-decode --help >/dev/null 2>&1) \
  && say "braw-decode installed OK" \
  || { echo "braw-decode built but failed to run (CPU/SDK compatibility?)" >&2; exit 1; }

say "Enable in FrameFound with:"
say "  docker compose -f docker-compose.yml -f docker-compose.braw.yml up -d"
