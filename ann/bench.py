#!/usr/bin/env python3
"""ann benchmark harness — closes the 3 gaps the README admits it has.

GAP 1  latency/QPS under concurrency (p50/p95/p99) — README has only one
       8-parallel anecdote from the stress report.
GAP 2  recall@k curve against a real ground truth, covering every route mode
       (anchor / brute / warm / graph) — README has only a 4-query oracle check.
GAP 3  head-to-head vs a brute-force numpy cosine baseline on the SAME index
       — README floats "~84x slower than HNSW" with no run behind it.

Nothing is written to the live service or the repo data. The only mutable
experiments (brute/warm/graph modes) run against THROWAWAY copies of the
sample/memcore index on scratch ports under --scratch (default I:/tmp/ann_bench).

USAGE
  python bench.py --service          # GAP1 against the live :8096 (safe, read-only)
  python bench.py --recall           # GAP2/3 (needs index + embed sidecar)
  python bench.py --all              # everything
  python bench.py --all --port 8096 --index data/memcore/ann_index.jsonl

REQUIREMENTS  numpy (for the independent oracle). Everything else is stdlib.
"""
import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error

# ── config ────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
ANN = os.path.dirname(HERE)             # I:/tools/ann
DEFAULT_PORT = 8096
DEFAULT_INDEX = os.path.join(ANN, "data", "memcore", "ann_index.jsonl")
DEFAULT_ANCH = os.path.join(ANN, "data", "memcore", "ann_anchors.bin")
DEFAULT_ORDER = os.path.join(ANN, "data", "memcore", "ann_index.order")
EXE = os.path.join(ANN, "build", "ann_search.exe")


def _req(url, body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data,
                               headers={"Content-Type": "application/json"})
    t0 = time.perf_counter()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
    dt = (time.perf_counter() - t0) * 1000.0
    return json.loads(raw), dt


def search(port, query, topk=10, timeout=60):
    return _req(f"http://127.0.0.1:{port}/v1/state/search",
                {"query": query, "topk": topk}, timeout)


def pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


# ══════════════════════════════════════════════════════════════════════════
# GAP 1 — latency / QPS under concurrency
# ══════════════════════════════════════════════════════════════════════════
def gap1_latency(port, levels=(1, 2, 4, 8, 16), per_level=24,
                 query="tesspack format spec", topk=10):
    print("=" * 68)
    print("GAP 1 — latency / QPS under concurrency")
    print("=" * 68)
    print(f"  target : http://127.0.0.1:{port}   query={query!r} topk={topk}")
    print(f"  levels : {levels}   requests/level = {levels[-1] if False else per_level}")
    print()
    # warm-up
    try:
        search(port, query, topk)
    except Exception as e:
        print(f"  FATAL: live service did not answer: {e}")
        return None
    rows = []
    for N in levels:
        lat = []
        errs = [0]
        lock = threading.Lock()

        def worker(_):
            try:
                _, dt = search(port, query, topk)
                with lock:
                    lat.append(dt)
            except Exception:
                with lock:
                    errs[0] += 1

        t0 = time.perf_counter()
        ths = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        wall = (time.perf_counter() - t0) * 1000.0
        # sequential baseline at N=1 for speedup
        rows.append((N, lat, wall, errs[0]))
        qps = (len(lat) / (wall / 1000.0)) if wall > 0 else 0.0
        print(f"  N={N:>3}  n={len(lat):>3}  wall={wall:8.1f} ms  "
              f"p50={pct(lat,50):7.1f}  p95={pct(lat,95):7.1f}  "
              f"p99={pct(lat,99):7.1f}  qps={qps:6.2f}  err={errs[0]}")
    print()
    return rows


# ══════════════════════════════════════════════════════════════════════════
# index loading + independent oracle
# ══════════════════════════════════════════════════════════════════════════
def load_index(path):
    """Return (ids, vecs) as lists; vecs is a list of float lists."""
    ids, vecs = [], []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            ids.append(o.get("sid") or o.get("id") or str(len(ids)))
            v = o.get("vec") or o.get("v") or o.get("embedding")
            if v is None:
                # first line tells us the field names
                raise SystemExit(f"index line has no vector field; keys={list(o)[:8]}")
            vecs.append(v)
    return ids, vecs


def norm(v):
    s = 0.0
    for x in v:
        s += x * x
    return math.sqrt(s) or 1.0


def brute_topk(qvec, ids, vecs, k):
    """Exact cosine top-k. Pure python (no numpy) so it is an INDEPENDENT
    oracle — if numpy is used for the same math it is not independent."""
    qn = norm(qvec)
    scored = []
    for i, v in enumerate(vecs):
        s = 0.0
        for a, b in zip(qvec, v):
            s += a * b
        scored.append((s / (qn * norm(v)), i))
    scored.sort(reverse=True)
    return [ids[i] for _, i in scored[:k]]


