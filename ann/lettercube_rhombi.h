/*
 * lettercube_rhombi.h — face-pair bonding for Great / Small Rhombicuboctahedron
 *
 * Both solids: 26 faces = 13 antipodal pairs = directions {-1,0,1}^3 \ 0
 *   kind 1 (axial,  6) : Great=octagon  Small=square
 *   kind 2 (edge,  12) : Great=square   Small=square
 *   kind 3 (corner, 8) : Great=hexagon  Small=triangle
 *
 * Face code (6 bit): 2 bits/axis, 00=0 01=+1 10=-1 (11 invalid, 000000 = centre)
 *   tight partner  = LCR_COMP(code)             (bit swap, offset = 0)
 *   expanded       = -v + offset  (mod 3/axis)  (involution, O(1) via table)
 *   offset != 0 -> 12 pairs + 1 SELF face (-offset) + 1 FREE face (offset)
 *   offset  = 0 -> 13 tight pairs. Reversible: set offset back to 0.
 *
 * Layers (Lego): L1 encoding | L2 partner map | L3 bond/angle | L4 assemble | L5 lanes/io
 *                L6 levels + active mask + per-entry lean (soft assignment)
 *
 * Per-entry lean (L6):  entry = {face, offset};  true direction d = face + offset (mod 3/axis)
 *   offset 0 = sits on its true face.  Stored face = nearest ACTIVE face of the level;
 *   promote = re-bucket by d, no address remap, d is never lost.
 */
#ifndef LETTERCUBE_RHOMBI_H
#define LETTERCUBE_RHOMBI_H

#include <stdint.h>
#include <string.h>

/* ── CONFIG ───────────────────────────────────────────────────── */
#define LCR_SHAPE_GREAT  0
#define LCR_SHAPE_SMALL  1
#define LCR_FACES        26
#define LCR_PAIRS        13
#define LCR_LANES        6          /* 6 axial faces = 6 lanes */
#define LCR_CODES        64         /* 6-bit code space */
#define LCR_ANGLE_TOL    0          /* allowed cyclic angle distance for same-kind pair */
#define LCR_NONE         0xFF
#define LCR_SER_SZ       (2 + LCR_FACES)

#define LCR_FREE  0
#define LCR_PAIR  1
#define LCR_SELF  2

/* n-gon sides by [shape][kind]: Great {oct,sq,hex}, Small {sq,sq,tri} */
static const uint8_t LCR_SIDES[2][4] = { {0, 8, 4, 6}, {0, 4, 4, 3} };

/* ── L1: encoding ─────────────────────────────────────────────── */
#define LCR_COMP(c)  ((uint8_t)((((c) & 0x2A) >> 1) | (((c) & 0x15) << 1)))

static inline uint8_t lcr_vec(int x, int y, int z)       /* each -1/0/1 */
{
    static const uint8_t T[3] = {2, 0, 1};               /* -1,0,+1 -> trit */
    return (uint8_t)(T[x + 1] | (T[y + 1] << 2) | (T[z + 1] << 4));
}
static inline int lcr_offset_ok(uint8_t o)               /* 0 allowed */
{
    return o < LCR_CODES && !(o & (o >> 1) & 0x15);
}
static inline int lcr_valid(uint8_t c)                   /* a real face */
{
    return c != 0 && lcr_offset_ok(c);
}
static inline int lcr_kind(uint8_t c)                    /* nonzero axes: 1/2/3 */
{
    uint8_t x = (uint8_t)(c | (c >> 1));
    return (x & 1) + ((x >> 2) & 1) + ((x >> 4) & 1);
}

/* dense id <-> code tables (built once) */
static uint8_t LCR_FACE_CODE[LCR_FACES];
static uint8_t LCR_FACE_ID[LCR_CODES];
static int     LCR_TABLES_READY = 0;

static inline void lcr_tables_init(void)
{
    if (LCR_TABLES_READY) return;
    memset(LCR_FACE_ID, LCR_NONE, sizeof LCR_FACE_ID);
    int id = 0;
    for (int c = 1; c < LCR_CODES; c++)
        if (lcr_valid((uint8_t)c)) { LCR_FACE_CODE[id] = (uint8_t)c; LCR_FACE_ID[c] = (uint8_t)id++; }
    LCR_TABLES_READY = 1;
}

/* ── L2: partner map ──────────────────────────────────────────── */
/* [offset trit][face trit] -> (-t + o) mod 3 */
static const uint8_t LCR_NA[3][3] = { {0, 2, 1}, {1, 0, 2}, {2, 1, 0} };

static inline uint8_t lcr_partner_code(uint8_t c, uint8_t off)   /* c, off must be valid */
{
    uint8_t r = 0;
    for (int i = 0; i < 3; i++)
        r |= (uint8_t)(LCR_NA[(off >> (2 * i)) & 3][(c >> (2 * i)) & 3] << (2 * i));
    return r;                                                    /* 0 = centre = no partner */
}

