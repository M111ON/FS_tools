"""bench_zone_floor.py — where does a zone-scoped query spend its time?

The 2026-10-26 soft-zone bench showed hard-zone p50 8.8 ms scoring only 220
rows/query against global p50 13.5 ms scoring 16,712. Slope = 0.28 us/row, so
~8.7 ms of a zone query is NOT row scanning. Building a per-zone mini vector DB
only pays if that fixed cost is the prepass (which a zone roster removes), not
embedding / HTTP (which it cannot touch).

Probe: same box, same server, pre-embedded query vecs (no embedder in the path),
p50 over N queries for
  - /health                      : HTTP floor
  - topk 1 / 5 / 50              : heap cost
  - scope zone:<tiny> .. <huge>   : does latency track zone size at all?
  - file:<grp>                   : a 2-pass strcmp scope of similar size
  - no scope                     : the global walk

If zone latency is flat across 59 -> 21k rows, the row scan is not the cost and
the fixed overhead is what a zone index would have to attack.
"""
import argparse
import json
import random
import os
import statistics
import time
import urllib.request
import urllib.error

H = "http://127.0.0.1:8096"  # overridden by --url
DIM = 384


def post(body, to=120):
    r = urllib.request.Request(H + "/v1/state/search", json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    t0 = time.perf_counter()
    try:
        d = json.loads(urllib.request.urlopen(r, timeout=to).read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"refused: { {k: (v if k != 'vec' else '<vec>') for k, v in body.items()} }: {msg}")
    return (time.perf_counter() - t0) * 1000.0, d


def get(path, to=120):
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    return (time.perf_counter() - t0) * 1000.0, d


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return -1.0
    k = max(0, min(len(xs) - 1, int(round((len(xs) - 1) * p / 100.0))))
    return xs[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=40)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--url", default=None)
    a = ap.parse_args()
    if a.url:
        globals()["H"] = a.url.rstrip("/")

    _, tags = get("/v1/state/tags")
    zones = [z for z in tags.get("zones", []) if z.get("rows", 0) > 0]
    zones.sort(key=lambda z: z["rows"])

    # a tiny, a mid and the biggest zone
    picks = [zones[0], zones[len(zones) // 2], zones[-1]]
    tagz = picks

    rng = random.Random(a.seed)
    qs = []
    # dim from the real index (a zero vec is rejected by the sanitizer, so it
    # cannot be used to probe the echo)
    dim = DIM
    idx = os.environ.get("ANN_BENCH_INDEX", r"I:\tools\ann\data\memcore\ann_index.jsonl")
    with open(idx, "r", encoding="utf-8") as f:
        r = json.loads(f.readline())
        dim = int(r.get("dim") or len(r.get("vec") or []) or DIM)
    print(f"dim {dim}: {len(zones)} zones, rows {zones[0]['rows']} .. {zones[-1]['rows']}")
    # REAL query distribution: sample rows from the index and use their own
    # vectors. Random gaussians do not land anywhere near the trained route
    # (measured: they trip the full-scan fallback, 63,853 rows, 62 ms), so a
    # gaussian probe measures the fallback, not the routed path.
    rows = []
    with open(idx, "r", encoding="utf-8") as f:
        for line in f:
            if rng.random() < 0.02:
                r = json.loads(line)
                v = r.get("vec") or []
                if len(v) == dim:
                    rows.append([float(x) for x in v])
            if len(rows) >= a.queries * 4:
                break
    rng.shuffle(rows)
    qs = rows[:a.queries]
    print(f"queries: {len(qs)} real row vectors (self-included, latency only)")

    def run(label, make_body):
        lat, scored = [], []
        for v in qs:
            try:
                ms, d = post(make_body(v))
            except RuntimeError as e:
                print(f"{label:<26} REFUSED  {e}")
                return None
            lat.append(ms)
            scored.append(d.get("route", {}).get("items_scored", -1))
        print(f"{label:<26} p50 {pct(lat,50):6.1f}  p90 {pct(lat,90):6.1f}  "
              f"scored {int(statistics.median(scored)):>6}  n={len(lat)}")
        return pct(lat, 50)

    hl, _ = get("/health")
    print(f"{'/health (HTTP floor)':<26} {hl:6.1f}")

    base = run(f"global topk={a.topk}", lambda v: {"vec": v, "topk": a.topk})
    run("global topk=1", lambda v: {"vec": v, "topk": 1})
    run("global topk=32 (cap)", lambda v: {"vec": v, "topk": 32})

    for z in tagz:
        run(f"zone:{z['zone']} ({z['rows']} rows)",
            lambda v, z=z: {"vec": v, "topk": a.topk, "scope": f"zone:{z['zone']}"})

    # a file group of similar size to the mid zone, if any
    _, apps = get("/v1/state/tags")
    files = sorted(apps.get("zones", []), key=lambda z: z["rows"])

    print()
    print("read: if zone latency is flat from the smallest to the biggest zone,")
    print("the row scan is not the cost; the fixed part (prepass/HTTP/heap) is.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
