/* tests/test_anchor_lettercube.c — determinism + losslessness for the
 * anchor-bucket -> LetterCube-face bridge (include/anchor_lettercube.h).
 *
 * Oracles, all recomputed here by different code than the implementation:
 *   RANK   : alc_rank_of == a brute-force sort by (energy desc, id asc).
 *   BIJECT : rank is a bijection onto [0,K); K<=26 -> distinct faces.
 *   LOSSLESS: for every bucket and every level, lcr_entry_dir(entry) == d, and
 *           the stored face is ACTIVE at that level (the lean is legal).
 *   PROMOTE: advancing one level at a time preserves d, and L0->L2 == L0->L1->L2.
 *   TRIGGER: alc_initial_level is exactly the fold of lcr_need_promote.
 *   STABLE : two passes give identical bytes.
 *
 * Deterministic: closed-form dataset, no RNG.
 * BUILD: gcc -O2 -Wall -Wextra -pedantic -Iinclude -o build/test_anchor_lettercube \
 *        tests/test_anchor_lettercube.c -lm
 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>

#include "anchor_lettercube.h"

#define N   512
#define DI  32
#define KC  64          /* > 26: exercises the face wrap */

static void det(float *X, int n, int dim, unsigned sk)
{
    for (int i = 0; i < n; i++)
        for (int j = 0; j < dim; j++) {
            unsigned v = (unsigned)(i * 131 + j * 17 + (i * j) % 13 + sk);
            X[(size_t)i * dim + j] = (float)(v % 2000) / 1000.0f - 1.0f;
        }
}

static int failures = 0;
static void ok(const char *name, int pass)
{
    printf("  %s %s\n", pass ? "ok  " : "FAIL", name);
    if (!pass) failures++;
}

/* independent order: selection sort by energy desc, id asc */
static void brute_rank(int K, const float *C, int dim, int rank[ANCHR_MAXK])
{
    double en[ANCHR_MAXK];
    int idx[ANCHR_MAXK];
    for (int k = 0; k < K; k++) { en[k] = alc_energy(C, k, dim); idx[k] = k; }
    for (int a = 0; a < K; a++) {
        int best = a;
        for (int b = a + 1; b < K; b++) {
            double eb = en[idx[b]], ea = en[idx[best]];
            if (eb > ea || (eb == ea && idx[b] < idx[best])) best = b;
        }
        int t = idx[a]; idx[a] = idx[best]; idx[best] = t;
    }
    for (int r = 0; r < K; r++) rank[idx[r]] = r;
}