typedef struct {
    uint8_t shape;
    uint8_t offset;
    uint8_t partner[LCR_CODES];   /* code -> partner code (0 = none) */
    uint8_t angle[LCR_CODES];     /* rotation of face, 0..sides-1 */
    uint8_t state[LCR_CODES];     /* LCR_FREE / PAIR / SELF */
} LCRCube;

typedef struct { uint8_t pairs, self_, free_, mismatch; } LCRStats;

static inline void lcr_build_partner(LCRCube *q)
{
    memset(q->partner, 0, sizeof q->partner);
    for (int c = 1; c < LCR_CODES; c++)
        if (lcr_valid((uint8_t)c)) q->partner[c] = lcr_partner_code((uint8_t)c, q->offset);
}

static inline void lcr_init(LCRCube *q, uint8_t shape)
{
    lcr_tables_init();
    memset(q, 0, sizeof *q);
    q->shape = shape ? LCR_SHAPE_SMALL : LCR_SHAPE_GREAT;
    lcr_build_partner(q);
}

/* offset = lcr_vec(...) or 0 for tight. Returns 1 ok, 0 invalid. Clears bonds. */
static inline int lcr_set_offset(LCRCube *q, uint8_t off)
{
    if (!lcr_offset_ok(off)) return 0;
    q->offset = off;
    lcr_build_partner(q);
    memset(q->state, LCR_FREE, sizeof q->state);
    return 1;
}

/* ── L3: bond / angle ─────────────────────────────────────────── */
static inline int lcr_sides(const LCRCube *q, uint8_t c) { return LCR_SIDES[q->shape][lcr_kind(c)]; }

static inline int lcr_set_angle(LCRCube *q, uint8_t c, uint8_t a)
{
    if (!lcr_valid(c)) return 0;
    q->angle[c] = (uint8_t)(a % lcr_sides(q, c));
    return 1;
}

static inline int lcr_angle_ok(const LCRCube *q, uint8_t a, uint8_t b)
{
    int n = lcr_sides(q, a);
    int d = ((int)q->angle[a] - (int)q->angle[b] + n) % n;
    if (n - d < d) d = n - d;
    return d <= LCR_ANGLE_TOL;
}

/* kind mismatch (only possible when offset != 0) = intentional break, angle ignored */
static inline int lcr_pair_ok(const LCRCube *q, uint8_t a, uint8_t b)
{
    if (!lcr_valid(a) || !lcr_valid(b)) return 0;
    if (q->partner[a] != b) return 0;
    if (a == b || lcr_kind(a) != lcr_kind(b)) return 1;
    return lcr_angle_ok(q, a, b);
}

/* ── L4: assemble / verify ────────────────────────────────────── */
static inline LCRStats lcr_stats(const LCRCube *q)
{
    LCRStats s = {0, 0, 0, 0};
    for (int c = 1; c < LCR_CODES; c++) {
        if (!lcr_valid((uint8_t)c)) continue;
        if (q->state[c] == LCR_PAIR) {
            if (c < q->partner[c]) { s.pairs++; if (lcr_kind((uint8_t)c) != lcr_kind(q->partner[c])) s.mismatch++; }
        } else if (q->state[c] == LCR_SELF) s.self_++;
        else s.free_++;
    }
    return s;
}

/* O(26): partner is direct lookup, no search. Returns stats. */
static inline LCRStats lcr_assemble(LCRCube *q)
{
    memset(q->state, LCR_FREE, sizeof q->state);
    for (int c = 1; c < LCR_CODES; c++) {
        if (!lcr_valid((uint8_t)c) || q->state[c] != LCR_FREE) continue;
        uint8_t p = q->partner[c];
        if (p == 0) continue;                                   /* faces onto centre: stays FREE */
        if (p == c) { q->state[c] = LCR_SELF; continue; }
        if (q->state[p] == LCR_FREE && lcr_pair_ok(q, (uint8_t)c, p))
            q->state[c] = q->state[p] = LCR_PAIR;
    }
    return lcr_stats(q);
}

/* 1 if every bondable face is bonded and states are consistent (angle-broken pairs fail) */
static inline int lcr_verify(const LCRCube *q)
{
    for (int c = 1; c < LCR_CODES; c++) {
        if (!lcr_valid((uint8_t)c)) continue;
        uint8_t p = q->partner[c];
        switch (q->state[c]) {
        case LCR_PAIR: if (p == c || q->partner[p] != c || q->state[p] != LCR_PAIR) return 0; break;
        case LCR_SELF: if (p != c) return 0; break;
        default:       if (p != 0) return 0; break;             /* FREE only allowed for centre-bound */
        }
    }
    return 1;
}

static inline int lcr_is_tight(const LCRCube *q)
{
    return q->offset == 0 && lcr_stats(q).pairs == LCR_PAIRS && lcr_verify(q);
}

