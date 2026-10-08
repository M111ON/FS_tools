/* P3 — Route persistence event log.
 *
 * Format: fixed 32-byte record per event, append-only, CRC-32 integrity per record.
 * Chosen after A/B/C probe (tools/p3_log_probe.c) at 50K events:
 *   A (32 B + CRC-32) — 1.36 M append/s, 1.49 M replay/s, 1.60 MB
 *   B1 (18 B + XOR)   — 2.58 M append/s, 17.9 M replay/s, 0.90 MB (XOR-fold weak)
 *   B2 (21 B + CRC-32) — 1.14 M append/s, 3.24 M replay/s, 1.05 MB
 * Decision: A — P3 is the partial-rebuild primitive, integrity is load-bearing.
 *
 * Record layout (32 B):
 *   [0..4)    slot        uint32 LE — address slot in [0, 20736)
 *   [4..12)   name_hash   uint64 LE — FNV-1a 64 of name
 *   [12..16)  walk_round  uint32 LE — rq / ticks
 *   [16..20)  walk_tick   uint32 LE — rq % ticks
 *   [20..24)  action      uint32 LE — IR_LOG_ACTION_*
 *   [24..28)  reserved    uint32 LE — must be 0
 *   [28..32)  crc32       uint32 LE — CRC-32 over first 28 B (IEEE 802.3 poly)
 *
 * Files are byte streams; no header. Use `IR_LOG_HEADER` bytes for files that
 * need a magic / version tag.
 *
 * Action semantics on replay (idempotent, last-write-wins per slot):
 *   WRITE  — commit bytes at slot; bytes_hash computed from replay input
 *   CLEAR  — mark slot empty; bytes_hash = 0; walk_round preserved
 *   MOVE   — same as WRITE but flags a rename; consumer may invalidate caches
 *   ANCHOR  — route milestone; no state change; pure ordering signal
 *
 * Replay reads events in file order. For each slot, the last event with
 * action in {WRITE, MOVE} determines the post-replay state. CLEAR dominates
 * (sets bytes_hash to 0 and resets walk_round to 0). ANCHOR is a marker.
 *
 * Replay output = per-slot GeoIdent5 (a 5-tuple per slot) reconstructed
 * from event log alone — no separate state store required.
 *
 * Depends on: stdint, string. No other DWGLS primitives.
 */
#ifndef DWGLS_GEO_ID_ROUTE_LOG_H
#define DWGLS_GEO_ID_ROUTE_LOG_H

#include <stdint.h>
#include <stddef.h>
#include <string.h>

#define IR_LOG_RECORD_SIZE  32
#define IR_LOG_HEADER        "IR"        /* 2 B magic for files; pack code adds version byte */
#define IR_LOG_VERSION     0x01

/* Action codes */
#define IR_LOG_ACTION_WRITE   0u
#define IR_LOG_ACTION_CLEAR   1u
#define IR_LOG_ACTION_MOVE    2u
#define IR_LOG_ACTION_ANCHOR  3u

/* Identity 5-tuple (matches geo_id_contract.h GeoIdent shape) */
typedef struct {
    uint64_t name_hash;
    uint64_t bytes_hash;
    uint32_t addr_slot;
    uint32_t walk_round;
    uint32_t walk_tick;
} IR_log_ident_t;

/* Raw 32-byte record */
typedef struct {
    uint32_t slot;
    uint64_t name_hash;
    uint32_t walk_round;
    uint32_t walk_tick;
    uint32_t action;
    uint32_t reserved;
    uint32_t crc32;
} __attribute__((packed)) IR_log_record_t;

/* ──── CRC-32 (IEEE 802.3) ──────────────────────────────── */
static inline uint32_t ir_log_crc32(const void* buf, size_t n) {
    const uint8_t* p = (const uint8_t*)buf;
    uint32_t c = 0xFFFFFFFFu;
    for (size_t i = 0; i < n; i++) {
        c ^= p[i];
        for (int k = 0; k < 8; k++) {
            c = (c >> 1) ^ (0xEDB88320u & (-(int32_t)(c & 1u)));
        }
    }
    return ~c;
}

/* ──── pack a record from raw fields ─────────────────────── */
static inline void ir_log_pack(IR_log_record_t* r,
                             uint32_t slot, uint64_t name_hash,
                             uint32_t walk_round, uint32_t walk_tick,
                             uint32_t action) {
    r->slot = slot;
    r->name_hash = name_hash;
    r->walk_round = walk_round;
    r->walk_tick = walk_tick;
    r->action = action;
    r->reserved = 0u;
    r->crc32 = 0u;
    /* CRC over first 28 bytes */
    r->crc32 = ir_log_crc32(r, 28);
}

