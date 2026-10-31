#include <stdio.h>
#include <assert.h>
#include <string.h>
#include "chain_addr.h"
#include "chain_promote.h"

int main(void) {
    /* test 2-means split */
    float vecs[6 * 2] = {
        1.0f, 1.0f,   /* cluster 0 */
        1.1f, 0.9f,   /* cluster 0 */
        0.9f, 1.1f,   /* cluster 0 */
        5.0f, 5.0f,   /* cluster 1 */
        5.1f, 4.9f,   /* cluster 1 */
        4.9f, 5.1f,   /* cluster 1 */
    };
    int assign[6] = {0};
    float c0[2] = {0}, c1[2] = {0};
    promote_split_2means(vecs, 6, 2, assign, c0, c1);

    /* check: first 3 in cluster 0, last 3 in cluster 1 */
    for (int i = 0; i < 3; i++) assert(assign[i] == 0);
    for (int i = 3; i < 6; i++) assert(assign[i] == 1);

    /* check centroids */
    assert(c0[0] > 0.9f && c0[0] < 1.2f);
    assert(c1[0] > 4.9f && c1[0] < 5.2f);

    /* test chain_extend on promotion */
    ChainKey parent, child0, child1;
    chain_key(&parent, "AB");
    assert(chain_valid(&parent));

    assert(promote_extend_address(&parent, &child0, &child1, 1, 2) == 1);
    /* child0 should be parent + digit 1 */
    assert(child0.n == parent.n + 1);
    assert(child1.n == parent.n + 1);
    /* prefix-stable: parent is prefix of both children */
    assert(chain_is_prefix(&parent, &child0));
    assert(chain_is_prefix(&parent, &child1));
    /* children are different */
    assert(chain_cmp(&child0, &child1) != 0);

    /* test promote_needed */
    assert(promote_needed(501, 500) == 1);
    assert(promote_needed(500, 500) == 0);
    assert(promote_needed(100, 500) == 0);

    /* edge: max depth */
    ChainKey deep;
    /* build a deep key (30 levels) */
    memset(&deep, 0, sizeof(deep));
    deep.n = 32;
    for (int i = 0; i < 30; i++) deep.d[i] = 1;
    ChainKey c0d, c1d;
    assert(promote_extend_address(&deep, &c0d, &c1d, 1, 2) == 0);  /* max depth */

    /* NULL safety */
    assert(promote_extend_address(NULL, &child0, &child1, 1, 2) == 0);
    assert(promote_extend_address(&parent, NULL, &child1, 1, 2) == 0);

    printf("ALL PASS: 2-means split, chain_extend, promote_needed, max-depth\n");
    printf("split: c0=(%.2f,%.2f) c1=(%.2f,%.2f)\n", c0[0], c0[1], c1[0], c1[1]);
    printf("parent=%.*s child0=%.*s child1=%.*s\n",
           parent.n, "", child0.n, "", child1.n, "");
    return 0;
}
