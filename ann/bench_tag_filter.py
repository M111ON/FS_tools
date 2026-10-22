#!/usr/bin/env python3
"""bench_tag_filter.py — Phase 1 of the "tag embedded in the vector db" idea.

Question: every live query scores the whole budget (24,000 of 62,629 rows)
because the route is cosine-only. The index already carries a tag per row
(`file` = kind/session, and `kind` derived from its prefix). Does narrowing the
candidate set by tag cut the work, and what does that cost in recall?

Method (nothing in the server is trusted; numpy oracle, raw vec queries):
  1. Load ann_index.jsonl -> X (n x 384, L2-normalized) + tag per row.
  2. Oracle = exact cosine top-5 over ALL n, for Q fixed-seed query rows.
  3. Arms (offline, no server):
       fine  = candidates share the query row's `file` tag
       kind  = candidates share the query row's `kind` tag
       chain = fine -> widen to kind -> widen to full (fallback, never loses)
     For each: items_scored (candidate count) and recall@5 against the oracle
     (how much of the global top-5 the filter throws away).
  4. Live baseline (--live): send the same query rows as raw `vec` to the
     running server, report its recall@5 + items_scored for comparison.

Nothing writes the repo data and no server code changes.

USAGE
  python bench_tag_filter.py                 # offline arms, 300 queries
  python bench_tag_filter.py --live 40       # + live server baseline
  python bench_tag_filter.py --queries 500 --seed 7
REQUIREMENTS  numpy (live arm needs :8096 up)
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(ROOT, "data", "memcore", "ann_index.jsonl")
LIVE = "http://127.0.0.1:8096/v1/state/search"


def load(path):
    sids, files, rows = [], [], []
    t0 = time.time()
    with open(path, encoding="utf-8") as f:
        for line in f:
            o = json.loads(line)
            sids.append(o["sid"])
            files.append(o.get("file", ""))
            rows.append(o["vec"])
    X = np.asarray(rows, dtype=np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    print(f"loaded {len(sids)} rows dim={X.shape[1]} in {time.time()-t0:.1f}s")
    return sids, files, X


def topk(Q, X, k, self_idx=None, chunk=64):
    """exact cosine top-k over the whole matrix, self-excluded"""
    out = np.empty((Q.shape[0], k), dtype=np.int64)
    for i in range(0, Q.shape[0], chunk):
        s = Q[i:i + chunk] @ X.T
        if self_idx is not None:
            for r in range(s.shape[0]):
                s[r, self_idx[i + r]] = -np.inf
        part = np.argpartition(-s, min(k, s.shape[1] - 1), axis=1)[:, :k]
        for r in range(part.shape[0]):
            row = part[r]
            out[i + r] = row[np.argsort(-s[r, row])]
    return out


def pr(xs):
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * len(xs)))]
    return f"min={xs[0]} p50={q(.50)} p90={q(.90)} max={xs[-1]} mean={statistics.fmean(xs):.1f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=300)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ks", type=str, default="5")
    ap.add_argument("--live", type=int, default=0, help="N query rows to also run against :8096")
    ap.add_argument("--scope-port", type=int, default=0,
                    help="scratch server WITH the scope param (e.g. 8097); N = --scope-n")
    ap.add_argument("--scope-n", type=int, default=80)
    ap.add_argument("--tag-port", type=int, default=0,
                    help="scratch server with the tag sidecar (scope=tag:<name>)")
    ap.add_argument("--tag-n", type=int, default=20, help="queries per tag")
    a = ap.parse_args()

    sids, files, X = load(INDEX)
    kinds = np.array([f.split("/")[0] for f in files])
    n = X.shape[0]
    rng = np.random.default_rng(a.seed)
    qi = rng.choice(n, size=min(a.queries, n), replace=False)
    Q = X[qi]
    k = int(a.ks.split(",")[0])

    t0 = time.time()
    oracle = topk(Q, X, k, self_idx=qi)
    print(f"oracle top-{k} for {len(qi)} queries in {time.time()-t0:.1f}s "
          f"(self-excluded — otherwise top-1 is the query row itself)")

    fine_of = {}
    for i, f in enumerate(files):
        fine_of.setdefault(f, []).append(i)
    fine = np.array([np.array(fine_of[files[i]], dtype=np.int64) for i in qi], dtype=object)
    kind_idx = {k_: np.where(kinds == k_)[0] for k_ in set(kinds)}

    arms = ("fine", "kind", "chain")
    rec = {a_: [] for a_ in arms}
    cost = {a_: [] for a_ in arms}
    hit1 = {a_: [] for a_ in arms}
    for r, i in enumerate(qi):
        fg = fine[r][fine[r] != i]
        kg = kind_idx[kinds[i]]
        kg = kg[kg != i]
        cands = {"fine": fg, "kind": kg}
        # chain: fine when it holds >= k rows, else kind, else full
        chain = fg if len(fg) >= k else kg
        use = {"fine": fg, "kind": kg, "chain": chain}
        for a_ in arms:
            c = use[a_]
            sub = X[c]
            s = sub @ X[i]
            take = c[np.argsort(-s)[:k]] if len(c) else np.array([], dtype=np.int64)
            ov = len(set(take.tolist()) & set(oracle[r].tolist()))
            rec[a_].append(ov / k)
            cost[a_].append(len(c))
            hit1[a_].append(1 if (len(take) and take[0] == oracle[r][0]) else 0)

    print(f"\nquery-side candidate set (items_scored equivalent): budget today = 24000")
    for a_ in arms:
        print(f"  {a_:5s}: {pr(cost[a_])}")
    print(f"\nrecall@{k} vs exact global oracle (what a hard tag filter throws away)")
    for a_ in arms:
        print(f"  {a_:5s}: recall@{k}={statistics.fmean(rec[a_]):.4f}  "
              f"hit@1={statistics.fmean(hit1[a_]):.4f}  "
              f"work vs budget 24000 = {24000/statistics.fmean(cost[a_]):.0f}x less")

    # quality of the scoped answer: cosine gap between the in-group best and the
    # global best (both self-excluded) — how much score the filter gives up
    for a_ in ("fine", "chain"):
        gaps = []
        for r, i in enumerate(qi):
            c = fine[r][fine[r] != i] if a_ == "fine" else (
                fine[r][fine[r] != i] if len(fine[r]) - 1 >= k
                else kind_idx[kinds[i]][kind_idx[kinds[i]] != i])
            if not len(c):
                continue
            gaps.append(float(X[oracle[r][0]] @ X[i] - np.max(X[c] @ X[i])))
        g = sorted(gaps)
        print(f"cosine gap {a_:5s} (global best - in-group best): "
              f"mean={statistics.fmean(g):.4f} p50={g[len(g)//2]:.4f} "
              f"p90={g[int(.9*len(g))]:.4f} max={g[-1]:.4f}")

    print(f"\nfallback trigger rate (fine group too small to hold top-{k} after self-drop): "
          f"{100*sum(1 for r in range(len(qi)) if len(fine[r]) - 1 < k)/len(qi):.1f}%")

    if a.live:
        sel = qi[:a.live]
        ok = 0
        errs = 0
        live_rec, live_cost, live_lat, live_top1 = [], [], [], []
        for pos, i in enumerate(sel):
            body = json.dumps({"vec": X[i].tolist(), "topk": k + 1}).encode()
            req = urllib.request.Request(LIVE, data=body,
                                         headers={"Content-Type": "application/json"})
            t1 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read())
            except Exception:
                errs += 1
                continue
            live_lat.append((time.perf_counter() - t1) * 1000)
            got = [h["sid"] for h in d.get("hits", []) if h["sid"] != sids[i]][:k]
            truth = [sids[j] for j in oracle[pos]]
            live_rec.append(len(set(got) & set(truth)) / k)
            live_top1.append(1 if (got and got[0] == truth[0]) else 0)
            live_cost.append(d.get("route", {}).get("items_scored", 0))
            ok += 1
        if ok:
            print(f"\nlive :8096 baseline ({ok} queries, {errs} errors, self-sid dropped)")
            print(f"  recall@{k}={statistics.fmean(live_rec):.4f}  "
                  f"hit@1={statistics.fmean(live_top1):.4f}  "
                  f"items_scored={int(statistics.median(live_cost))}  "
                  f"lat p50={sorted(live_lat)[len(live_lat)//2]:.0f} ms")



    if a.scope_port:
        base = f"http://127.0.0.1:{a.scope_port}/v1/state/search"
        kinds_all = np.array([f.split("/")[0] for f in files])
        n_ok = n_bad = n_narrow = widened = n_sid = 0
        lat, scored, wrong = [], [], []
        for i in qi[:a.scope_n]:
            scope = files[i]
            req = urllib.request.Request(
                base, json.dumps({"vec": X[i].tolist(), "topk": k, "scope": scope}).encode(),
                {"Content-Type": "application/json"})
            t1 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    d = json.loads(r.read())
            except urllib.error.HTTPError as e:
                try:
                    j = json.loads(e.read())
                except Exception:
                    j = None
                if j and j.get("error") == "scope_too_narrow":
                    n_narrow += 1
                    continue
                raise
            lat.append((time.perf_counter() - t1) * 1000)
            rt = d.get("route", {})
            scored.append(rt.get("items_scored", 0))
            if rt.get("mode") == "scope+kind":
                widened += 1
            # oracle over EXACTLY the candidate set the server is allowed to use
            fine_rows = np.where(np.array(files) == scope)[0]
            cand = fine_rows if len(fine_rows) >= k else np.where(kinds_all == kinds_all[i])[0]
            sub = X[cand]
            order = np.argsort(-(sub @ X[i]))[:k]
            truth = {sids[j] for j in cand[order]}
            got = {h["sid"] for h in d.get("hits", [])}
            # compare SCORES with a tolerance first: an exact tie in cosine
            # (measured gap 0.0) makes two equal-scoring rows swap rank between
            # the server's first-seen-wins insert and numpy's unstable sort,
            # which is a tie-break, not a wrong answer.
            got_sc = sorted((h.get("score", -9.0) for h in d.get("hits", [])), reverse=True)
            truth_sc = sorted((float(sub[j] @ X[i]) for j in order), reverse=True)
            score_eq = (len(got_sc) == len(truth_sc) and
                        all(abs(a - b) <= 1e-4 for a, b in zip(got_sc, truth_sc)))
            if score_eq:
                n_ok += 1
                if got == truth:
                    n_sid += 1
            else:
                n_bad += 1
                if len(wrong) < 3:
                    wrong.append((int(i), sorted(got - truth)[:2], sorted(truth - got)[:2]))
        # fair A/B: the SAME server, same moment, unscoped — so host load
        # cannot flatter one arm
        lat_unc = []
        for i in qi[:40]:
            req = urllib.request.Request(
                base, json.dumps({"vec": X[i].tolist(), "topk": k}).encode(),
                {"Content-Type": "application/json"})
            t1 = time.perf_counter()
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    json.loads(r.read())
            except Exception:
                continue
            lat_unc.append((time.perf_counter() - t1) * 1000)
        small = [l for l, sc in zip(lat, scored) if sc <= 1000]
        big = [l for l, sc in zip(lat, scored) if sc > 1000]
        print(f"\nscoped arm on :{a.scope_port} ({n_ok + n_bad + n_narrow} queries, "
              f"{n_narrow} rejected as scope_too_narrow)")
        print(f"  SAME-server A/B latency: unscoped p50={pr(lat_unc)}" if lat_unc else "")
        print(f"  server scores == numpy exact-in-group scores: {n_ok}/{n_ok + n_bad}"
              f"   (sid-identical: {n_sid})" + (f"  MISMATCH {wrong}" if wrong else ""))
        if scored:
            print(f"  items_scored: {pr(scored)}")
        if lat:
            print(f"  scoped latency: {pr(lat)}  widened to kind: {widened}")
            if small:
                print(f"  scoped, group <= 1000 rows (n={len(small)}): p50={sorted(small)[len(small)//2]:.1f} ms")
            if big:
                print(f"  scoped, group > 1000 rows (n={len(big)}): p50={sorted(big)[len(big)//2]:.1f} ms")
        if scored:
            print(f"  work vs the 24000 budget: {24000 / statistics.median(scored):.0f}x "
                  "less at the median")


    if a.tag_port:
        base = f"http://127.0.0.1:{a.tag_port}/v1/state/search"
        tag_of = {}
        tsv = os.path.join(os.path.dirname(INDEX), "ann_tags.tsv")
        with open(tsv, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 2:
                    tag_of[parts[0]] = parts[1]
        tags = sorted(set(tag_of.values()))
        print(f"\ntag arm on :{a.tag_port}  ({len(tags)} tags, {a.tag_n} queries each)")
        print(f"  {'tag':<16} {'rows':>6} {'q':>4} {'match':>7} {'scored':>8} "
              f"{'p50ms':>7} {'vs 24000':>9}")
        total_q = total_ok = 0
        for tg in tags:
            rows_of = [i for i in range(n) if tag_of.get(sids[i]) == tg]
            if len(rows_of) < 3:
                continue
            rng = np.random.default_rng(11)
            pick = rng.choice(len(rows_of), size=min(a.tag_n, len(rows_of)), replace=False)
            lat, scored, okc = [], [], 0
            for j in pick:
                i = rows_of[j]
                body = json.dumps({"vec": X[i].tolist(), "topk": k,
                                   "scope": "tag:" + tg}).encode()
                req = urllib.request.Request(
                    base, body, {"Content-Type": "application/json"})
                t1 = time.perf_counter()
                try:
                    with urllib.request.urlopen(req, timeout=60) as r:
                        d = json.loads(r.read())
                except urllib.error.HTTPError as e:
                    j2 = None
                    try:
                        j2 = json.loads(e.read())
                    except Exception:
                        pass
                    if j2 and j2.get("error") in ("scope_too_narrow", "scope_not_found"):
                        continue
                    raise
                lat.append((time.perf_counter() - t1) * 1000)
                scored.append(d.get("route", {}).get("items_scored", 0))
                # oracle: exact cosine top-k inside the tag (self included,
                # matching the server, which does not drop the query row)
                cand = np.asarray(rows_of)
                sub = X[cand]
                order = np.argsort(-(sub @ X[i]))[:k]
                truth = {sids[cand[o]] for o in order}
                got = {h["sid"] for h in d.get("hits", [])}
                gs = sorted((h.get("score", -9.0) for h in d.get("hits", [])), reverse=True)
                ts = sorted((float(sub[o] @ X[i]) for o in order), reverse=True)
                score_eq = (len(gs) == len(ts) and
                            all(abs(a2 - b2) <= 1e-4 for a2, b2 in zip(gs, ts)))
                if score_eq:
                    okc += 1
            total_q += len(pick); total_ok += okc
            p50 = sorted(lat)[len(lat) // 2] if lat else float("nan")
            med = sorted(scored)[len(scored) // 2] if scored else 0
            print(f"  {tg:<16} {len(rows_of):>6} {len(pick):>4} {okc:>7} {med:>8} "
                  f"{p50:>7.1f} {24000 / max(1, med):>8.1f}x")
        print(f"  server scores == numpy exact-in-tag scores: {total_ok}/{total_q}")

if __name__ == "__main__":
    sys.exit(main())
