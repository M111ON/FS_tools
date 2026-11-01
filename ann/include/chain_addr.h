/* chain_addr.h — the chain container: address = letter sequence, nothing else.
 *
 * WHY THIS EXISTS
 *   Owner spec (2026-10-29): a container whose structure IS the address. A chain
 *   is a sequence of geometry nodes; each node is a letter-cube face marked with
 *   a letter (A-Z, case significant = 52 values). Data is addressed by the letter
 *   sequence itself, e.g. head A at level 1 -> tail X at level 12 is the key
 *   [A 1, X 12]; you jump straight there instead of walking the chain.
 *
 * RULES (each one is a property of the encoding, not a policy layer)
 *   1. digit 0 is RESERVED = maintenance/fault marker. Valid digits are 1..52
 *      (A-Z = 1..26, a-z = 27..52). This mirrors lettercube_rhombi.h, where face
 *      code 0 is already the invalid/centre value (lcr_valid() rejects it), so
 *      "0 means closed here" costs no extra field.
 *   2. truncating the tail and wrapping a bigger container are the SAME
 *      operation: prefix. A key cut to k levels is a PREFIX of the full key; a
 *      promoted (outer) node EXTENDS the key at the tail with the old key as its
 *      prefix. Neither direction rewrites an existing key -> no remap table, and
 *      old data keeps its address when the container grows (owner: "the old cube
 *      does not disappear, it lives inside").
 *   3. lookup is a hash of the digit string: O(n_levels), independent of how much
 *      is stored. No walk, no comparison against the whole corpus.
 *   4. a range query is the prefix scan: all keys under a k-level prefix are the
 *      block [prefix, prefix+1) in lexicographic order, i.e. every key that starts
 *      with those digits. Repair ("find every level marked 0") is the same scan
 *      with the marker digit fixed to 0.
 *
 * WHY digits-as-bytes instead of an integer
 *   52^12 = 3.9e20 overflows uint64 (max 1.8e19). Keeping the digit vector keeps
 *   L arbitrary, makes prefix a memcmp, and keeps compare/hash trivially exact.
 *
 * Header-only, std-only, no allocation.
 */
#ifndef CHAIN_ADDR_H
#define CHAIN_ADDR_H

#include <stdint.h>
#include <string.h>

#define CHAIN_BASE 52      /* 26 faces x 2 cases */
#define CHAIN_MAXL 32      /* key budget per node path */
#define CHAIN_MARK 0       /* reserved maintenance digit */

typedef struct {
    uint8_t n;                    /* levels used, 1..CHAIN_MAXL */
    uint8_t d[CHAIN_MAXL];        /* d[0] = head (level 1) ... d[n-1] = tail */
} ChainKey;

/* letter value: 'A'..'Z' -> 1..26, 'a'..'z' -> 27..52, anything else -> 0 */
static inline uint8_t chain_digit_of(char c)
{
    if (c >= 'A' && c <= 'Z') return (uint8_t)(c - 'A' + 1);
    if (c >= 'a' && c <= 'z') return (uint8_t)(c - 'a' + 27);
    return CHAIN_MARK;
}

static inline char chain_letter_of(uint8_t v)
{
    if (v >= 1 && v <= 26) return (char)('A' + v - 1);
    if (v >= 27 && v <= 52) return (char)('a' + v - 27);
    return '0';                   /* marker / invalid */
}

/* Build a key from a literal, e.g. chain_key("AXq") -> {3,{1,24,43}}.
 * Returns 0 and leaves k->n = 0 if the string is empty, too long, or holds a
 * character outside A-Za-z (that includes '0': the marker is not addressable). */
static inline int chain_key(ChainKey *k, const char *s)
{
    size_t n = s ? strlen(s) : 0;
    k->n = 0;
    if (n == 0 || n > CHAIN_MAXL) return 0;
    for (size_t i = 0; i < n; i++) {
        uint8_t v = chain_digit_of(s[i]);
        if (v == CHAIN_MARK) { k->n = 0; return 0; }
        k->d[i] = v;
    }
    k->n = (uint8_t)n;
    return 1;
}

