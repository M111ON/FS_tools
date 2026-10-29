/* chain_bucket.h — the one join between the chain container and a bucket store.
 *
 * WHY THIS EXISTS
 *   The chain resolves a letter key to a level; a level's digit names a face; the
 *   face must name a bucket. This is that last hop, and it is the ONLY thing the
 *   two spaces share (owner rule: chains talk only through input/output).
 *
 * THE CONFLICT THIS HEADER SETTLES
 *   include/anchor_lettercube.h already maps bucket -> face, but it does it by
 *   ENERGY RANK: alc_dir_of(k) = LCR_FACE_CODE[rank(k) % 26]. Rank counts how
 *   many buckets sort before k, so inserting one high-energy bucket increments the
 *   rank of every weaker bucket and every bucket's face changes. That is fine for
 *   naming a bucket, and fatal as an address: the container's whole promise is
 *   that adding data does not move what is already stored.
 *
 *   So the chain uses a pure map instead:
 *       face(bucket) = bucket % 26          (depends on the id, never on the data)
 *       bucket(digit) = face_id(digit)      (lowest id on that face)
 *   Stable under insertion by construction, and invertible in both directions.
 *   test_chain_bucket asserts both the stability AND that the rank-based map
 *   really does shift, so the reason for the split is on record rather than a
 *   comment nobody can check.
 *
 *   Consequence: a bucket's face is now defined here. Anything that needs the
 *   anchor's energy order (routing, "which bucket is first") keeps using
 *   alc_dir_of; anything that needs an ADDRESS uses this. They are two different
 *   questions and must not be merged.
 *
 * Header-only, std-only, no allocation.
 */
#ifndef CHAIN_BUCKET_H
#define CHAIN_BUCKET_H

#include <stdint.h>
#include "chain_node.h"

/* Bucket -> face id (0..25). Pure: no K, no data, no order. */
static inline uint8_t chain_bucket_face(int bucket)
{
    return bucket < 0 ? 0xFF : (uint8_t)(bucket % CHAIN_FACES);
}

/* Digit -> the canonical bucket on its face (lowest id carrying that face).
 * K < 26 wraps, so a small store shares faces instead of failing. */
static inline int chain_bucket_id(uint8_t digit, int K)
{
    uint8_t f;
    if (K <= 0 || digit < 1 || digit > CHAIN_BASE) return -1;
    f = chain_face_id(digit);
    return f < K ? (int)f : (int)(f % (uint8_t)K);
}

/* Every bucket carrying a digit's face, ascending, up to cap. Returns the count
 * written (which may exceed cap — the caller learns the true total). */
static inline int chain_bucket_all(uint8_t digit, int K, int *out, int cap)
{
    int n = 0;
    if (K <= 0 || digit < 1 || digit > CHAIN_BASE) return 0;
    for (int b = 0; b < K; b++) {
        if (chain_bucket_face(b) != chain_face_id(digit)) continue;
        if (out && n < cap) out[n] = b;
        n++;
    }
    return n;
}

#endif /* CHAIN_BUCKET_H */
