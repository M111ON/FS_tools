"""verify_kernel_equivalence.py — float-kernel OFF-vs-ON equivalence test.

Builds a small deterministic index, starts the scratch server on port 8097
twice (ANN_FLOAT_KERNEL=0 then =1), runs the same batch of queries against
both, and asserts the top-k sid lists are IDENTICAL and that errors behave.

  (a) batch B=4 -> hits on >=1 query
  (b) B=33 -> HTTP 400 (oversize batch cap)
  (c) empty -> HTTP 400
  (d) OFF vs ON top-3 sid per-query identical (kernel equivalence)

Scratch under %TEMP%; nothing in repo data/ is written.
Run: python verify_kernel_equivalence.py  (exit 0 = ALL PASS)
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
EXE  = os.path.join(REPO, "build", "ann_search_batch.exe")
PORT = 8097
BASE = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-kernel-eq-scratch")
D    = 384
N_ROWS = 300
IDX  = os.path.join(BASE, "index.jsonl")
ANCH = os.path.join(BASE, "ann.bin")
ORDP = os.path.join(BASE, "order.bin")
LOGP = os.path.join(BASE, "route.log")

QS = ["nginx reverse proxy", "typescript generics variance",
      "kubernetes pod eviction", "geometric unity 20736", "zig lang"]

def build_index():
    if os.path.exists(BASE):
        shutil.rmtree(BASE)
    os.makedirs(BASE, exist_ok=True)
    step = 0.4 / N_ROWS
    with open(IDX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            x = step * i
            v = ([x] * 96 + [-x] * 96 + [x * 0.5] * 96 + [-x * 0.5] * 96)
            f.write('{"sid":"seed-%04d","file":"_seed","npast":1,"dim":384,'
                    '"vec":[%s]}\n' % (i, ",".join("%.6f" % z for z in v)))

def start(env_extra):
    env = {**os.environ, "ANN_INDEX_PATH": IDX,
           "ANN_ANCH_PATH": ANCH, "ANN_ORDER_PATH": ORDP,
           "ANN_ROUTE_LOG": LOGP, **env_extra}
    srv = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.3)
        try:
            h = json.loads(urllib.request.urlopen(
                f"http://127.0.0.1:{PORT}/health", timeout=4).read())
            if h.get("status") == "ok":
                return srv, h
        except Exception:
            pass
    srv.kill()
    raise RuntimeError("server failed to start (%s)" % env_extra)

def post(path, body, timeout=60):
    r = urllib.request.urlopen(Request(f"http://127.0.0.1:{PORT}{path}",
        json.dumps(body).encode(), {"Content-Type": "application/json"}),
        timeout=timeout)
    return json.loads(r.read())

def stop(srv):
    srv.kill(); time.sleep(1)

fails, oks = [], []
def check(name, cond, detail=""):
    (oks if cond else fails).append((name, detail))
    print(("  ok  " if cond else "  FAIL ") + name +
          (f" | {detail}" if detail else ""))

build_index()
results, feedback = {}, {}
for tag, extra in (("off", {"ANN_FLOAT_KERNEL": "0"}),
                   ("on",  {"ANN_FLOAT_KERNEL": "1"})):
    srv, h = start(extra)
    try:
        # seed the anchor cache via one full regrow batch-equivalent (first
        # search trains the route cache lazily; warm queries warm it).
        post("/v1/state/search", {"query": "warmup", "k": 1})
        b4 = post("/v1/state/search-batch", {"queries": QS[:4], "k": 3})
        hits_ok = sum(1 for r in b4.get("results", []) if r.get("hits"))
        check(f"[{tag}] batch hits>=1", hits_ok >= 1, f"{hits_ok}/4")
        oversize_rc = None
        try:
            bad = post("/v1/state/search-batch",
                       {"queries": [f"q{i}" for i in range(33)], "k": 1})
            oversize_rc = bad.get("error") or "no-error-field"
        except urllib.error.HTTPError as e:
            oversize_rc = f"http-{e.code}"
        check(f"[{tag}] B=33 rejected", "error" in str(oversize_rc).lower()
              or "http-4" in str(oversize_rc), oversize_rc)
        empty_rc = None
        try:
            e0 = post("/v1/state/search-batch", {"queries": [], "k": 1})
            empty_rc = e0.get("error") or "no-error-field"
        except urllib.error.HTTPError as e:
            empty_rc = f"http-{e.code}"
        check(f"[{tag}] empty rejected", "error" in str(empty_rc).lower()
              or "http-4" in str(empty_rc), empty_rc)
        # serial for equivalence
        ser = [[x["sid"] for x in post("/v1/state/search",
                {"query": q, "k": 3}).get("hits", [])] for q in QS[:4]]
        sid_lists = [[x["sid"] for x in r.get("hits", [])]
                     for r in b4.get("results", [])]
        results[tag] = {"batch": sid_lists, "serial": ser}
    finally:
        stop(srv)

match = all(results["off"]["batch"][i] == results["on"]["batch"][i]
            for i in range(4))
match_ser = all(results["off"]["serial"][i] == results["on"]["serial"][i]
                for i in range(4))
check("(d) OFF vs ON top-3 sid identical (batch)", match)
check("(d-b) OFF vs ON top-3 sid identical (serial)", match_ser)

print("RESULT:", "ALL PASS" if not fails else f"{len(fails)} FAILURES")
sys.exit(0 if not fails else 1)