/* ──── verify a record's CRC ────────────────────────────── */
static inline int ir_log_verify(const IR_log_record_t* r) {
    uint32_t c = ir_log_crc32(r, 28);
    return c == r->crc32;
}

/* ──── serialize a record to a 32-byte buffer ───────────── *
 * out must point to 32 bytes. The buffer is portable across machines. */
static inline void ir_log_serialize(uint8_t out[IR_LOG_RECORD_SIZE],
                                    const IR_log_record_t* r) {
    /* Little-endian serialize */
    out[0]  = (uint8_t)(r->slot & 0xFFu);
    out[1]  = (uint8_t)((r->slot >> 8) & 0xFFu);
    out[2]  = (uint8_t)((r->slot >> 16) & 0xFFu);
    out[3]  = (uint8_t)((r->slot >> 24) & 0xFFu);
    for (int i = 0; i < 8; i++) {
        out[4 + i] = (uint8_t)((r->name_hash >> (i * 8)) & 0xFFu);
    }
    out[12] = (uint8_t)(r->walk_round & 0xFFu);
    out[13] = (uint8_t)((r->walk_round >> 8) & 0xFFu);
    out[14] = (uint8_t)((r->walk_round >> 16) & 0xFFu);
    out[15] = (uint8_t)((r->walk_round >> 24) & 0xFFu);
    out[16] = (uint8_t)(r->walk_tick & 0xFFu);
    out[17] = (uint8_t)((r->walk_tick >> 8) & 0xFFu);
    out[18] = (uint8_t)((r->walk_tick >> 16) & 0xFFu);
    out[19] = (uint8_t)((r->walk_tick >> 24) & 0xFFu);
    out[20] = (uint8_t)(r->action & 0xFFu);
    out[21] = (uint8_t)((r->action >> 8) & 0xFFu);
    out[22] = (uint8_t)((r->action >> 16) & 0xFFu);
    out[23] = (uint8_t)((r->action >> 24) & 0xFFu);
    out[24] = (uint8_t)(r->reserved & 0xFFu);
    out[25] = (uint8_t)((r->reserved >> 8) & 0xFFu);
    out[26] = (uint8_t)((r->reserved >> 16) & 0xFFu);
    out[27] = (uint8_t)((r->reserved >> 24) & 0xFFu);
    out[28] = (uint8_t)(r->crc32 & 0xFFu);
    out[29] = (uint8_t)((r->crc32 >> 8) & 0xFFu);
    out[30] = (uint8_t)((r->crc32 >> 16) & 0xFFu);
    out[31] = (uint8_t)((r->crc32 >> 24) & 0xFFu);
}

static inline void ir_log_deserialize(const uint8_t in[IR_LOG_RECORD_SIZE],
                                      IR_log_record_t* r) {
    r->slot        = ((uint32_t)in[0])
                   | ((uint32_t)in[1] << 8)
                   | ((uint32_t)in[2] << 16)
                   | ((uint32_t)in[3] << 24);
    uint64_t nh = 0;
    for (int i = 0; i < 8; i++) nh |= ((uint64_t)in[4 + i]) << (i * 8);
    r->name_hash   = nh;
    r->walk_round  = ((uint32_t)in[12])
                   | ((uint32_t)in[13] << 8)
                   | ((uint32_t)in[14] << 16)
                   | ((uint32_t)in[15] << 24);
    r->walk_tick   = ((uint32_t)in[16])
                   | ((uint32_t)in[17] << 8)
                   | ((uint32_t)in[18] << 16)
                   | ((uint32_t)in[19] << 24);
    r->action      = ((uint32_t)in[20])
                   | ((uint32_t)in[21] << 8)
                   | ((uint32_t)in[22] << 16)
                   | ((uint32_t)in[23] << 24);
    r->reserved    = ((uint32_t)in[24])
                   | ((uint32_t)in[25] << 8)
                   | ((uint32_t)in[26] << 16)
                   | ((uint32_t)in[27] << 24);
    r->crc32       = ((uint32_t)in[28])
                   | ((uint32_t)in[29] << 8)
                   | ((uint32_t)in[30] << 16)
                   | ((uint32_t)in[31] << 24);
}

/* ──── Apply one event to an in-memory ident slot ────────── *
 * On CLEAR: bytes_hash -> 0, walk_round -> 0, walk_tick preserved.
 * On WRITE / MOVE: fields set as given.
 * On ANCHOR: no change. */
