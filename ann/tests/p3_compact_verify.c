/* p3_compact_verify.c — prove a compacted P3 log replays bit-identically.
 *
 * Uses the REAL headers (geo_wang_latch.h -> wl_replay_log_file, wl_latch_t),
 * so this is the same code path the server's lz_fog_init() takes at boot.
 *
 * Usage: p3_compact_verify <original.log> <compacted.log>
 * Exit 0 = latches identical (lossless), 1 = differ.
 */
#include <stdio.h>
#include <string.h>
#include "geo_wang_latch.h"

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <orig> <compacted>\n", argv[0]); return 2; }

    wl_latch_t a, b;
    long ra = wl_replay_log_file(&a, argv[1]);
    long rb = wl_replay_log_file(&b, argv[2]);
    printf("replay original : %ld records applied\n", ra);
    printf("replay compacted: %ld records applied\n", rb);

    int same = (memcmp(&a, &b, sizeof(a)) == 0);
    int shut_a = 0, shut_b = 0;
    for (uint32_t i = 0; i < WL_COUNT; i++) {
        if (!wl_is_open(&a, i)) shut_a++;
        if (!wl_is_open(&b, i)) shut_b++;
    }
    printf("shut original   : %d\n", shut_a);
    printf("shut compacted  : %d\n", shut_b);

    /* print the shut ids so the two files can be diffed by hand too */
    printf("ids original    :");
    for (uint32_t i = 0; i < WL_COUNT; i++) if (!wl_is_open(&a, i)) printf(" %u", i);
    printf("\nids compacted   :");
    for (uint32_t i = 0; i < WL_COUNT; i++) if (!wl_is_open(&b, i)) printf(" %u", i);
    printf("\n");

    printf("LATCH IDENTICAL : %s\n", same ? "YES (lossless)" : "NO");
    return same ? 0 : 1;
}
