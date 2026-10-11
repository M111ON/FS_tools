"""ann_keepalive.py — supervisor for the ANN search server (:8096).

WHY THIS EXISTS
  I:/tools/ann/build/ann_search.exe was started by hand, so /semantic in
  chat-pool died with it. This keeps it (and the embed sidecar it depends on)
  up: at logon and every 5 minutes. Task Scheduler is the supervisor; this is
  one pass per run.

WHAT IT DOES (once())
  1. if GET :8096/health says status ok -> the ANN server is fine, skip
  2. else ensure the embed sidecar (:18095) is up, then start ann_search.exe
     detached, then wait for /health (the 57k index loads for ~15-20 s first)
  3. if the sidecar is down too, start it (llama-server must run from its own
     dir — its exe needs sibling DLLs)

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
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(DIR, "build", "ann_search.exe")
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
FAST_DEATH = 20
WAIT_HEALTHY = 60          # the 57k index takes ~15-20 s to load before bind


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
    """Pick the corpus: the derived memcore index if it exists, else the sample."""
    mem = os.path.join(DIR, "data", "memcore")
    if os.path.exists(os.path.join(mem, "kv_index.jsonl")):
        return (os.path.join(mem, "kv_index.jsonl"),
                os.path.join(mem, "kv_anchors.bin"),
                os.path.join(mem, "kv_index.order"),
                os.path.join(mem, "lz_route.log"))
    s = os.path.join(DIR, "data", "sample")
    return (os.path.join(s, "kv_index.jsonl"),
            os.path.join(s, "kv_anchors.bin"),
            os.path.join(s, "kv_index.order"),
            os.path.join(s, "lz_route.log"))


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
                ANN_WALK_FLOOR=os.environ.get("ANN_WALK_FLOOR", "0.5"))
    out = open(os.path.join(DIR, ".server.log"), "ab")
    err = open(os.path.join(DIR, ".server.err"), "ab")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
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


def once():
    if _healthy():
        return True
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
    _log("ann started but /health never answered")
    return False


def main():
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
        if time.time() - t0 < FAST_DEATH:
            backoff = min(BACKOFF_MAX, backoff * 2)
        _log("retry in %ds" % backoff)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
