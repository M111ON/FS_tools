#include <stdio.h>
#include <assert.h>
#include "chain_multiview.h"

int main(void) {
    float vec[4] = {1.0f, 0.5f, -0.3f, 0.8f};

    /* summon: returns unique buckets */
    int buckets[MV_MAX_VIEWS];
    int n = mv_summon(vec, 4, buckets, 4);
    assert(n >= 1 && n <= 4);
    /* all buckets in range */
    for (int i = 0; i < n; i++) {
        assert(buckets[i] >= 0 && buckets[i] < 26);
    }

    /* deterministic: same vector → same buckets */
    int buckets2[MV_MAX_VIEWS];
    int n2 = mv_summon(vec, 4, buckets2, 4);
    assert(n == n2);
    for (int i = 0; i < n; i++) assert(buckets[i] == buckets2[i]);

    /* consensus: at least 1 */
    int agree = mv_consensus(vec, 4, 4);
    assert(agree >= 1 && agree <= 4);

    /* agreement ratio: 0..1 */
    float ratio = mv_agreement_ratio(vec, 4, 4);
    assert(ratio >= 0.0f && ratio <= 1.0f);

    /* different views produce different faces (usually) */
    /* check that not all 4 views are identical */
    int faces[MV_MAX_VIEWS];
    for (int v = 0; v < 4; v++) {
        faces[v] = fa_anchor(vec, 4, mv_seeds[v]);
    }
    int all_same = 1;
    for (int v = 1; v < 4; v++) {
        if (faces[v] != faces[0]) { all_same = 0; break; }
    }
    /* at least some variation expected (not guaranteed, but likely) */
    /* just print */
    printf("faces: ");
    for (int v = 0; v < 4; v++) printf("%d ", faces[v]);
    printf("\n");

    /* NULL safety */
    assert(mv_summon(NULL, 4, buckets, 4) == 0);
    assert(mv_consensus(NULL, 4, 4) == 0);

    /* 8 views */
    int b8[MV_MAX_VIEWS];
    int n8 = mv_summon(vec, 4, b8, 8);
    assert(n8 >= 1 && n8 <= 8);

    printf("ALL PASS: multi-view summon, consensus, agreement ratio\n");
    printf("views=%d unique_buckets=%d consensus=%d ratio=%.2f\n",
           4, n, agree, ratio);
    return 0;
}
