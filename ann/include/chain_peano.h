/* chain_peano.h — Peano routing: Hamiltonian cycle through 8 cubes
 *
 * CORRECTIONS (2026-11-01, from review):
 *   1. This is Gray code (3-bit cube graph), NOT Peano/Hilbert.
 *      8 cubes with 12 edges = 3-bit cube graph. Hamiltonian cycle
 *      visits all 8 (Gray code: change 1 bit per step).
 *      Becomes Hilbert ONLY if recursively subdivided: each cube
 *      contains 8 sub-cubes, each sub-cube contains 8 sub-sub-cubes, etc.
 *      Orientation inheritance (twin pairs lock orientation) ensures
 *      the recursive subdivision is consistent — this is the
 *      "matryoshka" property the owner described.
 *      Current implementation = 1 level (8 cubes). Hilbert requires
 *      recursive levels (not yet implemented).
 *
 *   2. Immutability comes from LOCKED ASSIGNMENTS, not state space size.
 *      The 10^31 number is "how many random slots fit", not a guarantee.
 *      Chain is immutable because assignments are stored and never changed.
 *
 *   3. Shortcuts (p2, p6, p10, p12) are reserved for capo/offset.
 *      They are NOT part of the main cycle.
 *
 * HANDOFF: Vector Vault Gen 9 — Major/Minor & Peano Routing (2026-11-01)
 *
 * 8 cubes (C1-C8) from 3-octa compound. Gray code Hamiltonian cycle:
 *
 *   C1 → C2 → C4 → C3 → C7 → C8 → C6 → C5 → C1
 *   (0-based: 0→1→3→2→6→7→5→4→0)
 *
 * Vertical handoff: C1 → C5 uses p9 (inner) + P9 (outer)
 * Shortcuts: p2, p6, p10, p12 (reserved for capo/offset)
 *
 * Inner (12 twin pairs): must pair (twin) — orientation locked
 * Outer (12 free faces): independent
 */
#ifndef CHAIN_PEANO_H
#define CHAIN_PEANO_H

#include <stdint.h>

#define PEANO_N_CUBES  8
#define PEANO_N_FACES  6   /* faces per cube */

/* ── cube indices (0-based: C1=0, C2=1, ..., C8=7) ─────────────── */
/* Gray code Hamiltonian cycle (1-based): 1→2→4→3→7→8→6→5→1 */
/* 0-based:                            0→1→3→2→6→7→5→4→0 */
static const uint8_t peano_cycle[PEANO_N_CUBES] = {0, 1, 3, 2, 6, 7, 5, 4};

static inline uint8_t peano_position(uint8_t cube) {
    for (int i = 0; i < PEANO_N_CUBES; i++) {
        if (peano_cycle[i] == (cube & 7)) return (uint8_t)i;
    }
    return 0;
}

static inline uint8_t peano_next(uint8_t cube) {
    uint8_t pos = peano_position(cube);
    uint8_t next_pos = (pos + 1) % PEANO_N_CUBES;
    return peano_cycle[next_pos];
}

static inline uint8_t peano_prev(uint8_t cube) {
    uint8_t pos = peano_position(cube);
    uint8_t prev_pos = (pos == 0) ? (PEANO_N_CUBES - 1) : (pos - 1);
    return peano_cycle[prev_pos];
}

static inline int peano_is_vertical_handoff(uint8_t from, uint8_t to) {
    return ((from == 0 && to == 4) || (from == 4 && to == 0)) ? 1 : 0;
}

static inline uint8_t peano_cube_of(uint8_t cube_route) {
    return cube_route / PEANO_N_FACES;
}
static inline uint8_t peano_face_of(uint8_t cube_route) {
    return cube_route % PEANO_N_FACES;
}

static inline int peano_is_shortcut(uint8_t face_idx) {
    return (face_idx == 1 || face_idx == 5 || face_idx == 9 || face_idx == 11) ? 1 : 0;
}

static inline uint8_t peano_twin_pair(uint8_t face_idx) {
    if (face_idx >= 12) return 0xFF;
    return face_idx;
}

static inline int peano_verify_cycle(void) {
    uint8_t seen[PEANO_N_CUBES] = {0};
    for (int i = 0; i < PEANO_N_CUBES; i++) {
        uint8_t c = peano_cycle[i];
        if (c >= PEANO_N_CUBES) return 0;
        if (seen[c]) return 0;
        seen[c] = 1;
    }
    for (int i = 0; i < PEANO_N_CUBES; i++) {
        if (!seen[i]) return 0;
    }
    return 1;
}

static inline int peano_cycle_length(void) {
    return PEANO_N_CUBES;
}

/* ── Hilbert check: is this recursively subdivided? ───────────────
 * Current implementation = 1 level (8 cubes). For true Hilbert,
 * each cube must contain 8 sub-cubes with inherited orientation.
 * Returns 0 (not Hilbert) until recursive levels are implemented. */
static inline int peano_is_hilbert(void) {
    return 0;  /* 1 level only — not yet recursive */
}

#endif /* CHAIN_PEANO_H */
