#!/usr/bin/env python3
"""test_batch_route.py — self-tests for the additive /v1/state/search-batch
endpoint (scratch copy on port 8097 ONLY).

One server is assumed RESTARTED already with the right kernel env by the
driver kernel_eq_run.sh; this script tests the current one. The OFF-vs-ON
comparison is done by re-running this whole script after a server restart:
it dumps the top-5 sid lists, and the driver diffs the two dumps.

Asserts:
  (a) batch B=4 real strings -> 200, telemetry, >=1 query with hits
  (b) B=33 -> HTTP 400 with "error" field
  (c) empty array -> HTTP 400 with "error" field
  (e) timing B=8 batch vs 8 serial /search calls
"""
import json, sys, time, urllib.error
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8097"

def post(path, data, timeout=60):
    data = data if isinstance(data, bytes) else data.encode("utf-8")
    req = Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")

QUERIES = ["ubuntu server setup", "python asyncio sleep", "docker compose volumes",
           "rust ownership rules"]
OK = [True]
def check(name, cond, note=""):
    OK[0] = OK[0] and cond
    print(("PASS " if cond else "FAIL ") + name + ((" — " + str(note)) if note else ""))

def batch(qs):
    st, body = post("/v1/state/search-batch", json.dumps({"queries": qs, "k": 3}))
    return st, (json.loads(body) if body else {})

# (a)
st, d = batch(QUERIES)
check("a.status200", st == 200, st)
tel = d.get("telemetry", {})
check("a.telemetry_n4", tel.get("n") == 4, tel)
n_hits = sum(1 for r in d.get("results", []) if r.get("hits"))
check("a.hits_on_at_least_1", n_hits >= 1, str(n_hits) + "/4")
check("a.embed_telemetry", tel.get("embed_ms", -1) >= 0.0)

# sid dump used by the driver for the OFF-vs-ON diff
sids = [[h["sid"] for h in r.get("hits", [])][:5] for r in d.get("results", [])]
print("sid_dump=" + json.dumps(sids))

# (b) B=33 -> 400
st, body = post("/v1/state/search-batch",
                json.dumps({"queries": ["q%d" % i for i in range(33)], "k": 1}))
try:
    dd = json.loads(body)
except Exception:
    dd = {}
check("b.status400", st == 400, st)
check("b.error_field", isinstance(dd, dict) and "error" in dd, dd.get("error", "-"))

# (c) empty array -> 400
st, body = post("/v1/state/search-batch",
                json.dumps({"queries": [], "k": 1}))
try:
    dd = json.loads(body)
except Exception:
    dd = {}
check("c.status400", st == 400, st)
check("c.error_field", isinstance(dd, dict) and "error" in dd, dd.get("error", "-"))

# (e) timing: B=8 batch vs 8 serial
QS8 = ["nginx reverse proxy", "c sharp garbage collector", "typescript generics variance",
       "kubernetes pod eviction", "go context cancel", "sql window function",
       "linux cgroups v2", "jpeg huffman table"]
t0 = time.perf_counter()
st8, d8 = batch(QS8)
b_ms = (time.perf_counter() - t0) * 1000.0
check("e.batch_ok", st8 == 200, st8)
t0 = time.perf_counter()
for q in QS8:
    post("/v1/state/search", json.dumps({"query": q, "topk": 3}))
s_ms = (time.perf_counter() - t0) * 1000.0
print("TIMING batch8=%.2fms serial8=%.2fms speedup=%.2fx" % (b_ms, s_ms, (s_ms / b_ms if b_ms > 0 else 0)))

sys.exit(0 if OK[0] else 1)
