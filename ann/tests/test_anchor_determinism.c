/* tests/test_anchor_determinism.c — standing determinism proof for the anchor index.
 *
 * Oracles (independent of core/anchor_route.h, from math/spec):
 *   train: identical input bytes -> identical output bytes (pure function of
 *     input; Lloyd init = evenly-spaced rows, fixed iters, no RNG by spec).
 *   save: byte-identical files <=> byte-identical centroid buffers (fwrite of
 *     fixed header + raw floats; compared here with a fread loop, not the impl).
 *   route: identical query+centroids -> identical bucket ids and order
 *     (nearest-centroid argmin is a pure function; compared across two runs).
 *   tamper: 1-bit flip in centroid bytes -> FNV-1a mismatch -> anch_load
 *     must reject (checksum recomputed INLINE here, independent of anch_cksum).
 *
 * Deterministic: no RNG anywhere. Dataset is a closed-form arithmetic formula
 * (hardcoded constants), so the bytes are fixed by the source text.
 *
 * BUILD: gcc -O2 -Wall -I. -Icore -o build/test-test_anchor_determinism tests/test_anchor_determinism.c -lm
 * RUN:   ./build/test-test_anchor_determinism
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#include "anchor_route.h"

#define DET_N   200
#define DET_DIM 16
#define DET_K   8
#define DET_NQ  20
#define DET_TOPB 3

/* Fixed synthetic dataset: closed-form arithmetic, no RNG.
 * Values in [-1,1): ((i*131 + j*17 + (i*j)%13) % 2000)/1000 - 1. */
static void det_dataset(float *X, int n, int dim, unsigned seed_k) {
    for (int i = 0; i < n; i++)
        for (int j = 0; j < dim; j++) {
            unsigned v = (unsigned)(i * 131 + j * 17 + (i * j) % 13 + seed_k);
            X[(size_t)i * dim + j] = (float)(v % 2000) / 1000.0f - 1.0f;
        }
}

/* Independent FNV-1a over raw bytes (recomputed here, not via anch_cksum). */
static uint32_t det_fnv1a(const unsigned char *p, size_t nb) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < nb; i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

/* Read whole file into malloc'd buffer; returns size, *buf set (NULL on fail). */
static long det_readfile(const char *path, unsigned char **buf) {
    FILE *f = fopen(path, "rb");
    long sz = -1;
    *buf = NULL;
    if (!f) return -1;
    fseek(f, 0, SEEK_END);
    sz = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (sz < 0) { fclose(f); return -1; }
    *buf = (unsigned char *)malloc((size_t)sz ? (size_t)sz : 1);
    if (!*buf) { fclose(f); return -1; }
    if (sz && fread(*buf, 1, (size_t)sz, f) != (size_t)sz) {
        free(*buf); *buf = NULL; fclose(f); return -1;
    }
    fclose(f);
    return sz;
}

