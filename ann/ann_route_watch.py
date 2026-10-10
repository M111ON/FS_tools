#!/usr/bin/env python3
"""ann_route_watch.py — cold-path watcher for the ANN route (H1 fix).

HOLE IT CLOSES (handoff 2026-10-10, memory #7563; downgraded by #7565):
  H1 silent unroute on growth — the server's route cache (ann_search.c
  lz_route_cached) returns 0 whenever the anchor table's ntr != index nent
  (or anchors/order go missing), and every query then falls back to a brute
  scan with recall intact, latency climbing, and NOTHING alerting — not even
  the keepalive, which only checks /health "status" (ok while routed==0).

WHY A SEPARATE WATCHER (not in ann_sync.py, not in keepalive):
  ann_sync (every 15 min) rebuilds + POSTs regrow when the memcore STORE
  moves — the happy path is covered. The watcher covers what sync CANNOT
  see: sync believes all is synced (state == store sig) yet live serves
  routed==0 — deleted/corrupt anchors, torn order file, index touched
  outside sync, server restarted over mismatched files. That combination is
  exactly "silently wrong and nobody's job" — this script is that job.

WHAT ONE PASS DOES:
  1. GET /health (retries; slow right after a regrow — it pings embed).
     unreachable        -> log, exit 2 (keepalive's job, not ours).
     entries == 0       -> log degraded, exit 0 (nothing to route, not H1).
     routed == 1        -> exit 0, silent (cheap no-op, no log spam).
  2. routed == 0 && entries > 0 -> H1 FIRING. Guards before touching live:
     a. ann_sync lock held            -> defer ("sync in progress"), exit 0.
     b. store sig != sync .synced     -> defer ("sync's job"), exit 0.
     c. last heal attempt < COOLDOWN  -> defer ("cooling down"), exit 0.
  3. Heal = POST /v1/state/regrow (the SAME primitive sync uses: server-side
     k-means retrain, atomic anch save, order rewrite, index rewrite with
     anchor fields, in-process cache invalidate). No file copies, no restart
     (hot file-copy proved unreliable 2/5 stale — handoff §retracted).
  4. Re-poll /health, log TRUTHFULLY with before/after numbers
     (entries, routed 0->1, nent, K, mode). Failure exits 1, stated plainly.

RACES: if a sync rebuild lands after our regrow, route drops again and the
  next pass (after cooldown) heals it — converges, never loops (cooldown +
  defer-to-sync). Regrow holds the server lock while training, so this is
  cold-path by construction: it never runs inside a query.

RUN:
  python ann_route_watch.py --once    (single pass; scheduler runs this)
  python ann_route_watch.py --check   (read-only probe, never POSTs)

SCHEDULING (not yet wired — needs owner's go-ahead, live-touching):
  schtasks /create /tn \\ann-route-watch /tr "... --once" /sc minute /mo 5
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ANN_BASE = os.environ.get("MEMCORE_ANN_URL", "http://127.0.0.1:8096").rstrip("/")
HEALTH_URL = ANN_BASE + "/health"
REGROW_URL = ANN_BASE + "/v1/state/regrow"

MEMCORE_DIR = Path(os.environ.get("MEMCORE_DIR", r"I:\tools\cloud_ws\memcore"))
STORE = os.environ.get("MEMCORE_STORE_DB", str(MEMCORE_DIR / "fact_store.v2.sqlite3"))
SYNC_STATE = str(MEMCORE_DIR / ".ann_sync_state.json")
SYNC_LOCK = str(MEMCORE_DIR / ".ann_sync.lock")

STATE = str(HERE / ".ann_route_watch_state.json")
LOG = str(HERE / ".ann_route_watch.log")

COOLDOWN_S = int(os.environ.get("ANN_WATCH_COOLDOWN_S", "1800"))  # 30 min
TIMEOUT_S = int(os.environ.get("ANN_WATCH_TIMEOUT_S", "30"))


def log(msg):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def load_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(st):
    try:
        Path(STATE).write_text(json.dumps(st, indent=0), encoding="utf-8")
    except OSError:
        pass


def get_health(retries=3):
    for i in range(retries):
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=TIMEOUT_S) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:
            if i + 1 < retries:
                time.sleep(2)
    return None


def post_regrow():
    req = urllib.request.Request(REGROW_URL, data=b"{}",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def store_sig():
    """Same monotone signature as ann_sync.py (daemon only INSERTs)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STORE, uri=True, timeout=10)
        try:
            n = con.execute("SELECT count(*) FROM entries").fetchone()[0]
            m = con.execute("SELECT count(*) FROM embeddings").fetchone()[0]
            mx = con.execute("SELECT coalesce(max(rowid),0) FROM entries").fetchone()[0]
        finally:
            con.close()
        return "%d:%d:%d" % (n, m, mx)
    except Exception as e:
        return "ERR:%s" % e


