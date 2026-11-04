"""Adaptive Ico Selector v2 — feasibility FIRST, then cost, with p95 and updates.

Addresses the three open assumptions (§600):
  1. build_s is not necessarily one-off: single-insert cost, bulk insert cost and
     full rebuild are all modelled, so a workload with inserts is priced fairly.
     The icosphere topology is fixed, so inserts never re-derive the graph; only
     the touched cell's buffer grows.
  2. Tail latency: the cost comparison reports p50 AND p95 totals, and an SLO on
     p95 is a hard gate, not a footnote.
  3. Feasibility before optimisation: NDCG target, truncation limit, hop limit
     and p95 SLO are hard gates. Only survivors of that filter are ranked by cost.

Still deliberately NOT a frozen formula: gates and SLO are caller parameters.
"""
import os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, 'ico_cost_quality.log')

TARGETS = [0.60, 0.70, 0.80, 0.85, 0.90]
Q_LIST = [100, 1_000, 10_000, 100_000, 1_000_000]
TRUNC_MAX = 0.10
HOP_MAX = 200
SLO_P95_MS = 250.0

# measured in ico_update_cost.py: (dataset, lvl) -> (insert_us_p50,
# append_us_p50, rebuild_s)
UPDATES = {
    ("SIFT1M", 1): (24.1, 1.50, 1.03),
    ("SIFT1M", 2): (24.3, 1.60, 2.05),
    ("SIFT1M", 3): (24.9, 1.60, 3.99),
    ("SIFT1M", 4): (28.6, 1.70, 11.68),
    ("MemCore", 1): (23.6, 1.50, 0.16),
    ("MemCore", 2): (24.1, 1.50, 0.14),
    ("MemCore", 3): (25.4, 1.60, 0.19),
    ("MemCore", 4): (28.9, 1.70, 0.56),
}


def parse_log(path):
    rows, ds, N = [], None, None
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            m = re.match(r'^# (SIFT1M|MemCore)\s+N=(\d+) dim=(\d+)', line.strip())
            if m:
                ds, N = m.group(1), int(m.group(2))
                continue
            if not ds or line.startswith(('lvl', '-', '=', '#')):
                continue
            p = line.split()
            if len(p) != 18:
                continue
            try:
                rows.append(dict(
                    ds=ds, N=N, lvl=int(p[0]), cells=int(p[1]),
                    bld_cell=float(p[2]), bld_graph=float(p[3]),
                    budget=float(p[4].rstrip('%')),
                    recall=float(p[5]), ceil=float(p[6]), pre=float(p[7]),
                    post=float(p[8]), loss=float(p[9]), gain=float(p[10]),
                    hops=float(p[11]), cands=int(p[12]),
                    trav50=float(p[13]), trav95=float(p[14]),
                    rank50=float(p[15]), rank95=float(p[16]),
                    trunc=float(p[17])))
            except ValueError:
                continue
    return rows


def gate(r, target):
    """Return (ok, reasons)."""
    bad = []
    if r['post'] < target:
        bad.append(f"NDCG {r['post']:.3f} < {target:.2f}")
    if r['trunc'] > TRUNC_MAX:
        bad.append(f"trunc {r['trunc']:.2f} > {TRUNC_MAX:.2f} (start cell > budget)")
    if r['hops'] > HOP_MAX:
        bad.append(f"hops {r['hops']:.0f} > {HOP_MAX}")
    p95 = r['trav95'] + r['rank95']
    if p95 > SLO_P95_MS:
        bad.append(f"p95 {p95:.0f}ms > SLO {SLO_P95_MS:.0f}ms")
    return (not bad), bad


