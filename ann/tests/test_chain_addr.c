/* tests/test_chain_addr.c — the four properties the chain container claims.
 *
 *   T1 address  : letters round-trip through key -> hash -> table -> key
 *   T2 no walk  : lookup cost does not grow with how much is stored
 *   T3 ambiguity: a short key collides, one more level makes it unique
 *   T4 marker 0 : a closed node stops search and repair finds exactly it
 *   T5 promotion: extending the chain keeps every address already handed out
 *
 * BUILD: gcc -O2 -Wall -Wextra -pedantic -Iinclude -o build/test_chain_addr \
 *        tests/test_chain_addr.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "chain_addr.h"

/* ── tiny open-addressing table (test harness only) ───────────────────── */
typedef struct { ChainKey k; int payload; int used; } Slot;

typedef struct { Slot *s; size_t cap, used; } Table;

static int tab_init(Table *t, size_t cap)
{
    t->s = calloc(cap, sizeof(Slot));
    t->cap = cap; t->used = 0;
    return t->s != NULL;
}
static void tab_free(Table *t) { free(t->s); memset(t, 0, sizeof *t); }

static int tab_put(Table *t, const ChainKey *k, int payload)
{
    size_t i = (size_t)(chain_hash(k) & (t->cap - 1));
    for (size_t probe = 0; probe < t->cap; probe++) {
        Slot *s = &t->s[(i + probe) & (t->cap - 1)];
        if (!s->used) { s->k = *k; s->payload = payload; s->used = 1; t->used++; return 1; }
        if (chain_cmp(&s->k, k) == 0) { s->payload = payload; return 0; } /* duplicate key */
    }
    return -1;
}
static int tab_get(const Table *t, const ChainKey *k, int *payload)
{
    size_t i = (size_t)(chain_hash(k) & (t->cap - 1));
    for (size_t probe = 0; probe < t->cap; probe++) {
        const Slot *s = &t->s[(i + probe) & (t->cap - 1)];
        if (!s->used) return 0;
        if (chain_cmp(&s->k, k) == 0) { if (payload) *payload = s->payload; return 1; }
    }
    return 0;
}

/* keys under a prefix = every stored row whose key starts with `p` */
static int tab_under_prefix(const Table *t, const ChainKey *p, int **out, int *count)
{
    int n = 0;
    *out = NULL;
    for (size_t i = 0; i < t->cap; i++) {
        if (!t->s[i].used) continue;
        if (!chain_is_prefix(p, &t->s[i].k)) continue;
        int *g = realloc(*out, sizeof(int) * (size_t)(n + 1));
        if (!g) return -1;
        *out = g;
        (*out)[n++] = t->s[i].payload;
    }
    *count = n;
    return 0;
}

/* longest-prefix resolve: probe q, then each shorter prefix, longest first.
 * At most q->n hash probes, still independent of how much is stored. Closed
 * levels are cut before probing (chain_resolve), so a marker is never matched. */
static int tab_get_prefix(const Table *t, const ChainKey *q, int *payload, uint8_t *depth)
{
    ChainKey r;
    uint8_t n = chain_resolve(q, &r);
    for (uint8_t len = n; len >= 1; len--) {
        ChainKey p = r;
        p.n = len;
        if (tab_get(t, &p, payload)) { if (depth) *depth = len; return 1; }
    }
    return 0;
}

static double now_ns(void)
{
    return (double)clock() * (1e9 / (double)CLOCKS_PER_SEC);
}

static unsigned rng_state = 12345u;
static unsigned rnd(void) { rng_state = rng_state * 1103515245u + 12345u; return (rng_state >> 16) & 0x7fffu; }

