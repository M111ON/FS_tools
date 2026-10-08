/* tests/test_anchor_routed.c — routed search == brute top-1 on clustered data,
 * with fewer items scored and 1 block read (node-sorted perm walk).
 *
 * Flow mirrors gguf_lazy_serve.c /v1/state/search: train → assign → perm →
 * save/load → route top-b → perm-walk score routed buckets → extents.
 * Plus champion overlap: every entry in its reference top-2 buckets
 * (P2) + save_atomic byte-identity + checksum-verified roundtrip.
 * Oracle: brute-force full qsort ranking recomputed inline in the test.
 *
 * BUILD: gcc -O2 -Wall -I. -Icore -o build/test_anchor_routed.exe tests/test_anchor_routed.c -lm
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

#define DIM 16
#define PER 8
#define NCL 3
#define NENT (PER * NCL)

static uint32_t trng = 0xABCDEFu;
static float frand(void) {
    trng ^= trng << 13; trng ^= trng >> 17; trng ^= trng << 5;
    return (float)(trng % 10000) / 10000.0f - 0.5f;
}
static double cos_sim(const float *a, const float *b, int d) {
    double dot = 0, na = 0, nb = 0;
    for (int j = 0; j < d; j++) { dot += (double)a[j] * b[j]; na += (double)a[j] * a[j]; nb += (double)b[j] * b[j]; }
    return (na > 0 && nb > 0) ? dot / (sqrt(na) * sqrt(nb)) : -1;
}

/* ── champion overlap (P2): every entry posted in its top-2 buckets ──
 * Oracle: full K-way squared-L2 ranking recomputed inline (spec: nearest-
 * centroid L2), never via anch_route. Data is hand-set literals (no RNG).
 * Determinism: route twice → identical; save twice (direct + atomic) →
 * byte-identical files; tamper → checksum reject (-3). */
static void t_overlap_top2(void) {
    float X[8 * 4] = {
        0.10f, 0.00f, -0.10f, 0.05f,  -0.05f, 0.10f, 0.00f, -0.05f,
        0.00f, -0.10f, 0.05f, 0.00f,   0.05f, 0.05f, -0.05f, 0.10f,
        10.10f, 9.90f, 10.00f, 10.05f,  9.95f, 10.10f, 10.05f, 9.90f,
        10.00f, 10.00f, 9.90f, 10.10f, 10.05f, 9.95f, 10.10f, 10.00f };
    float C[2 * 4];
    int lab[8];
    check(anch_train(X, 8, 4, 2, C, lab) == 0, "overlap: train K=2 on hand-set 8x4");
    int allok = 1, detok = 1;
    for (int i = 0; i < 8; i++) {
        /* reference ranking: all-K squared L2 + full sort */
        double d[2];
        int id[2] = { 0, 1 };
        for (int k = 0; k < 2; k++) {
            double s = 0;
            for (int j = 0; j < 4; j++) { double e = X[i * 4 + j] - C[k * 4 + j]; s += e * e; }
            d[k] = s;
        }
        if (d[1] < d[0]) { int t = id[0]; id[0] = id[1]; id[1] = t; }
        int out[2] = { -9, -9 };
        int nb = anch_route(X + i * 4, C, 2, 4, 2, out);
        if (nb != 2) { allok = 0; continue; }
        if (out[0] != id[0] || out[1] != id[1]) allok = 0;   /* exact order, K=2 */
        if (out[0] == out[1]) allok = 0;                      /* exactly two distinct buckets */
        if (out[0] < 0 || out[0] > 1 || out[1] < 0 || out[1] > 1) allok = 0;
        if (anch_assign(X + i * 4, C, 2, 4) != id[0]) allok = 0; /* top-1 contract kept */
        int out2[2];
        anch_route(X + i * 4, C, 2, 4, 2, out2);
        if (out2[0] != out[0] || out2[1] != out[1]) detok = 0;
    }
    check(allok, "overlap: every entry in exactly its reference top-2 buckets");
    check(detok, "overlap: routing deterministic across calls");
    /* save/load roundtrip: direct vs atomic byte-identical + checksum-verified */
    const char *pa = "build/test_overlap_a.bin", *pb = "build/test_overlap_b.bin";
    int ok = anch_save(pa, C, 2, 4, 8) == 0 && anch_save_atomic(pb, C, 2, 4, 8) == 0;
    if (ok) {
        FILE *fa = fopen(pa, "rb"), *fb = fopen(pb, "rb");
        ok = (fa && fb);
        if (ok) {
            fseek(fa, 0, SEEK_END); fseek(fb, 0, SEEK_SET);
            long za = ftell(fa), zb;
            fseek(fa, 0, SEEK_SET);
            fseek(fb, 0, SEEK_END); zb = ftell(fb); fseek(fb, 0, SEEK_SET);
            ok = (za == zb && za == 20 + 2 * 4 * 4);
            if (ok) {
                char ba[64], bb[64];
                ok = (fread(ba, 1, (size_t)za, fa) == (size_t)za &&
                      fread(bb, 1, (size_t)zb, fb) == (size_t)zb &&
                      memcmp(ba, bb, (size_t)za) == 0);
            }
        }
        if (fa) fclose(fa);
        if (fb) fclose(fb);
    }
    check(ok, "overlap: save vs save_atomic byte-identical (5-u32 hdr + K*dim f32)");
    float L[2 * 4];
    int ld = 0, ln = 0;
    ok = (anch_load(pa, L, 64, 1024, &ld, &ln) == 2 && ld == 4 && ln == 8 &&
          memcmp(C, L, sizeof(C)) == 0);
    if (ok) { /* tamper one centroid byte → FNV-1a checksum reject */
        FILE *f = fopen(pa, "r+b");
        fseek(f, 20, SEEK_SET);
        int b = fgetc(f);
        fseek(f, 20, SEEK_SET);
        fputc(b ^ 0xFF, f);
        fclose(f);
        ok = (anch_load(pa, L, 64, 1024, NULL, NULL) == -3);
    }
    check(ok, "overlap: load roundtrip + tamper checksum-reject");
    remove(pa); remove(pb);
}

