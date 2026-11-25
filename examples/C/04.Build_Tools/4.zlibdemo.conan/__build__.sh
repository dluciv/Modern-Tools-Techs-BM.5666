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

BUILD_DIR="$SCRIPT_DIR/build"
CONAN_DIR="$BUILD_DIR/conan-$MODE"
CMAKE_DIR="$BUILD_DIR/cmake-$MODE"

# Pick a generator explicitly: CMAKE_MAKE_PROGRAM is passed as well,
# which avoids "CMAKE_MAKE_PROGRAM is not set" on minimal systems.
if command -v ninja >/dev/null 2>&1; then
    GENERATOR="Ninja"
    MAKE_PROGRAM="$(command -v ninja)"
elif command -v make >/dev/null 2>&1; then
    GENERATOR="Unix Makefiles"
    MAKE_PROGRAM="$(command -v make)"
else
    echo "Neither ninja nor make was found. Install one and retry." >&2
    exit 1
fi

cd "$SCRIPT_DIR"
# Create a default profile on a fresh Conan installation.
if ! conan profile show >/dev/null 2>&1; then
    conan profile detect --force
fi

conan install . --output-folder="$CONAN_DIR" --build=missing \
    -s build_type="$BUILD_TYPE"

# Conan places the toolchain in different folders for single- and
# multi-configuration generators, so look for it instead of hardcoding a path.
shopt -s nullglob
TOOLCHAIN=""
for candidate in \
    "$CONAN_DIR"/generators/conan_toolchain.cmake \
    "$CONAN_DIR"/build/*/generators/conan_toolchain.cmake; do
    if [[ -f "$candidate" ]]; then
        TOOLCHAIN="$candidate"
        break
    fi
done

if [[ -z "$TOOLCHAIN" ]]; then
    echo "conan_toolchain.cmake was not found under $CONAN_DIR" >&2
    exit 1
fi

cmake -S "$SCRIPT_DIR" -B "$CMAKE_DIR" \
    -G "$GENERATOR" \
    -DCMAKE_MAKE_PROGRAM="$MAKE_PROGRAM" \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE="$BUILD_TYPE"
cmake --build "$CMAKE_DIR" --config "$BUILD_TYPE" -j "$JOBS"