/* lexicographic order == numeric order for fixed-length keys, and
 * prefix order for variable length: a key that is a prefix of another sorts
 * first. This is what makes truncation and promotion both "go left". */
static inline int chain_cmp(const ChainKey *a, const ChainKey *b)
{
    uint8_t n = a->n < b->n ? a->n : b->n;
    int c = memcmp(a->d, b->d, n);
    if (c != 0) return c < 0 ? -1 : 1;
    if (a->n == b->n) return 0;
    return a->n < b->n ? -1 : 1;
}

/* A key is addressable only if every digit is a real face (no marker). A stored
 * record with a 0 in it is a CLOSED node: search stops at that level and repair
 * finds it, but nothing downstream of it answers. */
static inline int chain_valid(const ChainKey *k)
{
    if (k->n == 0) return 0;
    for (uint8_t i = 0; i < k->n; i++)
        if (k->d[i] == CHAIN_MARK || k->d[i] > CHAIN_BASE) return 0;
    return 1;
}

/* FNV-1a over the digit string; the caller's table does the rest. */
static inline uint64_t chain_hash(const ChainKey *k)
{
    uint64_t h = 1469598103934665603ull;
    for (uint8_t i = 0; i < k->n; i++) { h ^= k->d[i]; h *= 1099511628211ull; }
    h ^= k->n;
    return h;
}

/* Power of CHAIN_BASE^e, e <= 11 (fits u64: 52^11 = 7.5e18). e > 11 returns 0
 * so callers fall back to prefix scans instead of silently wrapping. */
static inline uint64_t chain_pow(uint8_t e)
{
    static const uint64_t P[12] = {
        1ull, 52ull, 2704ull, 140608ull, 7311616ull, 380204032ull,
        19770609664ull, 1028071702528ull, 53459728531456ull,
        2779905883635712ull, 144555105949057024ull, 7516865509350965248ull
    };
    return e < 12 ? P[e] : 0ull;
}

/* Truncate to k levels (k >= 1). Cutting the tail is the fault/close operation:
 * the node still answers for everything under the surviving prefix. */
static inline int chain_truncate(ChainKey *k, uint8_t levels)
{
    if (levels == 0 || levels > k->n) return 0;
    k->n = levels;
    return 1;
}

/* Extend with one outer level (promotion: a bigger geometry wraps the old one).
 * Returns 0 if the key is full or the digit is not a face. The old key stays the
 * prefix, so every address already handed out still resolves. */
static inline int chain_extend(ChainKey *k, uint8_t digit)
{
    if (k->n >= CHAIN_MAXL || digit == CHAIN_MARK || digit > CHAIN_BASE) return 0;
    k->d[k->n++] = digit;
    return 1;
}

/* The address you can actually reach: q cut at its first marker (or its end).
 * Search "stops at the closed level" = resolve returns the last healthy node;
 * returns the prefix length, or 0 when the head itself is closed. */
static inline uint8_t chain_resolve(const ChainKey *q, ChainKey *out)
{
    uint8_t n = 0;
    while (n < q->n && q->d[n] != CHAIN_MARK && q->d[n] <= CHAIN_BASE) n++;
    if (out) { *out = *q; out->n = n; }
    return n;
}

/* Is a a prefix of b (a.n <= b.n and digits equal)? */
static inline int chain_is_prefix(const ChainKey *a, const ChainKey *b)
{
    if (a->n > b->n) return 0;
    return memcmp(a->d, b->d, a->n) == 0;
}

/* Independent ask, the one the owner actually cares about: two different
 * branches can only land on the same key when the digits themselves collide,
 * i.e. when the head is enough to name the path. Widening the key (more levels)
 * is the only fix, and it costs one digit, never a table. */
static inline int chain_ambiguous(const ChainKey *a, const ChainKey *b)
{
    return chain_cmp(a, b) == 0;
}

#endif /* CHAIN_ADDR_H */
