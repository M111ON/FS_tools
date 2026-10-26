#!/usr/bin/env python3
"""bench_soft_zone.py — soft zone bias vs the hard zone filter (card tabbd47c5).

The two arms answer different questions, and nobody had measured the gap:

  hard  `scope:"zone:N"` REPLACES the walk with the zone's rows.  An answer
        outside the zone is unreachable — work drops to the zone size.
  soft  `scope:"zone:N", bias:b` keeps the route and ADDS b to the score of
        rows in that zone.  Every row stays reachable; work is unchanged; only
        the ranking moves.

So the honest comparison is:
  * how much of the GLOBAL answer survives (overlap with the unscoped arm)
  * how much work each arm does
  * how much the zone actually captures (share of returned rows in-zone)
  * latency, same server, same moment

All three arms hit ONE scratch server over the live index/tags (read-only), so
host load cannot flatter an arm. Query rows are real index rows; the server is
told to drop the query's own sid, so "self" cannot be the answer.

USAGE
  python bench_soft_zone.py --port 8098 --queries 60
  python bench_soft_zone.py --port 8098 --queries 40 --bias 0.005,0.01,0.02,0.05
"""
import argparse
import json
import os
import statistics
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "memcore")
INDEX = os.path.join(DATA, "ann_index.jsonl")
TAGS = os.path.join(DATA, "ann_tags.tsv")


def pr(xs):
    xs = sorted(xs)
    if not xs:
        return "n=0"
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return f"p50={q(.50):.1f} p90={q(.90):.1f} max={xs[-1]:.1f}"


def post(base, body, timeout=120):
    req = urllib.request.Request(base, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return d, (time.perf_counter() - t0) * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8098)
    ap.add_argument("--queries", type=int, default=60)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--bias", type=str, default="0.005,0.01,0.02,0.05")
    ap.add_argument("--seed", type=int, default=42)
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}/v1/state/search"

    tag_of = {}
    with open(TAGS, encoding="utf-8") as f:
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 3:
                tag_of[p[0]] = (p[1], int(p[2]))
    sids, vecs, zones = [], [], []
    with open(INDEX, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            sids.append(o["sid"])
            vecs.append(o["vec"])
            zones.append(tag_of.get(o["sid"], (None, -1))[1])
    n = len(sids)
    print(f"index {n} rows, {len(tag_of)} tagged")

    zmap = dict(zip(sids, zones))
    import random
    rng = random.Random(a.seed)
    pick = rng.sample(range(n), min(a.queries, n))
    biases = [float(x) for x in a.bias.split(",") if x.strip()]

    arms = ["global", "hard"] + [f"soft{b:g}" for b in biases]
    overlap = {t: [] for t in arms}
    scored = {t: [] for t in arms}
    lat = {t: [] for t in arms}
    inzone = {t: [] for t in arms}
    top1_zone = {t: [] for t in arms}
    skipped = 0
    for pos, i in enumerate(pick):
        z = zones[i]
        if z is None or z < 0:
            skipped += 1
            continue
        body = {"vec": vecs[i], "topk": a.k, "sid_filter": sids[i]}
        g, ms = post(base, body)
        if g.get("status") != "ok" or not g.get("hits"):
            skipped += 1
            continue
        gref = [h["sid"] for h in g["hits"]]
        results = {"global": (g, ms)}
        for arm in arms[1:]:
            b2 = dict(body)
            b2["scope"] = f"zone:{z}"
            if arm != "hard":
                b2["bias"] = float(arm[4:])
            try:
                d, m2 = post(base, b2)
            except urllib.error.HTTPError as e:
                j = None
                try:
                    j = json.loads(e.read())
                except Exception:
                    pass
                if j and j.get("error") in ("scope_too_narrow", "scope_not_found"):
                    skipped += 1
                    break
                raise
            results[arm] = (d, m2)
        else:
            for arm in arms:
                d, m2 = results[arm]
                got = [h["sid"] for h in d.get("hits", [])]
                overlap[arm].append(len(set(got) & set(gref)) / a.k)
                scored[arm].append(d.get("route", {}).get("items_scored", 0))
                lat[arm].append(m2)
                inzone[arm].append(sum(1 for s in got if zmap.get(s) == z) / max(1, len(got)))
                top1_zone[arm].append(1 if got and zmap.get(got[0]) == z else 0)

    q = len(overlap["global"])
    print(f"\n{q} queries scored ({skipped} skipped), top-{a.k}, same server, same moment")
    print(f"  {'arm':<9} {'keep global':>11} {'in-zone':>8} {'top1 in-zone':>12} "
          f"{'items_scored':>12} {'latency':>22}")
    for arm in arms:
        if not overlap[arm]:
            continue
        print(f"  {arm:<9} {statistics.fmean(overlap[arm]):>11.3f} "
              f"{statistics.fmean(inzone[arm]):>8.3f} "
              f"{statistics.fmean(top1_zone[arm]):>12.3f} "
              f"{int(statistics.median(scored[arm])):>12} {pr(lat[arm]):>22}")

    print("\nreading: 'keep global' is the share of the unscoped top-5 that the arm"
          " still returns.\n  hard = the filter's price (in-zone answers only),"
          " soft = the nudge's price.\n  items_scored is the walk's work — the soft arm"
          " does NOT cut it; that is what the mini-vector-DB card is for.")


if __name__ == "__main__":
    main()
