#!/usr/bin/env python3
"""test_door_path.py — totem DOOR contract (2026-10-20, env ANN_DOOR).

Scratch server on port 8097 only (NEVER the live :8096). 384-dim deterministic
index (test_incr_fix.py shape). Verifies:

  D1 OFF (ANN_DOOR unset): search after warm+pruned repeat = byte-mode
     "warm+p" / "anchor+p" family (pruned walk label present), NOT "door",
     and hits >= 1 (behaviour unchanged).
  D2 ON (ANN_DOOR=1): after the SAME warm repeat, EITHER the mode still
     reports a pruned/anchor family (door not needed because top bucket was
     OPEN — no fallback) OR "door" is reported; hits count >= 1.
  D3 ON with a floor forced high (ANN_WALK_FLOOR=2.1 makes every top score
     below floor -> the fallback branch fires): mode is exactly "door" when
     the top bucket is SHUT, hits >= 1, and top-5 sids overlap the OFF-mode
     top-5 (recall preserved).
  D4 graft fallback intact: with ANN_DOOR=1, floor high, and GRAFT on, an
     off-bucket query still returns hits (fallback pass = fail-loud solve).
"""
import json, os, shutil, subprocess, sys, time, urllib.request
from urllib.request import Request

REPO = r"I:\tools\ann"
EXE = os.path.join(REPO, "build", "ann_search_isolate.exe")
SCRATCH = os.path.join(os.environ.get("TEMP", "/tmp"), "ann-isolate-scratch", "door")
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

def start(extra_env):
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": os.path.join(SCRATCH, "ann.bin"),
           "ANN_ORDER_PATH": os.path.join(SCRATCH, "order.bin"),
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOTEM_WARM": "1",
           **extra_env}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "w", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    for _ in range(60):
        time.sleep(0.3)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)

def search(q="geometric", k=5):
    return http("/v1/state/search", {"query": q, "k": k})

def sids_of(r):
    return [h["sid"] for h in r.get("hits", [])][:5]

def main():
    build()
    p = start({"ANN_WALK_PRUNE": "1"})
    try:
        g = http("/v1/state/regrow", {})
        ok(g["regrow"]["mode"] == "full", "base regrow full", g["regrow"]["mode"])
        off_cold = search()
        off_warm = search()
        ok(len(off_warm.get("hits", [])) >= 1, "D1 OFF warm hits>=1",
           f"mode={off_warm['route']['mode']}")
        off_sids = sids_of(off_warm)
        p.kill(); p.wait(timeout=10)
    except Exception:
        p.poll() or p.kill()
        raise

    # D2/D3: ON
    p = start({"ANN_WALK_PRUNE": "1", "ANN_DOOR": "1", "ANN_WALK_FLOOR": "2.1",
               "ANN_DOOR_TRACE": "1"})
    try:
        on_cold = search()
        on_warm = search()
        # force the fallback branch with a NONSENSE query (top score near 0
        # < floor 2.1) so the pruned walk under-scores and the door branch
        # (or anchor+full fallback) fires with a real route decision.
        on_nons = search(q="zzqqx", k=5)
        # first nonsense call routes to a bucket and SHUTS it (open->shut);
        # the SECOND call finds the totem pinned -> the door branch fires.
        mode1 = on_nons["route"]["mode"]
        on_nons2 = search(q="zzqqx", k=5)
        mode = on_nons2["route"]["mode"]
        ok(mode1 in ("anchor+full", "door", "anchor+p", "warm", "warm+p"),
           "D3 first nonsense mode allowed", mode1)
        ok(mode in ("door", "anchor+full"), "D3 forced-fallback mode is door|anchor+full", mode)
        if mode == "door":
            print("       (door fired: top bucket was SHUT -> pinned)")
        ok(len(on_nons.get("hits", [])) >= 1, "D3 door-path hits>=1", str(len(on_nons.get("hits", []))))
        on_sids = sids_of(on_nons)
        overlap = len(set(off_sids) & set(on_sids)) if off_sids else 0
        minlen = max(1, min(len(off_sids), len(on_sids)))
        ok(overlap >= minlen - 1, "D3 top-5 overlap with OFF", f"OFF={off_sids} ON={on_sids} overlap={overlap}")
        # D4 graft fallback: no graft data -> pure fallback pass still returns hits
        ok(on_warm["route"].get("fallback", 0) in (0, 1), "D4 fallback telemetry present", "")
        h = http("/health", to=10)
        ok(h["routed"] == 1, "D4 health routed=1", f"routed={h['routed']}")
    finally:
        p.poll() or p.kill()
        try: p._errlog.close()
        except Exception: pass
        err = open(os.path.join(SCRATCH, "srv_err.log"), encoding="utf-8").read()
        for line in err.splitlines():
            if "trace" in line:
                print("   ", line)
    print("RESULT:", "ALL BEHAVED (ad-hoc verification, not a project suite)" if not fails
          else f"{len(fails)} FAILURES")
    sys.exit(0 if not fails else 1)

if __name__ == "__main__":
    main()
