# zlibdemo.conan

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses Conan 2 to download/build a specific zlib version (`1.3.1`)
and generates files for CMake (`CMakeToolchain` and `CMakeDeps`).

## Prerequisites

- GCC or another C compiler
- CMake 3.20+
- Conan 2.x
- Ninja or Make (the scripts pass `CMAKE_MAKE_PROGRAM` explicitly)
- Internet access on the first install (to download zlib)

Install Conan if needed:

```sh
python3 -m pip install --user conan
```

On a fresh Conan installation, a default profile is needed. `__build__.sh`
creates it automatically; to do it manually:

```sh
conan profile detect --force
```

## Quick start with scripts

```sh
./__build__.sh release   # or: ./__build__.sh debug
./__run__.sh release
./__run__.sh debug --force-assert
./__clean__.sh           # removes both modes and generated CMakeUserPresets.json
```

`__build__.sh` creates a default Conan profile if needed, detects Ninja or
Make and passes both `-G` and `-DCMAKE_MAKE_PROGRAM`, which avoids the error
`CMAKE_MAKE_PROGRAM is not set` on minimal systems. It also finds the
generated `conan_toolchain.cmake` automatically, since Conan may place it in
`generators/` or in `build/<build_type>/generators/` depending on the
generator.

The scripts work from any current directory and use the shared `src` symlink.

## Build modes

| Mode | Defines | Options | Conan / CMake setting |
| --- | --- | --- | --- |
| `release` (default) | `-DNDEBUG` | `-O2` | `-s build_type=Release`, `CMAKE_BUILD_TYPE=Release` |
| `debug` | `-DDEBUG` | `-g -O0` | `-s build_type=Debug`, `CMAKE_BUILD_TYPE=Debug` |

The two modes use `build/conan-release/` + `build/cmake-release/` and
`build/conan-debug/` + `build/cmake-debug/`.

## Build manually

Conan writes its generated files under `build/conan-<mode>`. Pick the
toolchain that Conan generated:

```sh
conan install . --output-folder=build/conan-release --build=missing \
    -s build_type=Release
TOOLCHAIN=$(find build/conan-release -name conan_toolchain.cmake -type f | head -n 1)
```

Configure CMake with an explicit generator and build program. For Ninja:

```sh
cmake -S . -B build/cmake-release -G Ninja \
    -DCMAKE_MAKE_PROGRAM="$(command -v ninja)" \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build/cmake-release --config Release
```

Or with Unix Makefiles:

```sh
cmake -S . -B build/cmake-release -G "Unix Makefiles" \
    -DCMAKE_MAKE_PROGRAM="$(command -v make)" \
    -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN" \
    -DCMAKE_BUILD_TYPE=Release
cmake --build build/cmake-release --config Release
```

For a debug build, repeat with `Debug`/`debug` in all paths and settings.

## Run

```sh
./__run__.sh release
./__run__.sh debug --force-assert   # aborts on assert()
```

Or directly, depending on the generator:

```sh
./build/cmake-release/zlibdemo                   # Ninja or single-config Makefiles
./build/cmake-release/Release/zlibdemo           # multi-config generators
```

Normal output looks like:

```text
zlib version: 1.3.1
Original size:   68 bytes
Compressed size: 71 bytes
Decompressed:    zlib demo: the same C source is built with different build systems.
```

The compressed size may differ depending on the platform and toolchain.
