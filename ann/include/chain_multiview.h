/* chain_multiview.h — multi-view summon: multiple fixed projections → multiple buckets
 *
 * Owner concept: data is STATIONARY (tesseract). Instead of buying bigger
 * embedding models (more dims), we ROTATE around the data using fixed-frame
 * indices. Each "view" = a different seeded projection (different angle).
 * Multi-view summon = call all views → union of buckets → scan.
 * Verify = consensus: all views should agree (or at least overlap).
 *
 * This is NOT spin (not multi-table LSH with ×L memory). Data is stored
 * ONCE. Views are computed on demand from the address (O(1) per view).
 *
 * Design:
 *   - N_VIEWS fixed seeds (e.g., 4, 8, or 24 for O group)
 *   - For a query vector, compute face for each seed → bucket for each view
 *   - Summon = union of all view-buckets (dedup)
 *   - Verify = intersection count (how many views agree on the same bucket)
 */
#ifndef CHAIN_MULTIVIEW_H
#define CHAIN_MULTIVIEW_H

#include <stdint.h>
#include "chain_fixedanchor.h"

#define MV_MAX_VIEWS  8   /* max views per summon (O group = 24, but 8 is practical) */
#define MV_DEFAULT_VIEWS 4

/* ── view seeds (deterministic, O group inspired) ────────────────
 * 8 seeds = 8 "angles" around the data. Each seed generates a different
 * projection matrix. Same vector → different faces for different seeds. */
static const uint32_t mv_seeds[MV_MAX_VIEWS] = {
    42, 137, 256, 512, 1024, 2048, 4096, 8192
};

/* ── summon: compute buckets for all views ───────────────────────
 * Returns number of unique buckets. Fills `buckets[]` with bucket ids.
 * buckets[] must have capacity >= MV_MAX_VIEWS. */
static inline int mv_summon(const float *vec, int dim,
                            int *buckets, int nviews) {
    if (!vec || !buckets || dim <= 0) return 0;
    if (nviews <= 0 || nviews > MV_MAX_VIEWS) nviews = MV_DEFAULT_VIEWS;

    int n = 0;
    for (int v = 0; v < nviews; v++) {
        int face = fa_anchor(vec, dim, mv_seeds[v]);
        /* dedup */
        int dup = 0;
        for (int i = 0; i < n; i++) {
            if (buckets[i] == face) { dup = 1; break; }
        }
        if (!dup) buckets[n++] = face;
    }
    return n;
}

/* ── verify: consensus count ─────────────────────────────────────
 * How many views agree on the same bucket?
 * Returns the max agreement count (0..nviews). */
static inline int mv_consensus(const float *vec, int dim, int nviews) {
    if (!vec || dim <= 0) return 0;
    if (nviews <= 0 || nviews > MV_MAX_VIEWS) nviews = MV_DEFAULT_VIEWS;

    int faces[MV_MAX_VIEWS];
    for (int v = 0; v < nviews; v++) {
        faces[v] = fa_anchor(vec, dim, mv_seeds[v]);
    }

    int max_agree = 1;
    for (int v = 0; v < nviews; v++) {
        int agree = 1;
        for (int w = v + 1; w < nviews; w++) {
            if (faces[v] == faces[w]) agree++;
        }
        if (agree > max_agree) max_agree = agree;
    }
    return max_agree;
}

/* ── agreement ratio ───────────────────────────────────────────── */
static inline float mv_agreement_ratio(const float *vec, int dim, int nviews) {
    int agree = mv_consensus(vec, dim, nviews);
    return (float)agree / (float)nviews;
}

#endif /* CHAIN_MULTIVIEW_H */
