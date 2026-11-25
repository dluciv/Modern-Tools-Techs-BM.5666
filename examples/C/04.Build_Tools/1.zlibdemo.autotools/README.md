# zlibdemo.autotools

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

## How this build works

This is the classic two-stage Autotools flow: a developer generates a portable
`configure` script, and every machine that builds the project runs that script
to adapt it to its own environment.

1. `autoreconf -fi` is the bootstrap step. It reads `configure.ac` and
   `Makefile.am`: Autoconf expands the macros in `configure.ac` into the
   executable `configure` script, Automake turns `Makefile.am` into
   `Makefile.in`, and `autoheader`/`aclocal` produce `config.h.in` and
   `aclocal.m4`; `-i` also installs the standard auxiliary helper files. These
   generated files are build inputs, not hand-written ones, which is why
   bootstrap is repeated before each configure/build.
2. `configure` runs from `build/<mode>/` (an out-of-tree build). It probes the
   local C compiler (`AC_PROG_CC`), asks `pkg-config` for zlib's include and
   linker flags (`PKG_CHECK_MODULES([ZLIB], [zlib >= 1.2])`), translates
   `--enable-debug` into `ZDEMO_MODE_CFLAGS`, and substitutes all of that into
   `Makefile` (from `Makefile.in`) and `config.h` (from `config.h.in`).
3. `make` reads the generated `Makefile`, compiles `src/main.c` (the symlink to
   `../src/main.c`) with `$(ZLIB_CFLAGS)` and `$(ZDEMO_MODE_CFLAGS)`, and links
   it with `$(ZLIB_LIBS)` into `zlibdemo`.

There is no dependency download: zlib comes from a system package, and
`pkg-config` is the only thing that locates it. Probing at configure time rather
than at compile time is the point of Autotools — the same generated `configure`
works on any Unix-like system — while separate `build/release` and
`build/debug` directories let the two flag sets coexist. `foreign` in
`AM_INIT_AUTOMAKE` skips GNU-mandated files such as `NEWS`, and `subdir-objects`
allows sources living in `src/`.

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
