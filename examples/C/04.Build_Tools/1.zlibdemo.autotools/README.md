# zlibdemo.autotools

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

## Prerequisites

- GCC or another C compiler
- Autotools: `autoconf`, `automake`, `pkg-config`
- zlib development files installed system-wide

On Debian/Ubuntu:

```sh
sudo apt install build-essential autoconf automake pkg-config zlib1g-dev
```

## Quick start with scripts

```sh
./__build__.sh release   # or: ./__build__.sh debug
./__run__.sh release
./__run__.sh debug --force-assert
./__clean__.sh           # removes both modes and generated Autotools files
```

The scripts work from any current directory and use the shared `src` symlink.

## Build modes

| Mode | Defines | Options | Configure flag |
| --- | --- | --- | --- |
| `release` (default) | `-DNDEBUG` | `-O2` | none |
| `debug` | `-DDEBUG` | `-g -O0` | `--enable-debug` |

Both modes are built into `build/release/` and `build/debug/`.

## Build manually

Generate the configure script and build in a separate build directory:

```sh
autoreconf -fi
mkdir -p build/release
cd build/release
../../configure
make
```

For a debug build, configure with `--enable-debug`:

```sh
mkdir -p build/debug
cd build/debug
../../configure --enable-debug
make
```

## Run

From a build directory:

```sh
./zlibdemo                 # release: assertions are disabled
./zlibdemo --force-assert  # release: the forced assert is ignored
```

A debug binary with `--force-assert` aborts with an assertion message:

```text
zlibdemo: src/main.c:40: main: Assertion `0 && "forced assertion (debug build only)"' failed.
```

Normal output looks like:

```text
zlib version: 1.3.2
Original size:   68 bytes
Compressed size: 71 bytes
Decompressed:    zlib demo: the same C source is built with different build systems.
```

The exact zlib version and compressed size may differ depending on the system.
