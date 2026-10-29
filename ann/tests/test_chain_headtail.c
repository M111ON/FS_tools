/* test_chain_headtail.c — call an address by head and tail, grow it on collision.
 *
 *   T1 the ladder         : bHyN gives b1N4, then b1H2N4, then b1H2y3N4
 *   T2 notation           : print/parse round-trips, bad strings rejected
 *   T3 match              : a compact key matches a stored key iff those
 *                           positions carry those letters
 *   T4 escalation         : the step that separates two keys is one more than
 *                           the first interior position where they differ
 *   T5 termination        : the top step is the full key, so no lookup can fail
 *                           to separate two different addresses
 *   T6 closed node        : a key carrying a marker has no compact form
 *
 * build: gcc -O2 -I.. -o test_chain_headtail.exe test_chain_headtail.c
 */
#include <stdio.h>
#include <string.h>
#include "../include/chain_headtail.h"

static int fails;

static void ck(const char *what, int ok, const char *detail)
{
    printf("  %-40s %s%s%s\n", what, ok ? "PASS" : "FAIL",
           detail ? "  " : "", detail ? detail : "");
    if (!ok) fails++;
}

int main(void)
{
    ChainKey full;
    ChainHT ht;
    char buf[64];

    printf("=== chain head+tail ===\n");

    /* T1 */
    {
        int ok = chain_key(&full, "bHyN");
        ck("T1 key bHyN built", ok && full.n == 4, NULL);

        ok = chain_ht_build(&full, 1, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 1 -> b1N4", ok && strcmp(buf, "b1N4") == 0, buf);

        ok = chain_ht_build(&full, 2, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 2 -> b1H2N4", ok && strcmp(buf, "b1H2N4") == 0, buf);

        ok = chain_ht_build(&full, 3, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 3 -> b1H2y3N4 (full)", ok && strcmp(buf, "b1H2y3N4") == 0, buf);

        ck("T1 step 4 rejected (beyond full)", chain_ht_build(&full, 4, &ht) == 0, NULL);
    }

    /* T2 */
    {
        int ok = chain_ht_parse(&ht, "b1N4") && chain_ht_str(&ht, buf, sizeof buf);
        ck("T2 parse b1N4 round-trips", ok && strcmp(buf, "b1N4") == 0, buf);

        ok = chain_ht_parse(&ht, "b1H2y3N4") && chain_ht_str(&ht, buf, sizeof buf);
        ck("T2 parse b1H2y3N4 round-trips", ok && strcmp(buf, "b1H2y3N4") == 0, buf);

        ok = chain_ht_parse(&ht, "A12X12") && chain_ht_str(&ht, buf, sizeof buf);
        ck("T2 two-digit positions", ok && strcmp(buf, "A12X12") == 0, buf);

        ck("T2 one letter rejected", chain_ht_parse(&ht, "b1") == 0, NULL);
        ck("T2 missing position rejected", chain_ht_parse(&ht, "bN") == 0, NULL);
        ck("T2 zero position rejected", chain_ht_parse(&ht, "b0N4") == 0, NULL);
        ck("T2 bad digit rejected", chain_ht_parse(&ht, "b1N9x") == 0, NULL);
    }

    /* T3 */
    {
        ChainKey stored;
        chain_key(&full, "bHyN");
        chain_ht_build(&full, 1, &ht);           /* b1N4 */
        ck("T3 matches the same key", chain_ht_match(&ht, &full) == 1, NULL);

        chain_key(&stored, "bXyN");              /* same head and tail */
        ck("T3 matches a different interior", chain_ht_match(&ht, &stored) == 1, NULL);

        chain_key(&stored, "bHyM");              /* different tail */
        ck("T3 rejects a different tail", chain_ht_match(&ht, &stored) == 0, NULL);

        chain_key(&stored, "bHy");               /* too short to carry pos 4 */
        ck("T3 rejects a key shorter than the tail", chain_ht_match(&ht, &stored) == 0, NULL);
    }

    /* T4 */
    {
        ChainKey a, b;
        chain_key(&a, "bHyN");
        chain_key(&b, "bXyN");
        ck("T4 differ at 2 -> step 2 separates",
           chain_ht_step_to_separate(&a, &b) == 2, NULL);

        chain_key(&b, "bHXN");
        ck("T4 differ at 3 -> step 3 separates",
           chain_ht_step_to_separate(&a, &b) == 3, NULL);

        chain_key(&b, "bHyM");
        ck("T4 tail differs -> step 1 separates",
           chain_ht_step_to_separate(&a, &b) == 1, NULL);

        chain_key(&b, "bHyN");
        ck("T4 identical -> 0 (same address)",
           chain_ht_step_to_separate(&a, &b) == 0, NULL);
    }

    /* T5 — walk the ladder and prove the top step always separates */
    {
        ChainKey a, b;
        int bad = 0, checked = 0;
        const char *pa[] = {"bHyN", "AAAA", "aZyX", "MMMM"};
        const char *pb[] = {"bXyN", "AAAB", "aZyY", "MMMN"};
        for (int i = 0; i < 4; i++) {
            chain_key(&a, pa[i]);
            chain_key(&b, pb[i]);
            uint8_t step = chain_ht_step_to_separate(&a, &b);
            if (step == 0 || step >= a.n) { bad++; continue; }
            ChainHT ha, hb;
            chain_ht_build(&a, step, &ha);
            chain_ht_build(&b, step, &hb);
            int sep = !(chain_ht_match(&ha, &b));
            if (!sep) bad++;
            checked++;
        }
        char s[64];
        snprintf(s, sizeof s, "%d pairs, top step separates every one", checked);
        ck("T5 the ladder always terminates", bad == 0 && checked == 4, s);
    }

    /* T6 */
    {
        ChainKey closed;
        closed.n = 3;
        closed.d[0] = 2; closed.d[1] = 0; closed.d[2] = 14;
        ck("T6 closed key has no compact form", chain_ht_build(&closed, 1, &ht) == 0, NULL);
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
