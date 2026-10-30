/* chain_ident.h — the composite identity of a container point, and a
 * measurement-only allocator audit.
 *
 * WHY THIS EXISTS (handoff 2026-10-30-LC-ANN, open decision 1)
 *   A letter is not an identity. The owner's rule: major roots run vertically,
 *   minor roots connect laterally through connectors, and each shape owns its
 *   own 52-symbol pool. So the same letter in two places is not a collision —
 *   it is two connectors that happen to share a symbol. Collision can only be
 *   decided from the full context:
 *
 *     (epoch, major, minor, conn_in, conn_out, shape, phase, level, path, offset)
 *
 *   There is deliberately NO global used-set here. Repeated letters are legal.
 *
 * TWO KINDS OF "SAME"
 *   same point   epoch + shape + phase + level + path. Two records landing on
 *                one point is not a fault — it is a merge (#7166: same place,
 *                different dimension, scoring reconciles them, nothing deleted).
 *   identical    every field, including the record id and the offset. Reserving
 *                that twice is a true duplicate.
 *
 * SHAPE PHASE IS NOT LEVEL
 *   phase = sides of the face polygon: 3 triangle, 4 square, 6 hexagon,
 *           8 octagon.
 *   level = LetterCube level, whose face counts are 6 / 14 / 26.
 *   Six means hexagon in one and level 0 in the other. They are separate
 *   fields, and the string form carries only the phase, so a decoder cannot
 *   read a phase as a level. Level stays container metadata.
 *
 * THE TWO ZEROS ARE DIFFERENT NAMESPACES
 *   CHAIN_MARK == 0 is a PATH DIGIT: a closed node, lookup stops there.
 *   CI_FREE    == 0 is a STATE: nothing reserved here yet.
 *   A path carrying 0 is classified CLOSED, never FREE. The audit never uses
 *   one 0 to mean both.
 *
 * Header-only, std-only, no allocation.
 */
#ifndef CHAIN_IDENT_H
#define CHAIN_IDENT_H

#include <stdint.h>
#include <string.h>
#include "chain_addr.h"

/* ── shape phase: sides of the face polygon, not face count ─────────────── */
#define CI_PHASE_TRI 3
#define CI_PHASE_SQ  4
#define CI_PHASE_HEX 6
#define CI_PHASE_OCT 8

static inline int chain_phase_ok(uint8_t p)
{
    return p == CI_PHASE_TRI || p == CI_PHASE_SQ ||
           p == CI_PHASE_HEX || p == CI_PHASE_OCT;
}

#define CI_LEVEL_MAX 2   /* L0/L1/L2 — see LCR_LEVEL_FACES {6,14,26} */

/* ── the identity ───────────────────────────────────────────────────────── */
typedef struct {
    uint32_t epoch;      /* container generation */
    uint32_t rec;        /* payload / record id: what makes two records differ */
    uint8_t  major;      /* major root id */
    uint8_t  minor;      /* minor root id; 0 = sitting on the major path */
    uint8_t  conn_in;    /* entry connector (letter value) */
    uint8_t  conn_out;   /* exit connector (letter value) */
    uint8_t  shape;      /* 0 great, 1 small (matches LCR_SHAPE_*) */
    uint8_t  phase;      /* sides 3/4/6/8 — NOT the level face count */
    uint8_t  level;      /* 0..CI_LEVEL_MAX */
    int16_t  offset;     /* distance from the exactly-named direction */
    ChainKey path;       /* local connector path */
} ChainIdent;

/* Where the record sits: epoch + root + shape + phase + level + path.
 * The root is part of the point, not decoration — two major roots that happen
 * to use the same letters are two different places (owner's correction). */
static inline int chain_ident_same_point(const ChainIdent *a, const ChainIdent *b)
{
    if (!a || !b) return 0;
    if (a->epoch != b->epoch) return 0;
    if (a->major != b->major || a->minor != b->minor) return 0;
    if (a->shape != b->shape) return 0;
    if (a->phase != b->phase) return 0;
    if (a->level != b->level) return 0;
    return chain_cmp(&a->path, &b->path) == 0;
}

/* Which record, and how it leans at that point. */
static inline int chain_ident_eq(const ChainIdent *a, const ChainIdent *b)
{
    if (!a || !b) return 0;
    if (a->rec != b->rec || a->offset != b->offset) return 0;
    if (a->conn_in != b->conn_in || a->conn_out != b->conn_out) return 0;
    return chain_ident_same_point(a, b);
}