def sync_locked():
    if not os.path.exists(SYNC_LOCK):
        return False
    try:
        pid = int((Path(SYNC_LOCK).read_text() or "0").strip())
    except (OSError, ValueError):
        return False
    if not pid:
        return False
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                    h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return False


def main():
    readonly = "--check" in sys.argv

    h = get_health()
    if h is None:
        log("route_watch: ANN unreachable at %s — keepalive's job, not ours"
            % ANN_BASE)
        return 2
    entries = int(h.get("entries") or 0)
    routed = int(h.get("routed") or 0)

    if entries == 0:
        log("route_watch: DEGRADED — server up but index empty (status=%s)"
            % h.get("status"))
        return 0
    if routed == 1:
        return 0  # healthy route: silent no-op

    # --- H1 firing: entries>0 but unrouted --------------------------------
    sig = store_sig()
    st = load_json(STATE)
    sync_st = load_json(SYNC_STATE)

    if readonly:
        log("route_watch[CHECK]: H1 FIRING — entries=%d routed=0 "
            "store=%s sync_synced=%s (no action, --check)" %
            (entries, sig, sync_st.get("synced")))
        return 1

    if sync_locked():
        log("route_watch: H1 firing (entries=%d) but sync lock held — "
            "deferring to ann-sync" % entries)
        return 0
    if sync_st.get("synced") != sig:
        log("route_watch: H1 firing (entries=%d) but store moved "
            "(sync %s -> store %s) — deferring to ann-sync" %
            (entries, sync_st.get("synced"), sig))
        return 0
    now = time.time()
    if now - float(st.get("last_heal", 0)) < COOLDOWN_S:
        log("route_watch: H1 firing (entries=%d) but last heal %ds ago — "
            "cooling down" % (entries, now - float(st.get("last_heal", 0))))
        return 0

    log("route_watch: H1 FIRING — entries=%d routed=0, sync believes synced "
        "(%s) — POSTing regrow" % (entries, sig))
    st["last_heal"] = now
    st["heals"] = int(st.get("heals", 0)) + 1
    save_state(st)
    try:
        r = post_regrow()
    except Exception as e:
        log("route_watch: HEAL FAILED — regrow POST failed: %s" % e)
        return 1
    if r.get("status") != "ok":
        log("route_watch: HEAL FAILED — regrow returned %s" % r)
        return 1

    h2 = get_health(retries=5)
    if h2 is None:
        log("route_watch: HEAL UNCERTAIN — regrow ok (nent=%s K=%s) but "
            "/health unreachable after" % (r.get("nent"), r.get("K")))
        return 1
    routed2 = int(h2.get("routed") or 0)
    if routed2 == 1:
        log("route_watch: HEALED — entries=%d routed 0->1 "
            "(regrow nent=%s K=%s mode=%s)" %
            (entries, r.get("nent"), r.get("K"), r.get("mode")))
        return 0
    log("route_watch: HEAL FAILED — regrow ok (nent=%s K=%s) but still "
        "routed=0 (entries=%s)" % (r.get("nent"), r.get("K"),
                                   h2.get("entries")))
    return 1


if __name__ == "__main__":
    sys.exit(main())