/* ── L5: lanes (6 axial faces) + serialize ────────────────────── */
/* lane l: axis = l>>1, sign = l&1 (0:+ 1:-)  ->  complement lane = l^1 */
static inline uint8_t lcr_lane_code(uint8_t l)
{
    return (uint8_t)((1 + (l & 1)) << (2 * (l >> 1)));
}
static inline uint8_t lcr_lane_of(uint8_t c)
{
    if (!lcr_valid(c) || lcr_kind(c) != 1) return LCR_NONE;
    for (int ax = 0; ax < 3; ax++) {
        uint8_t t = (c >> (2 * ax)) & 3;
        if (t) return (uint8_t)(ax * 2 + (t == 2));
    }
    return LCR_NONE;
}

static inline void lcr_serialize(const LCRCube *q, uint8_t *buf)       /* LCR_SER_SZ bytes */
{
    buf[0] = q->shape;
    buf[1] = q->offset;
    for (int i = 0; i < LCR_FACES; i++) buf[2 + i] = q->angle[LCR_FACE_CODE[i]];
}

static inline int lcr_deserialize(LCRCube *q, const uint8_t *buf)
{
    lcr_init(q, buf[0]);
    if (!lcr_set_offset(q, buf[1])) return 0;
    for (int i = 0; i < LCR_FACES; i++) lcr_set_angle(q, LCR_FACE_CODE[i], buf[2 + i]);
    return 1;
}

/* ── L6: levels, active mask, per-entry lean ──────────────────── */
#define LCR_LEVELS        3
#define LCR_MAX_DEPTH     LCR_LEVELS
#define LCR_OFFSET_BYTES  1

static const uint8_t LCR_LEVEL_FACES[LCR_LEVELS] = { 6, 14, 26 };
/* active kinds per level as bitmask (bit k = kind k): L0 axial | L1 +corner | L2 +edge */
static const uint8_t LCR_LEVEL_KINDS[LCR_LEVELS] = { 1u << 1, (1u << 1) | (1u << 3), (1u << 1) | (1u << 2) | (1u << 3) };
/* promote when steps needed > cap. Sized for <=0.1% residual: L0 for 1M, L1/L2 for 10M */
static const uint8_t LCR_MAX_STEPS[LCR_LEVELS]   = { 13, 9, 7 };
/* probes per level (tune per level / as fraction of faces; B2 decides) */
static const uint8_t LCR_TOPK[LCR_LEVELS]        = { 2, 2, 2 };

typedef struct { uint8_t face, offset; } LCREntry;      /* 2 bytes per vector */

static inline int lcr_active(uint8_t level, uint8_t c)  /* Active Mask */
{
    return level < LCR_LEVELS && lcr_valid(c) && ((LCR_LEVEL_KINDS[level] >> lcr_kind(c)) & 1);
}

/* lean = a + o (mod 3 per axis). o=0 -> a itself. Needs valid-trit a, o. 0 = centre (invalid) */
static inline uint8_t lcr_lean(uint8_t a, uint8_t o)
{
    uint8_t r = 0;
    for (int i = 0; i < 3; i++)
        r |= (uint8_t)(((((a >> (2 * i)) & 3) + ((o >> (2 * i)) & 3)) % 3) << (2 * i));
    return r;
}

/* offset that makes lean(a, o) == t */
static inline uint8_t lcr_offset_for(uint8_t a, uint8_t t)
{
    uint8_t r = 0;
    for (int i = 0; i < 3; i++)
        r |= (uint8_t)(((((t >> (2 * i)) & 3) + 3 - ((a >> (2 * i)) & 3)) % 3) << (2 * i));
    return r;
}

/* deny offsets that point at inactive / non-existent faces (or centre) */
static inline int lcr_lean_ok(uint8_t level, uint8_t a, uint8_t o)
{
    if (!lcr_active(level, a) || !lcr_offset_ok(o)) return 0;
    return lcr_active(level, lcr_lean(a, o));
}

/* nearest active face of d at this level: zero axes from the top until active (static, deterministic) */
static inline uint8_t lcr_parent(uint8_t d, uint8_t level)
{
    uint8_t c = d;
    for (int ax = 2; ax >= 0 && !lcr_active(level, c); ax--)
        c &= (uint8_t)~(3u << (2 * ax));
    return c;
}

static inline LCREntry lcr_entry_make(uint8_t d, uint8_t level)    /* d = true direction code */
{
    LCREntry e = {0, 0};
    if (!lcr_valid(d) || level >= LCR_LEVELS) return e;
    e.face   = lcr_parent(d, level);
    e.offset = lcr_offset_for(e.face, d);
    return e;
}
static inline uint8_t  lcr_entry_dir(LCREntry e) { return lcr_lean(e.face, e.offset); }
static inline LCREntry lcr_entry_promote(LCREntry e, uint8_t new_level)
{
    return lcr_entry_make(lcr_entry_dir(e), new_level);
}

/* deterministic trigger: steps needed at this level exceed its cap */
static inline int lcr_need_promote(uint8_t level, uint8_t steps_needed)
{
    return level + 1 < LCR_LEVELS && steps_needed > LCR_MAX_STEPS[level];
}

#endif /* LETTERCUBE_RHOMBI_H */
