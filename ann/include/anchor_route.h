/* anchor_route.h — C port of the SIFT-proven anchor-bucket router + geo_jump placement.
 *
 * Two jobs, kept separate (PLAN-HNSW-MAZE.md mechanism map):
 *   ANCHORS decide (semantic ranking: nearest-centroid L2, top-b route).
 *   geo_jump PLACES (MOD-37 slot(i)=(i*37)%n bijection + node-sorted perm).
 *
 * Determinism: Lloyd init picks K evenly-spaced rows (no RNG at all);
 * fixed iteration cap; file carries K/dim/ntrained/checksum. Same bytes in
 * → same anchors out, byte-identical file rewrite.
 *
 * Header-only, std-only (stdio/stdlib/string/stdint/float). No separate build.
 */
#ifndef ANCHOR_ROUTE_H
#define ANCHOR_ROUTE_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <float.h>

#define ANCHR_MAGIC   0x414E4352u /* "ANCR" */
#define ANCHR_MAXK    512
#define ANCHR_MAXD    1024
#define ANCHR_ITERS   25

/* placement f(i): (i*37)%n. 37 coprime with every n not divisible by 37;
 * for n = 37*m the map still permutes within residue classes — callers with
 * n%37==0 bump n by one slot. 0..n-1 in, 0..n-1 out. */
static inline uint32_t anch_slot(uint32_t i, uint32_t n) {
    return (uint32_t)(((uint64_t)i * 37u) % n);
}

/* nearest centroid (squared L2, float). Returns bucket id or -1. */
static inline int anch_assign(const float *q, const float *C, int K, int dim) {
    if (!q || !C || K <= 0 || dim <= 0) return -1;
    int best = -1;
    double bd = DBL_MAX;
    for (int k = 0; k < K; k++) {
        double d = 0;
        const float *c = C + (size_t)k * dim;
        for (int j = 0; j < dim; j++) {
            double e = (double)q[j] - c[j];
            d += e * e;
        }
        if (d < bd) { bd = d; best = k; }
    }
    return best;
}

/* top-b buckets by centroid distance (partial selection; K unbounded, O(K)
 * scratch via malloc; out must hold K ints). Returns min(topb,K). */
static inline int anch_route(const float *q, const float *C, int K, int dim,
                             int topb, int *out) {
    if (!q || !C || !out || K <= 0 || dim <= 0 || topb <= 0) return 0;
    if (topb > K) topb = K;
    double *bd = (double *)malloc((size_t)K * sizeof(double));
    if (!bd) return 0;
    for (int k = 0; k < K; k++) {
        double d = 0;
        const float *c = C + (size_t)k * dim;
        for (int j = 0; j < dim; j++) {
            double e = (double)q[j] - c[j];
            d += e * e;
        }
        bd[k] = d;
        out[k] = k;
    }
    /* partial selection sort on first topb */
    for (int a = 0; a < topb; a++)
        for (int b = a + 1; b < K; b++)
            if (bd[out[b]] < bd[out[a]]) { int t = out[a]; out[a] = out[b]; out[b] = t; }
    free(bd);
    return topb;
}

/* Lloyd k-means, deterministic init (K evenly-spaced rows), fixed iters.
 * X: n×dim row-major. C_out: K×dim. lab_out: n (may be NULL).
 * Returns 0=ok, -1=bad args. Empty cluster keeps its old centroid. */
static inline int anch_train(const float *X, int n, int dim, int K,
                             float *C_out, int *lab_out) {
    if (!X || !C_out || n <= 0 || dim <= 0 || K <= 0 || K > n) return -1;
    if (dim > ANCHR_MAXD || K > ANCHR_MAXK) return -1;
    for (int k = 0; k < K; k++)
        memcpy(C_out + (size_t)k * dim, X + (size_t)(k * n / K) * dim,
               (size_t)dim * sizeof(float));
    int *lab = lab_out ? lab_out : (int *)malloc((size_t)n * sizeof(int));
    if (!lab) return -1;
    double *sums = (double *)calloc((size_t)K * dim, sizeof(double));
    int *cnt = (int *)calloc((size_t)K, sizeof(int));
    if (!sums || !cnt) { if (!lab_out) free(lab); free(sums); free(cnt); return -1; }
    for (int it = 0; it < ANCHR_ITERS; it++) {
        for (int i = 0; i < n; i++)
            lab[i] = anch_assign(X + (size_t)i * dim, C_out, K, dim);
        memset(sums, 0, (size_t)K * dim * sizeof(double));
        memset(cnt, 0, (size_t)K * sizeof(int));
        for (int i = 0; i < n; i++) {
            int k = lab[i];
            if (k < 0) continue;
            cnt[k]++;
            for (int j = 0; j < dim; j++)
                sums[(size_t)k * dim + j] += X[(size_t)i * dim + j];
        }
        for (int k = 0; k < K; k++) {
            if (!cnt[k]) continue;
            for (int j = 0; j < dim; j++)
                C_out[(size_t)k * dim + j] = (float)(sums[(size_t)k * dim + j] / cnt[k]);
        }
    }
    free(sums);
    free(cnt);
    if (!lab_out) free(lab);
    return 0;
}

/* node-sorted perm: bucket-major (anchor 0..K-1), members inside a bucket
 * ordered by slot key (t*37)%nn (geo_jump MOD-bijection placement form).
 * Sorted by (key, member) so the output is always a true permutation.
 * perm[j] = row index of j-th stored item. rows: n anchor ids. 0=ok. */
