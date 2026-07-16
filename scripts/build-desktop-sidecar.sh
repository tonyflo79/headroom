#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${HEADROOM_DESKTOP_TARGET:-$(rustc --print host-tuple)}"
BIN_DIR="$ROOT/integrations/menubar/src-tauri/binaries"
BUILD_DIR="$ROOT/integrations/menubar/src-tauri/sidecar-build/$TARGET"
NAME="headroom-engine-$TARGET"

mkdir -p "$BIN_DIR" "$BUILD_DIR/work" "$BUILD_DIR/spec"

uvx --python 3.13.12 --from pyinstaller==6.21.0 pyinstaller \
  --noconfirm --clean --onefile \
  --name "$NAME" \
  --distpath "$BIN_DIR" \
  --workpath "$BUILD_DIR/work" \
  --specpath "$BUILD_DIR/spec" \
  --paths "$ROOT" \
  "$ROOT/desktop_bridge_entry.py"

"$BIN_DIR/$NAME" <<'EOF'
{"schema":"headroom_desktop_bridge@1","id":"verify","command":"handshake","args":{"accepted_schemas":["headroom_desktop_bridge@1"]}}
{"schema":"headroom_desktop_bridge@1","id":"stop","command":"shutdown","args":{}}
EOF
