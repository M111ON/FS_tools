/* chat_tower.h — tower addressing core (DWGLS style: header-only, int-only)
 *
 * tower = 1 tesseract = 1152 slots (32x36), 18 towers = FULL 20736.
 * Proven in python (facts.jsonl 211497 chunks): ticker bijection 1440/1440,
 * sibling involution 20736/20736 fixed 0, fp_vec8 coll 0.054%.
 * This header MUST match tower.py semantics byte-for-byte.
 */
#ifndef CHAT_TOWER_H
#define CHAT_TOWER_H

#include <stdint.h>
#include <stddef.h>

#define CT_CHUNK  144u
#define CT_ROW    32u
#define CT_COL    36u
#define CT_TSLOT  1152u   /* 32*36 = 8*144 = 1 tesseract */
#define CT_TESS   18u
#define CT_FULL   20736u  /* 18*32*36 */
#define CT_STRIDE 37u
#define CT_CYCLE  1440u

static inline uint16_t ct_enc(uint32_t t) {
    return (uint16_t)((t * CT_STRIDE) % CT_CYCLE);
}

static inline uint8_t ct_pair(uint8_t tess) { return (uint8_t)(tess % 9u); }

static inline uint16_t ct_beacon(uint8_t pair) {
    return (uint16_t)(111u * (pair + 1u));
}

/* B-twinview sibling: pairs (i,i+9), A=32x36 view, B=36x32, transpose link. */
static inline void ct_sibling(uint8_t tess, uint16_t slot,
                              uint8_t *t2, uint16_t *s2) {
    if (tess < 9u) {
        uint16_t r = (uint16_t)(slot / CT_COL);
        uint16_t c = (uint16_t)(slot % CT_COL);
        *t2 = (uint8_t)(tess + 9u);
        *s2 = (uint16_t)(c * CT_ROW + r);
    } else {
        uint16_t r2 = (uint16_t)(slot / CT_ROW);
        uint16_t c2 = (uint16_t)(slot % CT_ROW);
        *t2 = (uint8_t)(tess - 9u);
        *s2 = (uint16_t)(c2 * CT_COL + r2);
    }
}

/* 8-vector zero-deviation: 144B -> 9x16B sums, center v4, 8 line sums.
 * Short tails: missing bytes count 0 (matches python slice semantics). */
static inline void ct_fp_vec8(const uint8_t *data, size_t len, int32_t out[8]) {
    static const uint8_t L[8][3] = {
        {0,1,2},{3,4,5},{6,7,8},{0,3,6},
        {1,4,7},{2,5,8},{0,4,8},{2,4,6}
    };
    int32_t v[9];
    uint8_t i, k;
    for (i = 0; i < 9u; i++) {
        int32_t s = 0;
        for (k = 0; k < 16u; k++) {
            size_t at = (size_t)i * 16u + k;
            if (at < len) s += (int32_t)data[at];
        }
        v[i] = s;
    }
    for (i = 0; i < 8u; i++)
        out[i] = (v[L[i][0]] - v[4]) + (v[L[i][1]] - v[4]) + (v[L[i][2]] - v[4]);
}

#endif /* CHAT_TOWER_H */
