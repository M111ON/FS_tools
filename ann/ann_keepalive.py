"""ann_keepalive.py — supervisor for the ANN search server (:8096).

WHY THIS EXISTS
  I:/tools/ann/build/ann_search.exe was started by hand, so /semantic in
  chat-pool died with it. This keeps it (and the embed sidecar it depends on)
  up: at logon and every 5 minutes. Task Scheduler is the supervisor; this is
  one pass per run.

WHAT IT DOES (once())
  1. if GET :8096/health says status ok -> the ANN server is fine, skip
  2. else ensure the embed sidecar (:18095) is up, then start the ANN server
     detached, then wait for /health (the 57k index loads for ~15-20 s first)
  3. if the sidecar is down too, start it (llama-server must run from its own
     dir — its exe needs sibling DLLs)

WHICH ANN SERVER BINARY (activation order)
  EXE prefers build/ann_search_batch.exe (committed a63e9b5: adds the
  /v1/state/search-batch endpoint + the ANN_FLOAT_KERNEL float kernel) and
  falls back to build/ann_search.exe when the batch one is absent.

GUARDS (same contract as pool_keepalive.py)
  * one keeper at a time (PID file)
  * never kill a healthy server — we only ever START
  * backoff on crash-loop

RUN
  pythonw.exe ann_keepalive.py        (the scheduled task; no console)
  python.exe  ann_keepalive.py --once (one pass, for a check)
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
# EXE (activation order): prefer the batch+float-kernel binary; fall back to
# the plain one if it is absent (same cwd/flags either way).
EXE = (os.path.join(DIR, "build", "ann_search_batch.exe")
       if os.path.exists(os.path.join(DIR, "build", "ann_search_batch.exe"))
       else os.path.join(DIR, "build", "ann_search.exe"))
PIDFILE = os.path.join(DIR, ".keepalive.pid")
LOG = os.path.join(DIR, ".keepalive.log")

PORT = int(os.environ.get("ANN_PORT", "8096"))
HEALTH = "http://127.0.0.1:%d/health" % PORT

# embed sidecar (llama-server, multilingual-e5-small)
EMBED_PORT = int(os.environ.get("LZ_EMBED_PORT", "18095"))
EMBED_HEALTH = "http://127.0.0.1:%d/health" % EMBED_PORT
EMBED_DIR = r"I:\llama\llama-b10830-win-vulkan-x64"
EMBED_EXE = os.path.join(EMBED_DIR, "llama-server.exe")
EMBED_MODEL = r"I:\model\multilingual-e5-small-q8_0-fixed.gguf"

CHECK = 60
BACKOFF_MIN = 60
BACKOFF_MAX = 900
# The index load is the long pole: 62,510 rows x 384 dims parsed from a
# 257 MB jsonl took 31 s on this box (measured 2026-10-21, the regrow
# telemetry's ms_per_phase.load), and the keepalive's own health wait
# measured 57 s end-to-end. A 60 s budget therefore failed at the margin
# and the supervisor logged "ann started but /health never answered" then
# started a SECOND instance — which cannot bind (exclusive) and only adds
# load. Give it room plus a margin, and treat "still starting" as not-dead.
WAIT_HEALTHY = 180
# A server that is still loading answers nothing; that is not a crash. The
# backoff must not fire on a slow start.
STARTUP_GRACE = 150


def _log(msg):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _healthy(url=HEALTH, key="status", want="ok", timeout=3):
    """True iff the endpoint answers with {key: want}. ann_search /health uses
    {"status":"ok"}; llama-server uses the same shape."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return str(data.get(key, "")).lower() == want
    except (urllib.error.URLError, ValueError, OSError):
        return False


def _index_paths():
    """Pick the corpus: the derived memcore index if it exists, else the sample.

    ANN owns the index (ann_*); kv_* is the legacy name kept working for older
    checkouts. Prefer ann_*, fall back to kv_* if only that exists."""
    def pick(d):
        idx = os.path.join(d, "ann_index.jsonl")
        if not os.path.exists(idx):
            idx = os.path.join(d, "kv_index.jsonl")
        anch = os.path.join(d, "ann_anchors.bin")
        if not os.path.exists(anch):
            anch = os.path.join(d, "kv_anchors.bin")
        order = os.path.join(d, "ann_index.order")
        if not os.path.exists(order):
            order = os.path.join(d, "kv_index.order")
        return (idx, anch, order, os.path.join(d, "lz_route.log"))

    mem = os.path.join(DIR, "data", "memcore")
    if os.path.exists(os.path.join(mem, "ann_index.jsonl")) or \
       os.path.exists(os.path.join(mem, "kv_index.jsonl")):
        return pick(mem)
    return pick(os.path.join(DIR, "data", "sample"))


