/* chain_fixedanchor.h — fixed deterministic anchor (no training, no RNG at runtime)
 *
 * Owner concept: data is STATIONARY (tesseract). We don't buy bigger
 * embedding models (more dims). Instead we ROTATE around the data using
 * fixed-frame indices — deterministic, bijection, O(1), no re-embed.
 *
 * This header implements the FIRST piece: a FIXED projection from
 * embedding vector → LetterCube face (0..25), without any training data.
 *
 * How: a seeded LCG generates a fixed 3×D projection matrix at compile
 * time (constant). f(vec) = argmax over 26 faces of (M_face · vec).
 * Same vector → same face, always. No k-means, no training, no RNG at runtime.
 *
 * The matrix is generated once (seed=FIXED_SEED) and stored as a constant.
 * Changing the seed = changing the "angle" of view (multi-view).
 *
 * This replaces anch_route() (learned) with a fixed function.
 * Quality may be lower than k-means (projection loses info) but
 * multi-view (multiple seeds) recovers it via XOR consensus.
 */
#ifndef CHAIN_FIXEDANCHOR_H
#define CHAIN_FIXEDANCHOR_H

#include <stdint.h>
#include <stdlib.h>
#include <math.h>
#include "chain_addr.h"   /* CHAIN_BASE */

#define FA_SEED_DEFAULT  42u
#define FA_NFACES        26u   /* LetterCube faces */
#define FA_MAXDIM        1024u /* max embedding dim */

/* ── seeded LCG (deterministic, no stdlib rand) ─────────────────── */
static inline uint32_t fa_lcg(uint32_t *state) {
    *state = *state * 1664525u + 1013904223u;
    return *state;
}

/* ── generate fixed projection matrix ─────────────────────────────
 * M[face][dim] = (lcg() / 2^32) * 2 - 1  → range [-1, 1]
 * Deterministic: same seed → same matrix. */
static inline void fa_gen_matrix(float *M, int nfaces, int dim, uint32_t seed) {
    uint32_t state = seed;
    for (int f = 0; f < nfaces; f++) {
        for (int d = 0; d < dim; d++) {
            uint32_t r = fa_lcg(&state);
            M[f * dim + d] = ((float)r / 4294967296.0f) * 2.0f - 1.0f;
        }
    }
}

/* ── vector → face (single view) ────────────────────────────────
 * f(vec) = argmax_face (M_face · vec)
 * Returns 0..25. O(nfaces × dim). No training, no RNG. */
static inline int fa_vector_to_face(const float *vec, int dim,
                                     const float *M, int nfaces) {
    if (!vec || !M || dim <= 0 || nfaces <= 0) return 0;
    int best = 0;
    float best_score = -1e30f;
    for (int f = 0; f < nfaces; f++) {
        const float *row = M + f * dim;
        float score = 0.0f;
        for (int d = 0; d < dim; d++) {
            score += row[d] * vec[d];
        }
        if (score > best_score) {
            best_score = score;
            best = f;
        }
    }
    return best;
}

/* ── fixed anchor: vector → LetterCube face (0..25) ─────────────
 * Convenience: generates matrix on first call, caches it.
 * Thread-safe? No — single-threaded use only (ANN server is single-threaded). */
static float *fa_cached_matrix = NULL;
static int fa_cached_dim = 0;
static int fa_cached_nfaces = 0;
static uint32_t fa_cached_seed = 0;

static inline const float *fa_get_matrix(int dim, int nfaces, uint32_t seed) {
    if (fa_cached_matrix && fa_cached_dim == dim &&
        fa_cached_nfaces == nfaces && fa_cached_seed == seed) {
        return fa_cached_matrix;
    }
    if (fa_cached_matrix) free(fa_cached_matrix);
    fa_cached_matrix = (float *)malloc((size_t)nfaces * dim * sizeof(float));
    if (!fa_cached_matrix) return NULL;
    fa_gen_matrix(fa_cached_matrix, nfaces, dim, seed);
    fa_cached_dim = dim;
    fa_cached_nfaces = nfaces;
    fa_cached_seed = seed;
    return fa_cached_matrix;
}

static inline int fa_anchor(const float *vec, int dim, uint32_t seed) {
    const float *M = fa_get_matrix(dim, (int)FA_NFACES, seed);
    if (!M) return 0;
    return fa_vector_to_face(vec, dim, M, (int)FA_NFACES);
}

/* ── cleanup ───────────────────────────────────────────────────── */
static inline void fa_cleanup(void) {
    if (fa_cached_matrix) { free(fa_cached_matrix); fa_cached_matrix = NULL; }
    fa_cached_dim = 0;
    fa_cached_nfaces = 0;
    fa_cached_seed = 0;
}

#endif /* CHAIN_FIXEDANCHOR_H */
