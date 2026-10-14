/* p3_append_test.c — prove the compacted log still accepts appends correctly.
 *
 * Replays the compacted log, appends a NEW latch id via wl_traverse_log, then
 * replays again and checks the new id is present AND the old ids survived.
 * This is the "does the file still work after compaction" gate.
 *
 * Usage: p3_append_test <log> <new_id>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "geo_wang_latch.h"

static int shut_count(const wl_latch_t *L) {
    int n = 0;
    for (uint32_t i = 0; i < WL_COUNT; i++) if (!wl_is_open(L, i)) n++;
    return n;
}

int main(int argc, char **argv) {
    if (argc < 3) { fprintf(stderr, "usage: %s <log> <new_id>\n", argv[0]); return 2; }
    uint32_t nid = (uint32_t)strtoul(argv[2], NULL, 10);

    wl_latch_t L;
    long r0 = wl_replay_log_file(&L, argv[1]);
    int s0 = shut_count(&L);
    printf("before: replayed=%ld shut=%d  new_id_open=%d\n", r0, s0, wl_is_open(&L, nid));

    wl_log_t lg;
    if (wl_log_open(&lg, argv[1]) != 0) { fprintf(stderr, "open for append FAILED\n"); return 1; }
    int tr = wl_traverse_log(&L, nid, &lg);
    wl_log_close(&lg);
    printf("append: traverse_log(%u) -> %d (1 = written)\n", nid, tr);

    wl_latch_t M;
    long r1 = wl_replay_log_file(&M, argv[1]);
    int s1 = shut_count(&M);
    printf("after : replayed=%ld shut=%d  new_id_shut=%d\n", r1, s1, !wl_is_open(&M, nid));

    /* old ids must survive: every id shut in L-after is shut in M */
    int lost = 0;
    for (uint32_t i = 0; i < WL_COUNT; i++)
        if (!wl_is_open(&L, i) && wl_is_open(&M, i)) lost++;

    int ok = (tr == 1) && (!wl_is_open(&M, nid)) && (lost == 0) && (r1 == r0 + 1);
    printf("old ids lost: %d\n", lost);
    printf("APPEND OK : %s\n", ok ? "YES" : "NO");
    return ok ? 0 : 1;
}
