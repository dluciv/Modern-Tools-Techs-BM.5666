#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

MODE="release"
if [[ "${1:-}" == release || "${1:-}" == debug ]]; then
    MODE="$1"
    shift
fi

BIN="$SCRIPT_DIR/build/$MODE/zlibdemo"

if [[ ! -x "$BIN" ]]; then
    echo "Binary not found. Run ./__build__.sh $MODE first." >&2
    exit 1
fi

exec "$BIN" "$@"
