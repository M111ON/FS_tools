#include <stdio.h>
#include <assert.h>
#include <string.h>
#include "chain_counter.h"
#include "chain_seedgen.h"

int main(void) {
    CapoTable t;
    capo_init(&t);
    assert(capo_count(&t) == 0);

    uint64_t seed = 12345ULL;

    /* desired letter is deterministic */
    uint8_t d1 = counter_desired_letter(seed, 1);
    uint8_t d2 = counter_desired_letter(seed, 1);
    assert(d1 == d2);
    assert(d1 < 52);

    /* different levels → different (usually) letters */
    uint8_t d3 = counter_desired_letter(seed, 2);
    /* may or may not differ, just check range */
    assert(d3 < 52);

    /* actual = desired when no capo */
    uint8_t actual = counter_actual_letter(&t, seed, 1);
    assert(actual == d1);

    /* store capo offset */
    assert(capo_store(&t, seed, 1, 5) == 1);
    assert(capo_count(&t) == 1);
    assert(capo_offset(&t, seed, 1) == 5);

    /* actual = desired + offset */
    actual = counter_actual_letter(&t, seed, 1);
    assert(actual == (d1 + 5) % 52);

    /* don't store zero offset */
    assert(capo_store(&t, seed, 2, 0) == 0);
    assert(capo_count(&t) == 1);

    /* update existing */
    assert(capo_store(&t, seed, 1, 3) == 1);
    assert(capo_offset(&t, seed, 1) == 3);
    assert(capo_count(&t) == 1);

    /* remove */
    assert(capo_remove(&t, seed, 1) == 1);
    assert(capo_offset(&t, seed, 1) == 0);
    assert(capo_count(&t) == 0);

    /* remove non-existent */
    assert(capo_remove(&t, seed, 99) == 0);

    /* walk */
    capo_store(&t, seed, 1, 7);
    uint8_t w = counter_walk(&t, seed, 1);
    assert(w == (d1 + 7) % 52);

    /* chain: compute all letters 1..N */
    uint8_t letters[5];
    int n = counter_chain(&t, seed, 5, letters);
    assert(n == 5);
    for (int i = 0; i < 5; i++) {
        assert(letters[i] < 52);
    }
    /* level 1 should have offset applied */
    assert(letters[0] == (counter_desired_letter(seed, 1) + 7) % 52);

    /* NULL safety */
    assert(capo_offset(NULL, seed, 1) == 0);
    assert(capo_store(NULL, seed, 1, 5) == 0);
    assert(capo_remove(NULL, seed, 1) == 0);
    assert(capo_count(NULL) == 0);
    assert(counter_walk(NULL, seed, 1) == 0);

    /* seed_from_vector integration */
    float vec[4] = {1.0f, 0.5f, -0.3f, 0.8f};
    uint64_t s = seed_from_vector(vec, 4, 1);
    uint8_t dl = counter_desired_letter(s, 1);
    assert(dl < 52);
    printf("seed=0x%016lx level=1 desired=%d\n", (unsigned long)s, dl);

    /* different seeds → different desired letters (usually) */
    uint64_t s2 = seed_from_vector(vec, 4, 2);
    /* not necessarily different, just check range */
    assert(counter_desired_letter(s2, 1) < 52);

    /* capo table full */
    CapoTable full;
    capo_init(&full);
    for (int i = 0; i < CAPO_MAX_ENTRIES; i++) {
        assert(capo_store(&full, (uint64_t)i, 1, 1) == 1);
    }
    assert(capo_store(&full, 99999, 1, 1) == 0);  /* full */

    printf("ALL PASS: counter-based access, capo store/remove/offset, chain walk\n");
    printf("capo entries: %u/%d\n", capo_count(&t), CAPO_MAX_ENTRIES);
    return 0;
}
