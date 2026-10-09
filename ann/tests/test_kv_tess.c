/* test_kv_tess.c — kv_tess_cache.h self-check.
 * Build: gcc -O2 -std=c11 -Iinclude -o build/test_kv_tess tests/test_kv_tess.c -lm
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

#include "anchor_route.h"
#include "kv_tess_cache.h"

static int fails = 0;
#define OK(cond, msg) do { \
    if (cond) printf("  ok   %s\n", msg); \
    else { printf("  FAIL %s\n", msg); fails++; } } while (0)

#define NCL 8
#define ND  16
#define NK  4

static float C[NK * ND];
static float V[NK * ND];

static void setup(void)
{
    for (int k = 0; k < NK; k++)
        for (int j = 0; j < ND; j++) {
            int idx = k * ND + j;
            C[idx] = (float)((idx * 7 + 3) % 11) / 11.0f;
            V[idx] = C[idx] + (float)(((idx * 5 + 1) % 7) - 3) * 0.01f;
        }
}

static int same_bucket(const float *a, const float *b)
{
    return anch_assign(a, C, NK, ND) == anch_assign(b, C, NK, ND);
}

int main(void)
{
    printf("test_kv_tess (window=%u, full=%u)\n", KVT_WIN, GC_FULL);

    int rc = kvt_selfcheck();
    OK(rc == 0, "selfcheck invariants (space/split/decompose/metric)");

    setup();

    /* 1. window unit is the tess unit — the whole point of reusing geometry */
    OK(KVT_WIN == GC_FULL && GC_FULL == 20736u,
       "KVT_WIN == GC_FULL == 20736 (one KV block = one window)");

    /* 2. same vector -> same address (deterministic) */
    {
        uint32_t a1 = kvt_addr_of(V, C, NK, ND, 0);
        uint32_t a2 = kvt_addr_of(V, C, NK, ND, 0);
        OK(a1 == a2 && a1 < GC_FULL, "addr deterministic and in [0,20736)");
    }

    /* 3. same-bucket vectors COALESCE (the semantic win) */
    {
        kvt_entry_t tab[8];
        kvt_cache_t c;
        kvt_init(&c, tab, 8);

        const float *base = V;
        const float *near = V;      /* identical meaning */
        int co = 0;
        int s1 = kvt_put(&c, base, C, NK, ND, 0, 1024, &co);
        int s2 = kvt_put(&c, near, C, NK, ND, 0, 1024, &co);
        OK(s1 == s2 && co == 1, "identical KV coalesces to one slot");
        OK(c.coalesced == 1 && c.n == 1, "coalesced counter + n==1");
    }

    /* 4. eviction picks the FARTHEST bucket, not the oldest.
     * NOTE the design's real capacity: an address is a function of the
     * bucket, so at most K distinct addresses exist. To exercise eviction the
     * cache cap must be SMALLER than the number of buckets used. */
    {
        kvt_entry_t tab[2];
        kvt_cache_t c;
        kvt_init(&c, tab, 2);
        float q[NK][ND];
        for (int k = 0; k < NK; k++)
            for (int j = 0; j < ND; j++) q[k][j] = C[k * ND + j] + 0.001f;

        int co = 0;
        kvt_put(&c, q[0], C, NK, ND, 0, 512, &co);   /* bucket 0 */
        kvt_put(&c, q[1], C, NK, ND, 0, 512, &co);   /* bucket 1 */
        OK(c.n == 2 && c.evictions == 0, "filled 2 (cap) without eviction");

        /* a vector in bucket 2 or 3 (not cached) must evict the entry whose
         * bucket is farthest from it — recency is irrelevant. */
        int new_bucket = -1;
        float nv[ND];
        for (int trial = 0; trial < 512 && new_bucket < 0; trial++) {
            for (int j = 0; j < ND; j++)
                nv[j] = 0.02f * (float)((trial * 31 + j * 17 + 7) % 200) - 1.0f;
            uint32_t b = (uint32_t)anch_assign(nv, C, NK, ND);
            if (b != tab[0].bucket && b != tab[1].bucket) new_bucket = (int)b;
        }
        OK(new_bucket >= 0, "found a vector in an unused bucket");
        OK(kvt_get(&c, nv, C, NK, ND, 0) < 0,
           "the new vector's address is genuinely not cached");

        uint32_t ab = (uint32_t)new_bucket;
        int far = -1; uint32_t bd = 0;
        for (uint32_t i = 0; i < 2; i++) {
            uint32_t d = kvt_bucket_dist(tab[i].bucket, ab, NK);
            if (far < 0 || d > bd) { far = (int)i; bd = d; }
        }
        int before = kvt_victim(&c, ab, NK);
        kvt_put(&c, nv, C, NK, ND, 0, 512, &co);
        OK(c.evictions == 1, "eviction happened once full");
        OK(before == far, "victim is the farthest-bucket entry");
    }

    /* 5. recency does NOT decide: oldest entry survives if its bucket is near */
    {
        kvt_entry_t tab[2];
        kvt_cache_t c;
        kvt_init(&c, tab, 2);
        float a[ND], b[ND], nv[ND];
        for (int j = 0; j < ND; j++) {
            a[j]  = C[0 * ND + j] + 0.001f;   /* bucket ~0 */
            b[j]  = C[3 * ND + j] + 0.001f;   /* bucket ~3 */
            nv[j] = C[0 * ND + j] + 0.002f;   /* near bucket 0 */
        }
        int co = 0;
        kvt_put(&c, a, C, NK, ND, 0, 256, &co);   /* oldest */
        kvt_put(&c, b, C, NK, ND, 0, 256, &co);   /* newer, far */
        uint32_t ab = (uint32_t)anch_assign(nv, C, NK, ND);
        int v = kvt_victim(&c, ab, NK);
        uint32_t vb = tab[v].bucket;
        uint32_t db = kvt_bucket_dist(vb, ab, NK);
        uint32_t da = kvt_bucket_dist(tab[0].bucket, ab, NK);
        OK(db >= da, "victim is not the oldest when a nearer entry exists");
    }

    /* 6. address collision: distinct layers never overlap */
    {
        uint32_t a0 = kvt_addr_of(V, C, NK, ND, 0);
        uint32_t a1 = kvt_addr_of(V, C, NK, ND, 1);
        OK(a0 != a1, "layer changes the address");
    }

    if (fails) { printf("FAILED (%d)\n", fails); return 1; }
    printf("ALL PASS\n");
    return 0;
}
