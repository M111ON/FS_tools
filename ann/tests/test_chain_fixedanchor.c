#include <stdio.h>
#include <assert.h>
#include <math.h>
#include "chain_fixedanchor.h"

int main(void) {
    float vec[4] = {1.0f, 0.5f, -0.3f, 0.8f};

    /* deterministic: same vector + seed → same face */
    int f1 = fa_anchor(vec, 4, 42);
    int f2 = fa_anchor(vec, 4, 42);
    assert(f1 == f2);
    assert(f1 >= 0 && f1 < 26);

    /* different seed → possibly different face (multi-view) */
    int f3 = fa_anchor(vec, 4, 99);
    /* may or may not differ — just check range */
    assert(f3 >= 0 && f3 < 26);

    /* NULL safety */
    assert(fa_anchor(NULL, 4, 42) == 0);

    /* different vectors → different faces (mostly) */
    float v2[4] = {-1.0f, -0.5f, 0.3f, -0.8f};
    int f4 = fa_anchor(v2, 4, 42);
    /* not necessarily different, but check range */
    assert(f4 >= 0 && f4 < 26);

    /* matrix generation deterministic */
    float M1[26 * 4], M2[26 * 4];
    fa_gen_matrix(M1, 26, 4, 42);
    fa_gen_matrix(M2, 26, 4, 42);
    for (int i = 0; i < 26 * 4; i++) assert(M1[i] == M2[i]);

    /* different seed → different matrix */
    float M3[26 * 4];
    fa_gen_matrix(M3, 26, 4, 99);
    int diff = 0;
    for (int i = 0; i < 26 * 4; i++) if (M1[i] != M3[i]) { diff = 1; break; }
    assert(diff);

    /* vector_to_face consistency */
    const float *M = fa_get_matrix(4, 26, 42);
    assert(M != NULL);
    int f5 = fa_vector_to_face(vec, 4, M, 26);
    assert(f5 == f1);

    /* cleanup */
    fa_cleanup();
    assert(fa_cached_matrix == NULL);

    printf("ALL PASS: fixed anchor deterministic, multi-seed, matrix gen\n");
    printf("vec=%f,%f,%f,%f seed=42 → face=%d\n", vec[0], vec[1], vec[2], vec[3], f1);
    printf("vec=%f,%f,%f,%f seed=42 → face=%d\n", v2[0], v2[1], v2[2], v2[3], f4);
    return 0;
}
