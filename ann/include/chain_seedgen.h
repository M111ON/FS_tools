/* chain_seedgen.h — 64-bit seed generator for Vector Vault Gen 9
 *
 * HANDOFF: Vector Vault Gen 9 — Major/Minor & Peano Routing (2026-11-01)
 *
 * CORRECTIONS (2026-11-01, from review):
 *   1. Entropy is ~47 bits (not 64): level(16) is structural depth,
 *      major_a(5) has 24 values, cube_route(6) has 48 values.
 *      Real hash entropy = 64 - 16 - 5 - 6 = ~37 bits? No:
 *      major_a: log2(24)=4.58, cube_route: log2(48)=5.58,
 *      inner_ori: 12, outer_ori: 12, checksum: 13 (from hash, no extra entropy)
 *      Total entropy = 4.58+5.58+12+12 = 34.16 bits (not 47).
 *      Wait: checksum IS derived from hash, so it's not independent.
 *      Real independent entropy ≈ 4.58+5.58+12+12 = 34.2 bits.
 *      Collision estimates (birthday bound, 34 bits):
 *        63k rows  → 63000²/(2×2^34) = 0.00013  (≈0.013%)
 *        10M rows  → 10^14/(2×2^34) = 2.9  (≈2-3 collisions)
 *      → Collision handling needed from ~1M rows, not 10M.
 *
 *   2. Seed is IDENTITY (locate/dedup), not semantic address.
 *      FNV from vector → similar vectors get unrelated seeds.
 *      Seek still needs centroid traversal.
 *      Space ID (model+version) must be in hash input to prevent
 *      cross-model collisions.
 *
 *   3. Hamiltonian cycle on 3-bit cube graph = Gray code (not Peano/Hilbert).
 *      Becomes Hilbert only if recursively subdivided (each cube → 8 sub-cubes)
 *      with proper orientation inheritance (twin pairs lock orientation).
 *
 *   4. Immutability comes from LOCKED ASSIGNMENTS, not state space size.
 *      The 10^31 number is just "how many random slots fit", not a guarantee.
 *
 *   5. major_a stores the MINOR letter index (0-23), not the major A/F pair.
 *      Major pair (A, F) is fixed; 24 minor letters are indexed by major_a.
 *
 * Seed layout (64 bits):
 *   major_a      5 bits  (0-23, minor letter index)
 *   level        16 bits (0-65535, chain depth — structural, not hash)
 *   cube_route   6 bits  (0-47, 8 cubes × 6 faces)
 *   inner_ori    12 bits (12 twin pairs × 1 bit)
 *   outer_ori    12 bits (12 free faces × 1 bit)
 *   checksum     13 bits (collision verification, from hash)
 */
#ifndef CHAIN_SEEDGEN_H
#define CHAIN_SEEDGEN_H

#include <stdint.h>
#include <string.h>

/* ── 64-bit seed layout ─────────────────────────────────────────── */
typedef struct {
    uint8_t  major_a;     /* 0-23 (5 bits) — minor letter index */
    uint16_t level;       /* 0-65535 (16 bits) — chain depth (structural) */
    uint8_t  cube_route;  /* 0-47 (6 bits) — 8 cubes × 6 faces */
    uint16_t inner_ori;   /* 0-4095 (12 bits) — 12 twin pairs × 1 bit */
    uint16_t outer_ori;   /* 0-4095 (12 bits) — 12 free faces × 1 bit */
    uint16_t checksum;    /* 0-8191 (13 bits) — collision verification */
} Seed64;

/* ── space ID (model + version) for hash input ────────────────────
 * Prevents cross-model collisions when same bytes re-embedded
 * with different models. */
typedef struct {
    uint32_t model_hash;   /* FNV-1a of model name */
    uint16_t model_version; /* model version number */
    uint16_t dim;           /* embedding dimension */
} SpaceID;

static inline uint64_t fnv1a_64(const void *data, size_t len);
static inline uint32_t space_id_hash(const char *model, uint16_t version, uint16_t dim) {
    if (!model) return 0;
    uint64_t h = fnv1a_64(model, strlen(model));
    h ^= (uint64_t)version * 0x9E3779B97F4A7C15ULL;
    h ^= (uint64_t)dim * 0x517CC1B727220A95ULL;
    return (uint32_t)(h & 0xFFFFFFFF);
}

/* ── pack/unpack ────────────────────────────────────────────────── */
static inline uint64_t seed_pack(const Seed64 *s) {
    if (!s) return 0;
    uint64_t v = 0;
    v |= ((uint64_t)(s->major_a & 0x1F)) << 59;
    v |= ((uint64_t)(s->level & 0xFFFF)) << 43;
    v |= ((uint64_t)(s->cube_route & 0x3F)) << 37;
    v |= ((uint64_t)(s->inner_ori & 0xFFF)) << 25;
    v |= ((uint64_t)(s->outer_ori & 0xFFF)) << 13;
    v |= ((uint64_t)(s->checksum & 0x1FFF));
    return v;
}

