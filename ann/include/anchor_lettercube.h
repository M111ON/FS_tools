/* anchor_lettercube.h — bridge: k-means anchor bucket id -> LetterCube identity.
 *
 * WHY THIS EXISTS
 *   lettercube_rhombi.h (ann root) encodes a DIRECTION d as {face, offset} at one
 *   of three levels (L0 6 axial / L1 14 / L2 26 faces) and promotes between them
 *   with lcr_entry_promote, which needs no vector. What was missing is the map
 *   from an anchor bucket id (anch_assign / anch_route output, [0,K)) into that
 *   face space, so a bucket can carry a level + face identity instead of only an
 *   integer. This is the sibling of anchor_letter.h, which does the same job for
 *   the 6-letter / 720-orbit scheme; this one targets the 26-face rhombi scheme.
 *
 * RULE (deterministic, no RNG, byte-stable, no allocation)
 *   Bucket k's TRUE direction is a full L2 (26-face) identity:
 *       d(k) = LCR_FACE_CODE[ rank(k) % 26 ]
 *   where rank(k) is k's position in the total order "centroid energy (L2 norm of
 *   the centroid row) DESC, ties to the lower bucket id" — the SAME order
 *   anchor_letter.h uses, so both bridges name the same bucket "first".
 *   K > 26 wraps (a face hosts several buckets), K < 26 leaves tail faces empty;
 *   both keep the map total, and rank is a bijection onto [0,K).
 *
 * LEVEL / PROMOTION
 *   d is level-independent; where a bucket SITS depends on the level. At a coarse
 *   level the stored face is the coarse parent lcr_parent(d, L) and the offset
 *   records the lean, so promotion (lcr_entry_promote) is O(1) and never reads a
 *   vector:
 *       lcr_entry_dir(lcr_entry_make(d, L)) == d   for every L
 *   The trigger is the geometry's own lcr_need_promote(level, steps) — the bridge
 *   adds no second rule. NOTE: LCR_MAX_STEPS is {13,9,7}, i.e. DECREASING, so
 *   "the first level whose cap admits `steps`" is not well defined (a bucket can
 *   exceed L1's cap while fitting L0's) and L1 is unreachable as a first level;
 *   read the caps as the per-corpus-size choice the header says they are, and
 *   evaluate lcr_need_promote at the bucket's CURRENT level.
 *
 * Header-only, std-only. Scratch is fixed-size on the stack.
 */
#ifndef ANCHOR_LETTERCUBE_H
#define ANCHOR_LETTERCUBE_H

#include <stdint.h>
#include "../lettercube_rhombi.h"   /* LCR_* geometry (lives at the ann root) */
#include "anchor_route.h"           /* ANCHR_MAXK, anch_route */

/* ── energy of one centroid row (same measure anchor_letter.h uses) ─────── */
static inline double alc_energy(const float *C, int k, int dim)
{
    const float *c = C + (size_t)k * dim;
    double e = 0.0;
    for (int j = 0; j < dim; j++) e += (double)c[j] * (double)c[j];
    return e;
}

/* ── rank of one bucket in the total order (energy desc, id asc) ─────────
 * Counts the buckets that sort before k; no sort, no scratch. -1 = bad arg. */
static inline int alc_rank_of(int bucket, int K, const float *C, int dim)
{
    if (!C || bucket < 0 || bucket >= K || K <= 0 || dim <= 0) return -1;
    double eb = alc_energy(C, bucket, dim);
    int r = 0;
    for (int m = 0; m < K; m++) {
        if (m == bucket) continue;
        double em = alc_energy(C, m, dim);
        if (em > eb || (em == eb && m < bucket)) r++;
    }
    return r;
}

/* ── bucket -> its TRUE direction: a full L2 (26-face) code, never 0 ────── */
static inline uint8_t alc_dir_of(int bucket, int K, const float *C, int dim)
{
    lcr_tables_init();
    int r = alc_rank_of(bucket, K, C, dim);
    if (r < 0) return 0;
    return LCR_FACE_CODE[r % LCR_FACES];
}

/* ── bucket at a level: stored face = coarse parent, offset = the lean ──── */
static inline LCREntry alc_entry_of(int bucket, int K, const float *C, int dim,
                                    uint8_t level)
{
    return lcr_entry_make(alc_dir_of(bucket, K, C, dim), level);
}

/* promotion is the geometry's own O(1) rule: re-parent from the recovered d */
static inline LCREntry alc_entry_promote(LCREntry e, uint8_t new_level)
{
    return lcr_entry_promote(e, new_level);
}

/* ── the shallowest level that names d EXACTLY (stored offset == 0) ──────
 * offset == 0  <=>  lcr_parent(d, L) == d, so this is a property of the
 * geometry, not a threshold: axial directions name at L0, corners at L1, edges
 * at L2. Below it the entry still recovers d, but the lean is carrying the
 * difference. LCR_LEVELS = d was not nameable (unreachable for a valid d — the
 * test asserts that) so a caller can fail loud instead of using a short level. */
static inline uint8_t alc_name_level(uint8_t d)
{
    lcr_tables_init();
    if (!lcr_valid(d)) return LCR_LEVELS;
    for (uint8_t L = 0; L < LCR_LEVELS; L++)
        if (lcr_entry_make(d, L).offset == 0) return L;
    return LCR_LEVELS;
}

#endif /* ANCHOR_LETTERCUBE_H */
