#!/usr/bin/env bash
# Called by AppBuilder's pack-deb.sh with STAGE, INSTALL_PREFIX, APP_INSTALL_DIR, PKG_NAME.
set -euo pipefail
chmod 0755 "$STAGE"   # mktemp -d makes it 0700
install -D -m 0644 zerodrive.py "$STAGE$APP_INSTALL_DIR/zerodrive.py"
install -D -m 0755 zd-helper    "$STAGE$APP_INSTALL_DIR/zd-helper"
install -D -m 0644 README.md    "$STAGE$APP_INSTALL_DIR/README.md"
cat >"$STAGE$INSTALL_PREFIX/bin/$PKG_NAME" <<SH
#!/bin/sh
exec python3 /usr/share/APPLaunch/apps/$PKG_NAME/zerodrive.py "\$@"
SH
chmod 0755 "$STAGE$INSTALL_PREFIX/bin/$PKG_NAME"