static inline int chain_ident_ok(const ChainIdent *id)
{
    if (!id) return 0;
    if (!chain_valid(&id->path)) return 0;   /* closed / malformed path */
    if (!chain_phase_ok(id->phase)) return 0;
    if (id->level > CI_LEVEL_MAX) return 0;
    return 1;
}

/* ── fingerprint: field by field, never raw struct bytes (padding) ──────── */
static inline uint64_t chain_fnv(uint64_t h, const void *p, size_t n)
{
    const uint8_t *b = (const uint8_t *)p;
    for (size_t i = 0; i < n; i++) {
        h ^= b[i];
        h *= 1099511628211ULL;          /* FNV-1a 64 */
    }
    return h;
}

static inline uint64_t chain_ident_fp(const ChainIdent *id)
{
    uint64_t h = 14695981039346656037ULL;
    uint8_t  small[8];
    uint16_t off;
    if (!id) return 0;
    h = chain_fnv(h, &id->epoch, sizeof id->epoch);
    h = chain_fnv(h, &id->rec, sizeof id->rec);
    small[0] = id->major;  small[1] = id->minor;
    small[2] = id->conn_in; small[3] = id->conn_out;
    small[4] = id->shape;  small[5] = id->phase;
    small[6] = id->level;
    h = chain_fnv(h, small, 7);
    off = (uint16_t)id->offset;
    h = chain_fnv(h, &off, sizeof off);
    h = chain_fnv(h, &id->path.n, sizeof id->path.n);
    h = chain_fnv(h, id->path.d, id->path.n);
    return h;
}

/* ── state: distinct values, one meaning each ───────────────────────────── */
typedef enum {
    CI_FREE = 0,      /* nothing reserved at this point yet */
    CI_RESERVED = 1,  /* held, not written */
    CI_COMMITTED = 2, /* written */
    CI_MERGE = 3,     /* same point, different record — intended */
    CI_RESIDUAL = 4,  /* appended as residual at the same point */
    CI_CLOSED = 5,    /* path carries the 0 marker */
    CI_DUP = 6,       /* identical identity reserved again — a true duplicate */
    CI_FULL = 7       /* audit table exhausted */
} ChainIdentState;

#define CI_NSTATES 8

static inline const char *chain_state_name(int s)
{
    static const char *n[CI_NSTATES] = {
        "free", "reserved", "committed", "merge",
        "residual", "closed", "dup", "full"
    };
    return (s >= 0 && s < CI_NSTATES) ? n[s] : "?";
}

/* ── measurement-only audit ──────────────────────────────────────────────
 * Linear scan on purpose: this is the "record before changing generation"
 * step, not a production index. Raise CI_AUDIT_MAX (or swap in a hash index)
 * for a full-corpus run. Declare the struct static — it is tens of KB.
 */
#ifndef CI_AUDIT_MAX
#define CI_AUDIT_MAX 512
#endif

typedef struct {
    ChainIdent id[CI_AUDIT_MAX];
    uint64_t   fp[CI_AUDIT_MAX];
    uint8_t    st[CI_AUDIT_MAX];
    int        n;
    int        cnt[CI_NSTATES];
} ChainIdentAudit;

static inline void chain_audit_init(ChainIdentAudit *a)
{
    if (!a) return;
    memset(a, 0, sizeof *a);
}

static inline int chain_audit_find(const ChainIdentAudit *a, const ChainIdent *id,
                                   uint64_t fp)
{
    for (int i = 0; i < a->n; i++)
        if (a->fp[i] == fp && chain_ident_eq(&a->id[i], id)) return i;
    return -1;
}

static inline int chain_audit_find_point(const ChainIdentAudit *a, const ChainIdent *id)
{
    for (int i = 0; i < a->n; i++)
        if (chain_ident_same_point(&a->id[i], id)) return i;
    return -1;
}

/* Reserve an identity. `want` is CI_RESERVED or CI_RESIDUAL; anything else is
 * treated as CI_RESERVED. Returns the classification that actually happened. */
