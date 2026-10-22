"""pool_keepalive.py — supervisor for pool_server.py (:8390).

WHY THIS EXISTS
  pool_server.py was run by hand. Nothing restarted it: if the box rebooted, or
  the process died (a real bug did this — a >4 KiB log record crashed every
  append), the hub stayed down until a human noticed. This is the keeper.

WHAT IT DOES
  one pass (once()): if /health answers, do nothing; else start the server
  detached and wait for /health. The scheduled task runs --once every 5 min and
  at logon; bare with no flag it loops in the foreground.

GUARDS (learned from the failure that motivated this)
  * ONE instance only. A PID file names the holder; a second keeper exits
    instead of stacking a second server.
  * Backoff on crash-loop. A server that dies quickly (a real bug, not a
    one-off) is retried on a growing delay, not a tight loop. A supervisor that
    spins on a broken server is worse than none.
  * Never kill a healthy server. We only ever START; we never taskkill.

RUN
  pythonw.exe pool_keepalive.py        (the scheduled task; no console)
  python.exe  pool_keepalive.py --once (one pass, for a check)
  python.exe  pool_keepalive.py demo   (isolated self-check, temp dir + port)
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

    Not perfect on Windows (PID reuse), but a false positive only skips one
    keeper, while no guard lets a second keeper stack a second server — worse.
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
    """Does PID look like a live python? Best-effort; cannot-tell means yes."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "python" in out.lower()


def _start_server():
    """Start the server detached, no console, logs beside the code."""
    out = open(os.path.join(DIR, ".server.log"), "ab")
    err = open(os.path.join(DIR, ".server.err"), "ab")
    flags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        flags |= subprocess.CREATE_NO_WINDOW
    return subprocess.Popen([PY, SERVER], cwd=DIR,
                            stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                            creationflags=flags, close_fds=True)


def _wait_healthy(seconds=25):
    """Poll /health until it answers or the budget runs out."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _healthy():
            return True
        time.sleep(1)
    return False


def once():
    """One supervision pass. True iff the server is healthy afterward."""
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
        if once():
            backoff = BACKOFF_MIN
            time.sleep(CHECK)
            continue
        if time.time() - t0 < FAST_DEATH:   # died fast: real bug, slow down
            backoff = min(BACKOFF_MAX, backoff * 2)
        _log("retry in %ds" % backoff)
        time.sleep(backoff)


# ── self-check (temp dir + temp port; never touches the live hub) ───────
def demo():
    """Runs once()/guards against a STUB server on a free port, in a temp dir.

    A stub, not the real pool_server, on purpose: this proves the keeper's
    contract (poll -> start on down -> wait for health -> never stack) fast and
    deterministically. Booting a 320 KB server in a self-check would be neither.
    """
    import socket
    import tempfile
    import shutil

    global DIR, SERVER, PIDFILE, LOG, PORT, HEALTH

    box = tempfile.mkdtemp(prefix="keepalive-demo-")
    stub = os.path.join(box, "stub_server.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import json, os\n"
            "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
            "open(os.path.join(os.path.dirname(os.path.abspath(__file__)),"
            " 'starts.log'), 'a').write('x\\n')\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        b = json.dumps({'ok': True}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Type', 'application/json')\n"
            "        self.send_header('Content-Length', str(len(b)))\n"
            "        self.end_headers(); self.wfile.write(b)\n"
            "    def log_message(self, *a): pass\n"
            "HTTPServer(('127.0.0.1', int(os.environ['POOL_PORT'])),"
            " H).serve_forever()\n")

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    DIR, SERVER = box, stub
    PIDFILE = os.path.join(box, ".keepalive.pid")
    LOG = os.path.join(box, ".keepalive.log")
    PORT = port
    HEALTH = "http://127.0.0.1:%d/health" % port
    os.environ["POOL_PORT"] = str(port)
    starts = os.path.join(box, "starts.log")

    ok = []
    ok.append(_healthy(timeout=2) is False)                 # nothing is up yet
    ok.append(once() is True)                               # starts it
    ok.append(_healthy() is True)                           # /health answers
    ok.append(once() is True)                               # second pass: no-op
    n = len(open(starts).read().split()) if os.path.exists(starts) else 0
    ok.append(n == 1)                                       # started exactly once
    ok.append(_wait_healthy(seconds=5) is True)             # poll returns fast
    open(PIDFILE, "w").write(str(os.getpid()))
    ok.append(_only_instance() is True)                     # own pid -> holder
    open(PIDFILE, "w").write("999999")
    ok.append(_only_instance() is True)                     # dead pid -> take it

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "(Get-NetTCPConnection -LocalPort %d -State Listen"
         " -EA SilentlyContinue).OwningProcess" % port],
        capture_output=True, text=True).stdout.split()
    for pid in set(out):
        if pid.isdigit():
            subprocess.run(["taskkill", "/F", "/PID", pid],
                           capture_output=True, text=True)
    shutil.rmtree(box, ignore_errors=True)

    fails = [i for i, x in enumerate(ok) if not x]
    print("pool_keepalive demo: %d/%d ok%s" % (sum(ok), len(ok),
          "  FAILED at " + ",".join(map(str, fails)) if fails else ""))
    return all(ok)


if __name__ == "__main__":
    if "demo" in sys.argv:
        sys.exit(0 if demo() else 1)
    sys.exit(main())
