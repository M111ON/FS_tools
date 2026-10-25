/* T1-T4 gate for core/geo_wang_latch.h — B-latch one-way flow latch.
 * Expectations from spec/math, never from the header's own output. */
#include <stdio.h>
#include <string.h>
#include "geo_wang_latch.h"
#include "geo_id_route_log.h"

static int fails = 0;
#define CHECK(c, msg) do { if (c) printf("  ok   %s\n", msg); \
                           else { printf("  FAIL %s\n", msg); fails++; } } while (0)

int main(void) {
    wl_latch_t L;
    wl_reset(&L);

    /* T1 count: 144 cell x 72 choice = 10368 unique ids, reserved {0,10367}, free 10366 */
    int total = 0, reserved = 0, distinct = 0;
    static unsigned char seen[WL_COUNT];
    memset(seen, 0, sizeof seen);
    for (unsigned cell = 0; cell < 144; cell++)
        for (unsigned ch = 0; ch < 72; ch++) {
            uint32_t id = wl_id(cell, ch);
            total++;
            if (id < WL_COUNT && !seen[id]) { seen[id] = 1; distinct++; }
            if (wl_is_reserved(id)) reserved++;
        }
    CHECK(total == 10368, "T1 enumerate 144x72 = 10368");
    CHECK(distinct == 10368, "T1 wl_id bijective over [0,10368)");
    CHECK(reserved == 2, "T1 exactly 2 reserved (0 and 10367)");
    CHECK(WL_FREE == 10366u, "T1 free = 10366");
    CHECK(wl_is_reserved(0) && wl_is_reserved(10367), "T1 reserved = {0,10367}");
    CHECK(WL_COUNT * 2 == 20736u, "T1 half-field: 10368*2 = 20736");

    /* T2 monotone: open -> traversed -> shut, never reverses; only CLEAR reopens */
    uint32_t id = wl_id(10, 5); /* 725, non-reserved */
    CHECK(wl_is_open(&L, id), "T2 init open");
    CHECK(wl_traverse(&L, id) == 1, "T2 traverse accepted");
    CHECK(!wl_is_open(&L, id), "T2 shut after walk");
    wl_traverse(&L, id);
    CHECK(!wl_is_open(&L, id), "T2 repeated traverse stays shut (monotone)");
    CHECK(wl_is_open(&L, wl_id(10, 6)), "T2 neighbor untouched");
    CHECK(wl_clear(&L, id) == 1 && wl_is_open(&L, id), "T2 CLEAR is the only reopen");

    /* T3 reserved signature: major-only ids refuse traverse and stay open */
    CHECK(wl_traverse(&L, 0) == 0, "T3 traverse(id 0) refused");
    CHECK(wl_traverse(&L, 10367) == 0, "T3 traverse(10367) refused");
    CHECK(wl_is_open(&L, 0) && wl_is_open(&L, 10367), "T3 reserved stay open");
    CHECK(wl_traverse(&L, WL_COUNT) == 0, "T3 out-of-range refused");

    /* T4 replay = recompute: P3 log bytes -> fresh latch == in-memory stepwise state */
    wl_latch_t live;
    wl_reset(&live);
    uint32_t a = wl_id(1, 1), b = wl_id(2, 3), c = wl_id(142, 71); /* 73, 147, 10295 */
    wl_traverse(&live, a);
    wl_traverse(&live, b);
    wl_clear(&live, a);      /* a reopens via CLEAR, b stays shut */
    uint8_t buf[5 * IR_LOG_RECORD_SIZE];
    IR_log_record_t r;
    ir_log_pack(&r, a, WL_NAME_HASH, 0, 0, IR_LOG_ACTION_WRITE);
    ir_log_serialize(&buf[0 * IR_LOG_RECORD_SIZE], &r);
    ir_log_pack(&r, b, WL_NAME_HASH, 0, 0, IR_LOG_ACTION_WRITE);
    ir_log_serialize(&buf[1 * IR_LOG_RECORD_SIZE], &r);
    ir_log_pack(&r, a, WL_NAME_HASH, 0, 0, IR_LOG_ACTION_CLEAR);
    ir_log_serialize(&buf[2 * IR_LOG_RECORD_SIZE], &r);
    /* foreign record (normal identity event) must be ignored by the latch */
    ir_log_pack(&r, c, 0xdeadbeefcafeull, 0, 0, IR_LOG_ACTION_WRITE);
    ir_log_serialize(&buf[3 * IR_LOG_RECORD_SIZE], &r);
    /* out-of-range slot must be ignored too */
    ir_log_pack(&r, 999999u, WL_NAME_HASH, 0, 0, IR_LOG_ACTION_WRITE);
    ir_log_serialize(&buf[4 * IR_LOG_RECORD_SIZE], &r);

    wl_latch_t replayed;
    wl_replay(&replayed, buf, sizeof buf);
    CHECK(memcmp(&replayed, &live, sizeof live) == 0, "T4 replay == stepwise in-memory (bit-identical)");
    CHECK(wl_is_open(&replayed, a), "T4 CLEAR in log reopens a");
    CHECK(!wl_is_open(&replayed, b), "T4 WRITE in log shuts b");
    CHECK(wl_is_open(&replayed, c), "T4 foreign name_hash ignored");
    wl_replay(&replayed, buf, sizeof buf - 5); /* misaligned size: resets, applies nothing */
    CHECK(memcmp(&replayed, &live, sizeof live) != 0, "T4 misaligned buffer applies nothing");
    (void)seen;

    /* T5 — SURVIVE RESTART: the fingerprint is appended to a P3 log file by
     * wl_traverse_log, and a FRESH latch rebuilt from that file on boot must
     * equal the one we walked in memory. This is the whole "tombstone index":
     * the walk is learned, written down, and read back — no pre-placed map. */
    {
        const char *path = "build/t5_wl_restart.log";
        remove(path);
        wl_latch_t walked; wl_reset(&walked);
        wl_log_t lg;
        CHECK(wl_log_open(&lg, path) == 0, "T5 log opens for append");
        /* header written once (2 magic + 1 version) */
        FILE *hf = fopen(path, "rb");
        long hsz = 0; if (hf) { fseek(hf, 0, SEEK_END); hsz = ftell(hf); fclose(hf); }
        CHECK(hsz == 3, "T5 fresh log = 3-byte header only");

        uint32_t ids[5] = { wl_id(0, 1), wl_id(3, 0), wl_id(77, 40),
                            wl_id(142, 71), wl_id(5, 9) };
        for (int i = 0; i < 5; i++)
            CHECK(wl_traverse_log(&walked, ids[i], &lg) == 1, "T5 traverse+append accepted");
        /* a reserved id must be refused AND write nothing */
        CHECK(wl_traverse_log(&walked, 0, &lg) == 0, "T5 reserved id refused (not appended)");
        CHECK(wl_traverse_log(&walked, 10367u, &lg) == 0, "T5 reserved HI id refused too");
        CHECK(lg.n_appended == 5, "T5 exactly 5 fingerprints appended");
        fflush(lg.fp);          /* append is buffered; flush = the serve-path contract */
        wl_log_close(&lg);

        FILE *cf = fopen(path, "rb");
        long csz = 0; if (cf) { fseek(cf, 0, SEEK_END); csz = ftell(cf); fclose(cf); }
        CHECK(csz == 3 + 5 * IR_LOG_RECORD_SIZE, "T5 file = header + 5 x 32B records");

        /* "restart": fresh latch, replay the file (the boot path) */
        wl_latch_t booted;
        long applied = wl_replay_log_file(&booted, path);
        CHECK(applied == 5, "T5 boot replay applied all 5 records");
        CHECK(memcmp(&booted, &walked, sizeof walked) == 0,
              "T5 rebooted latch == pre-restart latch (bit-identical)");
        for (int i = 0; i < 5; i++)
            CHECK(!wl_is_open(&booted, ids[i]), "T5 walked branch is SHUT after restart");
        CHECK(wl_is_open(&booted, wl_id(9, 9)), "T5 un-walked branch still OPEN after restart");

        /* reopening an existing log APPENDS (never truncates) — second run of
         * the same route adds nothing to state but stays consistent */
        CHECK(wl_log_open(&lg, path) == 0, "T5 reopen existing log for append");
        CHECK(lg.n_appended == 0, "T5 reopen starts a fresh append counter");
        wl_traverse_log(&walked, wl_id(9, 9), &lg);
        fflush(lg.fp);
        wl_log_close(&lg);
        wl_latch_t booted2;
        long applied2 = wl_replay_log_file(&booted2, path);
        CHECK(applied2 == 6, "T5 reopen appended (6 records total, not truncated)");
        CHECK(!wl_is_open(&booted2, wl_id(9, 9)), "T5 newly appended branch is SHUT");
        CHECK(memcmp(&booted2, &walked, sizeof walked) == 0,
              "T5 second-generation replay still bit-identical");

        remove(path);
    }

    /* T6 — the serve-path append discipline: a bucket must be logged ONLY on
     * its OPEN->SHUT transition, never on a repeat walk. Without the guard the
     * P3 log grows without bound (measured: 5661 records for 7 distinct slots,
     * 808x). This reproduces the guard both servers must use. */
    {
        const char *path = "wlt_t6.txt";
        remove(path);
        wl_latch_t L6; wl_reset(&L6);
        wl_log_t lg6;
        CHECK(wl_log_open(&lg6, path) == 0, "T6 log opens");
        uint32_t ids6[3] = { wl_id(20, 1), wl_id(30, 2), wl_id(40, 3) };
        for (int rep = 0; rep < 4; rep++)          /* walk the same 3 branches 4x */
            for (int i = 0; i < 3; i++) {
                uint32_t id = ids6[i];
                if (wl_is_open(&L6, id))            /* the guard under test */
                    wl_traverse_log(&L6, id, &lg6);
            }
        CHECK(lg6.n_appended == 3, "T6 guard appends 3 records for 3 branches over 4 repeats");
        fflush(lg6.fp);
        wl_log_close(&lg6);
        wl_latch_t r6;
        CHECK(wl_replay_log_file(&r6, path) == 3, "T6 replay applies exactly 3 records");
        for (int i = 0; i < 3; i++)
            CHECK(!wl_is_open(&r6, ids6[i]), "T6 branch SHUT after guarded walk");
        remove(path);
    }

    /* T7 — CELL GEOMETRY (phase-2): the flat cell reads as (pair<<1)|end and the
     * hourglass centre crossing is one XOR. Expectations from T14/T15 math, not
     * from the header's output: wl_cell_of is a bijection on [0,144) (with the
     * negative/wrap normalisation), and wl_cell_cross is a self-inverse
     * involution with 72 pairs and 0 fixed points. */
    {
        static unsigned char cseen[144];
        memset(cseen, 0, sizeof cseen);
        int bij = 1, wrap = 1;
        for (int c = 0; c < 144; c++) {
            uint32_t cell = wl_cell_of(c);
            if (cell >= 144u || cseen[cell]) bij = 0; else cseen[cell] = 1;
            if (wl_cell_of(c + 144) != cell || wl_cell_of(c - 144) != cell) wrap = 0;
        }
        CHECK(bij, "T7 wl_cell_of bijective over [0,144)");
        CHECK(wrap, "T7 wl_cell_of wraps +/-144 (mod-144 normalisation)");
        CHECK(wl_cell_of(-1) == 143u && wl_cell_of(144) == 0u, "T7 negative/overflow normalised");

        int inv = 1, fixed = 0, pairs = 0;
        for (uint32_t c = 0; c < 144u; c++) {
            uint32_t k = wl_cell_cross(c);
            if (wl_cell_cross(k) != c) inv = 0;      /* self-inverse  */
            if (k == c) fixed++;                      /* 0 fixed points */
            if (k > c) pairs++;                       /* count pairs once */
        }
        CHECK(inv, "T7 wl_cell_cross self-inverse on [0,144)");
        CHECK(fixed == 0 && pairs == 72, "T7 72 pairs, 0 fixed (== T14 d^1)");
    }

    /* T8 — G2.5: the two ends of an hourglass pair are INDEPENDENT cells.
     * Closing +X must leave -X OPEN and closing -X must leave +X open; the
     * crossing reaches exactly the partner cell and nothing else. Without this
     * a 3-PAIR collapse would falsely close both ends on one walk. The reserved
     * major ids are excluded from the minor pool, so exactly one end of the
     * pair set (id 0 = all-open major input) is not minor-latchable. */
    {
        int indep = 1, partner_ok = 1, reserved = 0, minors = 0;
        for (uint32_t p = 0; p < 72u; p++) {
            uint32_t cell0 = (p << 1) | 0u, cell1 = (p << 1) | 1u;
            uint32_t e0 = wl_id(cell0, 0u), e1 = wl_id(cell1, 0u);
            if (wl_cell_cross(cell0) != cell1 || e0 == e1) partner_ok = 0;
            int r0 = wl_is_reserved(e0), r1 = wl_is_reserved(e1);
            reserved += r0 + r1; minors += (!r0) + (!r1);
            if (!r0) {                            /* close +X on a fresh pair */
                wl_latch_t P; wl_reset(&P);
                wl_traverse(&P, e0);
                if (wl_is_open(&P, e0))  indep = 0;   /* +X closed      */
                if (!wl_is_open(&P, e1)) indep = 0;   /* -X must stay open */
            }
            if (!r1) {                            /* mirror: close -X only */
                wl_latch_t Q; wl_reset(&Q);
                wl_traverse(&Q, e1);
                if (wl_is_open(&Q, e1))  indep = 0;   /* -X closed      */
                if (!wl_is_open(&Q, e0)) indep = 0;   /* +X must stay open */
            }
        }
        CHECK(indep, "T8 closing one end leaves the other open (ends independent, G2.5)");
        CHECK(partner_ok, "T8 wl_cell_cross reaches exactly the pair partner");
        CHECK(reserved == 1, "T8 exactly 1 reserved end (id 0 = major input) in the pair set");
        CHECK(minors == 143, "T8 143/144 ends are minor-latchable (cell 0 is major-only)");
    }

    /* T9 — PRODUCTION ids: an anchor BUCKET is latched through wl_bucket_id
     * (choice 1). The phase-2 review asked whether bucket 0 collides with the
     * apex reserved id; it did (id 0 = WL_RESERVED_LO), which made bucket 0
     * permanently un-shutt-able. Assert the fix at the id level. */
    {
        int inrange = 1, unreserved = 1, bij = 1, period = 1;
        for (int b = 0; b < 4096; b++) {
            uint32_t id = wl_bucket_id(b);
            if (id >= WL_COUNT) inrange = 0;
            if (wl_is_reserved((int)id)) unreserved = 0;
            if (wl_bucket_id(b % 144) != id) period = 0;
        }
        for (int a = 0; a < 144 && bij; a++)
            for (int b = a + 1; b < 144; b++)
                if (wl_bucket_id(a) == wl_bucket_id(b)) { bij = 0; break; }
        CHECK(inrange, "T9 bucket latch ids stay inside [0, WL_COUNT)");
        CHECK(unreserved, "T9 no bucket maps to a reserved id (bucket 0 included)");
        CHECK(period, "T9 bucket id follows cell = bucket % 144");
        CHECK(bij, "T9 144 buckets -> 144 distinct cells");

        wl_latch_t L; wl_reset(&L);
        CHECK(wl_traverse(&L, wl_bucket_id(0)) == 1, "T9 bucket 0 IS latchable (was id 0 = reserved)");
        CHECK(!wl_is_open(&L, wl_bucket_id(0)), "T9 bucket 0 reads SHUT after traverse");
        CHECK(wl_is_open(&L, wl_bucket_id(2)), "T9 neighbouring bucket still OPEN");
        wl_reset(&L);
        CHECK(wl_traverse(&L, wl_bucket_id(143)) == 1 &&
              wl_bucket_id(143) != WL_RESERVED_HI,
              "T9 cell 143 → 10297, never the major-output id");
    }

    /* T10 — G2.5 under the PRODUCTION ids: shutting one end of an hourglass
     * pair must leave the other end open for every bucket id, so a walk can
     * never collapse both ends of a 3-PAIR at once. */
    {
        int indep = 1;
        for (int b = 0; b < 144; b++) {
            wl_latch_t L; wl_reset(&L);
            uint32_t own = wl_bucket_id(b);
            uint32_t other = wl_bucket_id(wl_cell_cross(wl_cell_of(b)));
            wl_traverse(&L, own);
            if (wl_is_open(&L, own)) indep = 0;      /* own must shut   */
            if (!wl_is_open(&L, other)) indep = 0;   /* other stays open */
        }
        CHECK(indep, "T10 one end shut leaves the pair end open (production ids)");
    }

    printf(fails ? "FAIL %d\n" : "ALL PASS (%d)\n", fails);
    return fails != 0;
}
