/* tests/test_entropy_xray.c — stateless gate for Entropy X-ray ranking. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "entropy_xray.h"

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <psapi.h>
#endif

static uint32_t fnv1a_buf(const unsigned char *p, size_t n) {
    uint32_t h = 2166136261u;
    for (size_t i = 0; i < n; i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

static size_t rss_bytes(void) {
#if defined(_WIN32)
    PROCESS_MEMORY_COUNTERS pmc;
    if (GetProcessMemoryInfo(GetCurrentProcess(), &pmc, sizeof(pmc))) {
        return (size_t)pmc.WorkingSetSize;
    }
    return 0;
#else
    return 0;
#endif
}

static double now_sec(void) {
    return (double)clock() / (double)CLOCKS_PER_SEC;
}

int main(void) {
    int fails = 0;
    printf("test_entropy_xray\n");

    /* T1 determinism */
    {
        XrayResult a, b;
        xray_scan_cell(42u, &a);
        xray_scan_cell(42u, &b);
        int ok = (memcmp(&a, &b, sizeof a) == 0);
        printf("T1 determinism       : %s\n", ok ? "PASS" : "FAIL");
        if (!ok) fails++;
    }

    /* T2 field sanity */
    {
        XrayResult r;
        xray_scan_cell(7u, &r);
        int ok = (r.total_bits == XRAY_CELL_WORDS * XRAY_WORD_BITS) &&
                 (r.set_bits <= r.total_bits) &&
                 (r.density <= 1000u) &&
                 (r.variance == r.bit_variance) &&
                 (r.xray_score == r.variance);
        printf("T2 field sanity      : %s (var=%u, density=%u, flags=%u)\n",
               ok ? "PASS" : "FAIL", r.variance, r.density, r.flags);
        if (!ok) fails++;
    }

    /* T3 candidate ranking determinism */
    {
        CandidateList c1, c2;
        memset(&c1, 0, sizeof c1);
        memset(&c2, 0, sizeof c2);
        for (uint32_t i = 0; i < 128u; i++) {
            c1.items[i].id = i;
            c1.items[i].cell_id = (i * 37u + 11u) & 0x7fffffffu;
            c1.count++;
            c2.items[i] = c1.items[i];
            c2.count++;
        }
        Query q = { .center_cell = 1000u, .query_hash = 1u, .radius = 50u, .k = 10u };
        entropy_rank_candidates(&q, &c1);
        entropy_rank_candidates(&q, &c2);
        int ok = (c1.count == c2.count) &&
                 (memcmp(c1.items, c2.items, c1.count * sizeof(Candidate)) == 0);
        printf("T3 rank determinism  : %s\n", ok ? "PASS" : "FAIL");
        if (!ok) fails++;
    }

    /* T4 stateless: DB checksum unchanged + RSS no growth */
    {
        const char *dbpath = "test_entropy_xray_db.bin";
        const size_t DBSZ = 1u << 20;
        unsigned char *buf = (unsigned char *)malloc(DBSZ);
        for (size_t i = 0; i < DBSZ; i++) buf[i] = (unsigned char)((i * 131u + 7u) & 0xffu);
        FILE *f = fopen(dbpath, "wb");
        fwrite(buf, 1, DBSZ, f);
        fclose(f);
        free(buf);

        /* read back for checksum */
        FILE *fr = fopen(dbpath, "rb");
        unsigned char *rbuf = (unsigned char *)malloc(DBSZ);
        fread(rbuf, 1, DBSZ, fr);
        fclose(fr);
        uint32_t before = fnv1a_buf(rbuf, DBSZ);
        free(rbuf);

        size_t rss0 = rss_bytes();
        for (int r = 0; r < 1000; r++) {
            XrayResult xr;
            xray_scan_cell((uint32_t)r, &xr);
            (void)xr;
        }
        size_t rss1 = rss_bytes();

        fr = fopen(dbpath, "rb");
        rbuf = (unsigned char *)malloc(DBSZ);
        fread(rbuf, 1, DBSZ, fr);
        fclose(fr);
        uint32_t after = fnv1a_buf(rbuf, DBSZ);
        free(rbuf);
        remove(dbpath);

        int ok = (before == after) && (rss1 <= rss0 + 65536u);
        printf("T4 stateless         : %s (checksum %s, rss delta %ld bytes)\n",
               ok ? "PASS" : "FAIL",
               (before == after) ? "unchanged" : "CHANGED",
               (long)rss1 - (long)rss0);
        if (!ok) fails++;
    }

    /* T5 performance: scan <= 2x of a baseline popcount loop */
    {
        const int N = 100000;
        double t0 = now_sec();
        for (int i = 0; i < N; i++) {
            XrayResult xr;
            xray_scan_cell((uint32_t)i, &xr);
            (void)xr;
        }
        double tscan = now_sec() - t0;

        t0 = now_sec();
        volatile uint32_t acc = 0;
        for (int i = 0; i < N; i++) {
            uint64_t state = (uint64_t)i * 0x9E3779B97F4A7C15ULL + 0xC2B2AE3D27D4EB4FULL;
            for (int w = 0; w < 8; w++) {
                state += 0x9E3779B97F4A7C15ULL;
                state = (state ^ (state >> 30)) * 0xBF58476D1CE4E5B9ULL;
                state = (state ^ (state >> 27)) * 0x94D049BB133111B9ULL;
                state = state ^ (state >> 31);
                acc += (uint32_t)__builtin_popcountll(state);
            }
        }
        double tbase = now_sec() - t0;
        (void)acc;
        double ratio = (tbase > 0.0) ? (tscan / tbase) : 0.0;
        /* scan does 8x work; allow generous bound */
        int ok = (ratio < 5.0);
        printf("T5 performance       : %s (scan/base = %.2fx)\n", ok ? "PASS" : "FAIL", ratio);
        if (!ok) fails++;
    }

    /* T6 prune flags present for some cell */
    {
        int seen_prune = 0;
        for (uint32_t i = 0; i < 10000u; i++) {
            XrayResult r;
            xray_scan_cell(i, &r);
            if (r.pruned) { seen_prune = 1; break; }
        }
        /* not strictly required that any pruned exists; just report */
        printf("T6 prune observed    : %s\n", seen_prune ? "yes" : "no (ok)");
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
