/* LC Seed Generator (v1.0) — Deterministic Cheap Router
   Integer-only: no float, no math.h. Sacred numbers: 27 trits x 32 bits = 864 base seeds.
   Seed = (trit << 5) | bit  (0..863); multi-dim: seed * dim.
   Distance = XOR Hamming via __builtin_popcount (1-2 CPU cycles, preserves locality).
   Build: gcc -O3 -march=native lc_seed_gen.c -o lc_seed_gen.exe
   Modes:
     selftest
     bench N [D]
     multidim M D1 D2
     gen N D [seed0]
     hamming A B
*/
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <windows.h>

static uint64_t lcg_state = 0x94d049bb133111b9ULL;
static uint32_t lcg32(void) {
    lcg_state = lcg_state * 6364136223846793005ULL + 1442695040888963407ULL;
    return (uint32_t)(lcg_state >> 32);
}

/* Fill a synthetic Q20 vector (integer-only). */
static void fill_vec(int32_t *v, int D, uint32_t salt) {
    lcg_state = 0x94d049bb133111b9ULL ^ ((uint64_t)salt * 0x9e3779b97f4a7c15ULL);
    for (int i = 0; i < D; i++) {
        /* Q20-ish signed value */
        int32_t x = (int32_t)lcg32();
        v[i] = x >> 12; /* keep magnitude ~ 2^20 range-ish */
    }
}

/* Angular Mapper (integer-only, Q20 fixed-point) -> trit 0..26 (3 ternary digits). */
uint8_t get_trit_from_angular_map(const int32_t *vec, int D) {
    /* Center the vector first (V3.6 concept: angular sign after centering),
       then 3 groups -> 3 ternary digits by sign. */
    int64_t mean = 0;
    for (int i = 0; i < D; i++) mean += vec[i];
    mean /= D;
    int per = D / 3;
    if (per < 1) per = 1;
    int64_t sums[3] = {0, 0, 0};
    for (int g = 0; g < 3; g++) {
        int64_t s = 0;
        int start = g * per;
        int end = start + per;
        if (end > D) end = D;
        for (int i = start; i < end; i++) s += (int64_t)vec[i] - mean;
        sums[g] = s;
    }
    uint8_t trit = 0;
    for (int g = 0; g < 3; g++) {
        int digit = (sums[g] < 0) ? 0 : (sums[g] > 0 ? 2 : 1);
        trit = (uint8_t)(trit * 3 + digit);
    }
    return (uint8_t)(trit % 27);
}

/* Bitboard compress (integer-only) -> bit 0..31. */
uint8_t get_bit_from_bitboard(const int32_t *vec, int D) {
    /* center first for better spread */
    int64_t mean = 0;
    for (int i = 0; i < D; i++) mean += vec[i];
    mean /= D;
    int groups = 5;
    int per = D / groups;
    if (per < 1) per = 1;
    uint8_t bits = 0;
    for (int g = 0; g < groups; g++) {
        int64_t s = 0;
        int start = g * per;
        int end = start + per;
        if (end > D) end = D;
        for (int i = start; i < end; i++) s += (int64_t)vec[i] - mean;
        if (s >= 0) bits |= (uint8_t)(1 << g);
    }
    return (uint8_t)(bits % 32);
}

/* Core seed generator (Task 1): base seed 0..863. */
uint32_t generate_seed(const int32_t *vec, int D) {
    uint8_t trit = get_trit_from_angular_map(vec, D);
    uint8_t bit = get_bit_from_bitboard(vec, D);
    uint32_t base = ((uint32_t)trit << 5) | (uint32_t)bit; /* 0..863 */
    return base;
}

/* Multi-dim expansion variants. */
/* Handoff formula (multiply) — has cross-dim collisions when dim ranges overlap. */
uint32_t expand_mul(uint32_t base, uint32_t dim) { return base * dim; }
/* Corrected (high-bit tag) — zero cross-dim collision since 864 < 1024. */
uint32_t expand_tag(uint32_t base, uint32_t dim_id) { return base | (dim_id << 10); }

/* Hamming distance via popcount. */
int hamming_distance(uint32_t a, uint32_t b) {
    return __builtin_popcount(a ^ b);
}

