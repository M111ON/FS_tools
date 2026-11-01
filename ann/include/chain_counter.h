/* chain_counter.h — counter-based random access to chain levels
 *
 * Owner concept (2026-11-01): seed × level = O(1) access to the letter
 * at that level. No need to walk from level 0.
 *
 * letter(L) = Mix64(seed ⊕ L·φ) mod ALPHABET_SIZE
 *
 * This is a counter-based RNG (like ChaCha counter mode):
 *   - seed = the chain's identity (from vector or from parent)
 *   - L = level (0, 1, 2, ...)
 *   - φ = golden ratio (0x9E3779B97F4A7C15) for good mixing
 *   - Mix64 = the finalizer from chain_seedgen.h
 *
 * Capo/offset:
 *   - If the desired letter at level L is occupied (capo), store offset
 *   - offset = (actual - desired) mod ALPHABET_SIZE
 *   - offset 0 = not displaced (no storage needed)
 *   - Storage: only non-zero offsets are stored
 */
#ifndef CHAIN_COUNTER_H
#define CHAIN_COUNTER_H

#include <stdint.h>
#include "chain_seedgen.h"   /* mix64 */

#define COUNTER_PHI  0x9E3779B97F4A7C15ULL
#define COUNTER_ALPHABET 52u

/* ── desired letter at level L ──────────────────────────────────── */
static inline uint8_t counter_desired_letter(uint64_t seed, uint16_t level) {
    uint64_t mixed = mix64(seed ^ ((uint64_t)level * COUNTER_PHI));
    return (uint8_t)(mixed % COUNTER_ALPHABET);
}

/* ── capo offset storage ──────────────────────────────────────────
 * offset = 0 → not displaced (desired = actual)
 * offset > 0 → displaced by offset positions
 * Stored per (seed, level) only when offset != 0 */
typedef struct {
    uint64_t seed;
    uint16_t level;
    uint8_t  offset;   /* 0 = not stored */
} CapoEntry;

#define CAPO_MAX_ENTRIES 1024

typedef struct {
    CapoEntry entries[CAPO_MAX_ENTRIES];
    uint32_t count;
} CapoTable;

/* ── init ───────────────────────────────────────────────────────── */
static inline void capo_init(CapoTable *t) {
    if (!t) return;
    memset(t, 0, sizeof(*t));
}

/* ── lookup offset for (seed, level) ────────────────────────────── */
static inline uint8_t capo_offset(const CapoTable *t, uint64_t seed, uint16_t level) {
    if (!t) return 0;
    for (uint32_t i = 0; i < t->count; i++) {
        if (t->entries[i].seed == seed && t->entries[i].level == level) {
            return t->entries[i].offset;
        }
    }
    return 0;  /* not found → offset 0 */
}

/* ── actual letter = desired + offset ───────────────────────────── */
static inline uint8_t counter_actual_letter(const CapoTable *t, uint64_t seed, uint16_t level) {
    uint8_t desired = counter_desired_letter(seed, level);
    uint8_t off = capo_offset(t, seed, level);
    return (uint8_t)((desired + off) % COUNTER_ALPHABET);
}

/* ── store offset (only if non-zero) ────────────────────────────── */
static inline int capo_store(CapoTable *t, uint64_t seed, uint16_t level, uint8_t offset) {
    if (!t || offset == 0) return 0;  /* don't store zero offsets */
    /* check if already exists */
    for (uint32_t i = 0; i < t->count; i++) {
        if (t->entries[i].seed == seed && t->entries[i].level == level) {
            t->entries[i].offset = offset;
            return 1;
        }
    }
    if (t->count >= CAPO_MAX_ENTRIES) return 0;  /* full */
    t->entries[t->count].seed = seed;
    t->entries[t->count].level = level;
    t->entries[t->count].offset = offset;
    t->count++;
    return 1;
}

/* ── remove offset (set to 0, effectively delete) ───────────────── */
static inline int capo_remove(CapoTable *t, uint64_t seed, uint16_t level) {
    if (!t) return 0;
    for (uint32_t i = 0; i < t->count; i++) {
        if (t->entries[i].seed == seed && t->entries[i].level == level) {
            /* swap with last and decrement */
            t->entries[i] = t->entries[t->count - 1];
            t->count--;
            return 1;
        }
    }
    return 0;
}

/* ── chain walk using counter access ──────────────────────────────
 * Walk from level 0 to target level, computing actual letter at each.
 * Returns the letter at target level. */
static inline uint8_t counter_walk(const CapoTable *t, uint64_t seed, uint16_t target_level) {
    if (!t || target_level == 0) return 0;  /* level 0 = root, NULL = 0 */
    return counter_actual_letter(t, seed, target_level);
}

/* ── full chain: compute all letters from level 1 to N ──────────── */
static inline int counter_chain(const CapoTable *t, uint64_t seed, uint16_t n_levels,
                                uint8_t *out_letters) {
    if (!out_letters || n_levels == 0) return 0;
    for (uint16_t L = 1; L <= n_levels; L++) {
        out_letters[L - 1] = counter_actual_letter(t, seed, L);
    }
    return n_levels;
}

/* ── stats ──────────────────────────────────────────────────────── */
static inline uint32_t capo_count(const CapoTable *t) {
    return t ? t->count : 0;
}

#endif /* CHAIN_COUNTER_H */
