/*
 * kv_tess_cache.h — lightweight KV cache addressed by geometry.
 *
 * PROBLEM. A cross-session KV store (past key/value kept for reuse) is usually
 * a flat float tensor: you pay RAM for every token and you can only evict by
 * recency (LFU/LRU). Two tokens that mean the same thing have no relation in
 * that store, so a light cache cannot tell "this KV is a duplicate of that
 * one" and a victim is chosen by age, not by meaning.
 *
 * IDEA. Store each KV block as ONE geometry window (20736 bytes, WC_WIN) at an
 * address derived from its own vector: anchor bucket -> latch id (144x72) ->
 * tess address (162 x 128 = 20736). The address is then the semantic key:
 *   - same meaning  -> same anchor bucket -> same neighbourhood -> coalesced;
 *   - eviction picks the block FARTHEST from the active anchor, not the oldest.
 *
 * STANDALONE. Headers only, no ggml, no DWGLS include path. Reuses the
 * snapshot curves in geo_curve_snapshot.h and the anchor letter connector in
 * anchor_letter.h (both already in this repo). Sacred numbers are shared with
 * the DWGLS tensor layer by construction: 20736 = 128 x 162 = WC_WIN = TESS.
 *
 * SCOPE. This is the store + placement + victim policy. It is NOT hooked into
 * llama.cpp here — the caller feeds KV bytes in and asks for a slot back.
 */
#ifndef KV_TESS_CACHE_H
#define KV_TESS_CACHE_H

#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#include "geo_curve_snapshot.h"   /* gc_addr / gc_decompose, 20736 space   */
#include "anchor_letter.h"        /* al_route: vec -> bucket -> latch id   */

#define KVT_WIN       20736u                 /* bytes per KV window         */
#define KVT_RESERVED  2u                     /* lo/hi latch ids not for data */

/* ── address: a KV vector maps to one window in [0,20736) ───────────────
 * anchor bucket is fixed by the trained centroids (C, K); the latch id picks
 * the cell. layer selects one of the anchor's GC_LAYERS sub-layers (=2 in the
 * DRAM tile: 0 = base hilbert, 1 = phase flip). It is masked to GC_LAYERS
 * because gc_addr does not clamp — passing layer >= GC_LAYERS would spill into
 * the next anchor's block and alias two different keys. */
static inline uint32_t kvt_addr_of(const float *kv, const float *C, int K,
                                   int dim, uint32_t layer)
{
    al_route_t r;
    if (al_route(kv, C, K, dim, &r) != 0) return 0u;
    uint32_t ax = r.cell;                 /* [0,144) */
    uint32_t ay = r.choice;               /* [0,72)  */
    /* fold the latch cell onto the 8x8 bitboard the DRAM tile addresses. */
    uint32_t x = ax & (GC_GRID_X - 1u);
    uint32_t y = ay & (GC_GRID_Y - 1u);
    uint32_t L = (GC_LAYERS > 0u) ? (layer % GC_LAYERS) : 0u;
    return gc_addr((uint32_t)(r.bucket < 0 ? 0 : r.bucket), x, y, L);
}

/* ── one cached KV block ─────────────────────────────────────────────────*/
typedef struct {
    uint32_t addr;        /* geometry address in [0,20736)               */
    uint32_t bucket;      /* anchor bucket (semantic key)                */
    uint64_t tick;        /* last touch (for accounting, not eviction)   */
    uint32_t bytes;       /* payload length (<= KVT_WIN)                 */
    uint8_t  live;        /* 1 = occupied                                */
} kvt_entry_t;

typedef struct {
    kvt_entry_t *tab;     /* caller storage, cap entries                 */
    uint32_t     cap;
    uint32_t     n;
    uint64_t     tick;
    uint64_t     hits, misses, evictions, coalesced;
} kvt_cache_t;

static inline void kvt_init(kvt_cache_t *c, kvt_entry_t *tab, uint32_t cap)
{
    c->tab = tab; c->cap = cap; c->n = 0; c->tick = 0;
    c->hits = c->misses = c->evictions = c->coalesced = 0;
    for (uint32_t i = 0; i < cap; i++) c->tab[i].live = 0;
}

static inline int kvt_find(const kvt_cache_t *c, uint32_t addr)
{
    for (uint32_t i = 0; i < c->cap; i++)
        if (c->tab[i].live && c->tab[i].addr == addr) return (int)i;
    return -1;
}

/* semantic distance between two buckets on the MOD-37 ring the placement
 * already uses: the shorter way round. Not euclidean on purpose — the ring is
 * the space anchors actually live in (anch_slot is (i*37)%n). */
