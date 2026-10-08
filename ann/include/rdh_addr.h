/*
 * rdh_addr.h — RDH (Ring-Wedge-Mirror) Generic Addressing Module
 *
 * Unified address = (ring, wedge, mirror, u, v) → flat key
 *
 * This is a PURE INTEGER bijection (mixed-radix encoding):
 *   key = ((ring × n_wedges + wedge) × n_mirror + mirror) × max_u + u
 *
 * Guarantees:
 *   - No collision (each param tuple maps to unique key)
 *   - O(1) encode/decode
 *   - No hash, no lookup table
 *   - Deterministic across sessions, platforms, compilers
 *
 * Compare to hash-based addressing (addr_space.h):
 *   - Hash: collision possible, not reversible, no geometric meaning
 *   - RDH: collision-free, reversible, params have meaning (ring=layer, etc.)
 *
 * Usage: declare RDHConfig with your dimensions, then call rdh_key/rdh_decompose.
 * Common config presets provided below.
 */

#ifndef RDH_ADDR_H
#define RDH_ADDR_H

#include <stdint.h>

/* ═══════════════════════════════════════════════════════════════
 * ADDRESS SPACE CONFIG
 * ═══════════════════════════════════════════════════════════════
 * Each dimension corresponds to a geometric or architectural axis:
 *   n_rings   = how many "layers" or concentric rings   (e.g. 64 layers)
 *   n_wedges  = how many subdivisions per ring           (e.g. 24 wedges)
 *   n_mirror  = how many mirror states (e.g. 2 = K/V)   (A/B duality)
 *   max_u     = how many positions per (ring,wedge,mirror) (e.g. 256)
 *   n_v       = reserved sub-position dimension          (e.g. 1)
 */
typedef struct {
    int64_t n_rings;
    int64_t n_wedges;
    int64_t n_mirror;
    int64_t max_u;
    int64_t n_v;
} RDHConfig;

/* ── Preset configs ─────────────────────────────────────────── */

/* KV cache page store (kv_page_rdh.h):
 *   64 layers × 1 head-group × 2 (K/V) × 256 position ranges × 1
 *   Total: 32768 */
#define RDH_KV_PAGE  { 64, 1, 2, 256, 1 }

/* LFM2 KV cache (6 attn layers, 24 heads per layer):
 *   6 layers × 24 heads × 2 (K/V) × 256 positions × 1
 *   Total: 73728 — fine-grained per-head eviction */
#define RDH_KV_HEAD  { 6, 24, 2, 256, 1 }

/* Model weight address space (addr_space.h tier0):
 *   128 × 162 × 1 × 1 × 1
 *   Total: 20736 = 144² */
#define RDH_TIER0    { 128, 162, 1, 1, 1 }

/* ═══════════════════════════════════════════════════════════════
 * CORE API — encode / decode / capacity
 * ═══════════════════════════════════════════════════════════════ */

/* Encode 5-tuple (ring, wedge, mirror, u, v) → flat integer key.
 * O(1), no collision, no hash. */
static inline int64_t rdh_key(
    const RDHConfig *cfg,
    int64_t ring, int64_t wedge, int64_t mirror,
    int64_t u, int64_t v)
{
    (void)v;
    return ((ring * cfg->n_wedges + wedge) * cfg->n_mirror + mirror)
           * cfg->max_u + u;
}

/* Decompose flat key back into (ring, wedge, mirror, u).
 * v is not recovered (always 0). */
static inline void rdh_decompose(
    const RDHConfig *cfg, int64_t key,
    int64_t *ring, int64_t *wedge, int64_t *mirror, int64_t *u)
{
    int64_t t = key;
    *u = t % cfg->max_u;
    t /= cfg->max_u;
    *mirror = t % cfg->n_mirror;
    t /= cfg->n_mirror;
    *wedge = t % cfg->n_wedges;
    t /= cfg->n_wedges;
    *ring = t;
}

/* Total address space size = capacity */
static inline int64_t rdh_capacity(const RDHConfig *cfg) {
    return cfg->n_rings * cfg->n_wedges * cfg->n_mirror * cfg->max_u * cfg->n_v;
}

/* ═══════════════════════════════════════════════════════════════
 * UTILITY — check params are in bounds
 * ═══════════════════════════════════════════════════════════════ */

static inline int rdh_valid(
    const RDHConfig *cfg,
    int64_t ring, int64_t wedge, int64_t mirror,
    int64_t u, int64_t v)
{
    (void)v;
    return ring   >= 0 && ring   < cfg->n_rings
        && wedge  >= 0 && wedge  < cfg->n_wedges
        && mirror >= 0 && mirror < cfg->n_mirror
        && u      >= 0 && u      < cfg->max_u;
}

/* ═══════════════════════════════════════════════════════════════
 * WEDGE RING SYMMETRY — geometric transformations (optional)
 * ═══════════════════════════════════════════════════════════════
 * These implement the 6-fold symmetry described in wedge_ring_address.h.
 * Not needed for basic addressing, included for completeness.
 */

/* Rotate address by r_wedge wedges (mod n_wedges) */
static inline int64_t rdh_rotate(const RDHConfig *cfg, int64_t key, int64_t r_wedge) {
    int64_t r, w, m, u;
    rdh_decompose(cfg, key, &r, &w, &m, &u);
    w = (w + r_wedge) % cfg->n_wedges;
    return rdh_key(cfg, r, w, m, u, 0);
}

#endif /* RDH_ADDR_H */
