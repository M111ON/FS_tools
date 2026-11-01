/* test_seed_golden.c - pins chain_seedgen.h to the constant that
 * tests/test_seed_golden.py pins scripts/seedgen.py to. If either side drifts
 * (FNV basis, mix64 masking, field layout) one of the two tests fails.
 * Vector: v[i] = (i-192)/256 (exact in float32), dim 384, level 1. */
#include <stdio.h>
#include <stdint.h>
#include "chain_seedgen.h"

#define GOLDEN_DIM    384
#define GOLDEN_SPACE  0xdbec345fu                 /* multilingual-e5-small, v1, 384 */
#define GOLDEN_SEED   1152934729633737887ULL

int main(void) {
    float v[GOLDEN_DIM];
    for (int i = 0; i < GOLDEN_DIM; i++) v[i] = (float)(i - 192) / 256.0f;
    uint64_t s = seed_from_vector_ex(v, GOLDEN_DIM, 1, GOLDEN_SPACE);
    if (s != GOLDEN_SEED) {
        printf("FAIL: seed=%llu expected=%llu\n",
               (unsigned long long)s, (unsigned long long)GOLDEN_SEED);
        return 1;
    }
    printf("PASS: seed golden vector (%llu)\n", (unsigned long long)s);
    return 0;
}
