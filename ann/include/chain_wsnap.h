/* chain_wsnap.h — w-axis SNAP: O(1) retrieval by (Letter, Depth).
 *
 * Owner spec (2026-11-01): the address structure has NO antipodal jump.
 * Instead, retrieval is a direct snap: Seed = (Letter, Depth) → Anchor.
 * If the query's (Letter, Depth) matches a stored anchor, snap O(1).
 * No probe, no BFS, no scan.
 *
 * This replaces the antipodal-jump concept (which was tested and found
 * ineffective: k-means centroids aren't arranged antipodally).
 *
 * Design:
 *   - Table: w_snap[52][MAX_DEPTH] → anchor id (or -1 if empty)
 *   - Lookup: w_snap[letter][depth]  (O(1), array index)
 *   - Insert: w_snap[letter][depth] = anchor_id
 *   - Delete: w_snap[letter][depth] = -1
 *
 * Math: 52 letters × (MAX_DEPTH+1) depths = 52 × (N+1) slots.
 * For N=4: 52 × 5 = 260 slots. Tiny.
 *
 * Relationship to chain system:
 *   - Letter = chain head letter (0..51, from ChainKey.d[0])
 *   - Depth = chain length (1..MAX_DEPTH, from ChainKey.n)
 *   - Anchor = the bucket/centroid the chain points to
 *
 * This is NOT a hash — it's a direct array index (O(1), no collision).
 * The "snap" is: if the slot is occupied, return the anchor; else miss.
 */
#ifndef CHAIN_WSNAP_H
#define CHAIN_WSNAP_H

#include <stdint.h>
#include "chain_addr.h"   /* CHAIN_BASE, CHAIN_MAXL */

#define WSNAP_MAX_DEPTH  8   /* max chain depth for snap table */
#define WSNAP_EMPTY     (-1) /* slot not occupied */

/* ── snap table ─────────────────────────────────────────────────── */
typedef struct {
    int snap[CHAIN_BASE][WSNAP_MAX_DEPTH + 1];  /* [letter][depth] → anchor or -1 */
    uint32_t count;                              /* occupied slots */
} WsnapTable;

/* ── init ──────────────────────────────────────────────────────── */
static inline void wsnap_init(WsnapTable *t) {
    if (!t) return;
    for (int i = 0; i < CHAIN_BASE; i++)
        for (int d = 0; d <= WSNAP_MAX_DEPTH; d++)
            t->snap[i][d] = WSNAP_EMPTY;
    t->count = 0;
}

/* ── snap: O(1) lookup ─────────────────────────────────────────── */
static inline int wsnap_lookup(const WsnapTable *t, uint8_t letter, uint8_t depth) {
    if (!t || letter >= CHAIN_BASE || depth == 0 || depth > WSNAP_MAX_DEPTH)
        return WSNAP_EMPTY;
    return t->snap[letter][depth];
}

/* ── insert: O(1) ──────────────────────────────────────────────── */
static inline int wsnap_insert(WsnapTable *t, uint8_t letter, uint8_t depth, int anchor) {
    if (!t || letter >= CHAIN_BASE || depth == 0 || depth > WSNAP_MAX_DEPTH)
        return 0;  /* invalid */
    if (t->snap[letter][depth] != WSNAP_EMPTY) {
        /* already occupied — update (not a new slot) */
        t->snap[letter][depth] = anchor;
        return 1;
    }
    t->snap[letter][depth] = anchor;
    t->count++;
    return 1;
}

/* ── delete: O(1) ──────────────────────────────────────────────── */
static inline int wsnap_delete(WsnapTable *t, uint8_t letter, uint8_t depth) {
    if (!t || letter >= CHAIN_BASE || depth == 0 || depth > WSNAP_MAX_DEPTH)
        return 0;
    if (t->snap[letter][depth] == WSNAP_EMPTY) return 0;  /* already empty */
    t->snap[letter][depth] = WSNAP_EMPTY;
    t->count--;
    return 1;
}

/* ── snap from ChainKey ─────────────────────────────────────────── */
static inline int wsnap_from_key(const WsnapTable *t, const ChainKey *k) {
    if (!t || !k || !chain_valid(k) || k->n == 0) return WSNAP_EMPTY;
    uint8_t letter = k->d[0];  /* head letter */
    uint8_t depth = k->n;      /* chain length */
    if (depth > WSNAP_MAX_DEPTH) return WSNAP_EMPTY;
    return wsnap_lookup(t, letter, depth);
}

/* ── stats ──────────────────────────────────────────────────────── */
static inline uint32_t wsnap_count(const WsnapTable *t) {
    return t ? t->count : 0;
}
static inline float wsnap_fill(const WsnapTable *t) {
    if (!t) return 0.0f;
    uint32_t total = CHAIN_BASE * (WSNAP_MAX_DEPTH + 1);
    return (float)t->count / (float)total;
}

#endif /* CHAIN_WSNAP_H */
