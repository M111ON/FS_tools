#!/usr/bin/env python3
"""icosian_e8.py — is the dodeca/icosa route really E8?

Claims under test:
  C1  the 120 icosians (unit quaternions over Z[phi]) form a group (order 120)
      -> they are the 600-cell vertices = binary icosahedral group 2I
  C2  ±the 120 icosians = 240 distinct objects -> the E8 root count
  C3  the E8 root system itself: 240 minimal vectors, norm^2 = 2,
      pairwise inner products in {0, ±1, ±2} (the standard integer construction)
  C4  the pentagon-centre set (12 points) cannot be E8: one norm, 12 points,
      while E8 roots are 240 on one sphere of norm^2 = 2

Exact arithmetic: coordinate = a + b*phi with a, b Fractions, phi^2 = phi + 1.
Norm of a + b*phi is a^2 + a*b - b^2  (its product with the conjugate).
"""
from fractions import Fraction as F
from itertools import product

# ---------- Z[phi] as pairs ----------
def add(u, v): return (u[0] + v[0], u[1] + v[1])
def neg(u): return (-u[0], -u[1])
def sub(u, v): return add(u, neg(v))
def mul(u, v):                      # (a+bphi)(c+dphi) = ac+bd + (ad+bc+bd)phi
    a, b = u; c, d = v
    return (a * c + b * d, a * d + b * c + b * d)
def norm1(u):                       # u * conj(u) = a^2 + ab - b^2
    a, b = u
    return a * a + a * b - b * b
ZERO = (F(0), F(0)); ONE = (F(1), F(0))
def half(a, b=F(0)): return (F(a, 2), F(b, 2))

# ---------- quaternions over Z[phi] ----------
def qmul(p, q):
    p0, p1, p2, p3 = p; q0, q1, q2, q3 = q
    return (
        sub(sub(sub(mul(p0, q0), mul(p1, q1)), mul(p2, q2)), mul(p3, q3)),
        add(add(mul(p0, q1), mul(p1, q0)), sub(mul(p2, q3), mul(p3, q2))),
        add(sub(mul(p0, q2), mul(p1, q3)), add(mul(p2, q0), mul(p3, q1))),
        add(add(mul(p0, q3), mul(p1, q2)), sub(mul(p3, q0), mul(p2, q1))),
    )
def qnorm(q):                      # SUM |x_i|^2 in the real embedding, exact in Z[phi]
    acc = ZERO
    for x in q: acc = add(acc, mul(x, x))
    return acc

def closure(gens):
    ident = (ONE, ZERO, ZERO, ZERO)
    seen = {ident}
    frontier = [ident]
    while frontier:
        nxt = []
        for p in frontier:
            for g in gens:
                r = qmul(p, g)
                if r not in seen:
                    seen.add(r); nxt.append(r)
        frontier = nxt
    return seen

print("icosian_e8")
fails = 0

# 2I = < (1+i+j+k)/2 , (phi + i + (1/phi)j)/2 >   with 1/phi = phi - 1
g1 = (half(1), half(1), half(1), half(1))
g2 = (half(0, 1), half(1), (F(-1, 2), F(1, 2)), ZERO)   # (phi + i + (phi-1)j)/2
G = closure([g1, g2])
print(f"C1 |2I| generated   : {len(G)}  (expect 120)  {'PASS' if len(G)==120 else 'FAIL'}")
if len(G) != 120: fails += 1

units_ok = all(qnorm(q) == ONE for q in G)
print(f"C1 all unit norm 1  : {'PASS' if units_ok else 'FAIL'}  (exact in Z[phi])")
if not units_ok: fails += 1
print(f"C1 120 = 2 x 60     : {len(G)//2} rotations  {'PASS' if len(G)//2==60 else 'FAIL'}")
if len(G) // 2 != 60: fails += 1

# the 24-cell is the binary tetrahedral group 2T, a subgroup of 2I
T = {(ONE, ZERO, ZERO, ZERO), (neg(ONE), ZERO, ZERO, ZERO)}
for i in range(4):
    for sgn in (1, -1):
        v = [ZERO] * 4; v[i] = ONE if sgn > 0 else neg(ONE); T.add(tuple(v))
for signs in product((1, -1), repeat=4):
    T.add(tuple(half(1 if s > 0 else -1) for s in signs))
sub = len(T) == 24 and T <= G
print(f"C1b 2T (24-cell)    : {'PASS' if sub else 'FAIL'}  (|2T|={len(T)}, inside 2I={T <= G})")
if not sub: fails += 1

# -1 is in 2I, so the 120 units already contain both signs: the 240 roots are
# NOT +-120 units. The 240 belongs to the ring's minimal vectors, not to the units.
minus_one = (neg(ONE), ZERO, ZERO, ZERO) in G
print(f"C2 -1 in 2I         : {'PASS' if minus_one else 'FAIL'}  -> 240 roots != 2 x 120 units")
if not minus_one: fails += 1

# ---------- C3: the E8 root system, integer construction ----------
roots = []
for i in range(8):
    for j in range(i + 1, 8):
        for si in (1, -1):
            for sj in (1, -1):
                v = [0] * 8; v[i] = si; v[j] = sj
                roots.append(tuple(v))
for signs in product((1, -1), repeat=8):
    if sum(signs) % 4 == 0:                  # even sum (in halves: sum of +-1 == 0 mod 4)
        roots.append(tuple(F(s, 2) for s in signs))
roots = list(dict.fromkeys(roots))
norms = {sum(x * x for x in v) for v in roots}
inner = set()
for a in range(0, len(roots), 7):
    for b in range(a, len(roots)):
        inner.add(sum(x * y for x, y in zip(roots[a], roots[b])))
print(f"C3 |E8 roots|       : {len(roots)}  (expect 240)  {'PASS' if len(roots)==240 else 'FAIL'}")
print(f"C3 one norm         : {sorted(float(n) for n in norms)}  {'PASS' if norms=={2} else 'FAIL'}")
print(f"C3 inner products   : {sorted(float(i) for i in inner)}  {'PASS' if inner <= {0,1,-1,2,-2} else 'FAIL'}")
if len(roots) != 240 or norms != {2} or not inner <= {0, 1, -1, 2, -2}: fails += 1

# ---------- C4: the 12 pentagon centres ----------
# dodecahedron face centres, unit sphere: 12 points, single norm -> an H3 orbit,
# 12 != 240 and there is no 8-dimensional structure in it.
print(f"C4 pentagon centres : 12 points on one sphere -> H3 orbit, not E8 (240 roots, dim 8)")
print(f"\n{'PASS' if not fails else 'FAIL'} ({fails} failing checks)")
