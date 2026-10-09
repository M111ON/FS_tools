/*
 * test_geo_curve.c — snapshot invariants + source-parity for
 *                    geo_curve_snapshot.h (Hilbert-64 + Peano-9).
 *
 * Checks:
 *   1. Hilbert 8×8 bijective over 0..63
 *   2. stride-37 Hilbert LUT bijective
 *   3. Peano LUT: 45 outside + 36 inner
 *   4. border(28) ∪ inner(36) = all 64, disjoint
 *   5. 6×6 inner = 4 quads of 3×3 = 36
 *   6. gc_addr covers 20736 with zero collisions (162 anchors × 128)
 *   7. copied LUT bytes == the source values (frozen parity)
 */
#include <stdio.h>
#include <string.h>
#include "geo_curve_snapshot.h"

static int fails = 0;
static void ck(const char *name, int ok)
{
    printf("  %s %s\n", ok ? "ok  " : "FAIL", name);
    if (!ok) fails++;
}

/* Frozen copies of the source LUTs (I:\DWGLS-native-fs) for parity. */
static const uint8_t SRC_PEANO[GC_PEANO] = {
    255,255,255, 10,  9,255,255, 17, 18,
    255,255,255, 13, 12, 11, 19, 20, 21,
    255,255,255,255,255, 14, 22,255,255,
    255,255, 30, 38,255,255,255,255, 46,
     29, 28, 27, 35, 36, 37, 45, 44, 43,
     26, 25,255,255, 33, 34, 42, 41,255,
    255, 49, 50,255,255,255,255,255,255,
     51, 52, 53,255,255,255,255,255,255,
     54,255,255,255,255,255,255,255,255
};
static const uint8_t SRC_HILBERT[GC_GRID] = {
     0, 37, 10, 47, 20, 57, 30,  3,
    40, 13, 50, 23, 60, 33,  6, 43,
    16, 53, 26, 63, 36,  9, 46, 19,
    56, 29,  2, 39, 12, 49, 22, 59,
    32,  5, 42, 15, 52, 25, 62, 35,
     8, 45, 18, 55, 28,  1, 38, 11,
    48, 21, 58, 31,  4, 41, 14, 51,
    24, 61, 34,  7, 44, 17, 54, 27
};

int main(void)
{
    printf("test_geo_curve\n");

    ck("hilbert 8x8 bijective 0..63", gc_verify_hilbert() == 0);
    ck("hilbert stride-37 LUT bijective", gc_verify_hilbert_lut() == 0);
    ck("peano LUT 45 outside + 36 inner", gc_verify_peano_lut() == 0);
    ck("border(28) + inner(36) = 64 disjoint", gc_verify_split() == 0);
    ck("6x6 inner = 4 quads of 3x3 = 36", gc_verify_subcell_3x3() == 0);

    /* gc_addr covers 20736 with no collision, 162 × 128. */
    {
        uint8_t seen[GC_FULL / 8] ;
        memset(seen, 0, sizeof(seen));
        int collide = 0, count = 0;
        for (uint32_t a = 0; a < GC_ANCHORS && !collide; a++)
            for (uint32_t y = 0; y < 8 && !collide; y++)
                for (uint32_t x = 0; x < 8 && !collide; x++)
                    for (uint32_t l = 0; l < 2; l++) {
                        uint32_t ad = gc_addr(a, x, y, l);
                        if (ad >= GC_FULL) { collide = 1; break; }
                        uint32_t b = ad >> 3, m = 1u << (ad & 7u);
                        if (seen[b] & m) { collide = 1; break; }
                        seen[b] |= m; count++;
                    }
        ck("gc_addr covers 20736 zero collision", !collide && count == 20736);
    }

    /* Source parity: copied bytes == frozen source bytes. */
    ck("peano LUT == source bytes",
       memcmp(GC_PEANO_TO_GRID, SRC_PEANO, sizeof(SRC_PEANO)) == 0);
    ck("hilbert LUT == source bytes",
       memcmp(GC_HILBERT_TO_GRID, SRC_HILBERT, sizeof(SRC_HILBERT)) == 0);

    /* Spot check: dram formula 162 × 128 == 20736. */
    ck("162 anchors x 128 cells == 20736", GC_ANCHORS * GC_CELLS_PER == 20736);
    ck("GC_GRID 64 arrives from 8x8", GC_GRID == GC_GRID_W * GC_GRID_W);

    if (fails) { printf("FAILED (%d)\n", fails); return 1; }
    printf("ALL PASS\n");
    return 0;
}