int main(void) {
    printf("test_anchor_determinism\n");

    static float X[(size_t)DET_N * DET_DIM];
    static float Q[(size_t)DET_NQ * DET_DIM];
    static float C1[(size_t)DET_K * DET_DIM];
    static float C2[(size_t)DET_K * DET_DIM];
    static int lab1[DET_N], lab2[DET_N];
    det_dataset(X, DET_N, DET_DIM, 0u);
    det_dataset(Q, DET_NQ, DET_DIM, 7919u); /* disjoint query constants */

    /* ── 1. TRAIN TWICE -> SAVE TWICE -> BYTE-IDENTICAL ── */
    int rc1 = anch_train(X, DET_N, DET_DIM, DET_K, C1, lab1);
    int rc2 = anch_train(X, DET_N, DET_DIM, DET_K, C2, lab2);
    int train_memcmp = (rc1 == 0 && rc2 == 0)
        ? memcmp(C1, C2, sizeof(C1)) == 0 &&
          memcmp(lab1, lab2, sizeof(lab1)) == 0
        : 0;

    const char *pa = "build/anchor_det_a.bin";
    const char *pb = "build/anchor_det_b.bin";
    int sa = anch_save(pa, C1, DET_K, DET_DIM, DET_N);
    int sb = anch_save(pb, C2, DET_K, DET_DIM, DET_N);

    unsigned char *ba = NULL, *bb = NULL;
    long sza = det_readfile(pa, &ba);
    long szb = det_readfile(pb, &bb);
    int files_identical = 0;
    uint32_t cka = 0, ckb = 0;
    if (sa == 0 && sb == 0 && sza > 0 && sza == szb && ba && bb) {
        /* byte compare with fread-loop buffers (memcmp over full extent) */
        files_identical = memcmp(ba, bb, (size_t)sza) == 0;
        /* independent checksum: FNV-1a over centroid bytes vs header field */
        if ((size_t)sza >= 20 + sizeof(C1)) {
            uint32_t hdr_ck;
            memcpy(&hdr_ck, ba + 16, 4);
            cka = det_fnv1a(ba + 20, sizeof(C1));
            ckb = det_fnv1a(bb + 20, sizeof(C1));
            files_identical = files_identical && (cka == hdr_ck) && (ckb == hdr_ck);
        } else {
            files_identical = 0;
        }
    }
    printf("train files: a=%ld bytes cksum=0x%08x  b=%ld bytes cksum=0x%08x\n",
           sza, cka, szb, ckb);
    int train_ok = train_memcmp && files_identical;
    printf("RECEIPT TRAIN_IDENTICAL %s\n", train_ok ? "yes" : "no");

    /* ── 2. ASSIGN+ROUTE fixed query set TWICE -> identical ids+order ── */
    static int b1[DET_NQ], b2[DET_NQ];
    static int r1[DET_NQ * DET_TOPB], r2[DET_NQ * DET_TOPB];
    int route_ok = 1;
    for (int q = 0; q < DET_NQ; q++) {
        b1[q] = anch_assign(Q + (size_t)q * DET_DIM, C1, DET_K, DET_DIM);
        b2[q] = anch_assign(Q + (size_t)q * DET_DIM, C1, DET_K, DET_DIM);
        int n1 = anch_route(Q + (size_t)q * DET_DIM, C1, DET_K, DET_DIM,
                            DET_TOPB, r1 + (size_t)q * DET_TOPB);
        int n2 = anch_route(Q + (size_t)q * DET_DIM, C1, DET_K, DET_DIM,
                            DET_TOPB, r2 + (size_t)q * DET_TOPB);
        if (b1[q] != b2[q] || n1 != DET_TOPB || n2 != DET_TOPB) { route_ok = 0; break; }
        for (int t = 0; t < DET_TOPB; t++)
            if (r1[(size_t)q * DET_TOPB + t] != r2[(size_t)q * DET_TOPB + t]) {
                route_ok = 0; break;
            }
        if (!route_ok) break;
    }
    /* oracle cross-check (spec, not impl): assign == route[0] for every query */
    for (int q = 0; q < DET_NQ && route_ok; q++)
        if (b1[q] != r1[(size_t)q * DET_TOPB]) { route_ok = 0; break; }
    printf("RECEIPT ROUTE_IDENTICAL %s\n", route_ok ? "yes" : "no");

    /* ── 3. TAMPER one byte -> anch_load MUST fail checksum ── */
    const char *pt = "build/anchor_det_tamper.bin";
    int tamper_ok = 0;
    {
        /* copy file A byte-for-byte, flip one centroid byte */
        FILE *f = fopen(pa, "rb");
        unsigned char *tb = NULL;
        long szt = -1;
        if (f) {
            fseek(f, 0, SEEK_END);
            szt = ftell(f);
            fseek(f, 0, SEEK_SET);
            tb = (unsigned char *)malloc((size_t)szt);
            if (tb && fread(tb, 1, (size_t)szt, f) == (size_t)szt) {
                tb[20 + 5] ^= 0xFF; /* 1-byte flip inside centroid payload */
                FILE *g = fopen(pt, "wb");
                if (g) { fwrite(tb, 1, (size_t)szt, g); fclose(g); }
            }
            free(tb);
            fclose(f);
        }
        static float CL[(size_t)DET_K * DET_DIM];
        int r = anch_load(pt, CL, ANCHR_MAXK, ANCHR_MAXD, NULL, NULL);
        /* spec: checksum mismatch -> negative (impl uses -3 corrupt) */
        tamper_ok = (r < 0);
        /* control: untampered file still loads */
        int rc = anch_load(pa, CL, ANCHR_MAXK, ANCHR_MAXD, NULL, NULL);
        tamper_ok = tamper_ok && (rc == DET_K);
        if (!tamper_ok)
            printf("tamper debug: tampered_load=%d untampered_load=%d\n", r, rc);
    }
    printf("RECEIPT TAMPER_REJECTED %s\n", tamper_ok ? "yes" : "no");

    free(ba); free(bb);
    remove(pa); remove(pb); remove(pt);

    int pass = train_ok && route_ok && tamper_ok;
    printf("%s\n", pass ? "1 passed (determinism standing proof)" : "STANDING PROOF FAILED");
    return pass ? 0 : 1;
}
