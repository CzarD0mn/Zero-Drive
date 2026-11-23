#!/usr/bin/env bash
# Build the Zero-Drive .deb with the official CardputerZero AppBuilder
# packaging flow (scripts/pack-deb.sh — the same one czdev-published Python
# framebuffer apps use). Output: dist/zero-drive_<ver>-<rev>_arm64.deb
#
#   ./build-deb.sh                 # clones AppBuilder into .appbuilder/ if needed
#   APPBUILDER=/path/to/CardputerZero-AppBuilder ./build-deb.sh
#
# Publish to the CardputerZero AppStore afterwards with:
#   $APPBUILDER/czdev login
#   $APPBUILDER/czdev publish --deb dist/zero-drive_<ver>-1_arm64.deb
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AB="${APPBUILDER:-$HERE/.appbuilder}"
[ -x "$AB/scripts/pack-deb.sh" ] || git clone --depth 1 https://github.com/m5stack/CardputerZero-AppBuilder "$AB"
python3 "$HERE/tools/make_icons.py" "$HERE/packaging/icon.png" "$HERE/packaging/store-icon.png"
bash "$AB/scripts/pack-deb.sh" "$HERE" --output-dir "$HERE/dist"