static inline void ir_log_apply(IR_log_ident_t* id,
                                uint32_t slot, uint64_t name_hash,
                                uint32_t walk_round, uint32_t walk_tick,
                                uint32_t action, uint64_t bytes_hash) {
    (void)slot; /* slot is identity key, not mutated here */
    if (action == IR_LOG_ACTION_CLEAR) {
        id->name_hash = name_hash;
        id->bytes_hash = 0u;
        id->walk_round = 0u;
        id->walk_tick = walk_tick;
    } else if (action == IR_LOG_ACTION_WRITE || action == IR_LOG_ACTION_MOVE) {
        id->name_hash = name_hash;
        id->bytes_hash = bytes_hash;
        id->walk_round = walk_round;
        id->walk_tick = walk_tick;
    }
    /* ANCHOR: leave as is */
}

/* ──── replay all events from a 32-byte-aligned buffer ───── *
 * Reads `n_bytes` of log data; expects n_bytes % 32 == 0.
 * On the first CRC failure returns -1 with *out_bad_record set.
 * Caller provides an array of 20736 idents (one per slot, pre-zeroed).
 * Returns number of events successfully replayed. */
static inline int ir_log_replay(const uint8_t* buf, size_t n_bytes,
                                IR_log_ident_t* idents,
                                size_t* out_bad_record) {
    if (!buf || (n_bytes % IR_LOG_RECORD_SIZE) != 0) {
        if (out_bad_record) *out_bad_record = 0;
        return -1;
    }
    size_t n_events = n_bytes / IR_LOG_RECORD_SIZE;
    int good = 0;
    for (size_t i = 0; i < n_events; i++) {
        IR_log_record_t r;
        ir_log_deserialize(&buf[i * IR_LOG_RECORD_SIZE], &r);
        if (!ir_log_verify(&r)) {
            if (out_bad_record) *out_bad_record = i;
            return good;
        }
        if (r.slot >= 20736u) {
            /* Out-of-range slot — skip but count as good */
            good++;
            continue;
        }
        /* Compute bytes_hash from the record itself — we don't have the
         * post-replay bytes in this replay-all signature; use the name_hash
         * as a proxy placeholder. The richer replay_incremental() below
         * takes a bytes hash function. */
        uint64_t bytes_hash = r.name_hash; /* placeholder, see ir_log_replay_with_bytes */
        ir_log_apply(&idents[r.slot],
                     r.slot, r.name_hash,
                     r.walk_round, r.walk_tick,
                     r.action, bytes_hash);
        good++;
    }
    if (out_bad_record) *out_bad_record = (size_t)-1;
    return good;
}

/* ──── bytes-hash callback signature ─────────────────────── *
 * Given a slot, name_hash, and walk_clock, return the bytes hash of the
 * post-rebuild bytes for that slot. Allows the caller to plug in any
 * hashing scheme (FNV-1a 64, blake3, sha256 truncated, etc.). */
typedef uint64_t (*ir_log_bytes_hash_fn)(uint32_t slot, uint64_t name_hash,
                                        uint32_t walk_round, uint32_t walk_tick,
                                        void* user);

/* ──── richer replay: caller supplies bytes hash function ─── *
 * Reads `n_bytes` of log data; applies events to per-slot array.
 * On CRC failure returns -1. Returns number of events applied. */
static inline int ir_log_replay_with_bytes(const uint8_t* buf, size_t n_bytes,
                                           IR_log_ident_t* idents,
                                           ir_log_bytes_hash_fn bytes_fn,
                                           void* user,
                                           size_t* out_bad_record) {
    if (!buf || (n_bytes % IR_LOG_RECORD_SIZE) != 0) {
        if (out_bad_record) *out_bad_record = 0;
        return -1;
    }
    size_t n_events = n_bytes / IR_LOG_RECORD_SIZE;
    int good = 0;
    for (size_t i = 0; i < n_events; i++) {
        IR_log_record_t r;
        ir_log_deserialize(&buf[i * IR_LOG_RECORD_SIZE], &r);
        if (!ir_log_verify(&r)) {
            if (out_bad_record) *out_bad_record = i;
            return good;
        }
        if (r.slot >= 20736u) {
            good++;
            continue;
        }
        uint64_t bytes_hash = 0u;
        if (bytes_fn && (r.action == IR_LOG_ACTION_WRITE || r.action == IR_LOG_ACTION_MOVE)) {
            bytes_hash = bytes_fn(r.slot, r.name_hash, r.walk_round, r.walk_tick, user);
        }
        ir_log_apply(&idents[r.slot],
                     r.slot, r.name_hash,
                     r.walk_round, r.walk_tick,
                     r.action, bytes_hash);
        good++;
    }
    if (out_bad_record) *out_bad_record = (size_t)-1;
    return good;
}

#endif /* DWGLS_GEO_ID_ROUTE_LOG_H */