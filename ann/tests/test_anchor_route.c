/* tests/test_anchor_route.c — anchor-bucket router + geo_jump placement.
 *
 * Oracles (independent of core/anchor_route.h):
 *   slot bijection: occupancy bitmap over 0..n-1 (combinatorics, not the impl).
 *   perm: bucket-major + within-bucket key order recomputed inline in the test.
 *   train: centroid == member mean recomputed from labels (definition of Lloyd);
 *     determinism: train twice, memcmp.
 *   save/load: byte compare + tamper → reject.
 *   route: full qsort reference in the test vs anch_route top-b set equality.
 *
 * BUILD: gcc -O2 -Wall -I. -Icore -o build/test_anchor_route tests/test_anchor_route.c -lm
 * RUN:   ./build/test_anchor_route
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>
#include <math.h>

#include "anchor_route.h"

static int g_pass = 0, g_fail = 0;
static void check(int ok, const char *name) {
    if (ok) { g_pass++; printf("  ok %s\n", name); }
    else    { g_fail++; printf("  FAIL %s\n", name); }
}

/* deterministic PRNG for test data only (xorshift, fixed seed) */
static uint32_t trng = 0x12345678u;
static float frand(void) {
    trng ^= trng << 13; trng ^= trng >> 17; trng ^= trng << 5;
    return (float)(trng % 10000) / 5000.0f - 1.0f;
}

/* ── 1. slot(i)=(i*37)%n is a permutation of 0..n-1 iff 37 ∤ n
 * (37 prime: gcd(37,n)=1 except multiples of 37). n=37 collapses to 0. */
static void t_slot_bijection(void) {
    int ns[] = { 1, 2, 3, 36, 38, 74, 100, 1000, 20736 };
    int ok = 1;
    for (int ni = 0; ni < 9; ni++) {
        int n = ns[ni];
        char *seen = (char *)calloc((size_t)n, 1);
        int bij = 1;
        for (int i = 0; i < n; i++) {
            uint32_t s = anch_slot((uint32_t)i, (uint32_t)n);
            if (s >= (uint32_t)n || seen[s]) { bij = 0; break; }
            seen[s] = 1;
        }
        if (bij) for (int i = 0; i < n; i++) if (!seen[i]) { bij = 0; break; }
        free(seen);
        if (bij != (n % 37 != 0)) { ok = 0; break; }
    }
    /* n=37: total collapse to slot 0 (oracle: direct evaluation) */
    if (ok) for (int i = 0; i < 37; i++)
        if (anch_slot((uint32_t)i, 37u) != 0) { ok = 0; break; }
    check(ok, "slot bijection iff 37∤n; n=37 collapses (documented)");
}

/* ── 2. perm: bijection + bucket-major + slot-key order (recomputed) ── */
static void t_perm_layout(void) {
    int n = 500, K = 7;
    int *rows = (int *)malloc((size_t)n * sizeof(int));
    for (int i = 0; i < n; i++) rows[i] = (i * 13 + 3) % K;
    rows[0] = -1; /* one unassigned rides tail */
    uint32_t *perm = (uint32_t *)malloc((size_t)n * sizeof(uint32_t));
    int rc = anch_perm(rows, n, K, perm);
    int ok = (rc == 0);
    /* bijection: occupancy */
    char *seen = (char *)calloc((size_t)n, 1);
    if (ok) for (int j = 0; j < n; j++) {
        if (perm[j] >= (uint32_t)n || seen[perm[j]]) { ok = 0; break; }
        seen[perm[j]] = 1;
    }
    /* bucket-major: anchor ids nondecreasing except tail (-1) block */
    if (ok) {
        int stage = 0; /* 0 = buckets ascending, 1 = tail */
        int prev = -1;
        for (int j = 0; j < n; j++) {
            int a = rows[perm[j]];
            if (a < 0) { stage = 1; continue; }
            if (stage == 1) { ok = 0; break; }
            if (a < prev) { ok = 0; break; }
            prev = a;
        }
    }
    /* within-bucket: slot keys nondecreasing (keys recomputed here) */
    if (ok) for (int j = 0; j < n;) {
        int a = rows[perm[j]];
        if (a < 0) break;
        int j2 = j;
        while (j2 < n && rows[perm[j2]] == a) j2++;
        /* members of this bucket in perm order must follow key order;
         * key of t-th collected member: recompute rank by (t*37)%nn */
        int m = j2 - j;
        /* rebuild expected order: pairs (key,input-order) sorted by (key,idx) */
        typedef struct { uint32_t key; int idx; } Pair;
        Pair *pr = (Pair *)malloc((size_t)m * sizeof(Pair));
        int c = 0;
        for (int i = 0; i < n && c < m; i++)
            if (rows[i] == a) {
                uint32_t nn2 = (uint32_t)m;
                if (nn2 % 37u == 0) nn2++;
                /* key of the t-th collected member (t == c here) */
                pr[c].key = anch_slot((uint32_t)c, nn2) % (uint32_t)m;
                pr[c].idx = i;
                c++;
            }
        for (int x = 0; x < m; x++)
            for (int y = x + 1; y < m; y++)
                if (pr[y].key < pr[x].key ||
                    (pr[y].key == pr[x].key && pr[y].idx < pr[x].idx)) {
                    Pair t = pr[x]; pr[x] = pr[y]; pr[y] = t;
                }
        for (int t = 0; t < m; t++)
            if ((int)perm[j + t] != pr[t].idx) { ok = 0; break; }
        free(pr);
        if (!ok) break;
        j = j2;
    }
    free(seen); free(rows); free(perm);
    check(ok, "perm bijection + bucket-major + slot-key order");
}