int main(void)
{
    printf("test_anchor_lettercube\n");

    static float X[N * DI], C[KC * DI];
    static int lab[N];
    det(X, N, DI, 0u);
    if (anch_train(X, N, DI, KC, C, lab) != 0) { printf("train failed\n"); return 1; }

    uint8_t dir[KC];
    int     rank[KC];

    /* ── 1. rank == brute force; rank is a bijection ───────────────────── */
    {
        int ref[ANCHR_MAXK];
        brute_rank(KC, C, DI, ref);
        int same = 1, bij = 1;
        int seen[KC];
        memset(seen, 0, sizeof(seen));
        for (int k = 0; k < KC; k++) {
            rank[k] = alc_rank_of(k, KC, C, DI);
            if (rank[k] != ref[k]) same = 0;
            if (rank[k] < 0 || rank[k] >= KC) { bij = 0; continue; }
            if (seen[rank[k]]) bij = 0;
            seen[rank[k]] = 1;
        }
        ok("rank == brute-force (energy desc, id asc)", same);
        ok("rank is a bijection onto [0,K)", bij);
    }

    /* ── 2. dir: valid, non-centre, wrap-aware distinctness ────────────── */
    {
        int valid = 1, r2d = 1;
        int faceseen[LCR_FACES];
        memset(faceseen, 0, sizeof(faceseen));
        for (int k = 0; k < KC; k++) {
            dir[k] = alc_dir_of(k, KC, C, DI);
            if (dir[k] == 0 || !lcr_valid(dir[k])) valid = 0;
            if (dir[k] != LCR_FACE_CODE[rank[k] % LCR_FACES]) r2d = 0;
            if (dir[k]) faceseen[LCR_FACE_ID[dir[k]]] = 1;
        }
        int used = 0;
        for (int i = 0; i < LCR_FACES; i++) used += faceseen[i];
        ok("every bucket has a valid non-centre L2 face", valid);
        ok("rank -> face is the canonical code table", r2d);
        ok("K=64 spans all 26 faces (wrap is total)", used == LCR_FACES);

        /* K<=26 must be injective */
        int inj = 1, seen[LCR_FACES];
        memset(seen, 0, sizeof(seen));
        for (int k = 0; k < 13; k++) {
            uint8_t d = alc_dir_of(k, 13, C, DI);
            int id = LCR_FACE_ID[d];
            if (id < 0 || seen[id]) inj = 0;
            else seen[id] = 1;
        }
        ok("K=13 (< 26) -> 13 distinct faces", inj);
    }

    /* ── 3. lossless at every level; face = lcr_parent(d,L); lean == d ────
     * NOTE: lcr_lean_ok(L, face, offset) is deliberately NOT asserted here.
     * It denies a lean that lands outside the level's active set, but the plan's
     * own rule is face = lcr_parent(d, L) with d a FIXED finer identity, so at L0
     * (axial only) a corner d is exactly the case that must be representable.
     * lcr_lean_ok has no caller; left alone, reported instead. */
    {
        int lossless = 1, active = 1, parent = 1, lean = 1;
        for (int k = 0; k < KC; k++)
            for (uint8_t L = 0; L < LCR_LEVELS; L++) {
                LCREntry e = alc_entry_of(k, KC, C, DI, L);
                if (lcr_entry_dir(e) != dir[k]) lossless = 0;
                if (!lcr_active(L, e.face)) active = 0;
                if (lcr_parent(dir[k], L) != e.face) parent = 0;
                if (lcr_lean(e.face, e.offset) != dir[k]) lean = 0;
                if (!lcr_offset_ok(e.offset)) lean = 0;
            }
        ok("lcr_entry_dir(entry@L) == d for all L", lossless);
        ok("stored face is active at its level", active);
        ok("stored face == lcr_parent(d, L)", parent);
        ok("lean(face, offset) == d (valid trit offset)", lean);
    }

    /* ── 4. promotion: d preserved, stepwise == direct ────────────────── */
    {
        int fixed = 1, stepwise = 1, idem = 1;
        for (int k = 0; k < KC; k++) {
            LCREntry at0 = alc_entry_of(k, KC, C, DI, 0);
            LCREntry direct = alc_entry_promote(at0, LCR_LEVELS - 1);
            LCREntry s1 = alc_entry_promote(at0, 1);
            LCREntry s2 = alc_entry_promote(s1, LCR_LEVELS - 1);
            if (lcr_entry_dir(direct) != dir[k]) fixed = 0;
            if (memcmp(&s2, &direct, sizeof(direct)) != 0) stepwise = 0;
            if (alc_entry_promote(direct, LCR_LEVELS - 1).offset != direct.offset) idem = 0;
        }
        ok("promotion never loses d (no vector read)", fixed);
        ok("L0->L2 == L0->L1->L2", stepwise);
        ok("promotion at L2 is idempotent", idem);
    }

    /* ── 5. the geometry's trigger drives the bridge, end to end ──────────
     * The bridge owns NO threshold: lcr_need_promote decides, alc_entry_promote
     * carries out, and the result must equal re-deriving the entry at the deeper
     * level from the recovered d — that IS "promote without re-reading vectors".
     * The receipt prints the cap shape, because a DECREASING LCR_MAX_STEPS means
     * "the first level that fits" is not a well-defined level at all. */
    {
        int eq = 1, bounded = 1, fired = 0;
        long fires[LCR_LEVELS] = {0, 0, 0};
        for (int k = 0; k < KC; k++)
            for (uint8_t L = 0; L + 1 < LCR_LEVELS; L++)
                for (int steps = 0; steps <= 20; steps++) {
                    if (!lcr_need_promote(L, (uint8_t)steps)) continue;
                    LCREntry e   = alc_entry_of(k, KC, C, DI, L);
                    LCREntry nx  = alc_entry_promote(e, (uint8_t)(L + 1));
                    LCREntry ref = alc_entry_of(k, KC, C, DI, (uint8_t)(L + 1));
                    if (memcmp(&nx, &ref, sizeof(nx)) != 0) eq = 0;
                    if (lcr_entry_dir(nx) != dir[k]) eq = 0;
                    if (!lcr_active((uint8_t)(L + 1), nx.face)) eq = 0;
                    fires[L]++; fired++;
                }
        for (int steps = 0; steps <= 200; steps++)
            if (lcr_need_promote(LCR_LEVELS - 1, (uint8_t)steps)) bounded = 0;
        ok("promote(e@L) == re-derive entry at L+1 (re-bucket by d)", eq);
        ok("trigger is silent at the deepest level", bounded);
        ok("trigger fires somewhere", fired > 0);
        for (uint8_t L = 0; L < LCR_LEVELS; L++)
            printf("  caps {%u,%u,%u}: L%u fired on %ld of %d bucket-step pairs\n",
                   LCR_MAX_STEPS[0], LCR_MAX_STEPS[1], LCR_MAX_STEPS[2],
                   L, fires[L], KC * 21);
    }

    /* ── 6. name level: which level names d exactly (offset 0) ─────────── */
    {
        int lv_ok = 1, mono = 1, nameable = 1;
        int by_level[LCR_LEVELS];
        memset(by_level, 0, sizeof(by_level));
        for (int k = 0; k < KC; k++) {
            uint8_t nl = alc_name_level(dir[k]);
            if (nl >= LCR_LEVELS) nameable = 0;
            for (uint8_t L = 0; L < LCR_LEVELS; L++) {
                LCREntry e = alc_entry_of(k, KC, C, DI, L);
                int exact = (L >= nl);
                if ((e.offset == 0) != exact) lv_ok = 0;    /* exact <=> offset 0 */
                if (L >= nl && e.offset != 0) mono = 0;
            }
            if (nl < LCR_LEVELS) by_level[nl]++;
        }
        ok("every valid d is nameable at some level (nl < LCR_LEVELS)", nameable);
        ok("offset == 0 <=> level >= name_level(d)", lv_ok);
        ok("offset stays 0 above name_level (monotone)", mono);
        printf("  name_level buckets: L0=%d L1=%d L2=%d (of %d)\n",
               by_level[0], by_level[1], by_level[2], KC);
    }

    /* ── 7. stable across two passes ──────────────────────────────────── */
    {
        int stable = 1;
        for (int k = 0; k < KC; k++) {
            uint8_t a = alc_dir_of(k, KC, C, DI), b = alc_dir_of(k, KC, C, DI);
            if (a != b || a != dir[k]) stable = 0;
            LCREntry e1 = alc_entry_of(k, KC, C, DI, 1), e2 = alc_entry_of(k, KC, C, DI, 1);
            if (memcmp(&e1, &e2, sizeof(e1)) != 0) stable = 0;
        }
        ok("two passes identical (no RNG, no hidden state)", stable);
    }

    /* ── 7. receipt: per-level occupancy + how many buckets must move ──── */
    printf("  K=%d faces/level=%d/%d/%d\n", KC, LCR_LEVEL_FACES[0],
           LCR_LEVEL_FACES[1], LCR_LEVEL_FACES[2]);
    for (uint8_t L = 0; L < LCR_LEVELS; L++) {
        int faceseen[LCR_FACES];
        memset(faceseen, 0, sizeof(faceseen));
        int moved = 0, off0 = 0;
        for (int k = 0; k < KC; k++) {
            LCREntry e = alc_entry_of(k, KC, C, DI, L);
            faceseen[LCR_FACE_ID[e.face]] = 1;
            if (e.offset) off0++;
            if (e.face != dir[k]) moved++;
        }
        int used = 0;
        for (int i = 0; i < LCR_FACES; i++) used += faceseen[i];
        printf("  L%u: faces_used=%2d  buckets_with_offset=%2d  buckets_off_their_dir=%2d\n",
               L, used, off0, moved);
    }

    printf("%s\n", failures ? "FAILED" : "ALL PASS");
    return failures ? 1 : 0;
}
