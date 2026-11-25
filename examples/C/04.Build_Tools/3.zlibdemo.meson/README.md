# zlibdemo.meson

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses Meson with a WrapDB subproject to download and build a
specific zlib version (`1.3.2`) for the project. No system-wide zlib
installation is required, but an internet connection is needed for the first
setup.

## Prerequisites

- GCC or another C compiler
- Meson
- Ninja
- Internet access on the first setup (to download zlib and its wrap patch)

## Add the zlib wrap

From this directory, install the wrap definition:

```sh
meson wrap install zlib
```

This creates `subprojects/zlib.wrap`. You can commit it so the exact wrapped
version is pinned for everyone.

## Quick start with scripts

```sh
./__build__.sh release   # or: ./__build__.sh debug
./__run__.sh release
./__run__.sh debug --force-assert
./__clean__.sh           # removes both modes and downloaded subproject sources
```

The scripts work from any current directory and use the shared `src` symlink.

## Build modes

| Mode | Defines | Meson options |
| --- | --- | --- |
| `release` (default) | `-DNDEBUG` | `--buildtype=release -Doptimization=2 -Ddebug=false` |
| `debug` | `-DDEBUG` | `--buildtype=debug -Doptimization=0 -Ddebug=true` |

Both modes are built into `build/release/` and `build/debug/`.

## Build manually

Use `forcefallback` to build the downloaded zlib even if zlib is installed on
the system:

```sh
meson setup build/release --wrap-mode=forcefallback \
    --buildtype=release -Doptimization=2 -Ddebug=false
meson compile -C build/release

meson setup build/debug --wrap-mode=forcefallback \
    --buildtype=debug -Doptimization=0 -Ddebug=true
meson compile -C build/debug
```

## Run

```sh
./build/release/zlibdemo                  # assertions disabled
./build/release/zlibdemo --force-assert   # forced assert is ignored
./build/debug/zlibdemo --force-assert     # aborts on assert()
```

Normal output looks like:

```text
zlib version: 1.3.2
Original size:   68 bytes
Compressed size: 71 bytes
Decompressed:    zlib demo: the same C source is built with different build systems.
```

The compressed size may differ depending on the platform and toolchain.