static inline int chain_audit_reserve(ChainIdentAudit *a, const ChainIdent *id,
                                      int want)
{
    uint64_t fp;
    int i;
    if (!a || !id) return CI_CLOSED;
    if (!chain_valid(&id->path)) { a->cnt[CI_CLOSED]++; return CI_CLOSED; }

    fp = chain_ident_fp(id);
    i = chain_audit_find(a, id, fp);
    if (i >= 0) {                      /* identical identity, reserved again */
        a->cnt[CI_DUP]++;
        return CI_DUP;
    }
    if (chain_audit_find_point(a, id) >= 0) {
        /* Same point, different record: legal, this is how two records share
         * a container point without anything being deleted (#7166). A caller
         * that says RESIDUAL is telling us what kind of share it is, and that
         * is more than the table can know — honour it. */
        if (a->n >= CI_AUDIT_MAX) { a->cnt[CI_FULL]++; return CI_FULL; }
        a->id[a->n] = *id;
        a->fp[a->n] = fp;
        a->st[a->n] = (uint8_t)((want == CI_RESIDUAL) ? CI_RESIDUAL : CI_MERGE);
        a->n++;
        a->cnt[a->st[a->n - 1]]++;
        return a->st[a->n - 1];
    }
    if (a->n >= CI_AUDIT_MAX) { a->cnt[CI_FULL]++; return CI_FULL; }

    a->id[a->n] = *id;
    a->fp[a->n] = fp;
    a->st[a->n] = (uint8_t)((want == CI_RESIDUAL) ? CI_RESIDUAL : CI_RESERVED);
    a->n++;
    a->cnt[a->st[a->n - 1]]++;
    return a->st[a->n - 1];
}

/* Promote a reservation to committed. Returns 0 when it is not on record. */
static inline int chain_audit_commit(ChainIdentAudit *a, const ChainIdent *id)
{
    int i;
    if (!a || !id) return 0;
    i = chain_audit_find(a, id, chain_ident_fp(id));
    if (i < 0) return 0;
    if (a->st[i] != CI_RESERVED && a->st[i] != CI_RESIDUAL) return 0;
    a->cnt[a->st[i]]--;
    a->st[i] = CI_COMMITTED;
    a->cnt[CI_COMMITTED]++;
    return 1;
}

/* Close the point a record sits on. The path digit 0 is the marker; closing is
 * a POINT operation, so every record sharing that point becomes unreachable —
 * leaving one of them open would mean the point is half closed. */
static inline int chain_audit_close(ChainIdentAudit *a, const ChainIdent *id)
{
    int hit = 0;
    if (!a || !id) return 0;
    for (int i = 0; i < a->n; i++) {
        if (!chain_ident_same_point(&a->id[i], id)) continue;
        hit = 1;
        if (a->st[i] == CI_CLOSED) continue;
        a->cnt[a->st[i]]--;
        a->st[i] = CI_CLOSED;
        a->cnt[CI_CLOSED]++;
    }
    return hit;
}

static inline int chain_audit_state_of(const ChainIdentAudit *a, const ChainIdent *id)
{
    int i;
    if (!a || !id) return CI_FREE;
    i = chain_audit_find(a, id, chain_ident_fp(id));
    return i < 0 ? CI_FREE : (int)a->st[i];
}

/* ── notation: path letters + one phase digit ────────────────────────────
 * "bHyN6" = connector path bHyN on a hexagon face. Level is NOT written: it
 * is container metadata, which is what keeps the 6 from being read as L0. */
static inline int chain_ident_str(const ChainIdent *id, char *buf, int cap)
{
    int n = 0;
    if (!id || !buf || cap <= 0 || !chain_valid(&id->path)) return 0;
    if (!chain_phase_ok(id->phase)) return 0;
    if (id->path.n + 2 > cap) return 0;
    for (uint8_t i = 0; i < id->path.n; i++) {
        char c = chain_letter_of(id->path.d[i]);
        if (c == '0') return 0;                /* marker is not a letter */
        buf[n++] = c;
    }
    buf[n++] = (char)('0' + id->phase);
    buf[n] = '\0';
    return n;
}

/* Parse back. Level is left untouched — the caller supplies it from context,
 * which is the point: the string never claims a level. */
static inline int chain_ident_parse(ChainIdent *id, const char *s)
{
    size_t len;
    uint8_t p;
    if (!id || !s) return 0;
    len = strlen(s);
    if (len < 2 || len > CHAIN_MAXL + 1) return 0;
    p = (uint8_t)(s[len - 1] - '0');
    if (s[len - 1] < '0' || s[len - 1] > '9' || !chain_phase_ok(p)) return 0;
    id->phase = p;
    id->path.n = 0;
    for (size_t i = 0; i < len - 1; i++) {
        uint8_t d = chain_digit_of(s[i]);
        if (d == CHAIN_MARK || d > CHAIN_BASE) return 0;
        id->path.d[id->path.n++] = d;
    }
    return 1;
}

#endif /* CHAIN_IDENT_H */
