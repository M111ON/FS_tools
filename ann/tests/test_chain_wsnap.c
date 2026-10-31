#include <stdio.h>
#include <assert.h>
#include "chain_addr.h"
#include "chain_wsnap.h"

int main(void) {
    WsnapTable t;
    wsnap_init(&t);
    assert(wsnap_count(&t) == 0);
    assert(wsnap_fill(&t) == 0.0f);

    /* insert and lookup */
    assert(wsnap_insert(&t, 1, 2, 42) == 1);
    assert(wsnap_lookup(&t, 1, 2) == 42);
    assert(wsnap_count(&t) == 1);

    /* miss */
    assert(wsnap_lookup(&t, 1, 3) == WSNAP_EMPTY);
    assert(wsnap_lookup(&t, 2, 2) == WSNAP_EMPTY);

    /* update (same slot) */
    assert(wsnap_insert(&t, 1, 2, 99) == 1);
    assert(wsnap_lookup(&t, 1, 2) == 99);
    assert(wsnap_count(&t) == 1);  /* count unchanged */

    /* delete */
    assert(wsnap_delete(&t, 1, 2) == 1);
    assert(wsnap_lookup(&t, 1, 2) == WSNAP_EMPTY);
    assert(wsnap_count(&t) == 0);

    /* delete empty */
    assert(wsnap_delete(&t, 1, 2) == 0);

    /* invalid args */
    assert(wsnap_insert(&t, 52, 1, 1) == 0);   /* letter out of range */
    assert(wsnap_insert(&t, 0, 0, 1) == 0);     /* depth 0 invalid */
    assert(wsnap_insert(&t, 0, 9, 1) == 0);    /* depth > max */
    assert(wsnap_lookup(&t, 52, 1) == WSNAP_EMPTY);
    assert(wsnap_lookup(&t, 0, 0) == WSNAP_EMPTY);

    /* from ChainKey */
    ChainKey k;
    chain_key(&k, "AXq");
    /* k.n = 3, k.d[0] = 'A' = 1 */
    assert(wsnap_insert(&t, k.d[0], k.n, 77) == 1);
    assert(wsnap_from_key(&t, &k) == 77);

    /* NULL safety */
    assert(wsnap_lookup(NULL, 0, 1) == WSNAP_EMPTY);
    assert(wsnap_insert(NULL, 0, 1, 1) == 0);
    assert(wsnap_delete(NULL, 0, 1) == 0);
    assert(wsnap_count(NULL) == 0);

    /* fill multiple */
    wsnap_init(&t);
    for (int i = 0; i < 52; i++) {
        for (int d = 1; d <= 4; d++) {
            assert(wsnap_insert(&t, i, d, i * 100 + d) == 1);
        }
    }
    assert(wsnap_count(&t) == 52 * 4);
    assert(wsnap_lookup(&t, 25, 3) == 25 * 100 + 3);

    printf("ALL PASS: w-snap O(1) lookup, insert, delete, from-key\n");
    printf("table: %u/%u slots (%.1f%% fill)\n",
           wsnap_count(&t), 52 * 9, wsnap_fill(&t) * 100.0f);
    return 0;
}
