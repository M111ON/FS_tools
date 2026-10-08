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

    printf(fails ? "FAIL %d\n" : "ALL PASS (%d)\n", fails);
    return fails != 0;
}
