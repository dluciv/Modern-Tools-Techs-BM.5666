# zlib build examples

Four small projects that build the same plain C program with zlib using
different build tools. Each example directory contains `src -> ../src`, a
symlink to the shared source file `src/main.c`.

| Directory | Build system | zlib source |
| --- | --- | --- |
| `zlibdemo.autotools` | Autotools | system installation |
| `zlibdemo.cmake` | CMake FetchContent | downloaded 1.3.1 tarball (hash checked) |
| `zlibdemo.meson` | Meson WrapDB subproject | downloaded 1.3.2 source (wrap pinned) |
| `zlibdemo.conan` | Conan 2 + generated CMake files | downloaded 1.3.1 package |

Each example provides the same three helper scripts:

- `./__build__.sh [release|debug]` — build the selected mode (default: `release`)
- `./__run__.sh [release|debug] [args...]` — run that mode's binary
- `./__clean__.sh` — remove all build outputs

Both modes are kept in separate directories, so you can build and run them side
by side:

```sh
./__build__.sh release
./__build__.sh debug
./__run__.sh release
./__run__.sh debug --force-assert   # aborts on assert() in debug builds
```

Release builds define `NDEBUG` and use `-O2`; debug builds define `DEBUG` and
use `-g -O0`. The exact flags are shown in each example's README.

The scripts work from any current directory. See the `README.md` file in each
directory for details and manual commands.

The program compresses a string, decompresses it, checks the round-trip and
prints the zlib version reported by `zlibVersion()` so you can see which zlib
build was linked.