def main():
    rows = parse_log(LOG)
    if not rows:
        sys.exit("log not parsed")
    print(f"parsed {len(rows)} configs.  Gates: NDCG>=target, trunc<={TRUNC_MAX}, "
          f"hops<={HOP_MAX}, p95<={SLO_P95_MS:.0f}ms\n")

    for ds in sorted({r['ds'] for r in rows}):
        sub = sorted([r for r in rows if r['ds'] == ds], key=lambda x: (x['lvl'], x['budget']))
        N = sub[0]['N']
        print("=" * 100)
        print(f"{ds}  N={N}")
        print("=" * 100)

        # ---- Stage 1: FEASIBILITY (shown against the hardest target, 0.90) ----
        print(f"  STAGE 1 — feasibility gate table (evaluated against target 0.90)")
        print(f"  {'lvl':>3} {'budget':>6} {'NDCG':>6} {'trunc':>6} {'hops':>6} "
              f"{'p95ms':>7}  {'verdict'}")
        for r in sub:
            ok, bad = gate(r, 0.90)
            p95 = r['trav95'] + r['rank95']
            print(f"  {r['lvl']:>3} {r['budget']:>5.0f}% {r['post']:>6.3f} "
                  f"{r['trunc']:>6.2f} {r['hops']:>6.1f} {p95:>7.0f}  "
                  f"{'ELIGIBLE' if ok else 'REJECT: ' + '; '.join(bad)}")

        # ---- Stage 2: COST among survivors ----
        print(f"\n  STAGE 2 — cheapest by total cost among configs that PASS the gates")
        print(f"  {'target':>6} {'Q':>9} {'pick':>10} {'NDCG':>6} {'build_s':>8} "
              f"{'p50/query':>10} {'p95/query':>10} {'total p50':>11} {'total p95':>11}")
        for tgt in TARGETS:
            elig = [r for r in sub if gate(r, tgt)[0]]
            if not elig:
                # say which gate is the wall
                closest = max(sub, key=lambda r: r['post'])
                why = []
                if closest['post'] < tgt:
                    why.append(f"best NDCG {closest['post']:.3f}")
                print(f"  {tgt:>6.2f} {'—':>9} {'INFEASIBLE':>10} "
                      f"{closest['post']:>6.3f} {'—':>8} {'—':>10} {'—':>10} "
                      f"{'—':>11} {'—':>11}  ({', '.join(why)})")
                continue
            for Q in Q_LIST:
                def tot(r, p50=True):
                    per = (r['trav50'] + r['rank50']) if p50 else (r['trav95'] + r['rank95'])
                    return r['bld_cell'] + r['bld_graph'] + Q * per / 1000.0
                best = min(elig, key=tot)
                print(f"  {tgt:>6.2f} {Q:>9} "
                      f"{'L%d@%.0f%%' % (best['lvl'], best['budget']):>10} "
                      f"{best['post']:>6.3f} {best['bld_cell']+best['bld_graph']:>8.2f} "
                      f"{best['trav50']+best['rank50']:>10.1f} "
                      f"{best['trav95']+best['rank95']:>10.1f} "
                      f"{tot(best, True):>11.1f} {tot(best, False):>11.1f}")

        # ---- Stage 3: UPDATE COST ----
        print(f"\n  STAGE 3 — update cost (is build one-off?)")
        print(f"  {'lvl':>3} {'insert_us':>10} {'append_us':>10} {'rebuild_s':>10} "
              f"{'rebuild/insert':>15} {'breakeven inserts':>18}")
        for lvl in sorted({r['lvl'] for r in sub}):
            ins, app, reb = UPDATES[(ds, lvl)]
            ratio = reb * 1e6 / ins
            print(f"  {lvl:>3} {ins:>10.1f} {app:>10.2f} {reb:>10.2f} "
                  f"{ratio:>15.0f} {int(ratio):>18}")
        print("  reading: topology is fixed, so an insert = project + argmax + buffer")
        print("  append (no graph rebuild, no re-clustering). A full rebuild only pays")
        print("  off for batch loads larger than the breakeven count, or for compaction.")
        print()


if __name__ == '__main__':
    main()