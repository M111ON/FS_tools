/* test_chain_ident.c — composite identity, shape phase, and the allocator audit.
 *
 * The rules under test come from the owner's correction (handoff
 * 2026-10-30-LC-ANN): a letter is a connector signal in a local pool, not a
 * global id, so repeated letters are legal; collision needs the full context;
 * and the shape number is a phase, never a level.
 */
#include <stdio.h>
#include <string.h>
#include "../include/chain_ident.h"

static int fails = 0;

static void ck(const char *what, int cond, const char *extra)
{
    printf("  %-52s %s", what, cond ? "PASS" : "FAIL");
    if (extra && *extra) printf("  %s", extra);
    printf("\n");
    if (!cond) fails++;
}

static ChainKey key_of(const char *s)
{
    ChainKey k;
    memset(&k, 0, sizeof k);
    for (size_t i = 0; s[i]; i++) k.d[k.n++] = chain_digit_of(s[i]);
    return k;
}

static ChainIdent mk(const char *path, uint32_t rec, uint32_t epoch,
                     uint8_t major, uint8_t minor, uint8_t shape,
                     uint8_t phase, uint8_t level, int16_t off)
{
    ChainIdent id;
    memset(&id, 0, sizeof id);
    id.epoch = epoch;
    id.rec = rec;
    id.major = major;
    id.minor = minor;
    id.shape = shape;
    id.phase = phase;
    id.level = level;
    id.offset = off;
    id.path = key_of(path);
    id.conn_in = id.path.d[0];
    id.conn_out = id.path.d[id.path.n - 1];
    return id;
}

