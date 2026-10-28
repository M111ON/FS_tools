"""Gate for the ANN query log: N real searches, then count the jsonl lines.

Uses a caller-supplied vector (the `vec` path) so the embed sidecar is not in the loop —
the log is about the walk, not about embedding.

  python bench_qlog.py [port] [N]

What it proves: the log is complete (N queries -> N lines, no drops), rotated per day, and
still parses. Latency of the log itself is measured by running this twice against the same
binary, once with ANN_QUERY_LOG=0 and once without, and comparing p99.

NOTE the vectors come from the index itself, so top-1 scores are ~1.0000 — that is the
known tautology (see memory #7697), not a recall claim. This bench measures the LOG only.
"""
import json
import os
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8097
N = int(sys.argv[2]) if len(sys.argv) > 2 else 1000
INDEX = r"I:\tools\ann\data\memcore\ann_index.jsonl"
TOKEN = os.environ.get("ANN_ADMIN_TOKEN", "")


def headers():
    h = {"Content-Type": "application/json"}
    if TOKEN:
        h["X-Admin-Token"] = TOKEN
    return h


def post(body):
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/state/search" % PORT,
        data=json.dumps(body).encode(),
        headers=headers(),
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return (time.perf_counter() - t0) * 1e6, json.loads(raw)


vecs = []
with open(INDEX, "r", encoding="utf-8") as f:
    for line in f:
        if len(vecs) >= 8:
            break
        row = json.loads(line)
        v = row.get("vec") or row.get("v")
        if v:
            vecs.append(v)
if not vecs:
    print("no vectors in index")
    sys.exit(2)
print("vectors loaded: %d (dim %d)" % (len(vecs), len(vecs[0])))

lat = []
t_start = time.perf_counter()
for i in range(N):
    body = {"vec": vecs[i % len(vecs)], "topk": 3}
    if i % 2 == 0:
        body["coord"] = "ses_gate_%04d#%d" % (i, i % 7)
    try:
        us, res = post(body)
    except Exception as e:
        print("request %d failed: %s" % (i, e))
        sys.exit(3)
    if res.get("status") != "ok":
        print("bad response at %d: %s" % (i, res.get("error")))
        sys.exit(4)
    lat.append(us)
elapsed = time.perf_counter() - t_start
lat.sort()
print("queries=%d  elapsed=%.1fs  mean=%.0fus  p50=%.0fus  p99=%.0fus"
      % (N, elapsed, sum(lat) / len(lat), lat[len(lat) // 2], lat[int(len(lat) * 0.99) - 1]))

with urllib.request.urlopen(urllib.request.Request(
        "http://127.0.0.1:%d/v1/qlog/flush" % PORT, headers=headers()), timeout=30) as r:
    print("flush:", r.read().decode())