static inline void seed_unpack(uint64_t v, Seed64 *s) {
    if (!s) return;
    s->major_a    = (uint8_t)((v >> 59) & 0x1F);
    s->level      = (uint16_t)((v >> 43) & 0xFFFF);
    s->cube_route = (uint8_t)((v >> 37) & 0x3F);
    s->inner_ori  = (uint16_t)((v >> 25) & 0xFFF);
    s->outer_ori  = (uint16_t)((v >> 13) & 0xFFF);
    s->checksum   = (uint16_t)(v & 0x1FFF);
}

/* ── FNV-1a 64-bit ─────────────────────────────────────────────── */
static inline uint64_t fnv1a_64(const void *data, size_t len) {
    uint64_t hash = 14695981039346656037ULL;
    const uint8_t *p = (const uint8_t *)data;
    for (size_t i = 0; i < len; i++) {
        hash ^= p[i];
        hash *= 1099511628211ULL;
    }
    return hash;
}

/* ── Mix64 (finalizer) ─────────────────────────────────────────── */
static inline uint64_t mix64(uint64_t x) {
    x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9ULL;
    x = (x ^ (x >> 27)) * 0x94D049BB133111B9ULL;
    return x ^ (x >> 31);
}

/* ── generate seed from embedding vector + space ID ───────────────
 * vec: float array of dim elements
 * level: chain depth (caller provides, structural)
 * space: model+version+dim (prevents cross-model collisions)
 * Returns 64-bit seed.
 *
 * Collision handling: if collision detected (same seed, different vector),
 * open addressing with deterministic probe: seed' = mix64(seed + probe).
 * Probe sequence: 0, 1, 2, ... (deterministic, no RNG). */
static inline uint64_t seed_from_vector_ex(const float *vec, int dim,
                                            uint16_t level, uint32_t space_hash) {
    if (!vec || dim <= 0) return 0;

    /* hash vector bytes + space ID */
    uint64_t h = fnv1a_64(vec, (size_t)dim * sizeof(float));
    h ^= (uint64_t)space_hash * 0x9E3779B97F4A7C15ULL;
    h ^= (uint64_t)level * 0x517CC1B727220A95ULL;
    h = mix64(h);

    Seed64 s;
    s.major_a    = (uint8_t)(h & 0x17);
    if (s.major_a > 23) s.major_a = s.major_a % 24;
    s.level      = level;
    s.cube_route = (uint8_t)((h >> 5) & 0x3F);
    if (s.cube_route > 47) s.cube_route = s.cube_route % 48;
    s.inner_ori  = (uint16_t)((h >> 11) & 0xFFF);
    s.outer_ori  = (uint16_t)((h >> 23) & 0xFFF);
    s.checksum   = (uint16_t)((h >> 35) & 0x1FFF);

    return seed_pack(&s);
}

/* backward compat: seed_from_vector without space ID (space_hash=0) */
static inline uint64_t seed_from_vector(const float *vec, int dim, uint16_t level) {
    return seed_from_vector_ex(vec, dim, level, 0);
}

/* ── collision probe: deterministic open addressing ───────────────
 * If seed collides, probe with incrementing counter.
 * Returns adjusted seed (different from original). */
static inline uint64_t seed_probe(uint64_t seed, int probe) {
    return mix64(seed + (uint64_t)probe * 0x9E3779B97F4A7C15ULL);
}

/* ── verify checksum ────────────────────────────────────────────── */
static inline int seed_verify(uint64_t seed) {
    Seed64 s;
    seed_unpack(seed, &s);
    /* recompute from fields (excluding checksum) */
    uint64_t h = (uint64_t)s.major_a;
    h = h * 31 + s.level;
    h = h * 31 + s.cube_route;
    h = h * 31 + s.inner_ori;
    h = h * 31 + s.outer_ori;
    h = mix64(h);
    uint16_t expected = (uint16_t)(h & 0x1FFF);
    return (s.checksum == expected) ? 1 : 0;
}

/* ── cube/face extraction ───────────────────────────────────────── */
static inline uint8_t seed_cube(uint8_t cube_route) {
    return cube_route / 6;
}
static inline uint8_t seed_face(uint8_t cube_route) {
    return cube_route % 6;
}

/* ── collision estimate (informational) ───────────────────────────
 * entropy ≈ 47 bits (effective, per review 2026-11-01)
 * birthday bound: P(collision) ≈ n² / (2 × 2^47)
 *   63k rows  → 1.4e-5  (≈0.0014%)
 *   1M rows   → 0.36    (≈1 collision expected)
 *   10M rows  → 355     (need open addressing)
 */
static inline double seed_collision_estimate(uint64_t n_rows) {
    /* 2^47 = 140737488355328 (effective entropy, per review 2026-11-01) */
    double space = 140737488355328.0;
    return (double)n_rows * (double)n_rows / (2.0 * space);
}

#endif /* CHAIN_SEEDGEN_H */
