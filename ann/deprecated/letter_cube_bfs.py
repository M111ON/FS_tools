#!/usr/bin/env python3
"""Letter-cube BFS: diameter (God's number) of Cayley graph S_n under a generator set.
Vertex-transitive => one BFS from identity gives the true diameter.
Usage: python letter_cube_bfs.py [N ...]   (default: 6 8)
"""
import sys, math
from collections import deque

# ---------- CONFIG ----------
N_LIST      = [6, 8]        # letters (even; N/2 = pairs). 10 needs ~500MB RAM, slow in Python
WIN         = 4             # window size for GEN_SET = "window"
GEN_SET     = ["adj", "ring", "window"]   # which generator sets to test
# ----------------------------

def gen_adj(n):
    """adjacent swaps (i,i+1) — self-inverse"""
    out = []
    for i in range(n - 1):
        g = list(range(n)); g[i], g[i + 1] = g[i + 1], g[i]
        out.append(tuple(g))
    return out

def gen_ring(n):
    """rotate whole ring L/R + swap(0,1)  (cylinder-ish: spin + one local swap)"""
    l = tuple((i + 1) % n for i in range(n))
    r = tuple((i - 1) % n for i in range(n))
    s = list(range(n)); s[0], s[1] = s[1], s[0]
    return [l, r, tuple(s)]

def gen_window(n, w=WIN):
    """cube-face-like: cyclic rotate a w-window at every start offset (wrap), both dirs"""
    out = []
    for st in range(n):
        idx = [(st + k) % n for k in range(w)]
        for d in (1, -1):
            g = list(range(n))
            for k in range(w):
                g[idx[k]] = idx[(k + d) % w]
            out.append(tuple(g))
    return out

GENS = {"adj": gen_adj, "ring": gen_ring, "window": gen_window}

def bfs(n, gens):
    start = bytes(range(n))
    seen = {start}
    frontier = [start]
    layers = [1]
    while frontier:
        nxt = []
        for p in frontier:
            for g in gens:
                q = bytes([p[i] for i in g])
                if q not in seen:
                    seen.add(q); nxt.append(q)
        if nxt: layers.append(len(nxt))
        frontier = nxt
    return layers

def run(n, name):
    gens = list(dict.fromkeys(GENS[name](n)))       # dedupe
    layers = bfs(n, gens)
    total = math.factorial(n)
    assert sum(layers) == total, f"not full group: reached {sum(layers)}/{total}"
    b = len(gens)                                   # branching upper bound (gens closed under inverse here)
    lb = math.ceil(math.log(total, b)) if b > 1 else float("inf")
    print(f"n={n:2d} gens={name:6s} |G|={b:3d} states={total:>9,} "
          f"God's={len(layers)-1:3d}  lower_bound~{lb}")

if __name__ == "__main__":
    ns = [int(x) for x in sys.argv[1:]] or N_LIST
    for n in ns:
        for name in GEN_SET:
            run(n, name)