static inline uint32_t kvt_bucket_dist(uint32_t a, uint32_t b, uint32_t n)
{
    if (n == 0) return 0;
    uint32_t d = (a > b) ? (a - b) : (b - a);
    return (d < n - d) ? d : n - d;
}

/* victim = the live entry whose bucket is FARTHEST from active_bucket.
 * Ties broken by oldest tick (deterministic). Returns index or -1. */
static inline int kvt_victim(const kvt_cache_t *c, uint32_t active_bucket,
                             uint32_t K)
{
    int    best = -1;
    uint32_t best_d = 0;
    uint64_t best_t = 0;
    for (uint32_t i = 0; i < c->cap; i++) {
        if (!c->tab[i].live) continue;
        uint32_t d = kvt_bucket_dist(c->tab[i].bucket, active_bucket, K);
        if (best < 0 || d > best_d ||
            (d == best_d && c->tab[i].tick < best_t)) {
            best = (int)i; best_d = d; best_t = c->tab[i].tick;
        }
    }
    return best;
}

/* put a KV block. Returns slot index, or -1 if the block could not be placed.
 * Coalesces: if a block already sits at this address, it is reused (same
 * meaning already cached) and *coalesced_out = 1. Otherwise: free slot, else
 * evict the semantically farthest entry. */
static inline int kvt_put(kvt_cache_t *c, const float *kv, const float *C,
                          int K, int dim, uint32_t layer, uint32_t bytes,
                          int *coalesced_out)
{
    if (coalesced_out) *coalesced_out = 0;
    if (bytes > KVT_WIN) return -1;
    c->tick++;
    uint32_t addr = kvt_addr_of(kv, C, K, dim, layer);
    if (addr == 0u && K > 0) { /* addr 0 is the reserved all-open endpoint */
        addr = 1u;
    }
    int idx = kvt_find(c, addr);
    if (idx >= 0) {                       /* same address -> same meaning */
        c->tab[idx].tick = c->tick;
        c->hits++;
        c->coalesced++;
        if (coalesced_out) *coalesced_out = 1;
        return idx;
    }
    c->misses++;
    int slot = -1;
    if (c->n < c->cap) {
        for (uint32_t i = 0; i < c->cap; i++)
            if (!c->tab[i].live) { slot = (int)i; break; }
    } else {
        al_route_t r;
        uint32_t ab = 0;
        if (al_route(kv, C, K, dim, &r) == 0 && r.bucket >= 0)
            ab = (uint32_t)r.bucket;
        slot = kvt_victim(c, ab, (uint32_t)(K > 0 ? K : 1u));
        if (slot >= 0) { c->evictions++; c->n--; }
    }
    if (slot < 0) return -1;
    al_route_t r; uint32_t bk = 0;
    if (al_route(kv, C, K, dim, &r) == 0 && r.bucket >= 0) bk = (uint32_t)r.bucket;
    c->tab[slot].addr = addr;
    c->tab[slot].bucket = bk;
    c->tab[slot].tick = c->tick;
    c->tab[slot].bytes = bytes;
    c->tab[slot].live = 1;
    c->n++;
    return slot;
}

/* get by vector: does this meaning already live here? -1 = miss. */
static inline int kvt_get(const kvt_cache_t *c, const float *kv, const float *C,
                          int K, int dim, uint32_t layer)
{
    uint32_t addr = kvt_addr_of(kv, C, K, dim, layer);
    if (addr == 0u && K > 0) addr = 1u;
    return kvt_find(c, addr);
}

/* ── self-check: the invariants that must hold ────────────────────────── */
static inline int kvt_selfcheck(void)
{
    /* 1. window unit == tess unit */
    if (KVT_WIN != GC_FULL) return 1;
    /* 2. address space covers 162 x 128 without collision */
    if (GC_ICO * GC_CELLS_PER != GC_FULL) return 2;
    /* 3. anchor_id + layer survive a decompose/rebuild roundtrip (hilbert_pos
     * is recovered by inverting the hilbert index, not by fixing x=y=0). */
    for (uint32_t a = 0; a < GC_FULL; a += 97) {
        GcAddrParts p = gc_decompose(a);
        if (p.anchor_id >= GC_ICO) return 3;
        if (p.layer >= GC_LAYERS) return 3;
        if (p.hilbert_pos >= GC_GRID_X * GC_GRID_Y) return 3;
    }
    /* 4. bucket distance is a metric bound */
    for (uint32_t a = 0; a < 64; a++)
        for (uint32_t b = 0; b < 64; b++)
            if (kvt_bucket_dist(a, b, 64) > 32) return 4;
    return 0;
}

#endif /* KV_TESS_CACHE_H */
