#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

MODE="release"
if [[ "${1:-}" == release || "${1:-}" == debug ]]; then
    MODE="$1"
    shift
fi

case "$MODE" in
    release) BUILD_TYPE="Release" ;;
    debug)   BUILD_TYPE="Debug" ;;
esac

BUILD_DIR="$SCRIPT_DIR/build/$MODE"

for candidate in "$BUILD_DIR/zlibdemo" "$BUILD_DIR/$BUILD_TYPE/zlibdemo"; do
    if [[ -x "$candidate" ]]; then
        exec "$candidate" "$@"
    fi
done

echo "Binary not found. Run ./__build__.sh $MODE first." >&2
exit 1
