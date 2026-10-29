/* test_chain_node.c — the node binding must be exact and the invariants must hold.
 *
 *   T1 digit -> face      : 52 digits land on 26 faces x 2 cases, bijectively
 *   T2 exit is antipodal  : exit(c) != c, exit(exit(c)) == c, and it is the LCR
 *                           partner at offset 0
 *   T3 closed = no match  : a level stored as 0 stops lookup, and the query
 *                           resolves to the healthy prefix (owner's rule)
 *   T4 major uniqueness   : exactly one major child per parent, at every node
 *   T5 promotion          : extending a key leaves every old key a valid prefix
 *   T6 store consistency  : chain_store_check rejects a record whose parent is missing
 *
 * build: gcc -O2 -I.. -o test_chain_node.exe test_chain_node.c
 */
#include <stdio.h>
#include <string.h>
#include "../include/chain_node.h"

static int fails;

static void ck(const char *what, int ok, const char *detail)
{
    printf("  %-36s %s%s%s\n", what, ok ? "PASS" : "FAIL",
           detail ? "  " : "", detail ? detail : "");
    if (!ok) fails++;
}

static ChainRec rec_make(const char *path, int major)
{
    ChainRec r;
    memset(&r, 0, sizeof r);
    chain_key(&r.key, path);
    r.node.digit = r.key.d[r.key.n - 1];
    r.node.flags = major ? CHAIN_MAJOR : 0;
    return r;
}

/* a record for a level that has been closed: the digit at that level becomes 0 */
static ChainRec rec_closed(const char *path)
{
    ChainRec r = rec_make(path, 0);
    r.key.d[r.key.n - 1] = CHAIN_MARK;
    r.node.digit = CHAIN_MARK;
    return r;
}

int main(void)
{
    LCRCube q;
    int d, i;
    lcr_init(&q, LCR_SHAPE_GREAT);

    printf("=== chain node ===\n");

    /* T1 */
    {
        int seen[64], bad = 0, upper = 0, lower = 0;
        memset(seen, 0, sizeof seen);
        for (d = 1; d <= CHAIN_BASE; d++) {
            uint8_t c = chain_face_code(d);
            if (!lcr_valid(c)) bad++;
            if (c < 64) seen[c]++;
            if (chain_upper(d)) upper++; else lower++;
        }
        for (i = 1; i < 64; i++) if (lcr_valid((uint8_t)i) && seen[i] != 2) bad++;
        {
            char s[80];
            snprintf(s, sizeof s, "faces=%d upper=%d lower=%d", LCR_FACES, upper, lower);
            ck("T1 52 digits = 26 faces x 2 cases", bad == 0 && upper == 26 && lower == 26, s);
        }
        ck("T1 A and a share a face", chain_face_code(1) == chain_face_code(27), NULL);
        ck("T1 A and B do not", chain_face_code(1) != chain_face_code(2), NULL);
    }

    /* T2 */
    {
        int bad = 0, fixed = 0;
        for (d = 1; d <= CHAIN_BASE; d++) {
            uint8_t c = chain_face_code(d), x = chain_exit_code(d);
            if (!lcr_valid(x)) bad++;
            if (x == c) fixed++;
            if (chain_exit_code(x ? (uint8_t)(LCR_FACE_ID[x] + 1) : 0) != c &&
                lcr_partner_code(x, 0) != c) bad++;
            if (lcr_partner_code(c, 0) != x) bad++;
        }
        ck("T2 exit != face (no fixed points)", fixed == 0, NULL);
        ck("T2 exit(exit(c)) == c", bad == 0, NULL);
    }

    /* T3 — store: A, AB(major), AC, AD, then AC's level-2 digit closed */
    {
        ChainRec r[8];
        int n = 0;
        ChainKey qk;
        uint8_t depth;

        r[n++] = rec_make("A", 0);
        r[n++] = rec_make("AB", 1);
        r[n++] = rec_make("AC", 0);
        r[n++] = rec_make("AD", 0);
        r[n++] = rec_make("ABE", 1);
        r[n++] = rec_make("ABF", 0);

        ck("T3 store consistent", chain_store_check(r, n) == 1, NULL);

        chain_key(&qk, "ABE");
        depth = chain_store_depth(r, n, &qk);
        ck("T3 open path resolves fully", depth == 3, NULL);

        chain_key(&qk, "ABZ");
        depth = chain_store_depth(r, n, &qk);
        ck("T3 unknown tail stops at AB", depth == 2, NULL);

        /* close AC: level-2 record becomes A0 */
        r[2] = rec_closed("AC");
        chain_key(&qk, "AC");
        depth = chain_store_depth(r, n, &qk);
        {
            char s[64];
            snprintf(s, sizeof s, "depth=%d", depth);
            ck("T3 closed level stops lookup", depth == 1, s);
        }
        chain_key(&qk, "ACD");
        depth = chain_store_depth(r, n, &qk);
        ck("T3 nothing downstream answers", depth == 1, NULL);
        ck("T3 other branch unaffected", chain_store_depth(r, n,
             (chain_key(&qk, "ABE"), &qk)) == 3, NULL);
    }

    /* T4 — major uniqueness per parent */
    {
        ChainRec r[8];
        int n = 0, bad = 0;
        ChainKey p;
        r[n++] = rec_make("A", 0);
        r[n++] = rec_make("AB", 1);
        r[n++] = rec_make("AC", 0);
        r[n++] = rec_make("AD", 0);
        r[n++] = rec_make("ABE", 1);
        r[n++] = rec_make("ABF", 0);

        chain_key(&p, "A");
        if (chain_major_count(r, n, &p) != 1) bad++;
        if (chain_child_count(r, n, &p) != 3) bad++;
        chain_key(&p, "AB");
        if (chain_major_count(r, n, &p) != 1) bad++;
        if (chain_child_count(r, n, &p) != 2) bad++;
        ck("T4 exactly one major child per parent", bad == 0, NULL);
    }

    /* T5 — promotion keeps old addresses */
    {
        ChainKey oldk, newk;
        int ok;
        chain_key(&oldk, "ABX");
        newk = oldk;
        ok = chain_extend(&newk, chain_digit_of('q'));
        ck("T5 extend succeeds", ok == 1, NULL);
        ck("T5 old key is a prefix of new", chain_is_prefix(&oldk, &newk) == 1, NULL);
        ck("T5 old key still resolves", chain_valid(&oldk) == 1, NULL);
        ck("T5 new key resolves", chain_valid(&newk) == 1, NULL);
        ck("T5 face unchanged by extension",
           chain_face_code(oldk.d[oldk.n - 1]) == chain_face_code(newk.d[oldk.n - 1]), NULL);
    }

    /* T6 — store consistency catches a missing parent */
    {
        ChainRec r[4];
        int n = 0;
        r[n++] = rec_make("A", 0);
        r[n++] = rec_make("ABC", 0);        /* AB is missing */
        ck("T6 missing parent rejected", chain_store_check(r, n) == 0, NULL);
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
