/* chain_node.h — one level of a chain: the letter, its face, and its exit.
 *
 * WHY THIS EXISTS
 *   chain_addr.h resolves a letter string to a position. It has no notion of
 *   what a level IS. This is that: one node of the container.
 *
 * THE BINDING (owner spec 2026-10-29, and it closes exactly)
 *   26 faces x 2 cases = 52 digits  ->  A-Z = 1..26 (upper), a-z = 27..52 (lower)
 *   digit -> face = (digit-1) % 26, case = (digit-1) / 26
 *   So the letter names the face, the case only distinguishes two addresses that
 *   share one face. That is why case "has meaning" without needing 52 faces.
 *
 * EXIT IS DERIVED, NOT STORED
 *   "enter one face, exit through the opposite" — the opposite of a face code is
 *   LCR_COMP(code), an involution that costs nothing. Storing the exit would let
 *   the two disagree; deriving it cannot.
 *
 * CLOSING A NODE COSTS NO FIELD
 *   A closed level is a 0 in the key (chain_addr.h: CHAIN_MARK). Since digits are
 *   1..52, a closed node's key matches no query at that position, so lookup stops
 *   there on its own. No state byte, no tombstone, no fingerprint: the key is the
 *   fingerprint and the 0 is the tombstone (owner: "it is the same thing").
 *
 * MAJOR / MINOR
 *   Exactly one child of a node is major (the story line); any number are minor.
 *   That is a 1-bit flag, and the invariant is checkable per parent.
 *
 * Header-only, std-only, no allocation.
 */
#ifndef CHAIN_NODE_H
#define CHAIN_NODE_H

#include <stdint.h>
#include <string.h>
#include "chain_addr.h"
#include "../lettercube_rhombi.h"

#define CHAIN_FACES   26
#define CHAIN_MAJOR   0x01
#define CHAIN_NODE_MAX 512

typedef struct {
    uint8_t digit;      /* 0 = closed, 1..52 = A-Z a-z */
    uint8_t flags;      /* CHAIN_MAJOR */
} ChainNode;

/* One stored level: the path prefix ending at this node, and the node itself. */
typedef struct {
    ChainKey  key;      /* full prefix, key.n = this node's level */
    ChainNode node;
} ChainRec;

static inline int chain_node_open(const ChainNode *n) { return n->digit != CHAIN_MARK; }
static inline int chain_node_major(const ChainNode *n) { return (n->flags & CHAIN_MAJOR) != 0; }

static inline uint8_t chain_face_id(uint8_t digit)   { return (uint8_t)((digit - 1) % CHAIN_FACES); }
static inline int     chain_upper(uint8_t digit)     { return digit >= 1 && digit <= 26; }

/* Face code for a digit. Requires lcr_tables_init() (lcr_init does it). */
static inline uint8_t chain_face_code(uint8_t digit)
{
    if (digit < 1 || digit > CHAIN_BASE) return 0;
    return LCR_FACE_CODE[chain_face_id(digit)];
}

/* The face you leave by: the antipodal face, an involution. */
static inline uint8_t chain_exit_code(uint8_t digit)
{
    uint8_t c = chain_face_code(digit);
    return c ? LCR_COMP(c) : 0;
}

/* Node at level `i` (0-based) of a key, if that level exists and is open. */
static inline int chain_node_at(const ChainKey *k, uint8_t i, ChainNode *out)
{
    if (i >= k->n) return 0;
    out->digit = k->d[i];
    out->flags = 0;
    return chain_node_open(out);
}

/* ── store ─────────────────────────────────────────────────────────── */

/* How deep a query resolves against a store: the longest prefix whose every
 * level has a record. A closed level has 0 where the query has 1..52, so it
 * simply fails to match and the walk stops one level earlier — no special case.
 * Linear scan; shortcut: sort recs and binary search when this leaves tests. */
static inline uint8_t chain_store_depth(const ChainRec *recs, int n, const ChainKey *q)
{
    uint8_t d = 0;
    while (d < q->n) {
        int hit = 0;
        for (int i = 0; i < n; i++)
            if (recs[i].key.n == (uint8_t)(d + 1) &&
                memcmp(recs[i].key.d, q->d, (size_t)(d + 1)) == 0) { hit = 1; break; }
        if (!hit) break;
        d++;
    }
    return d;
}

/* How many children of `parent` are marked major. The invariant is == 1 for a
 * node that has any children at all (the story line cannot fork, cannot vanish). */
static inline int chain_major_count(const ChainRec *recs, int n, const ChainKey *parent)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (recs[i].key.n != (uint8_t)(parent->n + 1)) continue;
        if (memcmp(recs[i].key.d, parent->d, parent->n) != 0) continue;
        if (chain_node_major(&recs[i].node)) c++;
    }
    return c;
}

/* Children of `parent` in the store. */
static inline int chain_child_count(const ChainRec *recs, int n, const ChainKey *parent)
{
    int c = 0;
    for (int i = 0; i < n; i++) {
        if (recs[i].key.n != (uint8_t)(parent->n + 1)) continue;
        if (memcmp(recs[i].key.d, parent->d, parent->n) == 0) c++;
    }
    return c;
}

/* Every record's key must be open at its own level, and every level below it
 * must also be present — otherwise a level is reachable in the store but not by
 * walking, which is exactly the inconsistency that silently loses data. */
static inline int chain_store_check(const ChainRec *recs, int n)
{
    for (int i = 0; i < n; i++) {
        const ChainKey *k = &recs[i].key;
        if (k->n == 0 || !chain_valid(k)) return 0;      /* closed or malformed record */
        if (k->d[k->n - 1] != recs[i].node.digit) return 0;
        for (uint8_t d = 1; d < k->n; d++) {             /* parent chain must exist */
            int hit = 0;
            for (int j = 0; j < n; j++)
                if (recs[j].key.n == d && memcmp(recs[j].key.d, k->d, d) == 0) { hit = 1; break; }
            if (!hit) return 0;
        }
    }
    return 1;
}

#endif /* CHAIN_NODE_H */
