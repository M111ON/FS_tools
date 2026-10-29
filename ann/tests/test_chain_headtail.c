/* test_chain_headtail.c — call an address by head and tail, grow it on collision.
 *
 *   T1 the ladder     : bHyN gives bN, then bHN, then bHyN (letters only)
 *   T2 notation       : print/parse round-trips; digits and markers rejected
 *   T3 match          : a compact key matches a stored key iff the head run and
 *                       the last letter agree AND the depth agrees when set
 *   T4 escalation     : the step that separates two keys is the first interior
 *                       position where they differ, and 1 for head or tail
 *   T5 termination    : the top step is the whole address, so two different
 *                       addresses are always separable
 *   T6 zero is kept   : 0 stays the maintenance marker — a stored key carrying it
 *                       has no compact form, and '0' is not a letter
 *   T7 depth metadata : bN4 is depth-exact, bN is any depth, and clearing the
 *                       depth turns the exact key back into the merge key
 *
 * build: gcc -O2 -I.. -o test_chain_headtail.exe test_chain_headtail.c
 */
#include <stdio.h>
#include <string.h>
#include "../include/chain_headtail.h"

static int fails;

static void ck(const char *what, int ok, const char *detail)
{
    printf("  %-42s %s%s%s\n", what, ok ? "PASS" : "FAIL",
           detail ? "  " : "", detail ? detail : "");
    if (!ok) fails++;
}

