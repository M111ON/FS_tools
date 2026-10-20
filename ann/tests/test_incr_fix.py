"""test_incr_fix.py — regression test for the incremental-regrow contract
(2026-10-20, commit 1e1e342).

Verifies the three regrow modes end-to-end on a scratch server (port 8097,
NEVER the live :8096), against a small locally-built index:

  C1  fresh base  -> mode "full", routed=1, search mode anchor
  C2  append rows -> mode "incremental", n_prev/n_new correct
  C3  anchors header ntr == index rows (the incident's root symptom)
  C4  after incremental, routing stays anchor (NOT brute fallback)
  C5  unchanged fast path < 1s (design target 0.5s, jitter room)

Scratch lives under %TEMP% and is rebuilt per run — nothing inside the repo's
data/ dir is written. Run: python test_incr_fix.py  (exit 0 = PASS).
"""
import os
import json
import time
import shutil
import struct
import subprocess
import sys
import urllib.request
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                      # I:/tools/ann
EXE  = os.path.join(REPO, "build", "ann_search_batch.exe")
PORT = 8097
BASE = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-incrfx-scratch")
D    = 384
N_ROWS = 400

IDX  = os.path.join(BASE, "data", "memcore", "index.jsonl")
ANCH = os.path.join(BASE, "ann.bin")
ORDP = os.path.join(BASE, "order.bin")
LOGP = os.path.join(BASE, "route.log")

env = {**os.environ,
       "ANN_INDEX_PATH": IDX,
       "ANN_ANCH_PATH":  ANCH,
       "ANN_ORDER_PATH": ORDP,
       "ANN_ROUTE_LOG":  LOGP,
       "ANN_TOMBSTONE_WARM": "1",
       "ANN_FLOAT_KERNEL": "1"}

def build_index():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(os.path.dirname(IDX), exist_ok=True)
    # Deterministic pseudo-vectors: row i varies along a 4-block pattern, so
    # kmeans splits stay stable and anchors are reproducible across runs.
    step = 0.4 / N_ROWS
    with open(IDX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            x = step * i
            v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
            assert len(v) == D, f"dim mismatch {len(v)}"
            f.write('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, ",".join("%.6f" % x for x in v)))

def srv_start():
    srv = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.3)
        try:
            h = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=4).read())
            if h.get("status") == "ok":
                return srv
        except Exception:
            pass
    srv.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)

def regrow(body=""):
    t0 = time.perf_counter()
    d = json.loads(urllib.request.urlopen(Request(
        f"http://127.0.0.1:{PORT}/v1/state/regrow", body.encode(),
        {"Content-Type": "application/json"}), timeout=300).read())
    return (time.perf_counter() - t0) * 1000, d

def search(q="geometric", k=2):
    return json.loads(urllib.request.urlopen(Request(
        f"http://127.0.0.1:{PORT}/v1/state/search",
        json.dumps({"query": q, "k": k}).encode(),
        {"Content-Type": "application/json"}), timeout=60).read())

def health():
    return json.loads(urllib.request.urlopen(
        f"http://127.0.0.1:{PORT}/health", timeout=10).read())

def anchors_ntr():
    return struct.unpack("<5I", open(ANCH, "rb").read(20))[3]

def row_count():
    return sum(1 for _ in open(IDX, encoding="utf-8"))

fails, checks = [], []

def check(name, cond, detail=""):
    (checks if cond else fails).append((name, detail))
    print(("  ok  " if cond else "  FAIL ") + name +
          (f" | {detail}" if detail else ""))

build_index()
srv = srv_start()
try:
    w1, d1 = regrow()
    rg1 = d1.get("regrow", {})
    check("C1 full mode", rg1.get("mode") == "full",
          f"{w1:.0f}ms {rg1.get('mode')}")
    check("C1-b routed=1 after full", health()["routed"] == 1)
    check("C1-c search anchor",
          search()["route"]["mode"].startswith("anchor"))

    VA = ",".join("0.01" for _ in range(D))
    with open(IDX, "a", encoding="utf-8") as f:
        for sid in ("probe-fix-a", "probe-fix-b"):
            f.write('{"sid":"%s","file":"_probe","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (sid, VA))
    w2, d2 = regrow()
    rg2 = d2.get("regrow", {})
    check("C2 incremental mode", rg2.get("mode") == "incremental",
          rg2.get("mode"))
    check("C2-b n_prev/n_new",
          rg2.get("n_prev") == N_ROWS and rg2.get("n_new") == 2,
          f"n_prev={rg2.get('n_prev')} n_new={rg2.get('n_new')}")

    check("C3 anchors ntr == rows",
          anchors_ntr() == row_count(),
          f"ntr={anchors_ntr()} rows={row_count()}")
    h = health()
    check("C4 routed stays 1",
          h["routed"] == 1 and h["entries"] == N_ROWS + 2,
          f"routed={h['routed']} entries={h['entries']}")
    check("C4-b search not brute",
          search()["route"]["mode"].startswith("anchor"))

    w3, d3 = regrow()
    check("C5 unchanged fast <1s",
          d3.get("regrow", {}).get("mode") == "unchanged" and w3 < 1000,
          f"{w3:.0f}ms {d3['regrow']['mode']}")
finally:
    srv.kill()
    time.sleep(1)
    try:
        shutil.rmtree(BASE)
    except OSError:
        pass

print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)"
      if not fails else f"{len(fails)} FAILURES")
sys.exit(0 if not fails else 1)
