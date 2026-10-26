"""test_route_watch.py — the H1/H2 act path must run, not just plan.

ann_route_watch.py is the cold-path watcher (scheduled every 5 min). Its H1
branch was never exercised, and the first time it would have fired it raised
NameError: st/sync_st/sig were bound further down than the dead-route heal that
used them. A watcher that dies before logging leaves exactly the silence it
exists to remove.

Contract under test:
  W1  H1: routed=0 with the store unchanged -> the watcher POSTs regrow, the
      pass reports HEALED, /health says routed=1 afterwards
  W2  H2/gate: a positional sidecar whose prefix proof does not match this
      buffer is REFUSED *visibly* — /health stamp.gate == prefix_mismatch
  W3  the watcher turns that gate into an ALERT line and exits non-zero
  W4  a repeated pass with the same signature does not duplicate the alert
  W5  a missing regrow state is reported as no_state, not as ok
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(REPO, "build", "ann_search_scope.exe")
WATCH = os.path.join(REPO, "ann_route_watch.py")
PY = sys.executable
SCRATCH = os.path.join(REPO, "build", "_route_watch_test")
INDEX = os.path.join(SCRATCH, "ann_index.jsonl")
ANCH = os.path.join(SCRATCH, "ann.bin")
ORDER = os.path.join(SCRATCH, "order.bin")
STATE = os.path.join(SCRATCH, "ann-state.json")   # <anchors>.bin -> -state.json
STORE = os.path.join(SCRATCH, "store.sqlite3")
WSTATE = os.path.join(SCRATCH, "watch_state.json")
WLOG = os.path.join(SCRATCH, "watch.log")
PORT = 8099
H = f"http://127.0.0.1:{PORT}"
N_ROWS = 240
DIM = 64

ok_l, fails = [], []


def ok(c, n, d=""):
    (ok_l if c else fails).append((n, d))
    print(f"  {'ok ' if c else 'FAIL'} {n}{(' | ' + d) if d else ''}")


def http(path, body=None, to=120):
    if body is None:
        return json.loads(urllib.request.urlopen(H + path, timeout=to).read())
    r = urllib.request.Request(H + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=to).read())


def build():
    shutil.rmtree(SCRATCH, ignore_errors=True)
    os.makedirs(SCRATCH, exist_ok=True)
    with open(INDEX, "w", encoding="utf-8") as f:
        for i in range(N_ROWS):
            c = i % 6
            v = [0.3 * c + 0.002 * (i // 6)
                 for _ in range(DIM)]
            f.write(json.dumps({"sid": f"seed-{i:04d}", "file": f"grp{c}",
                                "npast": 1, "dim": DIM,
                                "vec": [round(x, 6) for x in v]}) + "\n")
    con = sqlite3.connect(STORE)
    con.execute("CREATE TABLE entries (sid TEXT)")
    con.execute("CREATE TABLE embeddings (sid TEXT, v BLOB)")
    con.executemany("INSERT INTO entries VALUES (?)",
                    [(f"seed-{i:04d}",) for i in range(N_ROWS)])
    con.executemany("INSERT INTO embeddings VALUES (?,x'00')",
                    [(f"seed-{i:04d}",) for i in range(N_ROWS)])
    con.commit()
    con.close()
    open(os.path.join(SCRATCH, ".ann_sync_state.json"), "w",
         encoding="utf-8").write(json.dumps({"synced": "%d:%d:%d" % (N_ROWS, N_ROWS, N_ROWS)}))


def start():
    env = {**os.environ,
           "ANN_INDEX_PATH": INDEX,
           "ANN_ANCH_PATH": ANCH,
           "ANN_ORDER_PATH": ORDER,
           "ANN_ROUTE_LOG": os.path.join(SCRATCH, "route.log"),
           "ANN_PORT": str(PORT),
           "ANN_TOTEM_WARM": "0",
           "ANN_ROSTER": "0"}
    errlog = open(os.path.join(SCRATCH, "srv_err.log"), "a", encoding="utf-8")
    p = subprocess.Popen([EXE, "--serve", str(PORT)], env=env,
                         stdout=subprocess.DEVNULL, stderr=errlog)
    p._errlog = errlog
    for _ in range(80):
        time.sleep(0.3)
        try:
            if http("/health", to=4).get("status") == "ok":
                return p
        except Exception:
            pass
    p.kill()
    raise RuntimeError("scratch server failed to start on port %d" % PORT)


def stop(p):
    p.poll() or p.kill()
    try:
        p.wait(timeout=10)
    except Exception:
        pass
    try:
        p._errlog.close()
    except Exception:
        pass


def watch(*extra):
    env = {**os.environ,
           "MEMCORE_ANN_URL": H,
           "MEMCORE_DIR": SCRATCH,
           "MEMCORE_STORE_DB": STORE,
           "ANN_WATCH_STATE": WSTATE,
           "ANN_WATCH_LOG": WLOG,
           "ANN_WATCH_TIMEOUT_S": "20",
           "ANN_WATCH_COOLDOWN_S": "0"}
    r = subprocess.run([PY, WATCH, "--once", *extra], env=env,
                       capture_output=True, text=True, timeout=600)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def log_lines():
    try:
        return open(WLOG, encoding="utf-8").read().splitlines()
    except OSError:
        return []


def main():
    if not os.path.exists(EXE):
        print(f"missing {EXE}")
        return 2
    build()
    print(f"scratch: {N_ROWS} rows, {DIM} dim, {SCRATCH}")
    p = start()
    try:
        http("/v1/state/regrow", {})
        h = http("/health", to=10)
        ok(h.get("routed") == 1, "setup: route trained", json.dumps(h.get("stamp")))

        # --- W1: H1 act path -------------------------------------------------
        # the route cache is in memory, so the order file going away is only
        # visible after a reload - the restart case the watcher exists for
        # (sync believes all is well, live is unrouted).
        stop(p)
        os.remove(ANCH)   # the table is the thing the route cannot live without
        p = start()
        h = http("/health", to=10)
        ok(h.get("routed") == 0, "W1 setup: table gone -> routed=0",
           f"routed={h.get('routed')}")
        rc, out = watch()
        ok(rc == 0, "W1 watcher exit 0", f"rc={rc}")
        ok(any("HEALED" in ln and "routed 0->1" in ln for ln in log_lines()),
           "W1 watcher logged the heal",
           " | ".join(ln[-90:] for ln in log_lines()[-2:]))
        h = http("/health", to=10)
        ok(h.get("routed") == 1, "W1 /health tells the truth afterwards",
           f"routed={h.get('routed')}")
    finally:
        stop(p)

    # --- W2/W3/W4/W5: the stamp gate becomes visible -------------------------
    json.dump({"n_prev": 10, "last_sid": "NOT-A-ROW-IN-THIS-INDEX"},
              open(STATE, "w", encoding="utf-8"))
    open(WLOG, "w").close()
    p = start()
    try:
        h = http("/health", to=10)
        g = (h.get("stamp") or {}).get("gate")
        ok(g == "prefix_mismatch", "W2 refused sidecar is visible in /health",
           json.dumps(h.get("stamp")))
        rc, out = watch()
        ok(rc == 1, "W3 watcher exits non-zero on the gate", f"rc={rc}")
        alerts = [ln for ln in log_lines() if "ALERT" in ln]
        ok(len(alerts) >= 1, "W3 watcher ALERTs instead of silence",
           alerts[0][-100:] if alerts else "no ALERT line")
        n1 = len(alerts)
        watch()
        n2 = len([ln for ln in log_lines() if "ALERT" in ln])
        ok(n2 == n1, "W4 repeated pass does not duplicate the alert",
           f"{n1} -> {n2}")
    finally:
        stop(p)

    os.remove(STATE)
    p = start()
    try:
        g = (http("/health", to=10).get("stamp") or {}).get("gate")
        ok(g == "no_state", "W5 missing state reported as no_state", str(g))
    finally:
        stop(p)

    print()
    if fails:
        print(f"RESULT: {len(fails)} FAILED / {len(ok_l) + len(fails)}")
        for n, d in fails:
            print(f"  FAIL {n} {d}")
        return 1
    print(f"ALL PASS ({len(ok_l)} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
