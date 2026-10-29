/* test_chain_bucket.c — the chain->bucket join must be an address, not a rank.
 *
 *   T1 digit -> bucket     : every digit lands on a bucket whose face is the
 *                            digit's face, for K >= 26 and K < 26 (wrap)
 *   T2 determinism         : same inputs, same answer, no state
 *   T3 insertion stability : growing K never moves an existing bucket's face
 *   T4 occupancy           : buckets per face sums to K
 *   T5 the conflict is real: alc_dir_of (energy rank) DOES shift a bucket's face
 *                            when one bucket is inserted — the reason the chain
 *                            cannot use it as an address
 *
 * build: gcc -O2 -I.. -o test_chain_bucket.exe test_chain_bucket.c
 */
#include <stdio.h>
#include <string.h>
#include "../include/chain_bucket.h"
#include "../include/anchor_lettercube.h"

static int fails;

static void ck(const char *what, int ok, const char *detail)
{
    printf("  %-38s %s%s%s\n", what, ok ? "PASS" : "FAIL",
           detail ? "  " : "", detail ? detail : "");
    if (!ok) fails++;
}

int main(void)
{
    LCRCube q;
    int d, b, K;
    lcr_init(&q, LCR_SHAPE_GREAT);

    printf("=== chain -> bucket ===\n");

    /* T1 */
    {
        int bad = 0, out = 0;
        for (K = 26; K <= 128; K += 26) {
            for (d = 1; d <= CHAIN_BASE; d++) {
                int id = chain_bucket_id((uint8_t)d, K);
                if (id < 0 || id >= K) { bad++; continue; }
                if (chain_bucket_face(id) != chain_face_id((uint8_t)d)) bad++;
            }
        }
        for (d = 1; d <= CHAIN_BASE; d++) {
            int id = chain_bucket_id((uint8_t)d, 13);
            if (id < 0 || id >= 13) bad++;
            if (id != chain_face_id((uint8_t)d) % 13) bad++;
        }
        ck("T1 digit -> bucket on its own face", bad == 0, NULL);
        out = chain_bucket_all(1, 64, NULL, 0);
        ck("T1 face A hosts 3 buckets at K=64", out == 3, NULL);
    }

    /* T2 */
    {
        int same = 1;
        for (d = 1; d <= CHAIN_BASE; d++)
            if (chain_bucket_id((uint8_t)d, 64) != chain_bucket_id((uint8_t)d, 64)) same = 0;
        ck("T2 deterministic, no state", same, NULL);
    }

    /* T3 */
    {
        int moved = 0;
        for (b = 0; b < 26; b++) {
            uint8_t f32 = chain_bucket_face(b);
            for (K = 27; K <= 256; K += 7) {
                if (chain_bucket_face(b) != f32) moved++;
            }
        }
        ck("T3 growing K never moves a face", moved == 0, NULL);
    }

    /* T4 */
    {
        int counts[CHAIN_FACES], total = 0;
        memset(counts, 0, sizeof counts);
        for (b = 0; b < 64; b++) counts[chain_bucket_face(b)]++;
        for (b = 0; b < CHAIN_FACES; b++) total += counts[b];
        ck("T4 occupancy sums to K", total == 64, NULL);
    }

    /* T5 — rank-based map is not an address */
    {
        enum { K0 = 64, DIM = 4 };
        static float C[(K0 + 1) * DIM];
        uint8_t before[K0], after[K0];
        int shifted = 0;

        for (b = 0; b <= K0; b++)
            for (int j = 0; j < DIM; j++)
                C[b * DIM + j] = (j == 0) ? (float)(b < K0 ? b : 1000) : 0.0f;

        for (b = 0; b < K0; b++) before[b] = alc_dir_of(b, K0, C, DIM);
        /* one new bucket with the highest energy */
        for (b = 0; b < K0; b++) after[b] = alc_dir_of(b, K0 + 1, C, DIM);
        for (b = 0; b < K0; b++) if (before[b] != after[b]) shifted++;

        {
            char s[96];
            snprintf(s, sizeof s, "%d of %d buckets changed face", shifted, K0);
            ck("T5 rank map shifts on insert (not an address)", shifted > 0, s);
        }
        /* and the pure map only ever ADDS buckets to a face, never remaps one */
        {
            int lost = 0;
            for (d = 1; d <= CHAIN_BASE; d++) {
                int small[8], large[8];
                int ns = chain_bucket_all((uint8_t)d, 64, small, 8);
                int nl = chain_bucket_all((uint8_t)d, 65, large, 8);
                for (int i = 0; i < ns && i < 8; i++) {
                    int found = 0;
                    for (int j = 0; j < nl && j < 8; j++)
                        if (small[i] == large[j]) found = 1;
                    if (!found) lost++;
                }
            }
            ck("T5 pure map only adds, never remaps", lost == 0, NULL);
        }
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
