#ifndef DWGLS_GEO_WANG_LATCH_H
#define DWGLS_GEO_WANG_LATCH_H
/* B-latch: one-way flow latch over the wang6 branch space (144 cell x 72 choice).
 *
 * Spec (owner 2026-10-04):
 *   - latch id = cell*72 + choice, cell in [0,144), choice in [0,72) -> [0,10368)
 *   - never traversed = OPEN; traversed = SHUT (monotone within an epoch)
 *   - only CLEAR reopens; epoch reset = CLEAR of touched ids (or replay from empty log)
 *   - reserved ids 0 (all-open = major input) and 10367 (all-shut = major output)
 *     are excluded from the minor pool -> 10366 free branches per anchor
 *   - 10368 x 2 = 20736: half-field identity of the GJ family
 *
 * Persistence: P3 route log (core/geo_id_route_log.h), no new file format.
 *   Records are tagged WL_NAME_HASH so latch events can never collide with
 *   regular identity records that reuse the slot field.
 *   action WRITE (slot=latch_id) = traversed -> SHUT
 *   action CLEAR (slot=latch_id) = reopen that id
 * The in-memory bitset below is a DERIVED cache only — replay = recompute
 * (computation-over-storage: nothing stored that cannot be rederived).
 *
 * mm_wang.h / mm_wang6.h stay untouched: color = "can this path go" (stateless),
 * latch = "has flow ever passed here" (state above the valve, never inside it).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "geo_id_route_log.h"

#define WL_COUNT       10368u
#define WL_RESERVED_LO 0u
#define WL_RESERVED_HI 10367u
#define WL_FREE        (WL_COUNT - 2u)   /* 10366 minor branches */
#define WL_WORDS       (WL_COUNT / 64u)  /* 162 */
#define WL_BYTES       (WL_WORDS * 8u)   /* 1296 B */

/* records tagged with this name_hash are latch events, not identity events */
#define WL_NAME_HASH   0x77616E676C617463ull  /* "wanglatc" */

typedef struct {
    uint64_t w[WL_WORDS];
} wl_latch_t;

static inline uint32_t wl_id(uint32_t cell, uint32_t choice) {
    return cell * 72u + choice;
}
static inline int wl_is_reserved(uint32_t id) {
    return id == WL_RESERVED_LO || id == WL_RESERVED_HI;
}
static inline void wl_reset(wl_latch_t *L) {
    memset(L, 0, sizeof *L);
}
static inline int wl_is_open(const wl_latch_t *L, uint32_t id) {
    if (id >= WL_COUNT) return 0;
    return (L->w[id >> 6] >> (id & 63u)) & 1u ? 0 : 1;
}
/* traverse: OPEN -> SHUT, monotone, idempotent. Reserved ids refused (major-only). */
static inline int wl_traverse(wl_latch_t *L, uint32_t id) {
    if (id >= WL_COUNT || wl_is_reserved(id)) return 0;
    L->w[id >> 6] |= 1ull << (id & 63u);
    return 1;
}
/* CLEAR: the only reopen. */
static inline int wl_clear(wl_latch_t *L, uint32_t id) {
    if (id >= WL_COUNT) return 0;
    L->w[id >> 6] &= ~(1ull << (id & 63u));
    return 1;
}

/* ──── P3 append sink (the FINGERPRINT) ───────────────────────────────
 * The bitset above is a DERIVED cache: it dies with the process. To make a
 * tombstone survive restart, every traverse must ALSO append its fingerprint
 * to the P3 route log, and boot replays the log into a fresh latch. That is
 * the whole "must train first": the latch is learned by walking, and the
 * walk is what gets written down.
 *
 * The pure wl_traverse stays side-effect-free (tests rely on it). Appending
 * is a separate call so a caller that does not want persistence pays nothing.
 *
 * File shape: IR_LOG_HEADER (2 B magic) + 1 version byte + 32-B records —
 * exactly what lz_fog_init() already replays (it skips buf+3).
 */
typedef struct {
    FILE    *fp;
    uint32_t round;   /* monotone walk round, stamped on each record */
    uint64_t n_appended;
} wl_log_t;

/* Open (or create) a P3 log for append. Writes the 3-byte header only when
 * the file is new/empty, so re-opening an existing log never corrupts it. */
