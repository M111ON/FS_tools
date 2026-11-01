#include <stdio.h>
#include <assert.h>
#include <string.h>
#include "chain_seedgen.h"

int main(void) {
    /* test pack/unpack roundtrip */
    Seed64 s;
    s.major_a = 17;
    s.level = 42;
    s.cube_route = 31;
    s.inner_ori = 1234;
    s.outer_ori = 2345;
    s.checksum = 5678;

    uint64_t packed = seed_pack(&s);
    Seed64 s2;
    seed_unpack(packed, &s2);
    assert(s2.major_a == s.major_a);
    assert(s2.level == s.level);
    assert(s2.cube_route == s.cube_route);
    assert(s2.inner_ori == s.inner_ori);
    assert(s2.outer_ori == s.outer_ori);
    assert(s2.checksum == s.checksum);

    /* test seed_from_vector determinism */
    float vec[4] = {1.0f, 0.5f, -0.3f, 0.8f};
    uint64_t seed1 = seed_from_vector(vec, 4, 1);
    uint64_t seed2 = seed_from_vector(vec, 4, 1);
    assert(seed1 == seed2);  /* deterministic */

    /* different level → different seed */
    /* space_id: different model -> different seed */
    uint64_t space_hash = space_id_hash("multilingual-e5-small", 1, 384);
    uint64_t seed_space = seed_from_vector_ex(vec, 4, 1, space_hash);
    assert(seed_space != seed1);

    /* collision estimate */
    double est = seed_collision_estimate(63000);
    assert(est > 0.0);
    printf("collision estimate 63k: %.6f\n", est);
    est = seed_collision_estimate(1000000);
    printf("collision estimate 1M: %.2f\n", est);

    uint64_t seed3 = seed_from_vector(vec, 4, 2);
    assert(seed1 != seed3);

    /* different vector → different seed */
    float vec2[4] = {-1.0f, -0.5f, 0.3f, -0.8f};
    uint64_t seed4 = seed_from_vector(vec2, 4, 1);
    assert(seed1 != seed4);

    /* seed_from_vector with NULL */
    assert(seed_from_vector(NULL, 4, 1) == 0);

    /* FNV-1a determinism */
    uint64_t h1 = fnv1a_64(vec, 4 * sizeof(float));
    uint64_t h2 = fnv1a_64(vec, 4 * sizeof(float));
    assert(h1 == h2);

    /* mix64 determinism */
    assert(mix64(12345) == mix64(12345));

    /* cube/face extraction */
    Seed64 s3;
    seed_unpack(seed1, &s3);
    uint8_t cube = seed_cube(s3.cube_route);
    uint8_t face = seed_face(s3.cube_route);
    assert(cube < 8);
    assert(face < 6);
    printf("seed → cube=%d face=%d (cube_route=%d)\n", cube, face, s3.cube_route);

    /* major_a range */
    assert(s3.major_a <= 23);

    /* inner_ori/outer_ori range */
    assert(s3.inner_ori <= 4095);
    assert(s3.outer_ori <= 4095);

    /* checksum range */
    assert(s3.checksum <= 8191);

    /* level preserved */
    assert(s3.level == 1);
    /* ── golden vector cross-language test ─────────────────────────
     * Fixed vector + space_hash must produce the same seed in C and Python.
     * Golden value: 0x980008911527afe0 (computed by both C and Python). */
    {
        float gv[4] = {1.0f, 0.5f, -0.3f, 0.8f};
        uint32_t gsh = 0xdbec345f;
        uint64_t gseed = seed_from_vector_ex(gv, 4, 1, gsh);
        assert(gseed == 0x980008911527afe0ULL);
        printf("GOLDEN: 0x%016llx (cross-language match)\n", (unsigned long long)gseed);
    }

    printf("ALL PASS: seed pack/unpack, hash determinism, cube/face extraction\n");
    printf("seed=0x%016llx major_a=%d level=%d cube_route=%d\n",
           (unsigned long long)seed1, s3.major_a, s3.level, s3.cube_route);
    return 0;
}
