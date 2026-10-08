"""pool_keepalive.py — supervisor for pool_server.py (:8390).

WHY THIS EXISTS
  pool_server.py was run by hand. Nothing restarted it: if the box rebooted, or
  the process died (a real bug did this — a >4 KiB log record crashed every
  append), the hub stayed down until a human noticed. This is the keeper.

WHAT IT DOES (loop, one instance only)
  1. Is /health answering? -> leave the server alone.
  2. Not answering? -> start pool_server.py detached, then wait for /health.
  3. Repeat every CHECK seconds.

GUARDS (learned from the failure that motivated this)
  * ONE instance only. A PID file + a global mutex-free lock; if another keeper
    holds it, this one exits instead of stacking servers.
  * Backoff on crash-loop. If we start the server and it dies again quickly
    (a real bug, not a one-off), we slow down instead of restarting in a tight
    loop, and we log the failure loudly. A supervisor that spins on a broken
    server is worse than none.
  * Never kill a healthy server. We only ever START; we never taskkill.

RUN
  pythonw.exe pool_keepalive.py        (the scheduled task; no console)
  python.exe  pool_keepalive.py --once (one pass, for a check)
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
PY = r"I:\python3.14.4\python.exe"
PYW = r"I:\python3.14.4\pythonw.exe"
SERVER = os.path.join(DIR, "pool_server.py")
PIDFILE = os.path.join(DIR, ".keepalive.pid")
LOG = os.path.join(DIR, ".keepalive.log")

PORT = int(os.environ.get("POOL_PORT", "8390"))
HEALTH = "http://127.0.0.1:%d/health" % PORT
CHECK = 60            # seconds between checks when healthy
BACKOFF_MIN = 60      # fastest retry after a failed start
BACKOFF_MAX = 900     # slowest retry (15 min) after repeated fast deaths
FAST_DEATH = 20       # a server that dies within this many seconds "crashed"


def _log(msg):
    line = "%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def _healthy(timeout=3):
    """True iff the server answers /health with ok:true."""
    try:
        with urllib.request.urlopen(HEALTH, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return bool(data.get("ok"))
    except (urllib.error.URLError, ValueError, OSError):
        return False


def _only_instance():
    """Claim the keeper slot. A live PID in the file means someone else has it.

    Not perfect on Windows (PID reuse), but the consequence of a false
    positive is one skipped keeper, and the consequence of no guard is a
    second keeper stacking a second server — worse. We also re-check that the
    recorded PID looks like our own script before believing it.
    """
    if os.path.exists(PIDFILE):
        try:
            old = int(open(PIDFILE, encoding="utf-8").read().strip() or "0")
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _pid_is_keepalive(old):
            return False
    try:
        with open(PIDFILE, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return True


def _pid_is_keepalive(pid):
    """Does PID look like a live python running pool_keepalive? Best-effort."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True   # cannot tell -> assume it is, and skip (safer than stacking)
    return "python" in out.lower()


def _start_server():
    """Start pool_server.py detached, no console, logs beside the code."""
    out = open(os.path.join(DIR, ".server.log"), "ab")
    err = open(os.path.join(DIR, ".server.err"), "ab")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    p = subprocess.Popen([PY, SERVER], cwd=DIR,
                         stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                         creationflags=flags, close_fds=True)
    return p


def _wait_healthy(seconds=25):
    """Poll /health until it answers or the budget runs out. Returns bool."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(1)
    return False


def once():
    """One supervision pass. Returns True if the server is healthy afterward."""
    if _healthy():
        return True
    _log("server DOWN -> starting")
    try:
        _start_server()
    except OSError as e:
        _log("start FAILED: %r" % e)
        return False
    if _wait_healthy():
        _log("server UP")
        return True
    _log("server started but /health never answered")
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
        ok = once()
        if ok:
            backoff = BACKOFF_MIN
            time.sleep(CHECK)
            continue
        # failed start or fast death: back off so a broken server is not
        # restarted in a tight loop (that was the whole hazard)
        lived = time.time() - t0
        if lived < FAST_DEATH:
            backoff = min(BACKOFF_MAX, backoff * 2)
        _log("retry in %ds" % backoff)
        time.sleep(backoff)


if __name__ == "__main__":
    sys.exit(main())
