/* chain_exit.h — exit-letter constraint for the 6-letter skeleton.
 *
 * Owner spec (2026-11-01): skeleton = [A, x1, x2, x3, x4, F]
 *   A = entry (52 choices: A-Z, a-z)
 *   F = exit (50 choices: NOT A, NOT complement(A))
 *   complement(L) = (L + 26) % 52  (upper↔lower of same letter)
 *
 * So F has 50 choices = 52 - 2 (exclude self + complement).
 * This is a constraint on the LAST position of the skeleton only.
 * Other positions (x1..x4) have full 52 choices.
 *
 * Math: skeleton patterns = 52 × 52^4 × 50 = 52^5 × 50 = 380,204,032,000
 *       = 52 × 50 × 26^(N-1) × 100  (with promotion)
 */
#ifndef CHAIN_EXIT_H
#define CHAIN_EXIT_H

#include "chain_addr.h"   /* CHAIN_BASE */

/* complement of a letter: upper↔lower of same letter */
static inline uint8_t chain_complement(uint8_t letter) {
    if (letter >= CHAIN_BASE) return letter;  /* invalid, pass through */
    return (uint8_t)((letter + 26) % CHAIN_BASE);
}

/* is exit valid given entry? (exit != entry, exit != complement(entry)) */
static inline int chain_exit_valid(uint8_t entry, uint8_t exit) {
    if (entry >= CHAIN_BASE || exit >= CHAIN_BASE) return 0;
    if (exit == entry) return 0;
    if (exit == chain_complement(entry)) return 0;
    return 1;
}

/* number of valid exit choices (always 50 for CHAIN_BASE=52) */
static inline uint8_t chain_exit_choices(void) {
    return CHAIN_BASE - 2;  /* 50 */
}

/* count valid exits for a given entry (should be 50) */
static inline uint8_t chain_exit_count(uint8_t entry) {
    if (entry >= CHAIN_BASE) return 0;
    uint8_t cnt = 0;
    uint8_t comp = chain_complement(entry);
    for (uint8_t e = 0; e < CHAIN_BASE; e++) {
        if (e != entry && e != comp) cnt++;
    }
    return cnt;
}

/* validate a 6-letter skeleton: positions 0-4 any, position 5 (exit) constrained */
static inline int chain_skeleton_valid(const ChainKey *k) {
    if (!k || !chain_valid(k)) return 0;
    if (k->n < 6) return 0;  /* need at least 6 letters */
    /* positions 0-4: any valid letter */
    for (int i = 0; i < 5; i++) {
        if (k->d[i] == CHAIN_MARK || k->d[i] > CHAIN_BASE) return 0;
    }
    /* position 5: exit, constrained by position 0 (entry) */
    uint8_t entry = k->d[0];
    uint8_t exitv = k->d[5];
    return chain_exit_valid(entry, exitv);
}

#endif /* CHAIN_EXIT_H */
