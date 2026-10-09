/* tests/test_anchor_letter.c — determinism + invariants for the anchor->letter->orbit bridge.
 *
 * Oracles (independent of the impl, from the spec):
 *   FACE  : the 6 face letters are 6 DISTINCT buckets when K>=6, and the order
 *           is a total order (energy desc, id asc) — recomputed here by brute
 *           force with a different sort.
 *   ORBIT : al_orbit_of is a bijection on the 720 permutations of {0..5};
 *           every permutation maps to a distinct rank (720 in -> 720 out).
 *   DROP  : (cell,choice) always lands in the 144x72 space, cell<144,
 *           choice<72, and the reserved ids 0 / 10367 are never produced by a
 *           non-degenerate bucket (they are major in/out).
 *   STABLE: two runs, same input -> identical every field.
 *
 * Deterministic: closed-form dataset, no RNG.
 * BUILD: gcc -O2 -Wall -Iinclude -o build/test_anchor_letter tests/test_anchor_letter.c -lm
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <stdlib.h>

#include "anchor_letter.h"

#define N   240
#define DI  16
#define KC  8
#define NQ  32

static void det(float *X, int n, int dim, unsigned sk) {
    for (int i = 0; i < n; i++)
        for (int j = 0; j < dim; j++) {
            unsigned v = (unsigned)(i * 131 + j * 17 + (i * j) % 13 + sk);
            X[(size_t)i * dim + j] = (float)(v % 2000) / 1000.0f - 1.0f;
        }
}

/* independent brute-force face order: sort all K by energy desc / id asc */
static void brute_faces(int K, const float *C, int dim, int out[AL_FACES]) {
    double en[ANCHR_MAXK];
    int idx[ANCHR_MAXK];
    for (int k = 0; k < K; k++) {
        double e = 0;
        for (int j = 0; j < dim; j++) e += (double)C[(size_t)k * dim + j] * C[(size_t)k * dim + j];
        en[k] = e; idx[k] = k;
    }
    for (int a = 0; a < K; a++)
        for (int b = a + 1; b < K; b++)
            if (en[idx[b]] > en[idx[a]] || (en[idx[b]] == en[idx[a]] && idx[b] < idx[a])) {
                int t = idx[a]; idx[a] = idx[b]; idx[b] = t;
            }
    for (int f = 0; f < AL_FACES; f++) out[f] = idx[f % K];
}

static int failures = 0;
static void ok(const char *name, int pass) {
    printf("  %s %s\n", pass ? "ok  " : "FAIL", name);
    if (!pass) failures++;
}

/* enumerate all 720 permutations of {0,1,2,3,4,5}, collect ranks */
static void permute(int *a, int l, int r, unsigned char *seen, int *distinct) {
    if (l == r) {
        uint32_t rk = al_orbit_of(a);
        if (!seen[rk]) { seen[rk] = 1; (*distinct)++; }
        else failures++; /* collision -> not a bijection */
        return;
    }
    for (int i = l; i <= r; i++) {
        int t = a[l]; a[l] = a[i]; a[i] = t;
        permute(a, l + 1, r, seen, distinct);
        t = a[l]; a[l] = a[i]; a[i] = t;
    }
}

int main(void) {
    printf("test_anchor_letter\n");

    static float X[N * DI], Q[NQ * DI], C[KC * DI];
    static int lab[N];
    det(X, N, DI, 0u);
    det(Q, NQ, DI, 7919u);
    if (anch_train(X, N, DI, KC, C, lab) != 0) { printf("train failed\n"); return 1; }

    /* ── 1. face letters: 6 distinct buckets, order matches brute force ── */
    {
        int got[AL_FACES], ref[AL_FACES];
        al_letter_of(KC, C, DI, got);
        brute_faces(KC, C, DI, ref);
        int order_ok = memcmp(got, ref, sizeof(got)) == 0;
        int distinct = 1;
        for (int i = 0; i < AL_FACES; i++)
            for (int j = i + 1; j < AL_FACES; j++)
                if (got[i] == got[j]) distinct = 0;
        ok("face order == brute-force (energy desc, id asc)", order_ok);
        ok("K>=6 -> 6 distinct face buckets", distinct);
    }

    /* ── 2. orbit is a bijection over all 720 permutations ── */
    {
        unsigned char seen[AL_ORBITS];
        memset(seen, 0, sizeof(seen));
        int distinct = 0;
        int a[AL_FACES] = {0, 1, 2, 3, 4, 5};
        int before = failures;
        permute(a, 0, AL_FACES - 1, seen, &distinct);
        ok("all 720 permutations -> distinct ranks (bijection)",
           distinct == (int)AL_ORBITS && failures == before);
    }

    /* ── 3. drop lands in 144x72, never a reserved id ── */
    {
        int in_range = 1, not_reserved = 1;
        for (int k = 0; k < KC; k++) {
            for (uint32_t o = 0; o < AL_ORBITS; o += 37) {
                uint32_t cell, choice, round, id;
                id = al_cell_choice(k, KC, o, &cell, &choice, &round);
                if (cell >= 144u || choice >= 72u || id >= WL_COUNT) in_range = 0;
                if (id == WL_RESERVED_LO || id == WL_RESERVED_HI) not_reserved = 0;
                if (round >= 10u) in_range = 0;
            }
        }
        ok("cell<144, choice<72, id<10368, round<10", in_range);
        ok("no reserved id (0, 10367) produced", not_reserved);
    }

    /* ── 4. al_route is stable across two runs ── */
    {
        int stable = 1;
        for (int q = 0; q < NQ; q++) {
            al_route_t a, b;
            if (al_route(Q + (size_t)q * DI, C, KC, DI, &a) != 0) { stable = 0; break; }
            if (al_route(Q + (size_t)q * DI, C, KC, DI, &b) != 0) { stable = 0; break; }
            if (memcmp(&a, &b, sizeof(a)) != 0) { stable = 0; break; }
            /* bucket must equal anch_assign (the DECIDE layer is unchanged) */
            if (a.bucket != anch_assign(Q + (size_t)q * DI, C, KC, DI)) { stable = 0; break; }
            /* latch id must be consistent with (cell,choice) */
            if (a.latch_id != wl_id(a.cell, a.choice)) { stable = 0; break; }
        }
        ok("al_route stable + bucket==assign + latch_id==wl_id", stable);
    }

    /* ── 5. K<6 still yields a closed 6-face order (wrap) ── */
    {
        int letters[AL_FACES];
        int rc = al_letter_of(3, C, DI, letters);
        int all_in = 1;
        for (int f = 0; f < AL_FACES; f++)
            if (letters[f] < 0 || letters[f] >= 3) all_in = 0;
        uint32_t orb = al_orbit_of(letters);
        ok("K<6 -> 6 faces by wrap, all in [0,K)", rc == AL_FACES && all_in);
        ok("K<6 orbit still < 720", orb < AL_ORBITS);
    }

    printf("%s\n", failures ? "FAILED" : "ALL PASS");
    return failures ? 1 : 0;
}
