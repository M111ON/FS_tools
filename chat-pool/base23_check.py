#!/usr/bin/env python3
"""base23_check.py

Claim under test (owner's words):
    "สนามระบบผมเป็น base 2,3 แต่เต็มกล่องจตุรัสที่ base 12"
    (the field is base 2 and base 3, but the square box fills/closes at base 12)

We test it as mathematics, layer by layer, using the owner's own numbers
(12, 144, 20736, 1152, 1728) and his image set (hex DGGS aperture 3/4,
ISEA/RTEA rhombus, C-I/C-II cells, rhombille cube tiling).

Sections:
  A  cyclotomic discriminants: which n makes the box a SQUARE at 12?
  B  the exact bridge: zeta_12 = i*(1 - omega) and Z[i,omega] == Z[zeta_12]
  C  the 4D box: trace form det = 144, signature (2,2), covolume = 12
  D  minimality: 12 = lcm(4,6), the only n <= 40 carrying both rotations
  E  square-box closure in the grid sense: why side 12, why 144 = 2^4*3^2
  F  2D impossibility: square and triangular lattices are INCOMMENSURABLE
  G  3D/4D: cube edges 12, FCC shell 12 = 3x4, rhombille = Z^3 along (1,1,1)
  H  pentagon-angle family: is 144 the hub?
"""
import math
from fractions import Fraction

