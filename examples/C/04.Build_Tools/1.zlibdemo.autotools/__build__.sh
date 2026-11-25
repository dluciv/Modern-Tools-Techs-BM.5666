#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="${1:-release}"
JOBS="${JOBS:-4}"

case "$MODE" in
    release|debug) ;;
    *) echo "Usage: $0 [release|debug]" >&2; exit 2 ;;
esac

BUILD_DIR="$SCRIPT_DIR/build/$MODE"

cd "$SCRIPT_DIR"
autoreconf -fi

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
if [[ "$MODE" == debug ]]; then
    "$SCRIPT_DIR/configure" --enable-debug
else
    "$SCRIPT_DIR/configure"
fi
make -j"$JOBS"