int main(void)
{
    ChainKey full;
    ChainHT ht;
    char buf[64];

    printf("=== chain head+tail (letters only) ===\n");

    /* T1 */
    {
        int ok = chain_key(&full, "bHyN");
        ck("T1 key bHyN built", ok && full.n == 4, NULL);

        ok = chain_ht_build(&full, 1, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 1 -> bN", ok && strcmp(buf, "bN") == 0, buf);

        ok = chain_ht_build(&full, 2, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 2 -> bHN", ok && strcmp(buf, "bHN") == 0, buf);

        ok = chain_ht_build(&full, 3, &ht) && chain_ht_str(&ht, buf, sizeof buf);
        ck("T1 step 3 -> bHyN (whole address)", ok && strcmp(buf, "bHyN") == 0, buf);

        ck("T1 step 4 rejected (beyond full)", chain_ht_build(&full, 4, &ht) == 0, NULL);

        ok = chain_ht_build(&full, 1, &ht);
        ck("T1 compact key is 2 bytes of letters", ok && ht.n == 2 && ht.m == 1, NULL);
    }

    /* T2 */
    {
        int ok = chain_ht_parse(&ht, "bN") && chain_ht_str(&ht, buf, sizeof buf);
        ck("T2 parse bN round-trips", ok && strcmp(buf, "bN") == 0, buf);

        ok = chain_ht_parse(&ht, "bHyN") && chain_ht_str(&ht, buf, sizeof buf);
        ck("T2 parse bHyN round-trips", ok && strcmp(buf, "bHyN") == 0, buf);

        ok = chain_ht_parse(&ht, "bHyN");
        ck("T2 parsed tail count = n-1", ok && ht.m == 3 && ht.n == 4, NULL);

        ck("T2 single letter rejected", chain_ht_parse(&ht, "b") == 0, NULL);
        ck("T2 empty rejected", chain_ht_parse(&ht, "") == 0, NULL);
        ck("T2 digit rejected (no positions any more)",
           chain_ht_parse(&ht, "b1N4") == 0, NULL);
    }

    /* T3 */
    {
        ChainKey stored;
        chain_key(&full, "bHyN");
        chain_ht_build(&full, 1, &ht);            /* bN, depth 4 */
        ck("T3 matches the same key", chain_ht_match(&ht, &full) == 1, NULL);

        chain_key(&stored, "bXyN");               /* same head and tail */
        ck("T3 matches a different interior", chain_ht_match(&ht, &stored) == 1, NULL);

        chain_key(&stored, "bHyM");               /* different tail */
        ck("T3 rejects a different tail", chain_ht_match(&ht, &stored) == 0, NULL);

        chain_key(&stored, "cHyN");               /* different head */
        ck("T3 rejects a different head", chain_ht_match(&ht, &stored) == 0, NULL);

        chain_key(&full, "bHyN");
        chain_ht_build(&full, 2, &ht);            /* bHN, needs 3 letters */
        chain_key(&stored, "bN");                 /* shorter than the head run */
        ck("T3 rejects a stored key shorter than the head run",
           chain_ht_match(&ht, &stored) == 0, NULL);

        /* a 2-level chain is fully named by its own bN */
        {
            ChainHT h2;
            chain_ht_build(&stored, 1, &h2);
            ck("T3 a 2-level key is fully named by bN",
               chain_ht_match(&h2, &stored) == 1 && h2.depth == 2, NULL);
        }

        /* a longer chain sharing head and tail is a DIFFERENT depth */
        chain_key(&stored, "bHXyN");
        ck("T3 depth 4 does not match a 5-level key",
           chain_ht_match(&ht, &stored) == 0, NULL);

        chain_ht_any_depth(&ht);
        ck("T3 cleared depth matches any length",
           chain_ht_match(&ht, &stored) == 1, NULL);
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

        chain_key(&b, "cHyN");
        ck("T4 head differs -> step 1 separates",
           chain_ht_step_to_separate(&a, &b) == 1, NULL);

        chain_key(&b, "bHyN");
        ck("T4 identical -> 0 (same address)",
           chain_ht_step_to_separate(&a, &b) == 0, NULL);
    }

    /* T5 */
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
            if (chain_ht_match(&ha, &b)) bad++;
            checked++;
        }
        char s[64];
        snprintf(s, sizeof s, "%d pairs, top step separates every one", checked);
        ck("T5 the ladder always terminates", bad == 0 && checked == 4, s);
    }

    /* T6 — 0 stays the maintenance marker */
    {
        ChainKey closed;
        closed.n = 3;
        closed.d[0] = 2; closed.d[1] = CHAIN_MARK; closed.d[2] = 14;
        ck("T6 closed node has no compact form",
           chain_ht_build(&closed, 1, &ht) == 0, NULL);

        chain_key(&full, "bHyN");
        chain_ht_build(&full, 1, &ht);
        ck("T6 '0' is not a letter in the notation",
           chain_ht_parse(&ht, "b0N") == 0, NULL);

        ck("T6 0 is still the marker value", CHAIN_MARK == 0, NULL);
        ck("T6 a key with 0 is still invalid", chain_valid(&closed) == 0, NULL);

        /* and resolve still stops at the marker */
        ChainKey q, out;
        chain_key(&q, "bHyN");
        q.d[1] = CHAIN_MARK;
        ck("T6 resolve stops at the marker", chain_resolve(&q, &out) == 1, NULL);
    }

    /* T7 — depth as metadata on the name */
    {
        ChainKey deep;
        chain_key(&full, "bHyN");                 /* 4 levels */
        chain_ht_build(&full, 1, &ht);
        ck("T7 build records the depth", ht.depth == 4, NULL);

        int ok = chain_ht_str_meta(&ht, buf, sizeof buf);
        ck("T7 meta form is bN4", ok && strcmp(buf, "bN4") == 0, buf);

        ok = chain_ht_str(&ht, buf, sizeof buf);
        ck("T7 plain form stays bN (no digits)", ok && strcmp(buf, "bN") == 0, buf);

        ok = chain_ht_parse(&ht, "bN4");
        ck("T7 parse bN4 keeps the depth", ok && ht.depth == 4 && ht.m == 1, NULL);

        ok = chain_ht_parse(&ht, "bN");
        ck("T7 parse bN means any depth", ok && ht.depth == 0, NULL);

        ck("T7 bN0 rejected (0 is not a depth)",
           chain_ht_parse(&ht, "bN0") == 0, NULL);

        chain_key(&deep, "bHXyN");                /* 5 levels */
        chain_ht_build(&deep, 1, &ht);
        ok = chain_ht_str_meta(&ht, buf, sizeof buf);
        ck("T7 same name, different depth -> bN5",
           ok && strcmp(buf, "bN5") == 0, buf);
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}