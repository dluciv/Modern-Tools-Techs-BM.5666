# zlibdemo.conan

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses Conan 2 to download/build a specific zlib version (`1.3.1`)
and generates files for CMake (`CMakeToolchain` and `CMakeDeps`).

## How this build works

Conan and CMake do separate jobs here: Conan resolves a zlib *package* and
generates the CMake files needed to consume it; CMake only builds this project
against those generated files. No zlib source is downloaded into the project.

1. `conan profile detect --force` (done once on a fresh installation) records
   the OS, compiler, C/C++ library settings and default build type in a profile.
   Conan uses it to select a zlib binary that matches the local toolchain.
2. `conan install . --output-folder=build/conan-<mode> --build=missing
   -s build_type=<Release|Debug>` reads `conanfile.txt`. It resolves
   `zlib/1.3.1` from a remote, downloads the matching binary (or builds it from
   the recipe when `--build=missing` allows that), stores it in the Conan
   cache, and runs the declared generators: `CMakeDeps` writes `ZLIBConfig.cmake`
   and target files describing zlib's headers and libraries, while
   `CMakeToolchain` writes `conan_toolchain.cmake` carrying the compiler, flags
   and paths from the profile. `[layout] cmake_layout` decides where these files
   land; the path of `conan_toolchain.cmake` differs between single- and
   multi-config generators, so it has to be looked up rather than hardcoded.
3. CMake is configured with that toolchain file (`-DCMAKE_TOOLCHAIN_FILE=...`).
   Its `find_package(ZLIB REQUIRED)` consumes the Conan-generated config instead
   of searching the system, and `target_link_libraries(zlibdemo PRIVATE
   ZLIB::ZLIB)` uses the imported target from it. `cmake --build` then runs Ninja
   or Make to compile `src/main.c` and link the binary; generator expressions
   apply the mode-specific defines and flags.

The split is deliberate: the dependency is versioned and cached outside the
project by Conan, while CMake stays an ordinary project that only sees a normal
`find_package` result. Conan packages are settings-specific, so each mode gets
its own Conan install and its own CMake build directory. The generator and
`CMAKE_MAKE_PROGRAM` are passed explicitly at configure time because minimal
systems may otherwise leave `CMAKE_MAKE_PROGRAM` unset.

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
