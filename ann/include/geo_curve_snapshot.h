/*
 * geo_curve_snapshot.h — Hilbert-64 (bitboard) + Peano-9 (3×3) curves
 * ══════════════════════════════════════════════════════════════════
 *
 * SNAPSHOT — copied into `ann` so it stays standalone. Do NOT add a
 * cross-repo dependency to I:\DWGLS-native-fs; if the source changes,
 * re-copy by hand and bump SNAP_SOURCE_REV below.
 *
 *   source repo : I:\DWGLS-native-fs
 *   source files: core/geo_dram_tile.h        (Hilbert 8×8, dram_addr)
 *                 core/geo_dual_place.h       (PEANO/HILBERT/BORDER LUTs)
 *   copied      : 2026-10-10
 *
 * WHY THESE TWO CURVES
 *   The user's rule:   64 bitboard = Hilbert,   3×3 = Peano.
 *
 *     bitboard (64)  → 8×8 grid, Hilbert / ln curve (pure bit-interleave)
 *     sub-cell (9)   → 3×3 = 3², Peano ternary walk (3⁴ = 81 cropped to 36)
 *
 *   They are two DIFFERENT layers, not the same curve at two sizes:
 *     - Hilbert-64 = the bitboard placement (World A, border 28 cells)
 *     - Peano-81   = the sub-cell walk under an anchor (World B, inner 36)
 *   Invariant kept from source: 36 + 28 = 64  (DiamondBlock).
 *
 * NUMBERS (verified against source, not re-derived here)
 *   DRAM_ANCHORS 162 × DRAM_CELLS_PER 128 = 20736 = GEO_FULL
 *   8×8×2 layers = 128 cells per anchor
 *   gcd(37, 64) = 1  → stride-37 scatter covers all 64 positions
 *
 * No malloc. No float. Pure integer, static inline.
 * ══════════════════════════════════════════════════════════════════
 */

#ifndef GEO_CURVE_SNAPSHOT_H
#define GEO_CURVE_SNAPSHOT_H

#include <stdint.h>
#include <string.h>

#define SNAP_SOURCE_REV "DWGLS-native-fs@2026-10-10"

/* ── Hilbert 8×8 (bitboard layer) ───────────────────────────────
 * Copied from core/geo_dram_tile.h: DRAM_* + dram_hilbert_8x8().
 */

#define GC_GRID_X     8u
#define GC_GRID_Y     8u
#define GC_LAYERS     2u
#define GC_CELLS_PER  (GC_GRID_X * GC_GRID_Y * GC_LAYERS)   /* 128 */
#define GC_ANCHORS    162u
#define GC_FULL       (GC_ANCHORS * GC_CELLS_PER)           /* 20736 */

/* x,y ∈ [0,7] → Hilbert order 0..63. Bijective, pure shift/mask. */
static inline uint32_t gc_hilbert_8x8(uint32_t x, uint32_t y)
{
    uint32_t d = 0, n = 8;
    for (uint32_t s = n >> 1; s > 0; s >>= 1) {
        uint32_t rx = (x & s) > 0;
        uint32_t ry = (y & s) > 0;
        d = (d << 2) | ((3u * rx) ^ ry);
        if (ry == 0) {
            if (rx == 1) { x = n - 1u - x; y = n - 1u - y; }
            uint32_t t = x; x = y; y = t;
        }
    }
    return d;
}

/* anchor + bitboard cell + layer → flat address in [0, 20736). */
static inline uint32_t gc_addr(uint32_t anchor_id,
                               uint32_t x, uint32_t y, uint32_t layer)
{
    uint32_t tile_off = gc_hilbert_8x8(x, y)
                        + layer * (GC_GRID_X * GC_GRID_Y);
    return anchor_id * GC_CELLS_PER + tile_off;
}

typedef struct {
    uint32_t anchor_id;
    uint32_t layer;
    uint32_t hilbert_pos;   /* 0..63 within layer */
} GcAddrParts;

static inline GcAddrParts gc_decompose(uint32_t addr)
{
    GcAddrParts p;
    p.anchor_id   = addr / GC_CELLS_PER;
    uint32_t off  = addr % GC_CELLS_PER;
    p.layer       = off / (GC_GRID_X * GC_GRID_Y);
    p.hilbert_pos = off % (GC_GRID_X * GC_GRID_Y);
    return p;
}

/* ── Peano ternary (3×3 sub-cell layer) ─────────────────────────
 * Copied from core/geo_dual_place.h: GDP_* constants + LUTs.
 */

#define GC_GRID        64u    /* 8×8 bitboard (DiamondBlock)          */
#define GC_GRID_W       8u
#define GC_INNER       36u    /* World B active (6×6 = 4 quads of 3×3) */
#define GC_BORDER      28u    /* World A shadow border                 */
#define GC_PEANO       81u    /* 3⁴ ternary space                      */
#define GC_ICO        162u    /* icosphere L2 (81×2 poles)             */
#define GC_OUTSIDE     0xFF   /* peano cell outside 6×6 crop           */

