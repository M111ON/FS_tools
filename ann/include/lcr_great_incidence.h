/*
 * lcr_great_incidence.h — 26F / 48V / 72E / 144 incidences for the Great
 * rhombicuboctahedron, built from cube flags (v ⊂ e ⊂ f).
 *
 * Why flags: the Great has no integer-coordinate embedding (it needs 1+√2), so
 * any table built from Cartesian points is either irrational or silently wrong.
 * The flag structure is pure combinatorics and lands exactly on the LCR face
 * codes that lettercube_rhombi.h already uses:
 *
 *   6 octagons  ↔ axial  directions  (LCR kind 1)  ↔ 6 cube faces
 *  12 squares   ↔ edge   directions  (LCR kind 2)  ↔ 12 cube edges
 *   8 hexagons  ↔ corner directions  (LCR kind 3)  ↔ 8 cube corners
 *
 * A vertex is a flag (cube corner cv, cube edge axis ea, face axis fa) with
 * fa ≠ ea. 48 flags, every one a distinct point of the solid. Each flag is in
 * exactly one octagon, one square and one hexagon — that is vertex degree 3 and
 * the 144 = 3·48 = 2·72 identity, asserted in tests/test_lcr_great.c.
 *
 * Vertex id = cv*6 + ea*2 + side, side 0 = fa is the first remaining axis in the
 * cyclic order at cv (cyclic order is right-handed, so it flips with corner
 * parity — get this wrong and the edges stop pairing, which the test catches).
 */
#ifndef LCR_GREAT_INCIDENCE_H
#define LCR_GREAT_INCIDENCE_H

#include <stdint.h>
#include <string.h>

#define LGI_V        48
#define LGI_E        72
#define LGI_F        26
#define LGI_INC     144

#define LGI_OCT       6          /* faces  0..5  : axial  (kind 1, 8 sides) */
#define LGI_SQR      12          /* faces  6..17 : edge   (kind 2, 4 sides) */
#define LGI_HEX       8          /* faces 18..25 : corner (kind 3, 6 sides) */

#define LGI_MAXSIDES  8

typedef struct {
    uint8_t sides[LGI_F];                 /* per face: 8 / 4 / 6 */
    uint8_t verts[LGI_F][LGI_MAXSIDES];   /* cyclic order around the face */
    uint8_t lcr[LGI_F];                   /* LCR 6-bit face code */
} lgi_mesh;

/* ── cube combinatorics ───────────────────────────────────────────── */

static inline int lgi_bit(int cv, int axis) { return (cv >> axis) & 1; }

/* right-handed cyclic order of the 3 edges at cube corner cv */
static inline int lgi_cyc(int cv, int k)
{
    int even = ((lgi_bit(cv, 0) + lgi_bit(cv, 1) + lgi_bit(cv, 2)) & 1) == 0;
    static const int ord_even[3] = { 0, 1, 2 };
    static const int ord_odd[3]  = { 0, 2, 1 };
    return even ? ord_even[k] : ord_odd[k];
}

/* the axis that follows ea in the cyclic order at cv */
static inline int lgi_next_axis(int cv, int ea)
{
    for (int k = 0; k < 3; k++)
        if (lgi_cyc(cv, k) == ea) return lgi_cyc(cv, (k + 1) % 3);
    return -1;
}

static inline int lgi_vid(int cv, int ea, int fa)
{
    int side = (fa == lgi_next_axis(cv, ea)) ? 0 : 1;
    return cv * 6 + ea * 2 + side;
}

/* LCR code for a direction whose components are -1 / 0 / +1 */
static inline uint8_t lgi_code(int dx, int dy, int dz)
{
    static const uint8_t T[3] = { 2, 0, 1 };     /* -1,0,+1 -> trit */
    return (uint8_t)(T[dx + 1] | (T[dy + 1] << 2) | (T[dz + 1] << 4));
}
static inline int lgi_sgn(int bit) { return bit ? 1 : -1; }

