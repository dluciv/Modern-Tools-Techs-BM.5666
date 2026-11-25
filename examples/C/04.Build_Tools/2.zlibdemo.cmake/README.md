# zlibdemo.cmake

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses CMake `FetchContent` to download and build a specific zlib
version (`1.3.1`) as part of the project. No system-wide zlib installation is
required, but an internet connection is needed for the first configure.

## How this build works

Here CMake handles both the project and the dependency: zlib is fetched as
source at configure time and compiled together with the demo, so no system zlib
is needed.

1. `cmake -S . -B build/<mode> -DCMAKE_BUILD_TYPE=...` reads `CMakeLists.txt` and
   processes `FetchContent_Declare`: it downloads the zlib 1.3.1 tarball from
   GitHub, checks it against the pinned `URL_HASH SHA256`, unpacks it under
   `build/<mode>/_deps/`, and calls `FetchContent_MakeAvailable(zlib)`. That last
   step evaluates zlib's own `CMakeLists.txt` as a subproject
   (`add_subdirectory`), so its targets (`ZLIB::ZLIB` / `zlibstatic`) become
   part of this project in the same configure run. The output is a native build
   system (Ninja's `build.ninja` or Unix Makefiles) plus `CMakeCache.txt` in the
   build directory; the download only happens on the first configure.
2. `cmake --build build/<mode>` runs that generated build system. Ninja or Make
   compiles `src/main.c` and the zlib sources and links `zlibdemo` against the
   zlib target. The `$<$<CONFIG:Debug>:...>` generator expressions pick
   `-DDEBUG/-g/-O0` or `-DNDEBUG/-O2` from the build type chosen above.
3. Running the binary prints `zlibVersion()` from the bundled zlib, so the
   reported version confirms which copy was linked.

FetchContent is used instead of `find_package` to make the result reproducible:
the exact version and its hash live in `CMakeLists.txt`, and zlib is built with
the same compiler and settings as the demo. `ZLIB_BUILD_EXAMPLES OFF` keeps
zlib's examples out of the build, and the lookup that tries `ZLIB::ZLIB` and
then `zlibstatic` handles the different target names in zlib versions. Because
single-config generators fix the flags at configure time, the two modes need
two separate build directories.

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
