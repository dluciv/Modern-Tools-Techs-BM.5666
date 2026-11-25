# zlibdemo.cmake

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses CMake `FetchContent` to download and build a specific zlib
version (`1.3.1`) as part of the project. No system-wide zlib installation is
required, but an internet connection is needed for the first configure.

## Prerequisites

- GCC or another C compiler
- CMake 3.20+
- A build tool such as Ninja or Make
- Internet access on the first configure (to download zlib)

## Quick start with scripts

```sh
./__build__.sh release   # or: ./__build__.sh debug
./__run__.sh release
./__run__.sh debug --force-assert
./__clean__.sh           # removes both modes
```

The scripts work from any current directory and use the shared `src` symlink.

## Build modes

| Mode | Defines | Options | CMake build type |
| --- | --- | --- | --- |
| `release` (default) | `-DNDEBUG` | `-O2` | `Release` |
| `debug` | `-DDEBUG` | `-g -O0` | `Debug` |

Both modes are built into `build/release/` and `build/debug/`.

## Build manually

```sh
cmake -S . -B build/release -DCMAKE_BUILD_TYPE=Release
cmake --build build/release

cmake -S . -B build/debug -DCMAKE_BUILD_TYPE=Debug
cmake --build build/debug
```

To force a generator, add `-G Ninja` (or `-G "Unix Makefiles"`) to the
configure commands.

## Run

```sh
./build/release/zlibdemo                  # assertions disabled
./build/release/zlibdemo --force-assert   # forced assert is ignored
./build/debug/zlibdemo --force-assert     # aborts on assert()
```

Normal output looks like:

```text
zlib version: 1.3.1
Original size:   68 bytes
Compressed size: 71 bytes
Decompressed:    zlib demo: the same C source is built with different build systems.
```

The compressed size may differ depending on the platform and toolchain.