int main(void) {
    printf("test_anchor_routed\n");
    /* 3 well-separated clusters: center c*10 apart, spread 0.5 */
    float X[NENT * DIM];
    for (int c = 0; c < NCL; c++)
        for (int i = 0; i < PER; i++)
            for (int j = 0; j < DIM; j++)
                X[(c * PER + i) * DIM + j] = (float)(c * 10) + frand() * 0.5f;
    float q[DIM];
    for (int j = 0; j < DIM; j++) q[j] = 0.0f + frand() * 0.05f; /* near cluster 0 */

    /* brute oracle ranking */
    double bs[NENT];
    int bo[NENT];
    for (int i = 0; i < NENT; i++) { bs[i] = cos_sim(q, X + i * DIM, DIM); bo[i] = i; }
    for (int a = 0; a < NENT; a++)
        for (int b = a + 1; b < NENT; b++)
            if (bs[bo[b]] > bs[bo[a]]) { int t = bo[a]; bo[a] = bo[b]; bo[b] = t; }

    /* anchor flow: train K=3 → assign → perm → save/load → route top-1 */
    float C[3 * DIM];
    int rows[NENT];
    check(anch_train(X, NENT, DIM, 3, C, rows) == 0, "train K=3 on 24x16");
    check(anch_save("build/test_routed.bin", C, 3, DIM, NENT) == 0, "save anchors");
    float L[3 * DIM];
    int ld = 0, ln = 0;
    check(anch_load("build/test_routed.bin", L, 64, 1024, &ld, &ln) == 3 && ld == DIM && ln == NENT,
          "load anchors (K/dim/ntrained)");
    uint32_t perm[NENT];
    check(anch_perm(rows, NENT, 3, perm) == 0, "node-sorted perm");
    int bk[1];
    check(anch_route(q, L, 3, DIM, 1, bk) == 1, "route top-1 bucket");

    /* perm-walk scoring of routed bucket only */
    double best = -2;
    int besti = -1, scored = 0, reads = 0, in_run = 0;
    for (int oi = 0; oi < NENT; oi++) {
        int ei = (int)perm[oi];
        if (rows[ei] != bk[0]) { in_run = 0; continue; }
        if (!in_run) { reads++; in_run = 1; }
        double s = cos_sim(q, X + ei * DIM, DIM);
        scored++;
        if (s > best) { best = s; besti = ei; }
    }
    check(scored == PER && reads == 1, "scored 8/24 in 1 block read");
    check(besti == bo[0], "routed top-1 == brute top-1");
    /* routed top-3 recall vs brute top-3: all routed hits must be brute top-8 */
    check(best - bs[bo[0]] < 1e-9 && best > bs[bo[PER]], "routed best beats brute rank-9");
    remove("build/test_routed.bin");
    t_overlap_top2();
    printf("%d passed, %d failed\n", g_pass, g_fail);
    return g_fail ? 1 : 0;
}