typedef struct { uint32_t key; int idx; } AnchSlotPair;
static int anch_slotpair_cmp(const void *a, const void *b) {
    const AnchSlotPair *x = (const AnchSlotPair *)a, *y = (const AnchSlotPair *)b;
    if (x->key != y->key) return (x->key > y->key) - (x->key < y->key);
    return (x->idx > y->idx) - (x->idx < y->idx);
}
static inline int anch_perm(const int *rows, int n, int K, uint32_t *perm) {
    if (!rows || !perm || n <= 0 || K <= 0) return -1;
    int *cnt = (int *)calloc((size_t)K, sizeof(int));
    int *fill = (int *)calloc((size_t)K, sizeof(int));
    int **mem = (int **)calloc((size_t)K, sizeof(int *));
    if (!cnt || !fill || !mem) { free(cnt); free(fill); free(mem); return -1; }
    for (int i = 0; i < n; i++)
        if (rows[i] >= 0 && rows[i] < K) cnt[rows[i]]++;
    for (int k = 0; k < K; k++)
        if (cnt[k]) { mem[k] = (int *)malloc((size_t)cnt[k] * sizeof(int)); if (!mem[k]) { for (int j = 0; j < k; j++) free(mem[j]); free(mem); free(cnt); free(fill); return -1; } }
    for (int i = 0; i < n; i++) {
        int k = rows[i];
        if (k < 0 || k >= K) continue;
        mem[k][fill[k]++] = i;
    }
    AnchSlotPair *sp = NULL;
    size_t spcap = 0;
    uint32_t w = 0;
    for (int k = 0; k < K; k++) {
        int m = cnt[k];
        if (m == 0) continue;
        if ((size_t)m > spcap) {
            free(sp);
            sp = (AnchSlotPair *)malloc((size_t)m * sizeof(AnchSlotPair));
            if (!sp) { for (int j = 0; j < K; j++) free(mem[j]); free(mem); free(cnt); free(fill); return -1; }
            spcap = (size_t)m;
        }
        uint32_t nn = (uint32_t)m;
        if (nn % 37u == 0) nn++; /* keep the MOD map bijective */
        for (int t = 0; t < m; t++) {
            sp[t].key = anch_slot((uint32_t)t, nn) % (uint32_t)m;
            sp[t].idx = mem[k][t];
        }
        qsort(sp, (size_t)m, sizeof(AnchSlotPair), anch_slotpair_cmp);
        for (int t = 0; t < m; t++) perm[w++] = (uint32_t)sp[t].idx;
        free(mem[k]);
    }
    free(sp); free(mem); free(fill); free(cnt);
    if (w == (uint32_t)n) return 0;
    /* unassigned rows (anchor -1) ride at the tail in input order */
    for (int i = 0; i < n && w < (uint32_t)n; i++)
        if (rows[i] < 0 || rows[i] >= K) perm[w++] = (uint32_t)i;
    return (w == (uint32_t)n) ? 0 : -1;
}

/* persisted anchor file: magic,u32 K,dim,ntrained,checksum, then K*dim floats.
 * checksum = FNV-1a over centroid bytes (integrity is free per plan). */
static inline uint32_t anch_cksum(const float *C, int K, int dim) {
    uint32_t h = 2166136261u;
    const unsigned char *p = (const unsigned char *)C;
    size_t nb = (size_t)K * dim * sizeof(float);
    for (size_t i = 0; i < nb; i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

static inline int anch_save(const char *path, const float *C, int K, int dim, int ntr) {
    if (!path || !C || K <= 0 || dim <= 0) return -1;
    FILE *f = fopen(path, "wb");
    if (!f) return -1;
    uint32_t hdr[5] = { ANCHR_MAGIC, (uint32_t)K, (uint32_t)dim, (uint32_t)ntr,
                        anch_cksum(C, K, dim) };
    int ok = fwrite(hdr, sizeof(hdr), 1, f) == 1 &&
             fwrite(C, sizeof(float), (size_t)K * dim, f) == (size_t)K * dim;
    fclose(f);
    return ok ? 0 : -1;
}

/* Atomic variant: byte-identical to anch_save, written to path.tmp then
 * renamed over path (readers never see a half-written file; after a crash
 * either the old or the new complete file survives). 0=ok, -1=err. */
static inline int anch_save_atomic(const char *path, const float *C, int K, int dim, int ntr) {
    if (!path || !C || K <= 0 || dim <= 0) return -1;
    char tmp[512];
    if (snprintf(tmp, sizeof(tmp), "%s.tmp", path) >= (int)sizeof(tmp)) return -1;
    if (anch_save(tmp, C, K, dim, ntr) != 0) { remove(tmp); return -1; }
    remove(path);
    if (rename(tmp, path) != 0) { remove(tmp); return -1; }
    return 0;
}

/* Loads into caller buffer C (cap K*dim floats). Returns K or negative. */
static inline int anch_load(const char *path, float *C, int capK, int capD,
                            int *dim_out, int *ntr_out) {
    if (!path || !C) return -1;
    FILE *f = fopen(path, "rb");
    if (!f) return -1;
    uint32_t hdr[5];
    int r = -1;
    if (fread(hdr, sizeof(hdr), 1, f) == 1 && hdr[0] == ANCHR_MAGIC) {
        int K = (int)hdr[1], dim = (int)hdr[2];
        if (K > 0 && K <= capK && dim > 0 && dim <= capD) {
            if (fread(C, sizeof(float), (size_t)K * dim, f) == (size_t)K * dim &&
                anch_cksum(C, K, dim) == hdr[4]) {
                if (dim_out) *dim_out = dim;
                if (ntr_out) *ntr_out = (int)hdr[3];
                r = K;
            } else r = -3; /* corrupt */
        } else r = -2; /* shape mismatch */
    }
    fclose(f);
    return r;
}

#endif /* ANCHOR_ROUTE_H */