int main(void)
{
    int fails = 0;
    printf("test_chain_addr\n");

    /* ── T1 round-trip ─────────────────────────────────────────────── */
    {
        Table t; tab_init(&t, 1u << 12);
        int bad = 0;
        for (int i = 0; i < 1000; i++) {
            ChainKey k = {0};
            k.n = 12;
            for (int j = 0; j < 12; j++) k.d[j] = (uint8_t)(1 + rnd() % 52);
            if (!chain_valid(&k)) { bad++; continue; }
            tab_put(&t, &k, i);
            int px = -1;
            if (!tab_get(&t, &k, &px) || px != i) bad++;
        }
        printf("T1 round-trip        : %s (%d bad of 1000)\n", bad ? "FAIL" : "PASS", bad);
        if (bad) fails++;

        /* literal form: "AXq" -> 3 digits, and a marker inside a literal is refused */
        ChainKey lit = {0}, badlit = {0};
        int ok_lit = chain_key(&lit, "AXq");
        int ok_bad = chain_key(&badlit, "A0q");
        printf("T1 literal           : %s (%s, %s)\n",
               (ok_lit && lit.n == 3 && lit.d[0] == 1 && lit.d[1] == 24 && lit.d[2] == 43 && !ok_bad)
                   ? "PASS" : "FAIL",
               ok_lit ? "AXq accepted" : "AXq refused", ok_bad ? "A0q accepted" : "A0q refused");
        if (!(ok_lit && lit.n == 3 && !ok_bad)) fails++;
        tab_free(&t);
    }

    /* ── T2 lookup cost is flat ───────────────────────────────────── */
    {
        double ns_small = 0, ns_big = 0;
        size_t sizes[2] = { 100, 100000 };
        for (int s = 0; s < 2; s++) {
            Table t; tab_init(&t, 1u << 18);
            for (size_t i = 0; i < sizes[s]; i++) {
                ChainKey k = {0};
                k.n = 6;
                for (int j = 0; j < 6; j++) k.d[j] = (uint8_t)(1 + rnd() % 52);
                k.d[0] = (uint8_t)(1 + (i % 52));
                k.d[1] = (uint8_t)(1 + ((i / 52) % 52));
                k.d[2] = (uint8_t)(1 + ((i / 2704) % 52));
                tab_put(&t, &k, (int)i);
            }
            ChainKey probe = {0};
            probe.n = 6;
            for (int j = 0; j < 6; j++) probe.d[j] = (uint8_t)(1 + rnd() % 52);
            const int REP = 2000000;
            double t0 = now_ns();
            int hits = 0;
            for (int r = 0; r < REP; r++) { probe.d[5] = (uint8_t)(1 + (r % 52)); if (tab_get(&t, &probe, NULL)) hits++; }
            double dt = now_ns() - t0;
            if (s == 0) ns_small = dt / REP; else ns_big = dt / REP;
            printf("T2 lookup @%-6lu rows: %.1f ns/lookup (%d hits)\n", (unsigned long)sizes[s], dt / REP, hits);
            tab_free(&t);
        }
        double ratio = ns_small > 0 ? ns_big / ns_small : 99.0;
        printf("T2 no-walk           : %s (big/small = %.2fx)\n", ratio < 5.0 ? "PASS" : "FAIL", ratio);
        if (!(ratio < 5.0)) fails++;
    }

    /* ── T3 ambiguity vs key length ───────────────────────────────── */
    {
        int distinct[5] = {0};
        const int N = 100000;
        for (int len = 2; len <= 4; len++) {
            Table t; tab_init(&t, 1u << 18);
            for (int i = 0; i < N; i++) {
                ChainKey k = {0};
                k.n = (uint8_t)len;
                for (int j = 0; j < len; j++) k.d[j] = (uint8_t)(1 + rnd() % 52);
                tab_put(&t, &k, i);
            }
            distinct[len] = (int)t.used;
            printf("T3 %d levels         : %6d distinct of %d (space %lu)\n",
                   len, distinct[len], N, (unsigned long)chain_pow((uint8_t)len));
            tab_free(&t);
        }
        int ok = distinct[3] > distinct[2] && distinct[4] > distinct[3] && distinct[4] > 90000;
        printf("T3 ambiguity curve   : %s\n", ok ? "PASS" : "FAIL");
        if (!ok) fails++;
    }

    /* ── T4 marker 0 = closed node ────────────────────────────────── */
    {
        Table t; tab_init(&t, 1u << 12);
        ChainKey ab = {0};
        chain_key(&ab, "AB");
        tab_put(&t, &ab, 999);                    /* the last healthy node on the branch */
        int flagged = 0;
        for (int i = 0; i < 200; i++) {
            ChainKey k = {0};
            k.n = 5;
            k.d[0] = 1; k.d[1] = 2;               /* shared head "AB" */
            k.d[2] = (uint8_t)(1 + (i % 52));
            k.d[3] = (uint8_t)(1 + ((i / 52) % 52));
            k.d[4] = (uint8_t)(1 + (i % 7));
            if (i % 10 == 0) { k.d[2] = CHAIN_MARK; flagged++; }  /* level 3 closed */
            tab_put(&t, &k, i);                   /* closed rows are STORED, so repair can find them */
        }
        int found = 0;
        for (size_t i = 0; i < t.cap; i++)
            if (t.s[i].used && !chain_valid(&t.s[i].k)) found++;

        ChainKey closed = {0};
        closed.n = 5; closed.d[0] = 1; closed.d[1] = 2; closed.d[2] = CHAIN_MARK;
        closed.d[3] = 1; closed.d[4] = 1;
        int px = -1; uint8_t depth = 0;
        int hit = tab_get_prefix(&t, &closed, &px, &depth);
        int reached_healthy = hit && px == 999 && depth == 2;
        int entered_closed = chain_valid(&closed);

        printf("T4 repair scan       : %s (flagged %d, found %d)\n",
               (found == flagged) ? "PASS" : "FAIL", flagged, found);
        printf("T4 search stops      : %s (query AB[0]11 -> payload %d at depth %u)\n",
               reached_healthy ? "PASS" : "FAIL", hit ? px : -1, (unsigned)depth);
        printf("T4 closed not addr.  : %s\n", entered_closed ? "FAIL" : "PASS");
        if (found != flagged) fails++;
        if (!reached_healthy) fails++;
        if (entered_closed) fails++;
        tab_free(&t);
    }

    /* ── T5 promotion keeps addresses ─────────────────────────────── */
    {
        Table t; tab_init(&t, 1u << 12);
        ChainKey base = {0};
        chain_key(&base, "ABX");
        tab_put(&t, &base, 7);

        ChainKey grown = base;
        int ok_ext = chain_extend(&grown, chain_digit_of('q'));   /* bigger container wraps the old one */
        int px = -1; uint8_t depth = 0;
        int hit = tab_get_prefix(&t, &grown, &px, &depth);        /* new key still finds the old row */
        int kept_hit = (hit && px == 7 && depth == 3);

        int under = 0;
        int *payloads = NULL;
        tab_under_prefix(&t, &base, &payloads, &under);
        int kept = (under == 1 && payloads && payloads[0] == 7);
        free(payloads);

        ChainKey cut = grown;
        int ok_cut = chain_truncate(&cut, 3);
        int same = (chain_cmp(&cut, &base) == 0);

        printf("T5 promote (extend)  : %s (%s, old row found at depth %u)\n",
               (ok_ext && kept_hit) ? "PASS" : "FAIL", ok_ext ? "ABX->ABXq" : "extend refused", (unsigned)depth);
        printf("T5 truncate (close)  : %s (%s, identical=%d)\n",
               (ok_cut && same) ? "PASS" : "FAIL", ok_cut ? "back to 3 levels" : "truncate refused", same);
        printf("T5 old address kept  : %s (%d payload under ABX)\n", kept ? "PASS" : "FAIL", under);
        if (!(ok_ext && kept_hit)) fails++;
        if (!(ok_cut && same)) fails++;
        if (!kept) fails++;
        tab_free(&t);
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