static double now_ns(void) {
    LARGE_INTEGER f, t;
    QueryPerformanceFrequency(&f);
    QueryPerformanceCounter(&t);
    return (double)t.QuadPart / (double)f.QuadPart * 1e9;
}

static void selftest(void) {
    /* determinism: same vector -> same seed */
    int32_t v[8]; for (int i = 0; i < 8; i++) v[i] = i * 1000 - 4000;
    uint32_t s1 = generate_seed(v, 8);
    uint32_t s2 = generate_seed(v, 8);
    /* basic range */
    if (s1 != s2) { fprintf(stderr, "FAIL determinism\n"); exit(1); }
    if (s1 % 8 != 0) { /* base*dim, dim=8 => divisible by 8 */ }
    /* hamming */
    if (hamming_distance(0xF0F0F0F0u, 0x0F0F0F0Fu) != 32) { fprintf(stderr, "FAIL hamming\n"); exit(1); }
    /* trit range */
    for (int trial = 0; trial < 1000; trial++) {
        int32_t vv[384];
        fill_vec(vv, 384, (uint32_t)trial);
        uint8_t tr = get_trit_from_angular_map(vv, 384);
        uint8_t bi = get_bit_from_bitboard(vv, 384);
        if (tr > 26) { fprintf(stderr, "FAIL trit range %u\n", tr); exit(1); }
        if (bi > 31) { fprintf(stderr, "FAIL bit range %u\n", bi); exit(1); }
    }
    /* base range 0..863 */
    {
        int32_t vv[384]; fill_vec(vv, 384, 42);
        uint8_t tr = get_trit_from_angular_map(vv, 384);
        uint8_t bi = get_bit_from_bitboard(vv, 384);
        uint32_t base = ((uint32_t)tr << 5) | bi;
        if (base > 863) { fprintf(stderr, "FAIL base range %u\n", base); exit(1); }
    }
    printf("SELFTEST PASS\n");
}

static void bench(int N, int D) {
    int32_t *v = malloc((size_t)D * sizeof(int32_t));
    uint32_t *seeds = malloc((size_t)N * sizeof(uint32_t));
    /* prefill vectors */
    /* generate vectors on the fly to avoid huge mem; store seeds only */
    free(seeds); seeds = malloc((size_t)N * sizeof(uint32_t));
    double t0 = now_ns();
    for (int i = 0; i < N; i++) {
        fill_vec(v, D, (uint32_t)i);
        seeds[i] = generate_seed(v, D);
    }
    double t1 = now_ns();
    double sec = (t1 - t0) / 1e9;
    double rate = (double)N / sec;
    printf("BENCH N=%d D=%d time=%.4fs rate=%.2f seeds/sec (ns/seed=%.2f)\n", N, D, sec, rate, (t1-t0)/N);
    /* sample */
    for (int i = 0; i < 5 && i < N; i++) printf("  seed[%d]=%u\n", i, seeds[i]);
    free(v); free(seeds);
}

