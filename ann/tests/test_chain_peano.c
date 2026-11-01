#include <stdio.h>
#include <assert.h>
#include "chain_peano.h"
#include "chain_seedgen.h"

int main(void) {
    /* verify cycle */
    assert(peano_verify_cycle() == 1);
    assert(peano_cycle_length() == 8);

    /* cycle visits all 8 cubes */
    for (int i = 0; i < 8; i++) {
        printf("pos %d: cube %d\n", i, peano_cycle[i]);
    }

    /* next/prev */
    assert(peano_next(0) == 1);   /* C1 → C2 */
    assert(peano_next(1) == 3);   /* C2 → C4 */
    assert(peano_next(3) == 2);   /* C4 → C3 */
    assert(peano_next(4) == 0);   /* C5 → C1 (wrap) */

    assert(peano_prev(0) == 4);   /* C1 ← C5 (wrap) */
    assert(peano_prev(1) == 0);   /* C2 ← C1 */

    /* full cycle returns to start */
    uint8_t c = 0;
    for (int i = 0; i < 8; i++) c = peano_next(c);
    assert(c == 0);  /* back to C1 */

    /* vertical handoff */
    assert(peano_is_vertical_handoff(0, 4) == 1);  /* C1 → C5 */
    assert(peano_is_vertical_handoff(4, 0) == 1);  /* C5 → C1 */
    assert(peano_is_vertical_handoff(0, 1) == 0);  /* C1 → C2 (not vertical) */

    /* cube/face extraction */
    assert(peano_cube_of(0) == 0);   /* cube 0, face 0 */
    assert(peano_face_of(0) == 0);
    assert(peano_cube_of(6) == 1);   /* cube 1, face 0 */
    assert(peano_face_of(6) == 0);
    assert(peano_cube_of(47) == 7);  /* cube 7, face 5 */
    assert(peano_face_of(47) == 5);

    /* shortcuts */
    assert(peano_is_shortcut(1) == 1);
    assert(peano_is_shortcut(5) == 1);
    assert(peano_is_shortcut(9) == 1);
    assert(peano_is_shortcut(11) == 1);
    assert(peano_is_shortcut(0) == 0);
    assert(peano_is_shortcut(2) == 0);

    /* twin pair */
    assert(peano_twin_pair(0) == 0);
    assert(peano_twin_pair(11) == 11);
    assert(peano_twin_pair(12) == 0xFF);  /* not inner */

    /* seed integration */
    float vec[4] = {1.0f, 0.5f, -0.3f, 0.8f};
    uint64_t seed = seed_from_vector(vec, 4, 1);
    Seed64 s;
    seed_unpack(seed, &s);
    uint8_t cube = peano_cube_of(s.cube_route);
    uint8_t face = peano_face_of(s.cube_route);
    assert(cube < 8);
    assert(face < 6);
    printf("seed → cube=%d face=%d (cube_route=%d)\n", cube, face, s.cube_route);

    /* position inverse */
    for (int i = 0; i < 8; i++) {
        uint8_t c = peano_cycle[i];
        assert(peano_position(c) == i);
    }

    printf("ALL PASS: Peano cycle, next/prev, vertical handoff, cube/face, shortcuts, seed integration\n");
    return 0;
}
