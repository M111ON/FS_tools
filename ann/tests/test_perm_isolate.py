#!/usr/bin/env python3
"""test_perm_isolate.py — perm-sidecar isolation contract (2026-10-20).

Scratch server on port 8097 only (NEVER the live :8096). Builds a small
deterministic 384-dim index (same shape as test_incr_fix.py so the e5 embed
sidecar can embed the query text), then verifies the ISOLATION CONTRACT:

  P1 fresh base: regrow mode "full".
  P2 append + incremental regrow: jsonl untouched by the regrow itself
     (size after regrow == size after the tested append; mtime unchanged).
  P3 the sidecar <index>.stamp exists, size == n*8.
  P4 unchanged fast-path: mode "unchanged", with sidecar present.
  P5 search correctness after sidecar overlay: not brute, hits>=1,
     health routed=1 and entries == n.
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
EXE = os.path.join(REPO, "build", "ann_search_isolate.exe")
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-isolate-scratch", "perm")
PORT = 8097
H = f"http://127.0.0.1:{PORT}"
INDEX = os.path.join(SCRATCH, "data", "memcore", "index.jsonl")
N_ROWS = 400

ok_l, fails = [], []
def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")

def http(path, body=None, to=60):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = Request(H + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())

def build():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(os.path.dirname(INDEX), exist_ok=True)
    step = 0.4 / N_ROWS
    with open(INDEX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            x = step * i
            v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
            f.write('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, ",".join("%.6f" % x for x in v)))

def append(n_extra):
    step = 0.4 / N_ROWS
    with open(INDEX, "a", encoding="utf-8") as f:
        for i in range(N_ROWS, N_ROWS + n_extra):
            x = step * i
            v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
            f.write('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, ",".join("%.6f" % x for x in v)))
    return N_ROWS + n_extra

def start():
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOMBSTONE_WARM": "1"}
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.3)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)

def main():
    build()
    p = start()
    try:
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] == "full", "P1 fresh base mode full", g["regrow"]["mode"])
        time.sleep(1.1)
        n_after = append(4)
        size_after_append = os.path.getsize(INDEX)
        mtime_after_append = os.stat(INDEX).st_mtime
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] == "incremental", "P2 append mode incremental",
           f"{g['regrow']['mode']} n_new={g['regrow'].get('n_new')}")
        ok(os.path.getsize(INDEX) == size_after_append, "P2 jsonl size untouched by regrow",
           f"{os.path.getsize(INDEX)} vs {size_after_append}")
        ok(os.stat(INDEX).st_mtime == mtime_after_append, "P2 jsonl mtime untouched by regrow", "")
        sp = INDEX + ".stamp"
        sz = os.path.getsize(sp) if os.path.exists(sp) else -1
        ok(sz == n_after * 8, "P3 sidecar size == n*8", f"{sz} vs {n_after*8}")
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] == "unchanged", "P4 unchanged fast path", g["regrow"]["mode"])
        r = http("/v1/state/search", {"query": "geometric", "k": 4})
        rt = r["route"]
        ok(not rt["mode"].startswith("brute"), "P5 search not brute", rt["mode"])
        ok(len(r.get("hits", [])) >= 1, "P5 hits>=1", str(len(r.get("hits", []))))
        h = http("/health", to=10)
        ok(h["routed"] == 1 and h["entries"] == n_after, "P5 health routed=1 entries",
           f"routed={h['routed']} entries={h['entries']}")
    finally:
        p.poll() or p.kill()
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)" if not fails
          else f"{len(fails)} FAILURES")
    sys.exit(0 if not fails else 1)

if __name__ == "__main__":
    main()
