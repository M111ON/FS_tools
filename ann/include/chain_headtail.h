/* chain_headtail.h — call an address by its first and last letter, and grow it
 * only when it collides.
 *
 * OWNER SPEC (2026-10-30, revised)
 *   A chain b(1), H(2), y(3), N(4) is called bN: head letter, then tail letter.
 *   If that misses or lands on more than one record, add the second letter (bHN),
 *   then the third (bHyN) — which is the whole address.
 *
 *   No position numbers. The owner's reason is not bytes, it is reading: a person
 *   looking at bHyN sees four letters and knows the shape at a glance, while
 *   b1H2y3N4 forces them to count. The positions were also redundant — the ladder
 *   is always "letters 1..m, then the tail", so the order and the stored key's
 *   length already say where each letter sits. Measured on 20,000 real addresses,
 *   writing the digits made a lookup cost 14.23 chars against 7.03 for plain
 *   letters, for zero extra information (see the experiment report).
 *
 *   The reserved digit 0 is NOT removed by this. It stays the maintenance/fault
 *   marker defined in chain_addr.h: a stored key carrying 0 is a closed node,
 *   lookup stops there, and repair finds it. A closed node has no compact form
 *   because it has no address to look up — that is a rule, not an omission.
 *
 * WHAT A COMPACT KEY HOLDS
 *   m    how many letters came from the head (positions 1..m)
 *   d[]  those m letters, then the tail letter in d[m]
 *   The tail's position is not stored: it is the stored key's own length. So the
 *   compact key is m+1 bytes and nothing else.
 *
 * Header-only, std-only, no allocation.
 */
#ifndef CHAIN_HEADTAIL_H
#define CHAIN_HEADTAIL_H

#include <stdint.h>
#include <string.h>
#include "chain_addr.h"

#define CHAIN_HT_MAX CHAIN_MAXL   /* one slot per level at most */

typedef struct {
    uint8_t n;                        /* letters carried = m + 1, at least 2 */
    uint8_t m;                        /* head letters taken from positions 1..m */
    uint8_t d[CHAIN_HT_MAX];          /* d[0..m-1] = head, d[m] = tail */
} ChainHT;

/* Build the compact key at ladder step m (1..full->n-1).
 * m = 1 -> head + tail. m = n-1 -> the whole address. Returns 0 on a bad
 * argument or a key that carries the 0 marker (a closed node is not addressable). */
static inline int chain_ht_build(const ChainKey *full, uint8_t m, ChainHT *out)
{
    if (!full || !out || !chain_valid(full)) return 0;
    if (m == 0 || m >= full->n) return 0;      /* m = n-1 is the full key */
    for (uint8_t i = 0; i < m; i++) out->d[i] = full->d[i];
    out->d[m] = full->d[full->n - 1];          /* the tail */
    out->m = m;
    out->n = (uint8_t)(m + 1);
    return 1;
}

/* Does a stored key carry the compact key's letters? The head letters are
 * compared at their own positions; the tail is compared against the stored key's
 * LAST letter, which is what makes the compact form independent of length. */
static inline int chain_ht_match(const ChainHT *ht, const ChainKey *stored)
{
    if (!ht || !stored || ht->n < 2 || !chain_valid(stored)) return 0;
    if (stored->n < ht->n) return 0;           /* cannot even carry the head run */
    for (uint8_t i = 0; i < ht->m; i++)
        if (stored->d[i] != ht->d[i]) return 0;
    return stored->d[stored->n - 1] == ht->d[ht->m];
}

/* The smallest ladder step at which a and b stop colliding.
 *
 * Every step carries the head run AND the tail, so a difference in the head or in
 * the tail separates at step 1. Only an interior difference needs the ladder: a
 * difference at 1-based position p (2..n-1) needs step m = p. Returns 0 when the
 * keys are identical, and 255 when they differ only in length. */
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

/* ── notation: "bN", "bHN", "bHyN" — letters only ────────────────────────
 * The last letter is the tail; everything before it is the head run. No digits,
 * so the string is exactly as long as the letters it names. */
static inline int chain_ht_str(const ChainHT *ht, char *buf, int cap)
{
    if (!ht || !buf || cap <= 0 || ht->n < 2) return 0;
    if (ht->n >= cap) return 0;
    for (uint8_t i = 0; i < ht->n; i++) {
        char c = chain_letter_of(ht->d[i]);
        if (c == '0') return 0;                /* marker is not a letter */
        buf[i] = c;
    }
    buf[ht->n] = '\0';
    return ht->n;
}

static inline int chain_ht_parse(ChainHT *ht, const char *s)
{
    if (!ht || !s) return 0;
    size_t len = strlen(s);
    if (len < 2 || len > CHAIN_HT_MAX) return 0;
    for (size_t i = 0; i < len; i++) {
        uint8_t d = chain_digit_of(s[i]);
        if (d == CHAIN_MARK) return 0;         /* '0' and punctuation are not letters */
        ht->d[i] = d;
    }
    ht->n = (uint8_t)len;
    ht->m = (uint8_t)(len - 1);                /* last letter is the tail */
    return 1;
}

#endif /* CHAIN_HEADTAIL_H */
