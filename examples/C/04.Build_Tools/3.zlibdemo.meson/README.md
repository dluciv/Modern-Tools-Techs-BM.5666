# zlibdemo.meson

Minimal C example that compresses and decompresses a string with zlib.
The source code lives in `../src/main.c`; this directory contains a symlink
`src -> ../src` so the same code is shared by all examples.

This example uses Meson with a WrapDB subproject to download and build a
specific zlib version (`1.3.2`) for the project. No system-wide zlib
installation is required, but an internet connection is needed for the first
setup. The committed wrap downloads from GitHub release assets, with WrapDB as
a fallback, because the old `zlib.net` URL now returns 404.

## How this build works

Meson resolves zlib as a fallback dependency: the project asks for `zlib` by
name, and Meson either finds a system copy or builds a pinned source copy as a
subproject. This example always takes the second path.

1. The committed `subprojects/zlib.wrap` records the zlib 1.3.2 tarball and
   WrapDB patch URLs together with their SHA256 hashes. Both primary URLs point
   to GitHub release assets — byte-for-byte copies of the zlib.net tarball and
   the WrapDB patch, hence the upstream hashes still apply; the original WrapDB
   URLs remain as `*_fallback_url`. The patch matters because zlib has no native
   Meson build files: WrapDB supplies a `meson.build` and a `zlib_dep`
   dependency object for it. Pinning both hashes makes the downloads verifiable.
2. `meson setup build/<mode> --wrap-mode=forcefallback` (with `--buildtype` and
   the `-Doptimization`/`-Ddebug` options for the chosen mode) reads
   `meson.build`. The call `dependency('zlib', fallback: ['zlib', 'zlib_dep'])`
   would normally prefer a system library found through pkg-config, but
   `forcefallback` makes Meson read the wrap, download and verify the tarball
   and the patch, unpack them into `subprojects/zlib-1.3.2/`, apply the patch,
   and configure the subproject in the same run. Meson writes Ninja build files
   into `build/<mode>/`. The buildtype chooses optimization and debug info;
   `meson.build` adds `-DDEBUG` or `-DNDEBUG` based on it.
3. `meson compile -C build/<mode>` runs Ninja, which builds the subproject and
   `src/main.c` and links `zlibdemo` with `zlib_dep`.

The fallback keeps `meson.build` short — the executable just requests a
dependency and does not care where it came from — while the wrap file makes the
source version explicit and reproducible. As with CMake, setup is done once per
build directory, hence one per mode; `forcefallback` is what guarantees that a
system zlib is ignored and that the printed version is 1.3.2.

## Prerequisites

- GCC or another C compiler
- Meson
- Ninja
- Internet access on the first setup (to download zlib and its wrap patch)

## The zlib wrap

`subprojects/zlib.wrap` is committed here, so nothing needs to be installed.
It was originally created with `meson wrap install zlib`; that command refuses
to overwrite an existing file, and a freshly installed upstream wrap points at
`https://zlib.net/zlib-1.3.2.tar.xz`, which now returns 404. If you regenerate
the file, restore the GitHub URLs from the committed version. Both downloaded
files are checked against the hashes in the wrap, so the mirror cannot change
the contents unnoticed.

After the first successful download Meson keeps both files in
`subprojects/packagecache/`, so later builds need no network. For a completely
offline start, copy `zlib-1.3.2.tar.xz` and `zlib_1.3.2-1_patch.zip` there
yourself (keep your own copies elsewhere: that directory is generated data).
Meson verifies them against the same hashes and never contacts the URLs.

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