def _only_instance():
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE, encoding="utf-8").read().strip() or "0")
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_alive(old):
            return False
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def _pid_alive(pid):
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "python" in out.lower()


def _detached(args, cwd):
    out = open(os.path.join(DIR, ".server.log"), "ab")
    err = open(os.path.join(DIR, ".server.err"), "ab")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(args, cwd=cwd, stdout=out, stderr=err,
                            stdin=subprocess.DEVNULL, creationflags=flags,
                            close_fds=True)


def _start_embed():
    if not (os.path.exists(EMBED_EXE) and os.path.exists(EMBED_MODEL)):
        _log("embed sidecar missing (%s / %s) -> skip" % (EMBED_EXE, EMBED_MODEL))
        return False
    _log("embed DOWN -> starting llama-server :%d" % EMBED_PORT)
    _detached([EMBED_EXE, "-m", EMBED_MODEL, "--embedding",
               "--port", str(EMBED_PORT), "-c", "512"], cwd=EMBED_DIR)
    for _ in range(30):
        if _healthy(EMBED_HEALTH, timeout=2):
            return True
        time.sleep(1)
    return False


def _start_ann():
    idx, anch, order, log = _index_paths()
    env = dict(os.environ,
               ANN_PORT=str(PORT),
               LZ_EMBED_HOST="127.0.0.1", LZ_EMBED_PORT=str(EMBED_PORT),
               ANN_INDEX_PATH=idx, ANN_ANCH_PATH=anch,
               ANN_ORDER_PATH=order, ANN_ROUTE_LOG=log,
               # tombstone warm path (env ANN_TOMBSTONE_WARM, default 0 in the
               # server): fires ONLY when the route covers less than the
               # budget (WARM-PATH.md hot/cold rule). Default ON here —
               # the cover-vs-budget rule still gates it per query.
               ANN_TOMBSTONE_WARM=os.environ.get("ANN_TOMBSTONE_WARM", "1"),
               # float kernel (env ANN_FLOAT_KERNEL, default OFF in the
               # server): pure-float 4-acc lz_dotf + per-entry reciprocal
               # premul. Measured on this box: 353.9 -> 242.4 ns/dot on 24k x
               # 384 microbench = 1.46x; end-to-end p50 32 -> 23 ms single
               # query on the 62k index. Not bit-identical (max_rel_diff
               # 2.797e-07) — a 1e-5-grade numerics change, see FORK.md.
               ANN_FLOAT_KERNEL=os.environ.get("ANN_FLOAT_KERNEL", "1"),
               # tuned 2026-10-09: pin walked buckets to 5 (A/B 24 queries:
                # med 61ms vs 97ms, top-1 identical 24/24). Env overrides.
                LZ_ROUTE_BUCKETS=os.environ.get("LZ_ROUTE_BUCKETS", "5"),
                # prune walk live 2026-10-11 (re-label verdict + stale-exe
                # upgrade): score only routed-bucket members (bucket-major,
                # nearest first), cap scored, full-scan fallback when
                # pruned top < 0.5. Budget 8000 -> 24000 on 2026-10-11 after an
                # independent recall sweep (40 real-vec queries vs numpy oracle):
                # R@10 0.47@8000, 0.90@16000, 0.96@24000, 0.995@full. 24000 is
                # near the flat knee. Env overrides.
                LZ_SEARCH_BUDGET=os.environ.get("LZ_SEARCH_BUDGET", "24000"),
                ANN_WALK_PRUNE=os.environ.get("ANN_WALK_PRUNE", "1"),
                ANN_WALK_FLOOR=os.environ.get("ANN_WALK_FLOOR", "0.5"),
                # TOMBSTONE DOOR (env ANN_DOOR, default OFF = the pre-door
                # behaviour, byte-identical). Measured live on 2026-10-20
                # (300 real queries vs a numpy oracle, forced below-floor):
                # recall@5 1.0000 both arms, 300/300 paired equal — so it is
                # safe — but the door's walk is NOT faster than the plain
                # full scan it replaces (p50 43 vs 39 ms), and at the
                # production floor (0.5) a real query scores ~0.86, so the
                # door is almost never reached. Left OFF until someone
                # wants the ANN_DOOR_EXIT latency trade (that one gives up
                # recall: the exit floor is a heuristic).
                ANN_DOOR=os.environ.get("ANN_DOOR", "0"),
                ANN_DOOR_EXIT=os.environ.get("ANN_DOOR_EXIT", "0"))
    out = open(os.path.join(DIR, ".server.log"), "ab")
    err = open(os.path.join(DIR, ".server.err"), "ab")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([EXE, "--serve", str(PORT)], cwd=DIR, env=env,
                            stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)


