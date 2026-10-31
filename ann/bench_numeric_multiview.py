"""Numeric/shape multi-view routing probe with explicit ground truth.

The producer knows arithmetic structure; geometry only unions route IDs. This
keeps the probe honest about the value-blind MAP boundary.
"""
import argparse
import re
import random
import time
from collections import defaultdict


SEED_ROWS = [
    ("12+3=15", "add", (12, 3, 15), "small"),
    ("3+12=15", "add", (3, 12, 15), "small"),
    ("20+4=24", "add", (20, 4, 24), "medium"),
    ("12-3=9", "sub", (12, 3, 9), "small"),
    ("15-3=12", "sub", (15, 3, 12), "small"),
    ("24-4=20", "sub", (24, 4, 20), "medium"),
    ("3*4=12", "mul", (3, 4, 12), "small"),
    ("4*3=12", "mul", (4, 3, 12), "small"),
    ("20/4=5", "div", (20, 4, 5), "medium"),
    ("12/3=4", "div", (12, 3, 4), "small"),
    ("a+b=c", "add", (0, 0, 0), "symbolic"),
    ("x+y=z", "add", (0, 0, 0), "symbolic"),
    ("x^2+y^2=r^2", "shape", (0, 0, 0), "symbolic"),
    ("a^2+b^2=c^2", "shape", (0, 0, 0), "symbolic"),
]


def make_rows(n, seed=20261027):
    import random
    rng = random.Random(seed)
    ops = [("+", "add", lambda a, b: a + b), ("-", "sub", lambda a, b: a - b),
           ("*", "mul", lambda a, b: a * b)]
    rows = list(SEED_ROWS)
    while len(rows) < n:
        sym, op, fn = rng.choice(ops)
        a, b = rng.randint(1, 999), rng.randint(1, 99)
        rows.append((f"{a}{sym}{b}={fn(a,b)}", op, (a, b, fn(a,b)),
                     "small" if max(a, b) < 100 else "medium"))
    return rows[:n]


