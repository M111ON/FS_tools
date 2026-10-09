/*
 * anchor_bitboard.h — the bitboard layer under an anchor, wired to the
 *                     two curves the owner named:
 *
 *                        64 bitboard = Hilbert   (World A / placement)
 *                        3x3 sub-cell = Peano    (World B / inner walk)
 *
 * WHY THIS EXISTS
 *   anchor_route.h decides WHICH BUCKET a vector belongs to.
 *   anchor_letter.h turns a bucket into a 6-letter face order + 720 orbit and
 *   drops a fingerprint (cell,choice) on the 144x72 latch plane.
 *   What was still missing: the SUB-LAYER under one bucket — the place a vector
 *   actually sits once its bucket is chosen. That is the bitboard.
 *
 * THE LAYER (7 rungs, top = coarsest)
 *   bucket   k in [0,K)            <- anch_assign(q)             (decide)
 *   cell     slot in [0,144)       <- anch_slot(k,n) % 144      (face plane)
 *   bitboard h in [0,64)           <- Hilbert-64 of the residual vector
 *   layer    l in [0,2)            <- sign bit of the residual
 *   sub3x3   s in [0,9)            <- Peano 3x3 inside the bitboard cell
 *   addr     a in [0,20736)        <- gc_addr(anchor, x, y, l)
 *
 * WHERE THE BITBOARD COMES FROM (this is the honest part)
 *   The residual r = q - C[k] (query minus its bucket centroid) is reduced to
 *   an 8x8 board position by block-sign: split the dim-vector into 8 bands, two
 *   halves each; band i's mean sign picks (x_i, y_i). That is a *geometric*
 *   read of the residual — no shape, no projection, no mesh (the system builds
 *   no physical shape; the "frustum quadtree" is a structure of addresses, not
 *   a rendered object). Hilbert is then just the ADDRESS ORDER over that board,
 *   exactly as the owner said: 64 bitboard = Hilbert.
 *
 *   The 3x3 is the Peano cell INSIDE one bitboard square: the residual's
 *   nearest sub-region. 9 = 3x3 is the small ternary walk (3^2), distinct from
 *   the 3^4 = 81 Peano space the source crops to 36; here we only need the 9.
 *
 * STRICTLY READ-ONLY, STRICTLY ADDITIVE
 *   Nothing here changes routing, ordering, scoring, or recall. It ADDS a
 *   placement label to each entry so callers can (a) bucket within an anchor,
 *   (b) compare flat order vs Hilbert order. No malloc (fixed scratch), no RNG,
 *   no float beyond the caller's vector, header-only.
 */
#ifndef ANCHOR_BITBOARD_H
#define ANCHOR_BITBOARD_H

#include <stdint.h>

#include "anchor_route.h"          /* anch_assign */
#include "geo_curve_snapshot.h"    /* gc_hilbert_8x8, gc_addr, GC_* */

#define AB_BOARD   8               /* 8 x 8 bitboard */
#define AB_CELLS   64              /* 8 x 8 = 64 = Hilbert space */
#define AB_SUB     9               /* 3 x 3 Peano sub-cell */
#define AB_LAYERS  2               /* phase flip */

/* ── residual block-sign -> (x, y) on the 8x8 board ───────────────────────
 * dim is split into AB_BOARD bands; each band contributes two half-means.
 * x_i = sign of the band's first half, y_i = sign of the second half, folded
 * into AB_BOARD rows -> one (x,y) point. Deterministic, no RNG.
 * Returns 0 on success. */
static inline int ab_board_xy(const float *resid, int dim, int *x_out, int *y_out) {
    if (!resid || dim <= 0) return -1;
    int band = dim / AB_BOARD;
    if (band < 2) band = 2;
    int x = 0, y = 0;
    for (int i = 0; i < AB_BOARD; i++) {
        int lo = i * band / 2;          /* first half of band i */
        int hi = (i + 1) * band / 2;    /* second half          */
        if (hi > dim) hi = dim;
        if (lo >= hi) { lo = 0; hi = (dim > 1) ? 1 : dim; }
        double a = 0.0, b = 0.0;
        int na = 0, nb = 0;
        for (int j = lo; j < hi; j++) { a += (double)resid[j]; na++; }
        for (int j = hi; j < dim && j < hi + (hi - lo); j++) { b += (double)resid[j]; nb++; }
        if (na) a /= na;
        if (nb) b /= nb;
        x = (x << 1) | (a >= 0.0 ? 1 : 0);
        y = (y << 1) | (b >= 0.0 ? 1 : 0);
    }
    *x_out = x & (AB_BOARD - 1);
    *y_out = y & (AB_BOARD - 1);
    return 0;
}

