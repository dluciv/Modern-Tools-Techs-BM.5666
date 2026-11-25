AC_INIT([zlibdemo], [1.0], [demo@example.invalid])
AM_INIT_AUTOMAKE([-Wall -Werror foreign subdir-objects])
AC_CONFIG_SRCDIR([src/main.c])
AC_CONFIG_HEADERS([config.h])
AC_CONFIG_FILES([Makefile])

AC_PROG_CC

# Use pkg-config to locate the system-installed zlib.
PKG_CHECK_MODULES([ZLIB], [zlib >= 1.2])

# Build mode: --enable-debug adds -DDEBUG; release defines -DNDEBUG.
AC_ARG_ENABLE([debug],
    [AS_HELP_STRING([--enable-debug], [build with debug checks (default: no)])],
    [], [enable_debug=no])

AS_IF([test "x$enable_debug" = xyes],
    [ZDEMO_MODE_CFLAGS="-DDEBUG -g -O0"],
    [ZDEMO_MODE_CFLAGS="-DNDEBUG -O2"])
AC_SUBST([ZDEMO_MODE_CFLAGS])

AC_OUTPUT