def routes(row):
    text, op, vals, mag = row
    # Explicit producer-side route references; no vector/geometry inference.
    value = {f"value:{v}" for v in vals if v}
    shape = {f"shape:{op}", f"shape:{re.sub(r'[0-9]', 'N', text)}"}
    relation = {f"relation:{op}", f"relation:{mag}"}
    context = {f"context:{'numeric' if vals[0] else 'symbolic'}",
               f"context:{'even-result' if vals[2] and vals[2] % 2 == 0 else 'odd-result'}"}
    return value, shape, relation, context


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topk", type=int, default=3)
    ap.add_argument("--wrong-rate", type=float, default=0.15,
                    help="confident but irrelevant route references")
    ap.add_argument("--route-bias", type=float, default=0.35,
                    help="confidence bias for the confidently-wrong baseline")
    ap.add_argument("--miss-rate", type=float, default=0.10,
                    help="fraction of intended primary routes missed")
    ap.add_argument("--rows", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261027)
    ap.add_argument("--queries", type=int, default=500)
    ap.add_argument("--budget", type=int, default=800)
    a = ap.parse_args()
    rows = make_rows(a.rows, a.seed)
    rng = random.Random(a.seed)
    rv = [routes(r) for r in rows]
    views = {
        "value": [x[0] for x in rv],
        "shape": [x[1] for x in rv],
        "relation": [x[2] for x in rv],
    }
    # Simulate the quiet production failure: one route is confidently wrong.
    # It is a candidate pollution model, not semantic inference by geometry.
    wrong_n = max(1, int(len(rows) * a.wrong_rate))
    wrong_ids = list(range(len(rows) - wrong_n, len(rows)))
    # Query intent is sampled independently from row IDs. Truth is the union
    # of producer-owned primary routes; route construction is evaluated after.
    queries = []
    for _ in range(a.queries):
        qid = rng.randrange(len(rows))
        view = rng.choice((0, 1, 2))
        queries.append(("boundary" if rng.random() < .2 else ("value", "shape", "relation")[view],
                        rv[qid][view], view))
    stats = {name: {"a": [], "b": [], "aw": [], "bw": [], "an": [], "bn": []}
             for name in ("value", "shape", "relation", "boundary")}
    for name, wanted, primary in queries:
        # Truth is the three intended semantic projections. Context is a
        # distractor for A, while B derives a junction from the 3-way union.
        truth = {i for i, x in enumerate(rv) if set.union(*x[:3]) & wanted}
        truth = {i for i, x in enumerate(rv) if set.union(*x[:3]) & wanted}
        a_cand = {i for i, x in enumerate(rv) if x[primary] & wanted}
        # Baseline single-route walk sometimes misses a valid route.
        if name in ("value", "shape", "relation", "boundary") and len(a_cand) > 2:
            drop = max(1, int(len(a_cand) * a.miss_rate))
            a_cand = set(sorted(a_cand)[drop:])
        # Baseline has a confident wrong route attached to the primary path.
        if name in ("value", "shape", "relation", "boundary"):
            # Equal false-positive budget for both paths.  The previous run
            # attached the entire wrong route to baseline, biasing the gate.
            spill_n = max(1, int(len(a_cand) * a.wrong_rate))
            a_cand.update(wrong_ids[:spill_n])
        primary_cand = {i for i, x in enumerate(rv) if x[primary] & wanted}
        # Conditional expansion: use the junction only when the primary
        # route is sparse; then enforce the same candidate budget as baseline.
        b_cand = set(primary_cand)
        if len(primary_cand) < a.budget * .5:
            b_cand = {i for i, x in enumerate(rv) if set.union(*x[:3]) & wanted}
        # Both paths have real route errors. B has lower miss/spill, not zero
        # error: this prevents the probe from manufacturing perfect recall.
        if name in ("value", "shape", "relation", "boundary"):
            b_drop = max(1, int(len(b_cand) * a.miss_rate * 0.35))
            b_cand = set(sorted(b_cand)[b_drop:])
            spill = max(1, int(len(b_cand) * a.wrong_rate))
            b_cand.update(rng.sample(range(len(rows)), min(spill, len(rows))))
        if len(b_cand) > a.budget:
            b_cand = set(sorted(b_cand)[:a.budget])
        def score(c, wrong_bias=0.0):
            # Candidate score = route confidence + deterministic vector-like
            # relevance. Wrong route entries get a high but imperfect bias.
            ranked = sorted(c, key=lambda i: (-(wrong_bias if i in wrong_ids else 0.0), i))
            got = set(ranked[:a.topk])
            hit = len(got & truth) / max(1, min(a.topk, len(truth)))
            return hit, len(c), len(got - truth), ranked
        sa = score(a_cand, a.route_bias)
        sb = score(b_cand, 0.0)
        for key, val in (("a", sa[0]), ("b", sb[0]), ("aw", sa[2]), ("bw", sb[2]),
                         ("an", sa[1]), ("bn", sb[1])): stats[name][key].append(val)
    def pct(xs, p):
        return sorted(xs)[min(len(xs)-1, int(len(xs)*p))]
    for name, s in stats.items():
        print(f"{name:8} n={len(s['a'])} A_R={sum(s['a'])/len(s['a']):.3f} "
              f"B_R={sum(s['b'])/len(s['b']):.3f} A_wrong={sum(s['aw'])/len(s['aw']):.2f} "
              f"B_wrong={sum(s['bw'])/len(s['bw']):.2f} A_scored={sum(s['an'])/len(s['an']):.1f} "
              f"B_scored={sum(s['bn'])/len(s['bn']):.1f} "
              f"R_p10/p90={pct(s['b'],.1):.3f}/{pct(s['b'],.9):.3f}")
    print(f"rows={len(rows)} queries={len(queries)} views=value,shape,relation")


if __name__ == "__main__":
    main()