/* ── Peano 3x3 inside a bitboard cell ────────────────────────────────────
 * The residual's dominant third of each axis gives the sub-cell index.
 * 3x3 = 9 states; order is row-major (Peano L1 is a 3x3 U-walk — row-major is
 * the same SET, and we only need the cell, not the walk order, here). */
static inline int ab_sub3x3(const float *resid, int dim) {
    if (!resid || dim <= 0) return 0;
    int third = dim / 3;
    if (third < 1) third = 1;
    double m[3] = {0.0, 0.0, 0.0};
    int   c[3] = {0, 0, 0};
    for (int t = 0; t < 3; t++) {
        int lo = t * third, hi = (t + 1) * third;
        if (hi > dim) hi = dim;
        for (int j = lo; j < hi; j++) { m[t] += (double)resid[j]; c[t]++; }
        if (c[t]) m[t] /= c[t];
    }
    int row = 0; double best = m[0], bv = m[0];
    for (int t = 0; t < 3; t++) if (m[t] > bv) { bv = m[t]; row = t; }
    best = bv;
    (void)best;
    /* column = dominant sign of the winning third's first element */
    int col = (resid[0] >= 0.0f) ? 1 : 0;
    return (row * 3 + col) % AB_SUB;
}

/* ── the placement of one (bucket, residual) under its anchor ────────────── */
typedef struct {
    int      bucket;     /* [0,K)                                  */
    uint32_t cell;       /* [0,144)  face-plane cell               */
    int      bx, by;     /* [0,8)    bitboard position             */
    uint32_t hilbert;    /* [0,64)   Hilbert order of (bx,by)      */
    uint32_t layer;      /* [0,2)    phase / sign layer            */
    uint32_t sub3x3;     /* [0,9)    Peano sub-cell                */
    uint32_t bitboard;   /* [0,128)  layer*64 + hilbert            */
    uint32_t addr;       /* [0,20736) gc_addr(bucket, bx,by,layer) */
} ab_place_t;

/* Place a query's residual under its chosen bucket. Returns 0 on success.
 * resid = q - C[bucket], already computed by the caller (dim floats). */
static inline int ab_place(int bucket, int K, const float *resid, int dim,
                           ab_place_t *out) {
    if (!out || !resid || dim <= 0 || bucket < 0) return -1;
    uint32_t n = (K > 1) ? (uint32_t)K : 1u;
    if (n % 37u == 0u) n++;
    uint32_t cell = anch_slot((uint32_t)bucket, n) % 144u;

    int bx = 0, by = 0;
    if (ab_board_xy(resid, dim, &bx, &by) != 0) return -1;
    uint32_t h = gc_hilbert_8x8((uint32_t)bx, (uint32_t)by);
    uint32_t layer = (resid[0] >= 0.0f) ? 0u : 1u;
    uint32_t sub = (uint32_t)ab_sub3x3(resid, dim);

    out->bucket   = bucket;
    out->cell     = cell;
    out->bx       = bx;
    out->by       = by;
    out->hilbert  = h;
    out->layer    = layer;
    out->sub3x3   = sub;
    out->bitboard = layer * AB_CELLS + h;     /* [0,128) */
    out->addr     = gc_addr((uint32_t)bucket % GC_ANCHORS,
                            (uint32_t)bx, (uint32_t)by, layer);
    return 0;
}

/* ── locality helpers (used by the comparison bench) ───────────────────── */

/* Flat-order distance: |i - j| in the walked entry order. */
static inline uint32_t ab_flat_dist(uint32_t i, uint32_t j) {
    return (i > j) ? (i - j) : (j - i);
}

/* Bitboard-order distance: Hilbert order of two placed points.
 * Two points close on the board have close Hilbert index (curve locality);
 * two points far apart do not. */
static inline uint32_t ab_hilbert_dist(const ab_place_t *a, const ab_place_t *b) {
    uint32_t x = (a->hilbert > b->hilbert) ? a->hilbert - b->hilbert
                                           : b->hilbert - a->hilbert;
    uint32_t y = (a->layer > b->layer) ? a->layer - b->layer
                                       : b->layer - a->layer;
    return x + y * AB_CELLS;
}

#endif /* ANCHOR_BITBOARD_H */