def _wait_healthy(seconds=WAIT_HEALTHY):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(1)
    return False


def _port_listening(port):
    """True iff something accepts a connection on `port`.

    The server binds the port BEFORE it finishes loading the index? No — it
    binds AFTER the load (the load is in main(), before socket()). But a
    previous instance may still hold the port while a NEW one is loading.
    Either way: if the port answers a TCP connect, do NOT start another
    instance — it would fail the exclusive bind and only add load.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def once():
    if _healthy():
        _maybe_regrow()
        return True
    # A server that is UP but busy (loading the index, or the embed sidecar
    # is slow) answers no health yet still owns the port. Starting a second
    # instance there is the 2026-10-21 incident: the second cannot bind
    # (exclusive) and the supervisor then logged a false "never answered".
    if _port_listening(PORT):
        _log("ann port %d is busy (a server is up but not answering yet) "
             "- waiting instead of starting a second" % PORT)
        if _wait_healthy():
            _log("ann UP")
            return True
        _log("ann port %d still busy after the wait" % PORT)
        return False
    if not _healthy(EMBED_HEALTH, timeout=2):
        _start_embed()
    _log("ann DOWN -> starting")
    try:
        _start_ann()
    except OSError as e:
        _log("start FAILED: %r" % e)
        return False
    if _wait_healthy():
        _log("ann UP")
        return True
    _log("ann started but /health never answered (still loading? the index "
         "load takes ~31 s on 62k rows)")
    return False


# ---------------------------------------------------------------------------
# COLD-PATH WATCHER (2026-10-23)
#
# The index is append-only and grows continuously (ann-sync). Nothing watched
# it: the server self-heals labels from the rows + anchor table at boot, but
# the TABLE is a training-time snapshot and the bucket count K is derived from
# the row count. So two things must happen on the cold path, never inside a
# request:
#   * K crosses a breakpoint -> the table is trained for the OLD K and the
#     largest bucket grows past LZ_SEARCH_BUDGET again (measured: K=8 at 62.8k
#     rows gave one 25,693-row bucket vs a 24,000 budget -> every query
#     truncated; K=24 gave 9,081 -> no truncation, R@5 .9533 -> .9680)
#   * the route is dead (routed=0) -> re-anchor is the only repair
#
# Rule parity with the C code (src/ann_search.c, regrow path):
#   K = round_up_to_4(ceil(n / 1000)), clamped to [8, 64]
# The K cap binds from 63k rows on, so above that the K-breakpoint trigger can
# never fire again — past 63k the only auto-repair left is routed==0, and the
# next lever is placement/hierarchy, not a bigger table (measured 2026-10-23).
# `--selfcheck` asserts this against the values the C rule produces.
K_TARGET_ROWS = 1000
K_MIN, K_MAX = 8, 64   # K_MAX mirrors LZ_ANCH_MAXK (route scratch)
STATE = os.path.join(DIR, ".anchor_state.json")
REGROW_MIN_GAP = 600            # never regrow twice inside 10 minutes
REGROW_URL = "http://127.0.0.1:%d/v1/state/regrow" % PORT


def k_for(n):
    """Bucket count the C regrow path would pick for n rows (keep in sync)."""
    if n < 64:
        return 4 if n >= 32 else 2
    want = -(-n // K_TARGET_ROWS)
    want = ((want + 3) // 4) * 4
    return max(K_MIN, min(K_MAX, want))


def _health_json(timeout=10):
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except (urllib.error.URLError, ValueError, OSError):
        return None


def _load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    try:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _regrow_decision(h, st):
    """(act, why) — pure, so --selfcheck can exercise it without a server."""
    n = int(h.get("entries") or 0)
    k_have = int(h.get("k") or 0)
    routed = int(h.get("routed") or 0)
    if n <= 0:
        return False, "no entries"
    if routed == 0:
        return True, "route dead (routed=0)"
    if not k_have:
        return False, "server has no k field (old binary)"
    k_want = k_for(n)
    if k_want != k_have:
        return True, "K breakpoint: n=%d has k=%d, rule wants %d" % (n, k_have, k_want)
    last = int(st.get("n") or 0)
    if last and n < last:
        return False, "index shrank %d -> %d (rebuilt?) - watching, not regrowing" % (last, n)
    return False, "in contract (n=%d k=%d)" % (n, k_have)


def _maybe_regrow():
    h = _health_json()
    if not h:
        return
    st = _load_state()
    act, why = _regrow_decision(h, st)
    n = int(h.get("entries") or 0)
    if not st:
        st = {"n": n, "k": int(h.get("k") or 0), "at": 0, "note": "seeded"}
        _save_state(st)
        _log("watch: seeded n=%d k=%s" % (n, h.get("k")))
        if not act:
            return
    if not act:
        if st.get("note") != why:
            st["note"] = why
            _save_state(st)
        return
    if time.time() - float(st.get("at") or 0) < REGROW_MIN_GAP:
        _log("watch: %s (suppressed, regrow ran %.0fs ago)"
             % (why, time.time() - float(st.get("at") or 0)))
        return
    _do_regrow(why, n)


def _do_regrow(why, n):
    _log("watch: regrow because %s" % why)
    st = {"n": n, "k": 0, "at": time.time(), "note": ""}
    try:
        req = urllib.request.Request(
            REGROW_URL, data=b'{"force":1}',
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=1800) as r:
            res = json.loads(r.read().decode("utf-8", "ignore"))
        took = (res.get("regrow") or {}).get("ms")
        after = _health_json(timeout=180)
        ok = bool(after) and int(after.get("routed") or 0) == 1 and \
            int(after.get("k") or 0) == k_for(int(after.get("entries") or 0))
        st = {"n": int((after or {}).get("entries") or n),
              "k": int((after or {}).get("k") or 0),
              "at": time.time(),
              "note": ("regrow ok (%s ms)" % round(took)) if ok
                      else "REGROW FAILED: routed=%s k=%s"
                           % ((after or {}).get("routed"), (after or {}).get("k"))}
        _save_state(st)
        _log("watch: %s, n=%s k=%s" % (st["note"], st["n"], st["k"]))
    except (urllib.error.URLError, ValueError, OSError) as e:
        st["at"] = time.time()
        st["note"] = "REGROW ERROR: %r" % e
        _save_state(st)
        _log("watch: REGROW ERROR %r" % e)


def _selfcheck():
    """Rule parity with C + decision table. No server, no writes."""
    cases = {1: 2, 10: 2, 40: 4, 64: 8, 2999: 8, 3000: 8, 7999: 8, 8000: 8,
             8001: 12, 12000: 12, 24000: 24, 30000: 32, 47999: 48, 48000: 48,
             50000: 52, 62869: 64, 72000: 64, 96000: 64, 200000: 64}
    for n, want in cases.items():
        got = k_for(n)
        assert got == want, "k_for(%d)=%d want %d" % (n, got, want)
    base = {"entries": 62869, "k": 64, "routed": 1}
    assert _regrow_decision(base, {"n": 62869})[0] is False
    assert _regrow_decision(dict(base, routed=0), {})[0] is True
    assert _regrow_decision(dict(base, k=24), {})[0] is True
    assert _regrow_decision(dict(base, entries=8001), {})[0] is True
    assert _regrow_decision(dict(base, entries=62869, k=64), {"n": 70000})[0] is False
    assert _regrow_decision({"entries": 0, "k": 8, "routed": 1}, {})[0] is False
    print("selfcheck ok: %d k_for cases + 6 decisions" % len(cases))


def main():
    if "--selfcheck" in sys.argv:
        _selfcheck()
        return 0
    if "--plan" in sys.argv:
        h = _health_json() or {}
        act, why = _regrow_decision(h, _load_state())
        print(json.dumps({"act": act, "why": why,
                          "entries": h.get("entries"), "k": h.get("k")}))
        return 0
    if "--force-regrow" in sys.argv:
        h = _health_json() or {}
        _do_regrow("forced by --force-regrow", int(h.get("entries") or 0))
        print(open(STATE, encoding="utf-8").read())
        return 0
    if "--once" in sys.argv:
        ok = once()
        print("healthy" if ok else "not healthy")
        return 0 if ok else 1
    if not _only_instance():
        _log("another keepalive holds the slot -> exit")
        return 0
    _log("keepalive start (pid %d)" % os.getpid())
    backoff = BACKOFF_MIN
    while True:
        t0 = time.time()
        if once():
            backoff = BACKOFF_MIN
            time.sleep(CHECK)
            continue
        # A slow START is not a crash: the index load takes ~31 s and the
        # health wait up to 180 s, so only a failure FASTER than the
        # startup grace doubles the backoff. Before this, a 60 s health
        # budget on a 57 s load made every slow start look like a crash.
        if time.time() - t0 < STARTUP_GRACE:
            backoff = min(BACKOFF_MAX, backoff * 2)
        _log("retry in %ds" % backoff)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
