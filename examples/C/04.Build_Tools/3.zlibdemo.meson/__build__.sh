#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="${1:-release}"

case "$MODE" in
    release) OPTIMIZATION=2; DEBUG=false ;;
    debug)   OPTIMIZATION=0; DEBUG=true ;;
    *) echo "Usage: $0 [release|debug]" >&2; exit 2 ;;
esac

BUILD_DIR="$SCRIPT_DIR/build/$MODE"

# Always use the downloaded subproject, even if zlib exists on the system.
meson setup "$BUILD_DIR" "$SCRIPT_DIR" \
    --wrap-mode=forcefallback \
    --buildtype="$MODE" \
    -Doptimization="$OPTIMIZATION" \
    -Ddebug="$DEBUG"
meson compile -C "$BUILD_DIR"