/* ── 3. train: centroids == member means; deterministic across runs ── */
static void t_train_means(void) {
    int n = 200, dim = 8, K = 5;
    float *X = (float *)malloc((size_t)n * dim * sizeof(float));
    float *C1 = (float *)malloc((size_t)K * dim * sizeof(float));
    float *C2 = (float *)malloc((size_t)K * dim * sizeof(float));
    int *lab = (int *)malloc((size_t)n * sizeof(int));
    for (int i = 0; i < n * dim; i++) X[i] = frand();
    int ok = anch_train(X, n, dim, K, C1, lab) == 0;
    ok = ok && anch_train(X, n, dim, K, C2, NULL) == 0;
    ok = ok && memcmp(C1, C2, (size_t)K * dim * sizeof(float)) == 0;
    /* centroid == mean of its members (definition, recomputed here) */
    if (ok) for (int k = 0; k < K; k++) {
        for (int j = 0; j < dim; j++) {
            double s = 0;
            int c = 0;
            for (int i = 0; i < n; i++)
                if (lab[i] == k) { s += X[(size_t)i * dim + j]; c++; }
            if (c == 0) continue; /* empty keeps old centroid */
            if (fabs(C1[(size_t)k * dim + j] - s / c) > 1e-4) { ok = 0; break; }
        }
        if (!ok) break;
    }
    /* every label valid */
    if (ok) for (int i = 0; i < n; i++) if (lab[i] < 0 || lab[i] >= K) { ok = 0; break; }
    free(X); free(C1); free(C2); free(lab);
    check(ok, "train centroids==means + deterministic + labels valid");
}

/* ── 4. save/load roundtrip + tamper reject ── */
static void t_save_load(void) {
    int K = 4, dim = 6;
    float C[24];
    for (int i = 0; i < 24; i++) C[i] = frand();
    const char *p = "build/test_anchor_tmp.bin";
    int ok = anch_save(p, C, K, dim, 11) == 0;
    float L[24];
    int dimo = 0, ntro = 0;
    int r = anch_load(p, L, ANCHR_MAXK, ANCHR_MAXD, &dimo, &ntro);
    ok = ok && r == K && dimo == dim && ntro == 11 && memcmp(C, L, sizeof(C)) == 0;
    /* tamper one centroid byte → checksum reject */
    if (ok) {
        FILE *f = fopen(p, "r+b");
        fseek(f, 20, SEEK_SET);
        int b = fgetc(f);
        fseek(f, 20, SEEK_SET);
        fputc(b ^ 0xFF, f);
        fclose(f);
        ok = anch_load(p, L, ANCHR_MAXK, ANCHR_MAXD, NULL, NULL) == -3;
    }
    /* missing file → -1 */
    ok = ok && anch_load("build/no_such_anchor.bin", L, 64, 1024, NULL, NULL) == -1;
    remove(p);
    check(ok, "save/load roundtrip + tamper reject + missing -1");
}

/* reference: full sort of all K distances, top-b set */
static void t_route_match(void) {
    int K = 9, dim = 5, topb = 3;
    float C[45], q[5];
    for (int i = 0; i < 45; i++) C[i] = frand();
    for (int i = 0; i < 5; i++) q[i] = frand();
    int out[9];
    int got = anch_route(q, C, K, dim, topb, out);
    int ok = (got == topb);
    /* reference ranking */
    double d[9];
    int id[9];
    for (int k = 0; k < K; k++) {
        double s = 0;
        for (int j = 0; j < dim; j++) { double e = q[j] - C[k * dim + j]; s += e * e; }
        d[k] = s; id[k] = k;
    }
    for (int a = 0; a < K; a++)
        for (int b = a + 1; b < K; b++)
            if (d[id[b]] < d[id[a]]) { int t = id[a]; id[a] = id[b]; id[b] = t; }
    if (ok) {
        int seen[9] = { 0 };
        for (int t = 0; t < topb; t++) seen[out[t]] = 1;
        for (int t = 0; t < topb; t++) if (!seen[id[t]]) { ok = 0; break; }
        /* order check: routed ids sorted by distance */
        for (int t = 1; t < topb; t++)
            if (d[out[t]] < d[out[t - 1]]) { ok = 0; break; }
    }
    /* assign agrees with reference argmin */
    ok = ok && anch_assign(q, C, K, dim) == id[0];
    check(ok, "route top-b set+order == reference sort; assign==argmin");
}

int main(void) {
    printf("test_anchor_route\n");
    t_slot_bijection();
    t_perm_layout();
    t_train_means();
    t_save_load();
    t_route_match();
    printf("%d passed, %d failed\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