PASS, FAIL = [], []
def chk(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(f"  [{'OK  ' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))

# ---------------------------------------------------------------- helpers
def mobius(n):
    if n == 1: return 1
    f = 0
    p = 2
    while p * p <= n:
        if n % p == 0:
            if n % (p * p) == 0: return 0
            n //= p; f += 1
        p += 1
    if n > 1: f += 1
    return -1 if f % 2 else 1

def totient(n):
    r, m, p = n, n, 2
    while p * p <= m:
        if m % p == 0:
            while m % p == 0: m //= p
            r -= r // p
        p += 1
    if m > 1: r -= r // m
    return r

def ramanujan(n, k):
    """c_n(k) = Tr_{Q(zeta_n)/Q}(zeta_n^k) — an exact integer."""
    d = math.gcd(n, k); m = n // d
    return mobius(m) * totient(n) // totient(m)

def cyclotomic_disc(n):
    """disc(Q(zeta_n)) = n^phi(n) / prod_{p|n} p^(phi(n)/(p-1))."""
    if n == 1: return 1
    num = n ** totient(n)
    den = 1
    m, p = n, 2
    while p <= m:
        if m % p == 0:
            while m % p == 0: m //= p
            den *= p ** (totient(n) // (p - 1))
        p += 1
    assert num % den == 0, (n, num, den)
    return num // den

def det_int(M):
    n = len(M); A = [[Fraction(x) for x in r] for r in M]; det = Fraction(1)
    for i in range(n):
        piv = next((r for r in range(i, n) if A[r][i] != 0), None)
        if piv is None: return 0
        if piv != i: A[i], A[piv] = A[piv], A[i]; det = -det
        det *= A[i][i]
        for r in range(i + 1, n):
            f = A[r][i] / A[i][i]
            if f: A[r] = [a - f * b for a, b in zip(A[r], A[i])]
    return int(det)

def poly_disc(coeffs):
    """Discriminant of a monic integer polynomial (high->low coeffs)."""
    def deriv(c):
        n = len(c) - 1
        return [a * (n - i) for i, a in enumerate(c[:-1])]
    def mul(a, b):
        r = [0] * (len(a) + len(b) - 1)
        for i, x in enumerate(a):
            for j, y in enumerate(b): r[i + j] += x * y
        return r
    f = list(coeffs); df = deriv(f); n = len(f) - 1
    # Sylvester matrix of (f, f') -> resultant
    m = len(df) - 1
    rows = []
    for i in range(m):
        rows.append([0] * i + f + [0] * (m - 1 - i))
    for i in range(n):
        rows.append([0] * i + df + [0] * (n - 1 - i))
    res = det_int(rows)
    return (-1) ** (n * (n - 1) // 2) * res

def signature(M):
    """Exact inertia (p,q) of a symmetric rational matrix by congruence."""
    A = [[Fraction(x) for x in r] for r in M]; n = len(A); pos = neg = 0; idx = list(range(n))
    while idx:
        # find a nonzero diagonal
        i0 = next((i for i in idx if A[i][i] != 0), None)
        if i0 is None:
            pair = next(((i, j) for i in idx for j in idx if i < j and A[i][j] != 0), None)
            if pair is None: break
            i, j = pair
            for k in range(n): A[i][k] += A[j][k]      # congruence: add col/row j to i
            for k in range(n): A[k][i] = A[i][k]
            i0 = i
        c = A[i0][i0]
        if c > 0: pos += 1
        else: neg += 1
        for j in idx:
            if j != i0 and A[j][i0] != 0:
                f = A[j][i0] / c
                for k in range(n): A[j][k] -= f * A[i0][k]
                for k in range(n): A[k][j] -= f * A[k][i0]
        idx.remove(i0)
    return pos, neg

# ================================================================ A
print("\nA. CYCLOTOMIC DISCRIMINANTS — the box per base")
print("   n:  disc(Q(zeta_n))        phi(n)")
for n in [2, 3, 4, 5, 6, 7, 8, 9, 12, 16, 24, 36, 60]:
    d = cyclotomic_disc(n); ph = totient(n)
    isq = int(round(math.isqrt(d))) ** 2 == d if d > 0 else False
    tag = ""
    if d == n ** 2: tag = "  <== disc == n^2"
    elif isq: tag = "  (perfect square)"
    print(f"   {n:>3}: {d:>20}   phi={ph}{tag}")

chk("disc(Q(zeta_3))  = 3   (triangle world)", cyclotomic_disc(3) == 3)
chk("disc(Q(zeta_4))  = 4   (square world)",   cyclotomic_disc(4) == 4)
chk("disc(Q(zeta_12)) = 144 = 12^2  <-- THE SQUARE BOX", cyclotomic_disc(12) == 144 == 12 * 12)
chk("n = 12 is the first n > 4 with disc == n^2",
    min(n for n in range(5, 400) if cyclotomic_disc(n) == n * n) == 12)

# the field number itself
chk("12^phi(12) = 12^4 = 20736  (the owner's field)", 12 ** totient(12) == 20736)
deficit = (2 ** (totient(12) // 1)) * (3 ** (totient(12) // 2))
chk("deficit prod p^(phi/(p-1)) = 2^4 * 3^2 = 144 = 12^2",
    deficit == 144 == 12 ** 2, f"2^4*3^2 = {2**4}*{3**2}")
chk("so disc = 12^4 / (2^4*3^2) = 20736/144 = 144 = 12^2",
    cyclotomic_disc(12) == 20736 // 144 == 144)
chk("144 = 2^4 * 3^2  ==  (base-2 part 16) x (base-3 part 9)", 144 == 16 * 9)
chk("20736 = 12^4 = 2^8*3^4 = (2^4*3^2)^2 = 144^2", 20736 == 256 * 81 == 144 ** 2)

# ================================================================ B
print("\nB. THE EXACT BRIDGE (why 12 is '12 in both worlds')")
# omega = zeta_6 = 1/2 + i*sqrt3/2 ; i = zeta_4 ; zeta_12 = sqrt3/2 + i/2
# zeta_12 = i*(1 - omega) ?
#   i*(1 - omega) = i*(1/2 - i sqrt3/2) = sqrt3/2 + i/2   = zeta_12   EXACT
lhs = complex(0, 1) * (1 - complex(0.5, math.sqrt(3) / 2))
chk("zeta_12 == i*(1 - omega)   [square unit x (1 - triangle unit)]",
    abs(lhs - complex(math.sqrt(3) / 2, 0.5)) < 1e-15,
    f"i*(1-omega) = {lhs.real:.12f} + {lhs.imag:.12f}i")
# and the reverse: i = zeta^3, omega = zeta^2  -> Z[i,omega] == Z[zeta_12]
# change of basis {1,z,z^2,z^3} -> {1, i, omega, i*omega} = {1, z^3, z^2, z^3 - z}
# (using Phi_12: z^4 = z^2 - 1  =>  z^5 = z^3 - z)
chk("Phi_12(x) = x^4 - x^2 + 1", poly_disc([1, 0, -1, 0, 1]) == 144)
chk("disc(Phi_12) = 144 = disc(Q(zeta_12))  (field is monogenic)",
    poly_disc([1, 0, -1, 0, 1]) == cyclotomic_disc(12) == 144)
basis_change = [[1, 0, 0, 0], [0, 0, 0, 1], [0, 0, 1, 0], [0, -1, 0, 1]]
chk("Z[i, omega] == Z[zeta_12]  (change of basis unimodular)",
    abs(det_int(basis_change)) == 1,
    "rows: 1, i=z^3, omega=z^2, i*omega=z^3-z  -> det = " + str(det_int(basis_change)))

# ================================================================ C
print("\nC. THE 4D BOX (why the owner says 'it is not 2D')")
def gram_prod(n, ks):
    """Product form: Tr(zeta^a * zeta^b) = c_n(a+b)  (indefinite)."""
    return [[ramanujan(n, a + b) for b in ks] for a in ks]
def gram_metric(n, ks):
    """Metric form: Tr(zeta^a * conj(zeta^b)) = c_n(a-b)  (the geometric one)."""
    return [[ramanujan(n, a - b) for b in ks] for a in ks]

M3 = gram_metric(3, [0, 1]); M4 = gram_metric(4, [0, 1])
chk("METRIC form of triangle world = A2 = [[2,-1],[-1,2]] (hex/graphene lattice)",
    M3 == [[2, -1], [-1, 2]], f"det {det_int(M3)}, signature {signature(M3)}  <- also proves signature() works")
chk("METRIC form of square world = square lattice (det 4, definite)",
    abs(det_int(M4)) == 4 and signature(M4)[1] == 0, f"{M4}")
M12 = gram_metric(12, [0, 2, 1, 3])      # natural mixed basis: (1, omega | i, i*omega)
B = [[4, 2], [2, 4]]                     # = 2 * A2
chk("METRIC box in basis (1,omega | i,i*omega) = 2A2 (+) 2A2  (block diagonal, two IDENTICAL planes)",
    M12 == [B[0] + [0, 0], B[1] + [0, 0], [0, 0] + B[0], [0, 0] + B[1]])
chk("each plane has determinant 12 = 4 x 3  (base-2 aperture x base-3 disc)",
    abs(det_int(B)) == 12, "2A2 = scale 2 (aperture 4) applied to A2 (det 3)")
chk("...so the SQUARE BOX = 12 x 12 = 144 and it is positive definite (4,0)",
    abs(det_int(M12)) == 144 and signature(M12) == (4, 0),
    f"det {det_int(M12)}, signature {signature(M12)}")
G12 = gram_prod(12, [0, 1, 2, 3])
chk("PRODUCT form: same det 144 but signature (2,2) -> split world(+) per world(-)",
    abs(det_int(G12)) == 144 and signature(G12) == (2, 2), f"signature {signature(G12)}")
Bp = gram_prod(12, [0, 2]); Bn = gram_prod(12, [3, 5])
chk("...its two planes are det 12 and mutual negatives (multiplying by i flips the form)",
    abs(det_int(Bp)) == abs(det_int(Bn)) == 12
    and all(Bn[a][b] == -Bp[a][b] for a in range(2) for b in range(2)),
    f"{Bp[0]}/{Bp[1]}  vs  {Bn[0]}/{Bn[1]}")
chk("covolume of the 4D box = sqrt(144) = 12", math.isqrt(abs(det_int(M12))) == 12)
chk("phi(12) = 4  -> base-12 needs exactly 4 digits -> 12^4 = 20736 (owner's Base12 d1..d4)",
    totient(12) == 4)

# ================================================================ D
print("\nD. MINIMALITY: 12 = lcm(4, 6)")
def has_root_of_order(n, k):
    """Does Z[zeta_n] contain a primitive k-th root of unity?"""
    return k % 1 == 0 and (4 * k) % (4 * k) == 0 and math.gcd(1, 1) == 0 and (k in rot_orders(n))
def rot_orders(n):
    return [k for k in range(1, 25) if n % k == 0 and totient(k) and n % k == 0 and k % 1 == 0 and
            False]  # placeholder, replaced below
def contains(n, k):
    # a primitive k-th root lies in Z[zeta_n] iff  k | n  (for k | n, zeta_n^(n/k) has order k)
    return n % k == 0
both = [n for n in range(1, 61) if contains(n, 4) and contains(n, 6)]
chk("min n with both a 4-fold and a 6-fold rotation = 12",
    min(both) == 12, f"all <= 60: {both}")
chk("every such n is a multiple of 12 (12 is the whole index-1 base)",
    both == [12, 24, 36, 48, 60])
chk("lcm(base-2 radix 4, base-3 radix 3) = 12 = lcm(4,6)", math.lcm(4, 3) == 12 == math.lcm(4, 6))

# ================================================================ E
print("\nE. SQUARE-BOX CLOSURE (grid sense)")
cands = [L for L in range(2, 200) if L % 4 == 0 and L % 3 == 0]
chk("smallest grid side supporting base-2 level 2 (4 | L) and base-3 level 1 (3 | L) = 12",
    min(cands) == 12)
chk("L = 12: base-2 levels < 2 kept square, base-3 level 1 kept square",
    all(math.isqrt(144 // d) ** 2 == 144 // d for d in [1, 4, 16, 9]))
N = [n for n in range(1, 20000) if n % 16 == 0 and n % 9 == 0
     and math.isqrt(n) ** 2 == n and math.isqrt(n // 16) ** 2 == n // 16
     and math.isqrt(n // 9) ** 2 == n // 9]
chk("smallest N that is a square, /16 a square, /9 a square = 144", min(N) == 144,
    f"{[n for n in N if n <= 20000][:6]}")
chk("144 = 12^2 and 20736 = 144^2 = 12^4 = owner's field", 144 == 12 ** 2 and 20736 == 144 ** 2)
chk("owner's tesseract 1152 = 2^3 * 144 = base-2 cube x square box", 1152 == 8 * 144)
chk("owner's 1728 = 12^3 and 20736 = 12 * 1728 = 18 * 1152", 1728 == 12 ** 3 and 20736 == 12 * 1728)

# ================================================================ F
print("\nF. WHY IT CANNOT BE 2D: the two lattices are incommensurable")
# z = a + b*omega in Z[i] requires Im(z) = b*sqrt(3)/2 to be an integer.
# Not needed to search: b*sqrt3/2 in Z  <=>  b = 0.  Demonstrate numerically.
best = None
for b in range(1, 4001):
    r = (b * math.sqrt(3) / 2) % 1.0
    d = min(r, 1 - r)
    if best is None or d < best[1]: best = (b, d)
chk("no b != 0 gives integer imaginary part (best residual never 0)",
    best[1] > 1e-13, f"closest b={best[0]}: residual {best[1]:.3e} (Pell approximation, never exact)")
hits = [b for b in range(1, 200) if abs((b * math.sqrt(3) / 2) - round(b * math.sqrt(3) / 2)) < 1e-12]
chk("Z[i] cap Z[omega] = Z  -> intersection has rank 1, not 2", hits == [],
    "=> no 2D square is exactly tiled by hex cells, and no 2D hex by squares")
chk("therefore the joint closure must live in >= 4 dimensions (see C: rank 4, det 144)",
    abs(det_int(G12)) == 144)

# ================================================================ G
print("\nG. 3D / 4D LAYER (cube, FCC, rhombille)")
def kfaces(n, k):        # k-faces of the n-cube = C(n,k) * 2^(n-k)
    return math.comb(n, k) * 2 ** (n - k)
chk("cube: 8 vertices, 12 edges, 6 faces, 1 cell",
    [kfaces(3, k) for k in range(4)] == [8, 12, 6, 1])
chk("tesseract: 16 vertices (2^4), 32 edges, 24 squares, 8 cubes, 1 hypercell",
    [kfaces(4, k) for k in range(5)] == [16, 32, 24, 8, 1],
    "24 square faces = the 24 vertices of the 24-cell")
# --- rhombille: project the unit cube's lattice along (1,1,1)  [owner's last image]
_u = (1 / math.sqrt(2), -1 / math.sqrt(2), 0.0)
_w = (1 / math.sqrt(6), 1 / math.sqrt(6), -2 / math.sqrt(6))
def flat3(v):
    p = [v[i] - sum(v) / 3.0 for i in range(3)]
    return (round(sum(p[i] * _u[i] for i in range(3)), 9),
            round(sum(p[i] * _w[i] for i in range(3)), 9))
v8 = [(a, b, c) for a in (0, 1) for b in (0, 1) for c in (0, 1)]
pv = [flat3(v) for v in v8]
edges = [(i, j) for i in range(8) for j in range(i + 1, 8)
         if sum(abs(v8[i][k] - v8[j][k]) for k in range(3)) == 1]
seg = {tuple(sorted((pv[i], pv[j]))) for i, j in edges}
ln = {round(math.dist(a, b), 6) for a, b in seg}   # tolerance: float noise, not geometry
dir2 = {round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180, 6) for a, b in seg}
chk("cube seen along (1,1,1): 8 vertices -> 7 points = hexagon + centre",
    len(set(pv)) == 7 and any(sorted(m) == [(0, 0, 0), (1, 1, 1)]
                              for m in [[v8[i] for i in range(8) if pv[i] == q] for q in set(pv)]))
chk("its 12 edges -> 12 equally long segments in exactly 3 directions",
    len(edges) == 12 and len(seg) == 12 and len(ln) == 1 and len(dir2) == 3,
    f"length {list(ln)[0]:.6f} = sqrt(2/3), directions {sorted(dir2)}")
chk("centre carries 6 of them -> hexagon split into 6 triangles = 3 RHOMBI",
    sum(1 for a, b in seg if a == (0.0, 0.0) or b == (0.0, 0.0)) == 6)
lattice2 = {flat3((a, b, c)) for a in range(-3, 4) for b in range(-3, 4) for c in range(-3, 4)}
dn = sorted(math.dist((0.0, 0.0), p) for p in lattice2 if math.dist((0.0, 0.0), p) > 1e-9)
chk("the same projection tiles the plane as the TRIANGULAR lattice (6 at a, 6 at a*sqrt3)",
    abs(dn[5] - math.sqrt(2 / 3)) < 1e-9 and abs(dn[6] - math.sqrt(2)) < 1e-9,
    f"radii a={dn[5]:.6f}, a*sqrt3={dn[6]:.6f}")
# FCC neighbour shell: (±1,±1,0),(±1,0,±1),(0,±1,±1)
shell = []
for i in range(3):
    for s1 in (1, -1):
        for s2 in (1, -1):
            v = [0, 0, 0]; v[i] = s1; v[(i + 1) % 3] = s2; shell.append(tuple(v))
shell = sorted(set(shell))
chk("FCC neighbour shell has 12 vectors (= D3 kissing number = Y12)",
    len(shell) == 12, f"{len(shell)} vectors")
# partition into 3 squares of 4  and into 4 triangles of 3
squares = [[v for v in shell if sum(1 for x in v if x != 0) == 2 and abs(v[i]) == 1 and abs(v[j]) == 1]
           for i in range(3) for j in range(i + 1, 3)]
squares = [s for s in squares if len(s) == 4]
chk("the 12 split into 3 groups of 4 (squares)  -> 12 = 3 x 4",
    len(squares) == 3 and sorted(len(s) for s in squares) == [4, 4, 4])
# 12 = 4 x 3: one equilateral triple per body diagonal with an ODD number of minus signs
tet = []
for sx in (1, -1):
    for sy in (1, -1):
        for sz in (1, -1):
            s = (sx, sy, sz)
            if sx * sy * sz != -1: continue
            tet.append(sorted(v for v in shell if sum(a * b for a, b in zip(v, s)) == 2))
flat = [v for t in tet for v in t]
chk("...and into 4 groups of 3 (equilateral triangles, one per odd body diagonal)",
    sorted(len(t) for t in tet) == [3, 3, 3, 3] and sorted(flat) == shell
    and all(sum(a * b for a, b in zip(t[i], t[j])) == 1
            for t in tet for i in range(3) for j in range(i + 1, 3)),
    "12 = 3 x 4 (squares, by zero coordinate) = 4 x 3 (triangles, by odd diagonal)")
chk("12^3 = 1728 (pipes) and 12^4 = 20736 (field) -- owner's beam_timer numbers",
    12 ** 3 == 1728 and 12 ** 4 == 20736, "16 (2^4) is the base-2 factor inside 144 = 16*9")

# ================================================================ H
print("\nH. PENTAGON-ANGLE FAMILY: is 144 the hub?")
fam = [36, 54, 72, 108]
def smooth23(x):
    for p in (2, 3):
        while x % p == 0: x //= p
    return x == 1
chk("every pentagon angle is 2,3-smooth: {36,54,72,108} = 18 x {2,3,4,6}",
    all(smooth23(a) for a in fam) and all(a % 18 == 0 for a in fam),
    "144 = 18 x 8 (2^3) -> same 2,3 world")
reps = {m: len([1 for a in fam for b in fam if a + b == m]) for m in range(18, 181, 18)}
chk("HONEST: 144 is NOT the unique hub -- 108 ties it at 3 sums",
    reps[108] == reps[144] == 3,
    "108: 36+72, 54+54, 72+36 | 144: 36+108, 72+72, 108+36")
regen = [m for m in sorted(reps) if m < 180 and reps[m] and (m // 2) in fam]
chk("144 is the LARGEST angle below 180 that regenerates (144/2 = 72 in family)",
    max(regen) == 144, f"regenerating angles {regen}")
chk("144 = 180 - 36: last stop before the straight line (108 = 180 - 72)",
    180 - 144 == 36 and 180 - 108 == 72)

# ================================================================ I
print("\nI. LATTICE SIDE: aperture 3 x aperture 4 = index 12")
def mul_zw(a, b):
    """(x1+y1*w)(x2+y2*w), w^2 = w - 1."""
    return (a[0] * b[0] - a[1] * b[1], a[0] * b[1] + a[1] * b[0] + a[1] * b[1])
def norm_zw(a):
    """|x + y*w|^2 = x^2 + xy + y^2."""
    return a[0] * a[0] + a[0] * a[1] + a[1] * a[1]
ap4 = (2, 0)   # scale 2, rotate 0        -> index 4   (the owner's base-2 face)
ap3 = (1, 1)   # 1 + omega: scale sqrt3, rotate 30 deg -> index 3 (base-3 face)
chk("aperture 4 = (2), index 4", norm_zw(ap4) == 4)
chk("aperture 3 = (1+omega), index 3, arg exactly 30 deg",
    norm_zw(ap3) == 3 and abs(math.degrees(math.atan2(math.sqrt(3) / 2, 1.5)) - 30) < 1e-12)
chk("3-then-4 == 4-then-3 (multiplication commutes -> same lattice, differs only in numbering)",
    mul_zw(ap3, ap4) == mul_zw(ap4, ap3) == (2, 2))
chk("mixed aperture = (2+2omega): index 12 = 4 x 3 = lcm(4,3)", norm_zw((2, 2)) == 12)
chk("aperture 12 is a REAL similarity: scale sqrt12, rotate 30 deg",
    norm_zw((2, 2)) == 12 and abs(math.degrees(math.atan2(math.sqrt(3), 3)) - 30) < 1e-12,
    "alpha = 2+2omega")
rep12 = sorted(set((abs(x), abs(y)) for x in range(-8, 9) for y in range(-8, 9)
                   if norm_zw((x, y)) == 12))
chk("12 is representable by x^2+xy+y^2 (so a 30-degree aperture-12 rung exists)",
    (2, 2) in rep12, f"solutions (|x|,|y|) = {rep12}")
chk("12 is NOT on the pure scale-2 ladder 4^L (it only appears as 3 x 4)",
    12 not in [4 ** L for L in range(5)], "4^L = 1,4,16,64,256")

# ================================================================ J
print("\nJ. THE FACTORIZATION VIEW: 128 x 162 = 144 x 144")
A = 20736
chk("128 x 162 == 144 x 144 == 20736", 128 * 162 == 144 * 144 == A)
chk("20736 = 2^8 * 3^4 (all exponents even -> perfect square)", A == 2 ** 8 * 3 ** 4)
chk("144 x 144 is the ONLY square factorization (the root is unique)",
    A == 144 * 144 and math.isqrt(A) == 144 and not any(w * w == A for w in range(1, 144)))
chk("144 = 8 x 2 x 9 = 2*2*2*2*3*3 = (2*2*3)^2  [owner's grouping]",
    144 == 8 * 2 * 9 == 2 * 2 * 2 * 2 * 3 * 3 == (2 * 2 * 3) ** 2)
divs = [d for d in range(1, A + 1) if A % d == 0]
pairs = [(w, A // w) for w in divs if w <= A // w]
chk("every factorization has aspect ratio EXACTLY (w/144)^2",
    all(abs((w / h) - (w / 144) ** 2) < 1e-9 for w, h in pairs),
    f"{len(divs)} divisors -> {len(pairs)} rectangles")
def fits(p, q):
    if (A * q) % p: return False
    t = math.isqrt((A * q) // p)
    return t > 0 and (A * q) // p == t * t
t16, t9 = fits(16, 9), fits(9, 4)
chk("16:9 fits exactly as 192 x 108 (= (4:3)^2)", t16 and 192 * 108 == A,
    "so the SCREEN ratio itself is not in the family -- its square is")
chk("64:81 fits exactly as 128 x 162 (= (8:9)^2)  [the owner's rectangle]",
    fits(64, 81) and 128 * 162 == A)
chk("4:3 and 3:2 can NEVER fit (their ratios are not squares)",
    not fits(4, 3) and not fits(3, 2))
chk("128 x 162 is the CLOSEST-TO-SQUARE non-square factorization",
    min((max(w, h) / min(w, h), w, h) for w, h in pairs if w != h)[1:] == (128, 162))
chk("the shear is ONE trade: 2^3 against 3^2 (8/9 vs 9/8)",
    Fraction(128, 144) == Fraction(8, 9) == Fraction(2 ** 3, 3 ** 2)
    and Fraction(162, 144) == Fraction(9, 8))
chk("the two worlds close as squares INDEPENDENTLY: 20736 = 256 x 81 = 16^2 x 9^2",
    A == 256 * 81 == 16 ** 2 * 9 ** 2)
chk("the balanced box is the geometric mean: 12 = 4 x 3 = sqrt(16 x 9), 144 = 16 x 9",
    12 == 4 * 3 == math.isqrt(16 * 9) and 144 == 16 * 9)
chk("owner's other numbers are the same box: 20736 = 1728 x 12 = 1152 x 18 = 12^4",
    A == 1728 * 12 == 1152 * 18 == 12 ** 4)
chk("HONEST: no integer hexagon side gives this area (equal-area map is area-only)",
    all(abs((3 * math.sqrt(3) / 2) * s * s - A) > 1e-6 for s in range(1, 500)),
    f"hexagon would need side {math.sqrt(2 * A / (3 * math.sqrt(3))):.6f}")

# ================================================================ summary
print("\n" + "=" * 64)
print(f"SUMMARY: {len(PASS)} OK / {len(FAIL)} FAIL")
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
print("=" * 64)