int main(void)
{
    ChainIdentAudit a;

    printf("T1 phase is a side count, not a face count\n");
    ck("3 triangle accepted", chain_phase_ok(CI_PHASE_TRI), "");
    ck("4 square accepted", chain_phase_ok(CI_PHASE_SQ), "");
    ck("6 hexagon accepted", chain_phase_ok(CI_PHASE_HEX), "");
    ck("8 octagon accepted", chain_phase_ok(CI_PHASE_OCT), "");
    ck("0 rejected (not a shape)", !chain_phase_ok(0), "");
    ck("14 rejected (that is L1 face count)", !chain_phase_ok(14), "");
    ck("26 rejected (that is L2 face count)", !chain_phase_ok(26), "");

    printf("T2 phase and level are separate facts\n");
    {
        ChainIdent h = mk("bHyN", 7, 1, 1, 0, 0, CI_PHASE_HEX, 0, 0);
        char buf[16];
        int n;
        ck("hexagon on L0 is legal at once",
           chain_phase_ok(h.phase) && h.level == 0, "");
        n = chain_ident_str(&h, buf, sizeof buf);
        ck("string carries one digit only", n == 5 && strcmp(buf, "bHyN6") == 0, buf);
        {
            int digits = 0;
            for (int i = 0; i < n; i++) if (buf[i] >= '0' && buf[i] <= '9') digits++;
            ck("that digit can only be the phase", digits == 1, "");
        }
        ck("level stays in the struct", h.level == 0, "");
        h.level = 2;
        ck("changing level does not change the string",
           chain_ident_str(&h, buf, sizeof buf) == 5 && strcmp(buf, "bHyN6") == 0, buf);
    }

    printf("T3 same point vs identical\n");
    {
        ChainIdent p = mk("bHyN", 7, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent q = mk("bHyN", 9, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ck("two records share a point", chain_ident_same_point(&p, &q), "");
        ck("but they are not identical", !chain_ident_eq(&p, &q), "");
        q.rec = 7;
        ck("same record is identical", chain_ident_eq(&p, &q), "");
        q.offset = 3;
        ck("offset makes them different", !chain_ident_eq(&p, &q), "");
        q.offset = 0;
        q.epoch = 2;
        ck("epoch makes the point different", !chain_ident_same_point(&p, &q), "");
    }

    printf("T4 the audit classifies instead of forbidding\n");
    {
        ChainIdent r1 = mk("bHyN", 7, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent r2 = mk("bHyN", 9, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent res = mk("bHyN", 11, 1, 1, 0, 0, CI_PHASE_HEX, 1, 4);
        ChainIdent bad = mk("bHyN", 12, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);

        bad.path.d[2] = CHAIN_MARK;      /* a closed node on the path */
        chain_audit_init(&a);
        ck("first reserve is RESERVED",
           chain_audit_reserve(&a, &r1, CI_RESERVED) == CI_RESERVED, "");
        ck("same identity again is DUP",
           chain_audit_reserve(&a, &r1, CI_RESERVED) == CI_DUP, "");
        ck("a second record on the same point is MERGE",
           chain_audit_reserve(&a, &r2, CI_RESERVED) == CI_MERGE, "");
        ck("a declared residual is RESIDUAL",
           chain_audit_reserve(&a, &res, CI_RESIDUAL) == CI_RESIDUAL, "");
        ck("commit works", chain_audit_commit(&a, &r1) == 1, "");
        ck("committed state is visible",
           chain_audit_state_of(&a, &r1) == CI_COMMITTED, "");
        ck("closed path is CLOSED, never FREE",
           chain_audit_reserve(&a, &bad, CI_RESERVED) == CI_CLOSED, "");
        ck("close marks the point",
           chain_audit_close(&a, &r2) == 1 &&
           chain_audit_state_of(&a, &r2) == CI_CLOSED, "");
    }

    printf("T5 letters may repeat - there is no global used-set\n");
    {
        ChainIdent x = mk("bHyN", 1, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent other_major = mk("bHyN", 1, 1, 2, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent other_shape = mk("bHyN", 1, 1, 1, 0, 1, CI_PHASE_SQ, 1, 0);
        ChainIdent other_epoch = mk("bHyN", 1, 2, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent other_minor = mk("bHyN", 1, 1, 1, 5, 0, CI_PHASE_HEX, 1, 0);
        chain_audit_init(&a);
        ck("same letters, first reserve", chain_audit_reserve(&a, &x, CI_RESERVED) == CI_RESERVED, "");
        ck("same letters, other major root", chain_audit_reserve(&a, &other_major, CI_RESERVED) == CI_RESERVED, "");
        ck("same letters, other shape", chain_audit_reserve(&a, &other_shape, CI_RESERVED) == CI_RESERVED, "");
        ck("same letters, other epoch", chain_audit_reserve(&a, &other_epoch, CI_RESERVED) == CI_RESERVED, "");
        ck("same letters, other minor root", chain_audit_reserve(&a, &other_minor, CI_RESERVED) == CI_RESERVED, "");
        ck("five reservations, no duplicate", a.cnt[CI_DUP] == 0, "");
    }

    printf("T6 counters account for every decision\n");
    {
        int sum = 0;
        for (int s = 0; s < CI_NSTATES; s++) sum += a.cnt[s];
        ck("counters sum to the rows recorded", sum == a.n + a.cnt[CI_DUP], "");
        ck("rows recorded match", a.n == 5, "");
    }

    printf("T7 fingerprint\n");
    {
        ChainIdent x = mk("bHyN", 1, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent y = x;
        ck("same identity, same fingerprint", chain_ident_fp(&x) == chain_ident_fp(&y), "");
        y.phase = CI_PHASE_OCT;
        ck("phase change moves it", chain_ident_fp(&x) != chain_ident_fp(&y), "");
        y = x; y.minor = 4;
        ck("minor root change moves it", chain_ident_fp(&x) != chain_ident_fp(&y), "");
        y = x; y.path = key_of("bHyM");
        ck("path change moves it", chain_ident_fp(&x) != chain_ident_fp(&y), "");
        y = x; y.offset = -5;
        ck("negative offset moves it", chain_ident_fp(&x) != chain_ident_fp(&y), "");
    }

    printf("T8 the two zeros are different namespaces\n");
    {
        int distinct = 1;
        for (int s = 0; s < CI_NSTATES; s++)
            for (int t = s + 1; t < CI_NSTATES; t++)
                if (strcmp(chain_state_name(s), chain_state_name(t)) == 0) distinct = 0;
        ck("every state has its own name", distinct, "");
        ck("FREE is not the path marker's meaning",
           strcmp(chain_state_name(CI_FREE), "free") == 0 && CHAIN_MARK == 0, "");
        ck("CLOSED is the path marker's meaning",
           strcmp(chain_state_name(CI_CLOSED), "closed") == 0, "");
        ck("a path with 0 is never FREE", !chain_valid(&(ChainKey){
               .n = 3, .d = {1, CHAIN_MARK, 3} }), "");
    }

    printf("T9 notation round trip\n");
    {
        ChainIdent x = mk("bHyN", 1, 1, 1, 0, 0, CI_PHASE_HEX, 1, 0);
        ChainIdent back;
        char buf[16];
        memset(&back, 0, sizeof back);
        back.level = 2;
        ck("write", chain_ident_str(&x, buf, sizeof buf) == 5, buf);
        ck("read", chain_ident_parse(&back, buf) == 1, "");
        ck("path survives", chain_cmp(&x.path, &back.path) == 0, "");
        ck("phase survives", back.phase == CI_PHASE_HEX, "");
        ck("level was not in the string", back.level == 2, "");
        ck("bad phase rejected", !chain_ident_parse(&back, "bHyN5"), "");
        ck("missing phase rejected", !chain_ident_parse(&back, "bHyN"), "");
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
