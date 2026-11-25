/*
 * Small zlib demo: compress a string in memory and then decompress it back.
 * The same source file is shared by all build examples via symlinks.
 *
 * Build modes:
 *   release: -DNDEBUG, assertions are compiled out
 *   debug:   -DDEBUG, assertions are active
 *
 * Run with --force-assert to see the difference: a debug build aborts on the
 * assertion, while a release build ignores it and continues.
 */

#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>

static void check_zlib(int rc, const char *step)
{
    if (rc != Z_OK) {
        fprintf(stderr, "%s failed: %s (%d)\n", step, zError(rc), rc);
        exit(EXIT_FAILURE);
    }
}

int main(int argc, char **argv)
{
    const unsigned char input[] =
        "zlib demo: the same C source is built with different build systems.";
    const uLong input_size = (uLong)sizeof(input); /* includes the trailing '\0' */
    const uLong bound = compressBound(input_size);

    uLongf compressed_size = (uLongf)bound;
    unsigned char *compressed = (unsigned char *)malloc(bound);
    unsigned char *output = (unsigned char *)malloc(input_size);

    if (argc > 1 && strcmp(argv[1], "--force-assert") == 0) {
        /* Removed by -DNDEBUG in release builds; really fails in debug builds. */
        assert(0 && "forced assertion (debug build only)");
        printf("--force-assert ignored: assertions are disabled (NDEBUG)\n");
    }

    if (compressed == NULL || output == NULL) {
        fprintf(stderr, "Memory allocation failed\n");
        free(compressed);
        free(output);
        return EXIT_FAILURE;
    }

    check_zlib(compress(compressed, &compressed_size, input, input_size),
               "compress");
    assert(compressed_size <= bound); /* a normal debug-only sanity check */

    uLongf output_size = input_size;
    check_zlib(uncompress(output, &output_size, compressed, compressed_size),
               "uncompress");

    if (output_size != input_size || memcmp(input, output, input_size) != 0) {
        fprintf(stderr, "Round-trip check failed\n");
        free(compressed);
        free(output);
        return EXIT_FAILURE;
    }

    printf("zlib version: %s\n", zlibVersion());
    printf("Original size:   %lu bytes\n", (unsigned long)input_size);
    printf("Compressed size: %lu bytes\n", (unsigned long)compressed_size);
    printf("Decompressed:    %s\n", output);

    free(compressed);
    free(output);
    return EXIT_SUCCESS;
}