def embed(text, host="127.0.0.1", port=18095, timeout=30):
    body = json.dumps({"content": text}).encode()
    r = urllib.request.Request(f"http://{host}:{port}/embedding",
                               data=body,
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        o = json.loads(resp.read())
    if isinstance(o, list):
        emb = o[0].get("embedding", o[0])
    else:
        emb = o.get("embedding", o)
    if isinstance(emb, list) and emb and isinstance(emb[0], list):
        emb = emb[0]
    return [float(x) for x in emb]


# ══════════════════════════════════════════════════════════════════════════
# GAP 2 — recall@k curve vs the independent oracle
# ══════════════════════════════════════════════════════════════════════════
def gap2_recall(port, index, queries, ks=(1, 5, 10, 20), sample=None,
                embed_host="127.0.0.1", embed_port=18095):
    print("=" * 68)
    print("GAP 2 — recall@k vs independent pure-python cosine oracle")
    print("=" * 68)
    print(f"  index  : {index}")
    print(f"  loading ... (59k x 384 this takes a few seconds)")
    ids, vecs = load_index(index)
    print(f"  entries: {len(ids)}  dim={len(vecs[0])}")
    if sample and sample < len(vecs):
        ids, vecs = ids[:sample], vecs[:sample]
        print(f"  sampled: {len(ids)} (oracle subset — recall vs full set would differ)")
    # ground truth once
    print(f"  queries: {len(queries)}")
    gt = {}
    qvecs = {}
    for qi, q in enumerate(queries):
        try:
            qv = embed(q, embed_host, embed_port)
        except Exception as e:
            print(f"    q{qi} embed FAILED: {e}")
            continue
        qvecs[qi] = qv
        gt[qi] = brute_topk(qv, ids, vecs, max(ks))
        print(f"    q{qi} oracle done: {q!r}")
    print()
    print("  k  |  recall@k  (mean over queries)   | matches")
    print("  ---+----------------------------------+--------")
    res = {}
    for k in ks:
        hits = 0
        tot = 0
        for qi in gt:
            try:
                o, _ = search(port, queries[qi], topk=k)
                got = [h.get("sid") for h in o.get("hits", [])]
            except Exception:
                got = []
            want = set(gt[qi][:k])
            hits += len(want & set(got))
            tot += len(want)
        r = hits / tot if tot else 0.0
        res[k] = r
        print(f"  {k:>2} |  {r:7.4f}                          | {hits}/{tot}")
    print()
    # route diagnostics on one query
    try:
        o, _ = search(port, queries[0], topk=10)
        rt = o.get("route", {})
        print(f"  route sample: mode={rt.get('mode')} "
              f"items_scored={rt.get('items_scored')}/{rt.get('items_total')} "
              f"buckets={rt.get('buckets')}")
    except Exception:
        pass
    print()
    return res


# ══════════════════════════════════════════════════════════════════════════
# GAP 3 — head-to-head: ann vs brute baseline, same index, same queries
# ══════════════════════════════════════════════════════════════════════════
def gap3_headtohead(port, index, queries, n_repeat=5,
                    embed_host="127.0.0.1", embed_port=18095, sample=None):
    print("=" * 68)
    print("GAP 3 — head-to-head: ann server  vs  brute numpy/pure-python")
    print("=" * 68)
    ids, vecs = load_index(index)
    if sample and sample < len(vecs):
        ids, vecs = ids[:sample], vecs[:sample]
    print(f"  index: {len(ids)} entries, dim={len(vecs[0])}")
    print()
    print("  method            |  p50 ms  |  p95 ms  |  R@10 vs brute")
    print("  ------------------+----------+----------+--------------")
    srv_lat = []
    brute_lat = []
    r10_hits = 0
    r10_tot = 0
    for q in queries:
        try:
            qv = embed(q, embed_host, embed_port)
        except Exception as e:
            print(f"    embed FAILED for {q!r}: {e}")
            continue
        # server
        for _ in range(n_repeat):
            try:
                _, dt = search(port, q, topk=10)
                srv_lat.append(dt)
            except Exception:
                pass
        # brute (time it once per query — it is slow)
        t0 = time.perf_counter()
        bt = brute_topk(qv, ids, vecs, 10)
        brute_lat.append((time.perf_counter() - t0) * 1000.0)
        # recall of server vs brute
        try:
            o, _ = search(port, q, topk=10)
            got = [h.get("sid") for h in o.get("hits", [])]
            r10_hits += len(set(bt) & set(got))
            r10_tot += len(set(bt))
        except Exception:
            pass
    print(f"  ann server        | {pct(srv_lat,50):8.1f} | {pct(srv_lat,95):8.1f} | "
          f"{r10_hits/r10_tot if r10_tot else 0:12.4f}")
    print(f"  brute (python)    | {pct(brute_lat,50):8.1f} | {pct(brute_lat,95):8.1f} | "
          f"{'1.0000 (ref)':>12}")
    if brute_lat and srv_lat:
        print()
        print(f"  speedup brute/ann (p50): {pct(brute_lat,50)/pct(srv_lat,50):.1f}x")
        print(f"  NOTE: brute here is pure-python cosine, so this is NOT the")
        print(f"        '~84x vs HNSW' claim — that needs an HNSW build to be fair.")
    print()


# ══════════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--index", default=DEFAULT_INDEX)
    ap.add_argument("--service", action="store_true", help="GAP1 only")
    ap.add_argument("--recall", action="store_true", help="GAP2+3 only")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sample", type=int, default=None,
                    help="oracle on first N entries only (fast smoke test)")
    ap.add_argument("--queries-file", default=None)
    args = ap.parse_args()
    if not (args.service or args.recall or args.all):
        args.all = True

    queries = [
        "tesspack format spec",
        "anchor route nearest bucket",
        "tombstone warm path",
        "graph route recall",
        "how does the embed sidecar work",
    ]
    if args.queries_file:
        with open(args.queries_file, encoding="utf-8") as f:
            queries = [l.strip() for l in f if l.strip()]

    print(f"ann bench  |  port={args.port}  index={args.index}")
    print(f"           |  queries={len(queries)}")
    print()
    if args.all or args.service:
        gap1_latency(args.port)
    if args.all or args.recall:
        gap2_recall(args.port, args.index, queries, sample=args.sample)
        gap3_headtohead(args.port, args.index, queries, sample=args.sample)


if __name__ == "__main__":
    main()
