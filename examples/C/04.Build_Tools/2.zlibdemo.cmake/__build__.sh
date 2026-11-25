#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MODE="${1:-release}"
JOBS="${JOBS:-4}"

case "$MODE" in
    release) BUILD_TYPE="Release" ;;
    debug)   BUILD_TYPE="Debug" ;;
    *) echo "Usage: $0 [release|debug]" >&2; exit 2 ;;
esac

BUILD_DIR="$SCRIPT_DIR/build/$MODE"

if command -v ninja >/dev/null 2>&1; then
    GENERATOR="Ninja"
elif command -v make >/dev/null 2>&1; then
    GENERATOR="Unix Makefiles"
else
    echo "Neither ninja nor make was found. Install one and retry." >&2
    exit 1
fi

cmake -S "$SCRIPT_DIR" -B "$BUILD_DIR" -G "$GENERATOR" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE"
cmake --build "$BUILD_DIR" --config "$BUILD_TYPE" -j "$JOBS"