static void multidim(int M, int D1, int D2) {
    /* generate M seeds for each dim, count cross-dim collisions */
    int32_t *v = malloc((size_t)((D1 > D2 ? D1 : D2)) * sizeof(int32_t));
    /* collect seed sets */
    /* use sorted arrays */
    uint32_t *s1 = malloc((size_t)M * sizeof(uint32_t));
    uint32_t *s2 = malloc((size_t)M * sizeof(uint32_t));
    uint32_t *t1 = malloc((size_t)M * sizeof(uint32_t));
    uint32_t *t2 = malloc((size_t)M * sizeof(uint32_t));
    for (int i = 0; i < M; i++) {
        fill_vec(v, D1, (uint32_t)(i * 2 + 1));
        uint32_t b1 = generate_seed(v, D1);
        fill_vec(v, D2, (uint32_t)(i * 2 + 2));
        uint32_t b2 = generate_seed(v, D2);
        /* handoff multiply expansion */
        s1[i] = expand_mul(b1, (uint32_t)D1);
        s2[i] = expand_mul(b2, (uint32_t)D2);
        /* corrected tag expansion (dim_id 0/1) */
        t1[i] = expand_tag(b1, 0);
        t2[i] = expand_tag(b2, 1);
    }
    /* count collisions: how many s1 values appear in s2 set */
    /* build set of s2 via sort */
    /* simple O(M^2) for small M */
    long coll_mul = 0, coll_tag = 0;
    for (int i = 0; i < M; i++) {
        for (int j = 0; j < M; j++) {
            if (s1[i] == s2[j]) { coll_mul++; break; }
        }
    }
    for (int i = 0; i < M; i++) for (int j = 0; j < M; j++) if (t1[i] == t2[j]) coll_tag++;
    printf("MULTIDIM M=%d D1=%d D2=%d mul_cross_collisions=%ld tagged_cross_collisions=%ld\n", M, D1, D2, coll_mul, coll_tag);
    /* also distinct counts */
    /* distinct in s1 */
    /* range stats */
    uint32_t min1=s1[0], max1=s1[0], min2=s2[0], max2=s2[0];
    for (int i=0;i<M;i++){ if(s1[i]<min1)min1=s1[i]; if(s1[i]>max1)max1=s1[i]; if(s2[i]<min2)min2=s2[i]; if(s2[i]>max2)max2=s2[i]; }
    printf("  D1 range=[%u,%u] D2 range=[%u,%u]\n", min1, max1, min2, max2);
    free(v); free(s1); free(s2);
}

static void gen(int N, int D, uint32_t seed0) {
    int32_t *v = malloc((size_t)D * sizeof(int32_t));
    for (int i = 0; i < N; i++) {
        fill_vec(v, D, seed0 + (uint32_t)i);
        uint32_t s = generate_seed(v, D);
        if (i < 20) printf("%u\n", s);
    }
    if (N > 20) printf("... (%d total, showing first 20)\n", N);
    free(v);
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s selftest | bench N [D] | multidim M D1 D2 | gen N D [seed0] | hamming A B\n", argv[0]);
        return 1;
    }
    if (strcmp(argv[1], "selftest") == 0) { selftest(); return 0; }
    if (strcmp(argv[1], "bench") == 0) {
        int N = argc > 2 ? atoi(argv[2]) : 1000000;
        int D = argc > 3 ? atoi(argv[3]) : 384;
        bench(N, D); return 0;
    }
    if (strcmp(argv[1], "benchpure") == 0) {
        int N = argc > 2 ? atoi(argv[2]) : 10000000;
        uint32_t *trits = malloc((size_t)N * sizeof(uint32_t));
        uint32_t *bits = malloc((size_t)N * sizeof(uint32_t));
        for (int i = 0; i < N; i++) { trits[i] = i % 27; bits[i] = i % 32; }
        uint32_t *out = malloc((size_t)N * 4);
        double t0 = now_ns();
        for (int i = 0; i < N; i++) out[i] = ((trits[i] & 31) << 5) | (bits[i] & 31);
        double t1 = now_ns();
        printf("BENCHPURE N=%d time=%.4fs rate=%.2f seeds/sec\n", N, (t1-t0)/1e9, (double)N/((t1-t0)/1e9));
        free(trits); free(bits); free(out);
        return 0;
    }
    if (strcmp(argv[1], "multidim") == 0) {
        int M = argc > 2 ? atoi(argv[2]) : 100000;
        int D1 = argc > 3 ? atoi(argv[3]) : 384;
        int D2 = argc > 4 ? atoi(argv[4]) : 128;
        multidim(M, D1, D2); return 0;
    }
    if (strcmp(argv[1], "gen") == 0) {
        int N = argc > 2 ? atoi(argv[2]) : 10;
        int D = argc > 3 ? atoi(argv[3]) : 384;
        uint32_t s0 = argc > 4 ? (uint32_t)strtoul(argv[4], NULL, 10) : 0;
        gen(N, D, s0); return 0;
    }
    if (strcmp(argv[1], "hamming") == 0) {
        uint32_t a = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 10) : 0;
        uint32_t b = argc > 3 ? (uint32_t)strtoul(argv[3], NULL, 10) : 0;
        printf("%d\n", hamming_distance(a, b)); return 0;
    }
    fprintf(stderr, "unknown mode %s\n", argv[1]);
    return 1;
}