static inline int wl_log_open(wl_log_t *lg, const char *path) {
    if (!lg || !path || !path[0]) return -1;
    lg->fp = NULL; lg->round = 0; lg->n_appended = 0;
    FILE *probe = fopen(path, "rb");
    long sz = 0;
    if (probe) { fseek(probe, 0, SEEK_END); sz = ftell(probe); fclose(probe); }
    FILE *fp = fopen(path, sz > 0 ? "ab" : "wb");
    if (!fp) return -1;
    if (sz == 0) {
        const uint8_t hdr[3] = { (uint8_t)IR_LOG_HEADER[0], (uint8_t)IR_LOG_HEADER[1],
                                 (uint8_t)IR_LOG_VERSION };
        if (fwrite(hdr, 1, 3, fp) != 3) { fclose(fp); return -1; }
        fflush(fp);   /* header must be visible to a reader opened right after */
    }
    lg->fp = fp;
    return 0;
}

static inline void wl_log_close(wl_log_t *lg) {
    if (lg && lg->fp) { fflush(lg->fp); fclose(lg->fp); lg->fp = NULL; }
}

/* traverse + append one fingerprint record. Returns the traverse result
 * (0 = refused reserved/out-of-range, so nothing is appended either). */
static inline int wl_traverse_log(wl_latch_t *L, uint32_t id, wl_log_t *lg) {
    if (!wl_traverse(L, id)) return 0;
    if (lg && lg->fp) {
        IR_log_record_t r;
        ir_log_pack(&r, id, WL_NAME_HASH, lg->round, (uint32_t)lg->n_appended,
                    IR_LOG_ACTION_WRITE);
        uint8_t buf[IR_LOG_RECORD_SIZE];
        ir_log_serialize(buf, &r);
        if (fwrite(buf, 1, IR_LOG_RECORD_SIZE, lg->fp) == IR_LOG_RECORD_SIZE)
            lg->n_appended++;
    }
    return 1;
}

/* replay a whole P3 log file into a fresh latch (boot path). Returns the
 * number of records applied, or -1 if the file could not be read. Header of
 * 3 bytes is skipped; a 0-byte/absent file yields an all-OPEN latch (0).
 * Defined after wl_replay(), which it calls. */
static inline long wl_replay_log_file(wl_latch_t *L, const char *path);

/* Apply one P3 record. Foreign name_hash / out-of-range slot = no-op. */
static inline void wl_apply_record(wl_latch_t *L, const IR_log_record_t *r) {
    if (r->name_hash != WL_NAME_HASH) return;
    if (r->action == IR_LOG_ACTION_WRITE)      wl_traverse(L, r->slot);
    else if (r->action == IR_LOG_ACTION_CLEAR) wl_clear(L, r->slot);
}
/* Replay = recompute: fresh latch + byte buffer -> state. */
static inline void wl_replay(wl_latch_t *L, const uint8_t *buf, size_t n_bytes) {
    wl_reset(L);
    if (!buf || (n_bytes % IR_LOG_RECORD_SIZE) != 0) return;
    for (size_t i = 0; i + IR_LOG_RECORD_SIZE <= n_bytes; i += IR_LOG_RECORD_SIZE) {
        IR_log_record_t r;
        ir_log_deserialize(buf + i, &r);
        wl_apply_record(L, &r);
    }
}

/* replay a whole P3 log file into a fresh latch (boot path). Returns the
 * number of records applied, or -1 if the file could not be read. Header of
 * 3 bytes is skipped; a 0-byte/absent file yields an all-OPEN latch (0). */
static inline long wl_replay_log_file(wl_latch_t *L, const char *path) {
    wl_reset(L);
    if (!path || !path[0]) return 0;
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    fseek(f, 0, SEEK_END); long n = ftell(f); fseek(f, 0, SEEK_SET);
    if (n <= 3) { fclose(f); return 0; }
    uint8_t *buf = (uint8_t *)malloc((size_t)n);
    if (!buf) { fclose(f); return -1; }
    size_t got = fread(buf, 1, (size_t)n, f);
    fclose(f);
    long applied = 0;
    if (got > 3 && ((got - 3) % IR_LOG_RECORD_SIZE) == 0) {
        wl_replay(L, buf + 3, got - 3);
        applied = (long)((got - 3) / IR_LOG_RECORD_SIZE);
    }
    free(buf);
    return applied;
}

#endif /* DWGLS_GEO_WANG_LATCH_H */
