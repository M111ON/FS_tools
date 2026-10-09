/* anchor_letter.h — the bridge: anchor buckets -> 6-letter face ordering -> 720 orbit.
 *
 * WHY THIS EXISTS
 *   anchor_route.h decides WHICH BUCKET a vector belongs to (nearest-centroid).
 *   geo_wang_latch.h gives the 144x72 branch space a bucket's fingerprint can be
 *   dropped on. What was missing between them: a deterministic map from a bucket
 *   id to the 6-letter FACE ORDER of the owner's Rubik-anchor scheme, so
 *   placement becomes an orbit (720 states) instead of a flat index.
 *
 * THREE LAYERS, KEPT SEPARATE (matches the mechanism map in anchor_route.h)
 *   DECIDE  anch_assign(q)            -> bucket k in [0,K)
 *   FACE    al_letter_of(k, C, dim)   -> 6 letters, one per face (a face = a
 *           group of buckets; letters = buckets ordered by centroid energy)
 *   ORBIT   al_orbit_of(letters)      -> Lehmer rank in [0,720)  (6! = 720)
 *   DROP    al_cell_choice(...)       -> (cell, choice) on the 144x72 plane
 *
 * The 6 letters are A..F. Each letter is a NAME for a bucket (or for a group of
 * buckets under al_partition). The face order is derived from centroid energy
 * (L2 norm of each centroid's dim-vector), so the same anchors always yield the
 * same letter order: no RNG, byte-stable, identical across processes.
 *
 * KEY FACT (owner, 2026-10-09): 1 char per face is MANDATORY, not optional.
 *   Without it the face order is not well-defined and the orbit walk is not a
 *   closed permutation. al_letter_of() therefore always returns exactly 6
 *   letters (bucket ids), padding by wrapping when K < 6.
 *
 * LETTER WEIGHTS (owner sheet): each letter carries a 0..255 weight
 *   a=176 b=69 c=212 ... (CHARACTER WEIGHTS, 52 letters). We expose the first 6
 *   as AL_WEIGHT[0..5] as *seed material* only — they do NOT drive routing (a
 *   weight is magnitude, not meaning; see the popcount caveat in project memory
 *   #7412). al_weight_of() gives the weight attached to a face slot for callers
 *   that want it; nothing in the decide->drop path depends on it.
 *
 * Determinism: pure functions of (centroids, query). No RNG, no malloc on the
 * hot path (fixed 6-element scratch), no file I/O. Header-only, std-only.
 */
#ifndef ANCHOR_LETTER_H
#define ANCHOR_LETTER_H

#include <stdint.h>
#include <string.h>
#include <math.h>

#include "anchor_route.h"        /* anch_assign, ANCHR_MAXK, anch_slot */
#include "geo_wang_latch.h"      /* wl_id, WL_COUNT  (144 x 72 = 10368) */

#define AL_FACES   6
#define AL_ORBITS  720u          /* 6! */
#define AL_LETTERS 6

/* CHARACTER WEIGHTS (owner sheet, first 6 of the 52): seed material only. */
static const uint8_t AL_WEIGHT[AL_LETTERS] = { 176u, 69u, 212u, 31u, 219u, 56u };

/* ── energy of one centroid row (L2 norm, doubles for stability) ───────── */
static inline double al_centroid_energy(const float *C, int k, int dim) {
    const float *c = C + (size_t)k * dim;
    double e = 0.0;
    for (int j = 0; j < dim; j++) e += (double)c[j] * (double)c[j];
    return e;
}

/* ── FACE: pick the AL_FACES distinct buckets that name the six faces ────
 * Rule: rank ALL K buckets by centroid energy descending, tie broken by the
 * lower bucket id (so order is total and byte-stable). Faces 0..5 get the
 * first six ranks; when K < 6 the rank wraps modulo K, so a face order is
 * always returned and the orbit is always closed (owner rule: 1 char/face is
 * mandatory). letters[] receives bucket ids. Returns AL_FACES. */
static inline int al_letter_of(int K, const float *C, int dim, int letters[AL_FACES]) {
    if (!C || !letters || K <= 0 || dim <= 0) return 0;
    int rank[ANCHR_MAXK];
    int n = (K > ANCHR_MAXK) ? ANCHR_MAXK : K;
    for (int k = 0; k < n; k++) rank[k] = k;
    double en[ANCHR_MAXK];
    for (int k = 0; k < n; k++) en[k] = al_centroid_energy(C, k, dim);
    /* selection sort (n <= 512): energy desc, id asc on ties */
    for (int a = 0; a < n; a++) {
        int best = a;
        for (int b = a + 1; b < n; b++) {
            double eb = en[rank[b]], ea = en[rank[best]];
            if (eb > ea || (eb == ea && rank[b] < rank[best])) best = b;
        }
        int t = rank[a]; rank[a] = rank[best]; rank[best] = t;
    }
    for (int f = 0; f < AL_FACES; f++) letters[f] = rank[f % n];
    return AL_FACES;
}

