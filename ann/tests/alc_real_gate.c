/* tests/alc_real_gate.c — placement gate for the LetterCube anchor bridge, on
 * REAL index data (no mocks).
 *
 * WHAT IT PROVES, and what it refuses to claim:
 *   Every row of the index is routed to its anchor bucket by the SAME k-means
 *   call the served path uses (anch_route), then the bucket's L2 direction and
 *   its stored entry at each level are recovered WITHOUT touching the row vector
 *   again. A row whose recovered direction differs from its bucket's direction at
 *   any level fails the gate. It does NOT measure recall or latency: those need a
 *   graph/HNSW layer that this core does not have, so the gate says so and stops.
 *
 * Usage: alc_real_gate [index.jsonl] [ann_anchors.bin] [max_rows (0 = all)]
 * BUILD: gcc -O2 -std=c11 -Iinclude -o build/alc_real_gate tests/alc_real_gate.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#include "anchor_lettercube.h"

static const char *val(const char *line, const char *key)
{
    static char pat[32];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(line, pat);
    if (!p) return NULL;
    p = strchr(p + strlen(pat), ':');
    if (!p) return NULL;
    p++;
    while (*p == ' ') p++;
    return p;
}

/* same shape ann_search.c's lz_entry_parse uses: vec is a bare float array */
static int parse_vec(const char *line, float *vec, int *dim_out, int cap)
{
    const char *p = val(line, "vec");
    if (!p || *p != '[') return -1;
    p++;
    int n = 0;
    while (*p == '[') p++;
    while (*p && *p != ']' && n < cap) {
        while (*p == ' ' || *p == ',') p++;
        if (*p == ']' || !*p) break;
        vec[n++] = (float)atof(p);
        while (*p && *p != ',' && *p != ']') p++;
    }
    *dim_out = n;
    return n > 0 ? 0 : -1;
}

int main(int argc, char **argv)
{
    const char *ip = (argc > 1) ? argv[1] : "data/memcore/ann_index.jsonl";
    const char *ap = (argc > 2) ? argv[2] : "data/memcore/ann_anchors.bin";
    long maxrows = (argc > 3) ? atol(argv[3]) : 0;

    static float C[ANCHR_MAXK * ANCHR_MAXD];
    int dim = 0, ntr = 0;
    int K = anch_load(ap, C, ANCHR_MAXK, ANCHR_MAXD, &dim, &ntr);
    if (K <= 0) { fprintf(stderr, "anch_load(%s) -> %d\n", ap, K); return 2; }
    printf("{\"event\":\"anchors\",\"path\":\"%s\",\"K\":%d,\"dim\":%d,\"ntrained\":%d}\n",
           ap, K, dim, ntr);

    /* per-bucket identity + stored entry at each level, derived ONCE */
    static uint8_t  dir[ANCHR_MAXK];
    static LCREntry ent[LCR_LEVELS][ANCHR_MAXK];
    static int      off_face[LCR_LEVELS][ANCHR_MAXK];
    int invalid_dir = 0;
    for (int k = 0; k < K; k++) {
        dir[k] = alc_dir_of(k, K, C, dim);
        if (dir[k] == 0) invalid_dir++;
        for (uint8_t L = 0; L < LCR_LEVELS; L++) {
            ent[L][k] = alc_entry_of(k, K, C, dim, L);
            off_face[L][k] = (ent[L][k].face != dir[k]);
        }
    }
    {   /* bucket-level receipt: how many buckets sit off their own direction */
        for (uint8_t L = 0; L < LCR_LEVELS; L++) {
            int off = 0, used = 0, seen[LCR_FACES];
            memset(seen, 0, sizeof(seen));
            for (int k = 0; k < K; k++) {
                off += off_face[L][k];
                seen[LCR_FACE_ID[ent[L][k].face]] = 1;
            }
            for (int i = 0; i < LCR_FACES; i++) used += seen[i];
            printf("{\"event\":\"bucket-level\",\"level\":%u,\"active_faces\":%d,"
                   "\"faces_used\":%d,\"buckets_off_dir\":%d}\n",
                   L, LCR_LEVEL_FACES[L], used, off);
        }
    }

    FILE *f = fopen(ip, "r");
    if (!f) { perror(ip); return 2; }
    static char  line[16384];
    static float vec[ANCHR_MAXD];
    long lines = 0, parsed = 0, skipped = 0, mismatch = 0, finer = 0, pair_ok = 0;
    long face_rows[LCR_FACES];
    memset(face_rows, 0, sizeof(face_rows));

    while (fgets(line, sizeof(line), f)) {
        if (maxrows > 0 && lines >= maxrows) break;
        lines++;
        int d = 0;
        if (parse_vec(line, vec, &d, ANCHR_MAXD) != 0 || d != dim) { skipped++; continue; }
        int t2[ANCHR_MAXK];
        int nb = anch_route(vec, C, K, dim, 2, t2);   /* same topb as the served route */
        if (nb < 1) { skipped++; continue; }
        parsed++;
        int b = t2[0];
        if (b < 0 || b >= K) { skipped++; continue; }
        /* the whole promotion chain, read from the precomputed table only */
        for (uint8_t L = 0; L < LCR_LEVELS; L++)
            if (lcr_entry_dir(ent[L][b]) != dir[b]) mismatch++;
        face_rows[LCR_FACE_ID[dir[b]]]++;
        if (lcr_parent(dir[b], 0) != dir[b]) finer++;   /* needs L1/L2 to be named */
        if (nb == 2) pair_ok++;
    }
    fclose(f);

    int used = 0;
    for (int i = 0; i < LCR_FACES; i++) used += (face_rows[i] > 0);
    printf("{\"event\":\"rows\",\"file\":\"%s\",\"lines\":%ld,\"parsed\":%ld,"
           "\"skipped\":%ld,\"second_bucket\":%ld}\n",
           ip, lines, parsed, skipped, pair_ok);
    printf("{\"event\":\"placement\",\"L2_faces_used\":%d,\"rows_finer_than_L0\":%ld}\n",
           used, finer);
    printf("{\"event\":\"lossless\",\"checks\":%ld,\"mismatch\":%ld,\"invalid_dir\":%d}\n",
           parsed * LCR_LEVELS, mismatch, invalid_dir);
    int pass = (parsed > 0) && mismatch == 0 && invalid_dir == 0;
    printf("{\"event\":\"verdict\",\"pass\":%d,\"scope\":\"placement+losslessness only; "
           "recall/latency need a graph layer this core does not have\"}\n", pass);
    return pass ? 0 : 1;
}
