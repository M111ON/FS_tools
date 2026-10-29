/* test_lcr_great.c — the 26/48/72/144 incidence table must be a real solid.
 *
 * Checks (all structural, no coordinates):
 *   T1  face sizes      : 6 octagons + 12 squares + 8 hexagons
 *   T2  vertices        : all 48 used, each in exactly 3 faces
 *   T3  edges           : derived from face cycles -> exactly 72, each in exactly 2 faces
 *   T4  Euler           : V - E + F = 2
 *   T5  incidences      : 144 = sum(sides) = 3V = 2E
 *   T6  LCR face codes  : 26 distinct, kinds 6/12/8, sides match LCR_SIDES[GREAT]
 *
 * build: gcc -O2 -I../include -o test_lcr_great.exe test_lcr_great.c
 */
#include <stdio.h>
#include <string.h>
#include "../include/lcr_great_incidence.h"
#include "../lettercube_rhombi.h"

static int fails;

static void ck(const char *what, int ok, const char *detail)
{
    printf("  %-34s %s%s%s\n", what, ok ? "PASS" : "FAIL",
           detail ? "  " : "", detail ? detail : "");
    if (!ok) fails++;
}

/* edge table: key = lo*64 + hi */
#define EKEY(a, b) ((a) < (b) ? (a) * 64 + (b) : (b) * 64 + (a))

int main(void)
{
    lgi_mesh m;
    int f, k, i;
    int n_oct = 0, n_sqr = 0, n_hex = 0;
    int vface[LGI_V];
    int ecount[LGI_V * 64];
    int n_edges = 0, inc = 0, bad_edge = 0, bad_vface = 0;

    lgi_build(&m);
    memset(vface, 0, sizeof vface);
    memset(ecount, 0, sizeof ecount);

    printf("=== LCR Great rhombicuboctahedron incidence ===\n");

    /* T1 face sizes */
    for (f = 0; f < LGI_F; f++) {
        if (m.sides[f] == 8) n_oct++;
        else if (m.sides[f] == 4) n_sqr++;
        else if (m.sides[f] == 6) n_hex++;
    }
    {
        char d[64];
        snprintf(d, sizeof d, "oct=%d sqr=%d hex=%d", n_oct, n_sqr, n_hex);
        ck("T1 face sizes 6/12/8", n_oct == 6 && n_sqr == 12 && n_hex == 8, d);
    }

    /* T2 vertex usage + T3 edges + T5 incidences */
    for (f = 0; f < LGI_F; f++) {
        int n = m.sides[f];
        inc += n;
        for (k = 0; k < n; k++) {
            int a = m.verts[f][k];
            int b = m.verts[f][(k + 1) % n];
            vface[a]++;
            if (a >= LGI_V || b >= LGI_V) bad_edge++;
            ecount[EKEY(a, b)]++;
        }
    }
    for (i = 0; i < LGI_V; i++) if (vface[i] != 3) bad_vface++;
    for (i = 0; i < LGI_V * 64; i++) if (ecount[i]) {
        n_edges++;
        if (ecount[i] != 2) bad_edge++;
    }
    ck("T2 every vertex in 3 faces", bad_vface == 0, NULL);
    {
        char d[64];
        snprintf(d, sizeof d, "edges=%d bad=%d", n_edges, bad_edge);
        ck("T3 72 edges, each in 2 faces", n_edges == LGI_E && bad_edge == 0, d);
    }
    ck("T4 Euler V-E+F=2", LGI_V - n_edges + LGI_F == 2, NULL);
    {
        char d[96];
        snprintf(d, sizeof d, "sum(sides)=%d 3V=%d 2E=%d", inc, 3 * LGI_V, 2 * n_edges);
        ck("T5 incidences 144", inc == LGI_INC && inc == 3 * LGI_V && inc == 2 * n_edges, d);
    }

    /* T6 LCR face codes — ties the table to lettercube_rhombi.h */
    {
        int seen[64], dup = 0, kind_ok = 1, sides_ok = 1;
        memset(seen, 0, sizeof seen);
        for (f = 0; f < LGI_F; f++) {
            uint8_t c = m.lcr[f];
            int kind, want;
            if (!lcr_valid(c) || seen[c]) dup++;
            seen[c] = 1;
            kind = lcr_kind(c);
            if (!((f < LGI_OCT && kind == 1) ||
                  (f < LGI_OCT + LGI_SQR && kind == 2) ||
                  (f >= LGI_OCT + LGI_SQR && kind == 3))) kind_ok = 0;
            want = LCR_SIDES[LCR_SHAPE_GREAT][kind];
            if (want != m.sides[f]) sides_ok = 0;
        }
        ck("T6 26 distinct LCR codes", dup == 0, NULL);
        ck("T6 kind 1/2/3 = oct/sqr/hex", kind_ok, NULL);
        ck("T6 sides match LCR_SIDES", sides_ok, NULL);
    }

    printf("\n%s (%d failing checks)\n", fails ? "FAIL" : "PASS", fails);
    return fails ? 1 : 0;
}