/* ── ORBIT: Lehmer rank of a 6-letter order -> [0,720) ────────────────────
 * letters[] are face slots; two letters map to the same bucket only when K<6.
 * Rank is computed over the SEQUENCE OF SLOTS as seen (duplicates allowed:
 * a repeated slot is treated as its own value, which keeps 6! = 720 reachable
 * exactly when the six slots are distinct — the normal case K>=6). */
static inline uint32_t al_orbit_of(const int letters[AL_FACES]) {
    uint32_t rank = 0;
    for (int i = 0; i < AL_FACES; i++) {
        int smaller = 0;
        for (int j = i + 1; j < AL_FACES; j++)
            if (letters[j] < letters[i]) smaller++;
        /* factorial base: rank += smaller * (FACES-1-i)! */
        uint32_t f = 1;
        for (int t = 2; t <= (AL_FACES - 1 - i); t++) f *= (uint32_t)t;
        rank += (uint32_t)smaller * f;
    }
    return rank % AL_ORBITS;
}

/* ── DROP: where this bucket+orbit lands on the 144 x 72 plane ────────────
 * cell   in [0,144)  (WL id / 72)
 * choice in [0,72)   (WL id % 72)
 * cell   = bucket's slot, mapped onto 144 by the MOD-bijection (anch_slot).
 * choice = orbit reduced onto 72 (orbit is 720 = 72 * 10, so choice keeps the
 *          low-10 residue; the high part is the "round" the latch log stamps).
 * Reserved ids 0 (all-open = major input) and 10367 (all-shut = major output)
 * are the plane's own endpoints; a data drop must not land on them (wl_traverse
 * refuses reserved anyway). When the mapping would produce one we bump cell by
 * one — the drop still lands in [1, 10366] and stays reversible via the RECORD
 * (cell,choice) we hand back.
 * Returns the wl id in [1, WL_COUNT-1]. */
static inline uint32_t al_cell_choice(int bucket, int K, uint32_t orbit,
                                      uint32_t *cell_out, uint32_t *choice_out,
                                      uint32_t *round_out) {
    uint32_t n = (K > 1) ? (uint32_t)K : 1u;
    if (n % 37u == 0u) n++;                 /* keep anch_slot bijective */
    uint32_t slot = anch_slot((uint32_t)(bucket < 0 ? 0 : bucket), n);
    uint32_t cell   = slot % 144u;
    uint32_t choice = orbit % 72u;
    uint32_t round  = orbit / 72u;          /* 0..9 */
    if (cell == 0u) cell = 1u;              /* skip reserved LO */
    if (cell == 143u && choice == 71u) choice = 70u; /* skip reserved HI */
    if (cell_out)   *cell_out   = cell;
    if (choice_out) *choice_out = choice;
    if (round_out)  *round_out  = round;
    return wl_id(cell, choice);
}

/* ── convenience: one call, anchor -> latch id (+ orbit diagnostics) ───── */
typedef struct {
    int      bucket;   /* anch_assign result, [0,K) or -1 */
    int      letters[AL_FACES];
    uint32_t orbit;    /* [0,720) */
    uint32_t cell;     /* [0,144) */
    uint32_t choice;   /* [0,72) */
    uint32_t round;    /* [0,10) */
    uint32_t latch_id; /* wl_id(cell,choice) in [0,10368) */
} al_route_t;

static inline int al_route(const float *q, const float *C, int K, int dim,
                           al_route_t *out) {
    if (!q || !C || !out || K <= 0 || dim <= 0) return -1;
    int b = anch_assign(q, C, K, dim);
    if (b < 0) return -1;
    out->bucket = b;
    al_letter_of(K, C, dim, out->letters);
    out->orbit = al_orbit_of(out->letters);
    out->latch_id = al_cell_choice(b, K, out->orbit,
                                   &out->cell, &out->choice, &out->round);
    return 0;
}

/* ── weight lookup (seed material; not used by the routing path) ──────── */
static inline uint8_t al_weight_of(int face) {
    if (face < 0) face = -face;
    return AL_WEIGHT[face % AL_LETTERS];
}

/* ── letter name for a face slot (A..F) ───────────────────────────────── */
static inline char al_letter_name(int face) {
    if (face < 0) face = -face;
    return (char)('A' + (face % AL_FACES));
}

#endif /* ANCHOR_LETTER_H */
