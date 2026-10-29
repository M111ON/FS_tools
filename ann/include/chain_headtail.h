/* chain_headtail.h — call an address by its first and last letter, and grow it
 * only when it collides.
 *
 * OWNER SPEC (2026-10-30)
 *   A chain b(1), H(2), y(3), N(4) is written b1N4: head letter with its
 *   position, tail letter with its position. If that key misses or lands on more
 *   than one record, add the second position: b1H2N4. If it still collides, add
 *   the third: b1H2y3N4 — which is the whole address.
 *
 *   So the ladder is always "first m letters, then the tail":
 *       m = 1   ->  {1, n}            b1N4
 *       m = 2   ->  {1, 2, n}         b1H2N4
 *       m = 3   ->  {1, 2, 3, n}      b1H2y3N4   (= the full key)
 *   Every step adds exactly one letter and terminates at the full address, so a
 *   lookup never needs a table, a hash, or a walk: it needs one more digit.
 *
 * WHY THIS IS THE RIGHT SHAPE
 *   Keys in a container share prefixes on purpose (that is what makes them an
 *   address). Head alone is therefore useless as a discriminator, and the tail
 *   alone is where the newest promotions sit. Head + tail is the shortest pair
 *   that is stable for keys that already exist, and the interior letters are
 *   exactly the part that changed, so they are the cheapest thing to add back.
 *
 * COST
 *   The compact key is a subsequence of the stored key, so it is built by copy
 *   and compared by memcmp — no hashing, no allocation. A stored record keeps its
 *   full key; the compact form is a QUERY form only, never stored.
 *
 * Header-only, std-only.
 */
#ifndef CHAIN_HEADTAIL_H
#define CHAIN_HEADTAIL_H

#include <stdint.h>
#include <string.h>
#include "chain_addr.h"

#define CHAIN_HT_MAX CHAIN_MAXL   /* one slot per level at most */

typedef struct {
    uint8_t n;                        /* letters carried, 2..CHAIN_MAXL */
    uint8_t pos[CHAIN_HT_MAX];        /* 1-based position in the full key */
    uint8_t d[CHAIN_HT_MAX];          /* digit at that position */
} ChainHT;

/* Build the compact key at ladder step m (1..full->n-1).
 * m = 1 -> head+tail. m = n-1 -> the full key. Returns 0 on a bad argument or a
 * key that carries a marker (a closed node has no address to look up). */
static inline int chain_ht_build(const ChainKey *full, uint8_t m, ChainHT *out)
{
    if (!full || !out || !chain_valid(full)) return 0;
    if (m == 0 || m >= full->n) return 0;      /* m = n-1 is the full key */
    uint8_t k = 0;
    for (uint8_t i = 0; i < m; i++) {
        out->pos[k] = (uint8_t)(i + 1);
        out->d[k++] = full->d[i];
    }
    out->pos[k] = full->n;                     /* the tail */
    out->d[k++] = full->d[full->n - 1];
    out->n = k;
    return 1;
}

/* Does a stored full key carry the compact key's letters at those positions?
 * A stored key shorter than the compact key's tail position cannot match. */
static inline int chain_ht_match(const ChainHT *ht, const ChainKey *stored)
{
    if (!ht || !stored || ht->n == 0 || !chain_valid(stored)) return 0;
    for (uint8_t i = 0; i < ht->n; i++) {
        uint8_t p = ht->pos[i];
        if (p == 0 || p > stored->n) return 0;
        if (stored->d[p - 1] != ht->d[i]) return 0;
    }
    return 1;
}

/* The smallest ladder step at which a and b stop colliding.
 *
 * Every step carries position 1 and position n, so a difference in the head OR
 * the tail is already separated at step 1. Only an interior difference needs the
 * ladder: a difference at 1-based position p (2..n-1) needs step m = p, because
 * step m carries positions 1..m. Returns 0 when the keys are identical, and 255
 * when they differ only in length (unreachable for keys built from one geometry,
 * where length is the level count and is the same for both). */
static inline uint8_t chain_ht_step_to_separate(const ChainKey *a, const ChainKey *b)
{
    if (!a || !b || !chain_valid(a) || !chain_valid(b)) return 255;
    if (chain_cmp(a, b) == 0) return 0;
    uint8_t n = a->n < b->n ? a->n : b->n;
    if (a->d[0] != b->d[0]) return 1;                     /* head */
    if (a->d[a->n - 1] != b->d[b->n - 1]) return 1;       /* tail */
    for (uint8_t p = 1; p + 1 < n; p++)                   /* interior, 1-based 2..n-1 */
        if (a->d[p] != b->d[p]) return (uint8_t)(p + 1);
    return 255;
}

/* ── notation: "b1N4", "b1H2N4" ──────────────────────────────────────────
 * Format is <letter><1-based position> repeated. Letters are A-Za-z, positions
 * are decimal, so the string is unambiguous: a letter always starts a new pair. */
static inline int chain_ht_str(const ChainHT *ht, char *buf, int cap)
{
    if (!ht || !buf || cap <= 0 || ht->n == 0) return 0;
    int w = 0;
    for (uint8_t i = 0; i < ht->n; i++) {
        char c = chain_letter_of(ht->d[i]);
        if (c == '0') return 0;
        int p = ht->pos[i];
        int need = 1 + (p >= 10 ? 2 : 1);
        if (w + need >= cap) return 0;
        buf[w++] = c;
        if (p >= 10) { buf[w++] = (char)('0' + p / 10); }
        buf[w++] = (char)('0' + p % 10);
    }
    buf[w] = '\0';
    return w;
}

static inline int chain_ht_parse(ChainHT *ht, const char *s)
{
    if (!ht || !s) return 0;
    size_t len = strlen(s);
    uint8_t k = 0;
    size_t i = 0;
    while (i < len) {
        uint8_t d = chain_digit_of(s[i]);
        if (d == CHAIN_MARK) return 0;          /* must start with a letter */
        i++;
        if (i >= len) return 0;                 /* letter with no position */
        int p = 0, digits = 0;
        while (i < len && s[i] >= '0' && s[i] <= '9') {
            p = p * 10 + (s[i] - '0');
            i++;
            if (++digits > 2) return 0;
        }
        if (digits == 0 || p == 0 || p > CHAIN_MAXL) return 0;
        if (k >= CHAIN_HT_MAX) return 0;
        ht->d[k] = d;
        ht->pos[k] = (uint8_t)p;
        k++;
    }
    ht->n = k;
    return k >= 2;
}

#endif /* CHAIN_HEADTAIL_H */
