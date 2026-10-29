"""phase_d_headtail.py — how many letters does a real address need to be unique?

Owner pattern (2026-10-30): call an address by head and tail, b1N4. If it misses
or lands on more than one record, add the second position (b1H2N4), then the
third, until it is the whole address.

This measures that on real vectors: 12-level addresses derived from the actual
rows in ann_index.jsonl, then for every address the smallest ladder step at which
its compact key is unique among all of them.

Run: python phase_d_headtail.py [n_rows]
"""
import sys

import numpy as np

from phase_b_gate import load

LEVELS = 12
BITS = 6            # 2^6 = 64 codes folded onto the 52 letters


def build_addresses(X, levels=LEVELS, seed=11):
    """12 letters per row, from fixed seeded projections (pure, data-independent)."""
    rng = np.random.default_rng(seed)
    A = np.zeros((len(X), levels), dtype=np.int32)
    for L in range(levels):
        P = rng.standard_normal((BITS, X.shape[1])).astype(np.float32)
        S = (X @ P.T) > 0
        code = np.zeros(len(X), dtype=np.int32)
        for b in range(BITS):
            code |= S[:, b].astype(np.int32) << b
        A[:, L] = 1 + (code % 52)          # digits 1..52, as chain_addr.h defines
    return A


def compact(A, m):
    """Step m: positions 1..m plus the tail. Returns a hashable tuple per row."""
    return [tuple(a[:m]) + (a[-1],) for a in A]


def main():
    n_rows = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    print(f"loading up to {n_rows} rows ...", flush=True)
    X = load(n_rows)
    N = len(X)
    A = build_addresses(X)
    print(f"  {N} rows, {LEVELS}-letter addresses\n", flush=True)

    # how many records does each step leave ambiguous?
    print(f"{'step':>5} {'letters':>8} {'key space':>12} {'groups':>8} "
          f"{'unique':>8} {'max group':>10}", flush=True)
    print("-" * 58, flush=True)
    for m in range(1, LEVELS):
        keys = compact(A, m)
        uniq = len(set(keys))
        counts = {}
        for k in keys:
            counts[k] = counts.get(k, 0) + 1
        biggest = max(counts.values())
        print(f"{m:>5} {m+1:>8} {52**(m+1):>12} {uniq:>8} "
              f"{uniq/N:>7.1%} {biggest:>10}", flush=True)

    # the number the design cares about: the step each address actually needs
    print("\n--- ladder step needed for each address to be unique ---", flush=True)
    needed = np.zeros(N, dtype=np.int32)
    remaining = np.arange(N)
    for m in range(1, LEVELS):
        keys = compact(A, m)
        counts = {}
        for k in keys:
            counts[k] = counts.get(k, 0) + 1
        still = np.array([counts[keys[i]] > 1 for i in remaining])
        newly = remaining[~still]
        needed[newly] = m
        remaining = remaining[still]
        if len(remaining) == 0:
            break
    needed[remaining] = LEVELS          # never unique: identical addresses

    hist = np.bincount(needed, minlength=LEVELS + 1)
    for m in range(1, LEVELS + 1):
        if hist[m]:
            print(f"  step {m:>2} ({m+1:>2} letters): {hist[m]:>7} "
                  f"({hist[m]/N:>6.2%})", flush=True)

    mean = needed.mean()
    print(f"\n  mean letters per lookup = {mean+1:.2f} of {LEVELS}")
    print(f"  lookups answered by head+tail alone (b1N4) = "
          f"{hist[1]/N:.2%}")


if __name__ == "__main__":
    main()