/* ── build ───────────────────────────────────────────────────────── */

static inline void lgi_build(lgi_mesh *m)
{
    int i, k, cv, a, b, u, v;
    memset(m, 0, sizeof *m);

    /* ── 6 octagons: one per cube face (axis a, value b) ───────────── */
    for (a = 0; a < 3; a++) {
        u = (a + 1) % 3;
        v = (a + 2) % 3;
        for (b = 0; b < 2; b++) {
            int f = a * 2 + b;
            int dir[3] = { 0, 0, 0 };
            static const int corner_ij[4][2] = { {0,0}, {1,0}, {1,1}, {0,1} };
            dir[a] = lgi_sgn(b);
            m->sides[f] = 8;
            m->lcr[f]   = lgi_code(dir[0], dir[1], dir[2]);
            for (k = 0; k < 4; k++) {
                int c[3];
                int a_in  = (k & 1) ? u : v;
                int a_out = (k & 1) ? v : u;
                c[a] = b; c[u] = corner_ij[k][0]; c[v] = corner_ij[k][1];
                cv = c[0] | (c[1] << 1) | (c[2] << 2);
                m->verts[f][2 * k]     = (uint8_t)lgi_vid(cv, a_in,  a);
                m->verts[f][2 * k + 1] = (uint8_t)lgi_vid(cv, a_out, a);
            }
        }
    }

    /* ── 12 squares: one per cube edge (cv, axis a), cv has bit a == 0 ── */
    {
        int s = LGI_OCT;
        for (cv = 0; cv < 8; cv++) {
            for (a = 0; a < 3; a++) {
                if (lgi_bit(cv, a)) continue;            /* one id per edge */
                {
                    int cv2 = cv ^ (1 << a);
                    int p = -1, q = -1, dir[3] = { 0, 0, 0 };
                    for (k = 0; k < 3; k++)
                        if (k != a) { if (p < 0) p = k; else q = k; }
                    dir[p] = lgi_sgn(lgi_bit(cv, p));
                    dir[q] = lgi_sgn(lgi_bit(cv, q));
                    m->sides[s] = 4;
                    m->lcr[s]   = lgi_code(dir[0], dir[1], dir[2]);
                    m->verts[s][0] = (uint8_t)lgi_vid(cv,  a, p);
                    m->verts[s][1] = (uint8_t)lgi_vid(cv2, a, p);
                    m->verts[s][2] = (uint8_t)lgi_vid(cv2, a, q);
                    m->verts[s][3] = (uint8_t)lgi_vid(cv,  a, q);
                    s++;
                }
            }
        }
    }

    /* ── 8 hexagons: one per cube corner ───────────────────────────── */
    for (cv = 0; cv < 8; cv++) {
        int f = LGI_OCT + LGI_SQR + cv;
        int c0 = lgi_cyc(cv, 0), c1 = lgi_cyc(cv, 1), c2 = lgi_cyc(cv, 2);
        int dir[3];
        dir[0] = lgi_sgn(lgi_bit(cv, 0));
        dir[1] = lgi_sgn(lgi_bit(cv, 1));
        dir[2] = lgi_sgn(lgi_bit(cv, 2));
        m->sides[f] = 6;
        m->lcr[f]   = lgi_code(dir[0], dir[1], dir[2]);
        m->verts[f][0] = (uint8_t)lgi_vid(cv, c0, c1);
        m->verts[f][1] = (uint8_t)lgi_vid(cv, c2, c1);
        m->verts[f][2] = (uint8_t)lgi_vid(cv, c2, c0);
        m->verts[f][3] = (uint8_t)lgi_vid(cv, c1, c0);
        m->verts[f][4] = (uint8_t)lgi_vid(cv, c1, c2);
        m->verts[f][5] = (uint8_t)lgi_vid(cv, c0, c2);
    }
}

#endif /* LCR_GREAT_INCIDENCE_H */