/* Peano idx (0..80) → flat 8×8 position, 0xFF = outside 6×6 crop. */
static const uint8_t GC_PEANO_TO_GRID[GC_PEANO] = {
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

/* Hilbert idx (0..63) → flat 8×8 position, stride-37 scatter. */
static const uint8_t GC_HILBERT_TO_GRID[GC_GRID] = {
     0, 37, 10, 47, 20, 57, 30,  3,
    40, 13, 50, 23, 60, 33,  6, 43,
    16, 53, 26, 63, 36,  9, 46, 19,
    56, 29,  2, 39, 12, 49, 22, 59,
    32,  5, 42, 15, 52, 25, 62, 35,
     8, 45, 18, 55, 28,  1, 38, 11,
    48, 21, 58, 31,  4, 41, 14, 51,
    24, 61, 34,  7, 44, 17, 54, 27
};

/* Border cells = outer ring of 8×8 (World A, 28 cells). */
static const uint8_t GC_BORDER_IDX[GC_BORDER] = {
     0,  1,  2,  3,  4,  5,  6,  7,   /* row 0            */
     8, 15, 16, 23, 24, 31,            /* col 0,7 rows 1-3 */
    32, 39, 40, 47, 48, 55,            /* col 0,7 rows 4-6 */
    56, 57, 58, 59, 60, 61, 62, 63    /* row 7            */
};

/* ── Self-check: the invariants the snapshot must keep ─────────── */

/* Hilbert covers 0..63 exactly once. 0 = pass, -1 = fail. */
static inline int gc_verify_hilbert(void)
{
    uint8_t seen[64] = {0};
    for (uint32_t y = 0; y < GC_GRID_Y; y++) {
        for (uint32_t x = 0; x < GC_GRID_X; x++) {
            uint32_t h = gc_hilbert_8x8(x, y);
            if (h >= 64 || seen[h]) return -1;
            seen[h] = 1;
        }
    }
    return 0;
}

/* stride-37 LUT is a bijection over 0..63. 0 = pass, -1 = fail. */
static inline int gc_verify_hilbert_lut(void)
{
    uint8_t seen[64] = {0};
    for (uint32_t i = 0; i < GC_GRID; i++) {
        uint8_t p = GC_HILBERT_TO_GRID[i];
        if (p >= 64 || seen[p]) return -1;
        seen[p] = 1;
    }
    return 0;
}

/* Peano LUT: 45 outside (0xFF) + 36 inner, no dup on the 6×6 crop. */
static inline int gc_verify_peano_lut(void)
{
    uint8_t seen[64] = {0};
    uint32_t outside = 0, inner = 0;
    for (uint32_t i = 0; i < GC_PEANO; i++) {
        uint8_t p = GC_PEANO_TO_GRID[i];
        if (p == GC_OUTSIDE) { outside++; continue; }
        if (p >= 64 || seen[p]) return -1;
        seen[p] = 1;
        inner++;
    }
    if (inner != GC_INNER) return -1;
    if (outside != GC_PEANO - GC_INNER) return -1;
    return 0;
}

/* border(28) and inner-6×6(36) tile the whole 8×8 with no overlap. */
static inline int gc_verify_split(void)
{
    uint8_t seen[64] = {0};
    for (uint32_t i = 0; i < GC_BORDER; i++) {
        uint8_t p = GC_BORDER_IDX[i];
        if (p >= 64 || seen[p]) return -1;
        seen[p] = 1;
    }
    for (uint32_t i = 0; i < GC_PEANO; i++) {
        uint8_t p = GC_PEANO_TO_GRID[i];
        if (p == GC_OUTSIDE) continue;
        if (seen[p]) return -1;      /* border ∩ inner must be empty */
        seen[p] = 1;
    }
    for (uint32_t p = 0; p < 64; p++)
        if (!seen[p]) return -1;     /* must cover all 64 */
    return 0;
}

/* 3×3 sub-grid: each 6×6 inner region splits into 4 quads of 3×3. */
static inline int gc_verify_subcell_3x3(void)
{
    /* 6×6 → 4 non-overlapping 3×3 quads = 36 cells. */
    uint8_t seen[64] = {0};
    uint32_t cells = 0;
    for (uint32_t qy = 0; qy < 2; qy++) {
        for (uint32_t qx = 0; qx < 2; qx++) {
            for (uint32_t cy = 0; cy < 3; cy++) {
                for (uint32_t cx = 0; cx < 3; cx++) {
                    uint32_t row = 1 + qy * 3 + cy;   /* rows 1..6 */
                    uint32_t col = 1 + qx * 3 + cx;   /* cols 1..6 */
                    uint8_t p = (uint8_t)(row * 8 + col);
                    if (seen[p]) return -1;
                    seen[p] = 1;
                    cells++;
                }
            }
        }
    }
    if (cells != GC_INNER) return -1;   /* 36 == 4 × 9 */
    return 0;
}

#endif /* GEO_CURVE_SNAPSHOT_H */
