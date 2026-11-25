#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

rm -rf "$SCRIPT_DIR/build"
# Remove downloaded subproject sources too, so the next build downloads them again.
rm -rf "$SCRIPT_DIR/subprojects/packagecache" "$SCRIPT_DIR/subprojects/zlib-1.3.2"
rm -f "$SCRIPT_DIR/subprojects/.wraplock"
