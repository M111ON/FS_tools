#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* chain_promote.h — bucket split (chain_extend) for ANN
 *
 * When a bucket exceeds capacity, split it into 2 sub-buckets using
 * deterministic 2-means (no RNG). The parent bucket becomes the prefix,
 * sub-buckets get .0/.1 (chain_extend). Old addresses remain valid
 * (prefix-stable) — only new data gets the extended address.
 *
 * Owner spec: "learn partition once → freeze → new data appends →
 * full bucket opens one child layer" (container experiment, phase C)
 *
 * This is a STANDALONE module — not wired into ann_search.c regrow path.
 * Wire-up requires: capacity check in regrow, 2-means on bucket vectors,
 * chain_extend on affected rows, anchor table update.
 */
#ifndef CHAIN_PROMOTE_H
#define CHAIN_PROMOTE_H

#include <stdint.h>
#include "chain_addr.h"

#define PROMOTE_CAP_DEFAULT 500   /* rows per bucket before split */
#define PROMOTE_MAX_DEPTH    8    /* max chain depth for promotion */

/* ── split result ──────────────────────────────────────────────── */
typedef struct {
    int parent_bucket;    /* original bucket (becomes parent) */
    int child0;           /* sub-bucket 0 (parent.0) */
    int child1;           /* sub-bucket 1 (parent.1) */
    int n0, n1;           /* row counts per child */
    float centroid0[4];   /* sub-centroid 0 (first 4 dims, for demo) */
    float centroid1[4];   /* sub-centroid 1 */
} PromoteSplit;

/* ── deterministic 2-means split ────────────────────────────────
 * Splits vectors into 2 groups by L2 distance to 2 centroids.
 * Centroids initialized deterministically: first and last vector.
 * No RNG. Max 20 iterations. */
static inline void promote_split_2means(
    const float *vecs, int n, int dim,
    int *assign, float *c0, float *c1)
{
    if (n < 2 || !vecs || !assign || !c0 || !c1) return;

    /* init: first and last vector */
    memcpy(c0, vecs, dim * sizeof(float));
    memcpy(c1, vecs + (n-1) * dim, dim * sizeof(float));

    for (int iter = 0; iter < 20; iter++) {
        /* assign */
        int changed = 0;
        for (int i = 0; i < n; i++) {
            const float *v = vecs + i * dim;
            float d0 = 0, d1 = 0;
            for (int d = 0; d < dim; d++) {
                float diff0 = v[d] - c0[d];
                float diff1 = v[d] - c1[d];
                d0 += diff0 * diff0;
                d1 += diff1 * diff1;
            }
            int new_assign = (d0 <= d1) ? 0 : 1;
            if (new_assign != assign[i]) { assign[i] = new_assign; changed = 1; }
        }
        if (!changed) break;

        /* recompute centroids */
        float n0 = 0, n1 = 0;
        memset(c0, 0, dim * sizeof(float));
        memset(c1, 0, dim * sizeof(float));
        for (int i = 0; i < n; i++) {
            float *c = (assign[i] == 0) ? c0 : c1;
            const float *v = vecs + i * dim;
            for (int d = 0; d < dim; d++) c[d] += v[d];
            if (assign[i] == 0) n0++; else n1++;
        }
        if (n0 > 0) for (int d = 0; d < dim; d++) c0[d] /= n0;
        if (n1 > 0) for (int d = 0; d < dim; d++) c1[d] /= n1;
    }
}

/* ── chain_extend on promotion ───────────────────────────────────
 * Parent address: existing ChainKey (prefix)
 * Child 0: chain_extend(parent, digit_for_child0)
 * Child 1: chain_extend(parent, digit_for_child1)
 * digit = 1..26 for minor route (hex/tri/oct faces)
 */
static inline int promote_extend_address(
    const ChainKey *parent, ChainKey *child0, ChainKey *child1,
    uint8_t digit0, uint8_t digit1)
{
    if (!parent || !child0 || !child1 || !chain_valid(parent)) return 0;
    if (parent->n >= CHAIN_MAXL) return 0;
    /* copy parent to children */
    *child0 = *parent;
    *child1 = *parent;
    /* extend */
    if (!chain_extend(child0, digit0)) return 0;
    if (!chain_extend(child1, digit1)) return 0;
    return 1;
}

/* ── promote check ─────────────────────────────────────────────── */
static inline int promote_needed(int bucket_size, int cap) {
    return bucket_size > cap;
}

#endif /* CHAIN_PROMOTE_H */
