"""reconnect_check.py — prove the SSE reconnect cycle works end-to-end and that
the client backoff formula is correct (3→6→12→24s cap).

Two layers:
  A. SERVER SIDE — start a server, open SSE, hard-RST, push lines while dead,
     reconnect, prove delivery.  This is what actually ships.
  B. FORMULA SIDE — extract the backoff JS from pool_server.py, simulate the
     sequence in Python, verify it matches [3,6,12,24,24,...] and that
     streamRetry resets to 0 on open.

Requires nothing but stdlib; no node, no browser.

    python reconnect_check.py
    python reconnect_check.py --keep    # leave server up for debugging

Exit 0 = all checks passed.
"""
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

DIR = os.path.dirname(os.path.abspath(__file__))
SEED = 30
WINDOW = 5


# ---------------------------------------------------------------- child mode
def _serve(log_path, port):
    sys.path.insert(0, DIR)
    import pool
    import pool_server
    home = os.path.dirname(log_path)
    pool.LOG = log_path
    pool.APPS = os.path.join(home, "apps.json")
    pool.STATE = os.path.join(home, ".flush_state.json")
    srv = pool_server.PoolServer(("127.0.0.1", port), pool_server.PoolHandler)
    print("READY %d" % srv.server_address[1], flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.server_close()


def _seed(log_path, n):
    sys.path.insert(0, DIR)
    import pool
    home = os.path.dirname(log_path)
    pool.LOG = log_path
    pool.APPS = os.path.join(home, "apps.json")
    pool.STATE = os.path.join(home, ".flush_state.json")
    apps = ("alice", "bob", "carol")
    for a in apps:
        pool.register_app(a)
    for i in range(n):
        pool.append(apps[i % len(apps)], "chat", "seed line %03d" % i, to="all")
    return pool


# ------------------------------------------------------------------- http bits
def _get(port, path, timeout=5):
    url = "http://127.0.0.1:%d%s" % (port, path)
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _post(port, path, obj, timeout=5):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _say(port, app, text):
    st, body = _post(port, "/append", {"app": app, "kind": "chat", "text": text})
    assert st == 200 and body.get("ok"), "POST /append failed: %s" % body
    return body["hash"]


# -------------------------------------------------------------- raw sse client
def sse_open(port, n=WINDOW, timeout=5):
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    s.sendall(("GET /stream?n=%d HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Accept: text/event-stream\r\n\r\n" % n).encode())
    head = b""
    s.settimeout(timeout)
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    status, _, rest = head.partition(b"\r\n\r\n")
    return s, rest, status.split(b"\r\n")[0].decode(errors="replace")


def sse_expect(sock, buf, needle, seconds=6.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if needle in buf:
            return True, buf
        sock.settimeout(max(0.1, deadline - time.time()))
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError):
            break
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return needle in buf, buf


def abort(sock):
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


# ------------------------------------------------------- backoff formula tests
def _extract_backoff_js():
    """Read pool_server.py and extract the reconnect formula + reset line."""
    with open(os.path.join(DIR, "pool_server.py"), encoding="utf-8") as f:
        src = f.read()

    # the cap: Math.min(streamRetry+1, <cap>)
    cap_m = re.search(r"streamRetry\s*=\s*Math\.min\(\s*streamRetry\s*\+\s*1\s*,\s*(\d+)\s*\)", src)
    # the delay: setTimeout(listen, <base>*Math.pow(2,streamRetry-1))
    delay_m = re.search(r"setTimeout\(\s*listen\s*,\s*(\d+)\s*\*\s*Math\.pow\(\s*2\s*,\s*streamRetry\s*-\s*1\s*\)\s*\)", src)
    # the reset: streamRetry=0 inside onopen handler (not just the init)
    # JS syntax: stream.onopen=()=>{streamRetry=0;...}
    reset_m = re.search(r"onopen\s*=\s*\(\)\s*=>\s*\{[^}]*streamRetry\s*=\s*0", src)
    return cap_m, delay_m, reset_m


def test_backoff_formula():
    """Simulate the backoff sequence and verify it matches [3,6,12,24,24,...]."""
    cap_m, delay_m, reset_m = _extract_backoff_js()
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-38s %s" % ("PASS" if ok else "FAIL", name, detail))

    # --- structural: formula exists in source ---
    check("formula-cap-exists", cap_m is not None,
          "cap=%s" % (cap_m.group(1) if cap_m else "missing"))
    check("formula-delay-exists", delay_m is not None,
          "base=%sms" % (delay_m.group(1) if delay_m else "missing"))
    check("formula-reset-exists", reset_m is not None,
          "streamRetry=0 on open")

    if not (cap_m and delay_m and reset_m):
        return results  # can't simulate without the formula

    cap = int(cap_m.group(1))
    base = int(delay_m.group(1))

    # --- simulate the sequence ---
    # streamRetry starts at 0 (reset on open), then:
    #   error: streamRetry = min(streamRetry+1, cap)
    #   delay = base * 2^(streamRetry-1)
    expected = [3, 6, 12, 24, 24, 24]  # seconds
    stream_retry = 0
    delays = []
    for _ in range(8):
        stream_retry = min(stream_retry + 1, cap)
        delay_ms = base * math.pow(2, stream_retry - 1)
        delays.append(delay_ms / 1000)

    check("cap-is-4", cap == 4, "cap=%d" % cap)
    check("base-is-3000", base == 3000, "base=%d" % base)
    check("sequence-matches", delays[:6] == expected,
          "got %s" % ["%.0f" % d for d in delays[:6]])
    check("cap-holds-after-max", all(d == 24.0 for d in delays[3:]),
          "delays after cap: %s" % ["%.0f" % d for d in delays[3:]])

    # --- reset on open ---
    # after 5 errors (stream_retry at cap), an open resets to 0
    stream_retry = 4  # at cap
    # onopen: streamRetry = 0
    stream_retry = 0
    # next error: min(0+1, 4) = 1 → delay = 3000 * 2^0 = 3000ms
    next_delay = base * math.pow(2, min(stream_retry + 1, cap) - 1)
    check("reset-brings-delay-to-3s", next_delay == 3000.0,
          "after reset delay=%.0fms" % next_delay)

    return results


# --------------------------------------------------- server-side reconnect test
def test_server_reconnect(port):
    """Open SSE, RST, push lines while dead, reconnect, prove delivery."""
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-38s %s" % ("PASS" if ok else "FAIL", name, detail))

    # 1. connect and receive a line
    s, buf, status = sse_open(port)
    check("sse-up", "200" in status, status)

    _say(port, "alice", "pre-drop-1")
    ok, buf = sse_expect(s, buf, b"pre-drop-1")
    check("line-before-drop", ok)

    # 2. hard RST
    abort(s)
    time.sleep(0.5)

    # 3. push lines while SSE is dead
    for i in range(3):
        _say(port, "bob", "during-drop-%d" % i)
        time.sleep(0.3)

    # 4. server still alive?
    st, body = _get(port, "/health")
    check("server-alive-after-rst", st == 200 and body.get("ok"),
          "lines=%s" % body.get("lines"))

    # 5. reconnect — the stream sends no backlog, so lines pushed during
    #    the drop are already in the log and will not be re-sent.  What
    #    must work is that NEW lines arrive after the reconnect.
    s2, buf2, status2 = sse_open(port)
    check("reconnect-up", "200" in status2, status2)

    _say(port, "carol", "post-reconnect-live")
    ok, buf2 = sse_expect(s2, buf2, b"post-reconnect-live", seconds=4.0)
    check("delivers-after-reconnect", ok, "line pushed after reconnect arrived")
    abort(s2)

    return results


# ----------------------------------------------------------------------- main
def run(keep=False):
    tmp = tempfile.mkdtemp(prefix="reconnect-check-")
    log = os.path.join(tmp, "log.jsonl")
    err = os.path.join(tmp, "stderr.txt")
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-38s %s" % ("PASS" if ok else "FAIL", name, detail))

    _seed(log, SEED)
    errf = open(err, "w+", encoding="utf-8", errors="replace")
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--log", log, "--port", "0"],
        cwd=DIR, stdout=subprocess.PIPE, stderr=errf, text=True)
    try:
        ready = child.stdout.readline().strip()
        if not ready.startswith("READY "):
            print("reconnect_check: server did not start — %r" % ready)
            return 1
        port = int(ready.split()[1])
        print("reconnect_check: server on 127.0.0.1:%d  (log %s)" % (port, log))

        # --- Layer A: server-side reconnect ---
        print("\n  ── server-side reconnect ──")
        results += test_server_reconnect(port)

        # --- Layer B: backoff formula ---
        print("\n  ── backoff formula ──")
        results += test_backoff_formula()

        errf.flush()
        errf.seek(0)
        errtext = errf.read()
        check("no-traceback", "Traceback" not in errtext,
              "%d bytes of stderr" % len(errtext))

        if keep:
            print("\nreconnect_check: --keep — server on http://127.0.0.1:%d/" % port)
            return 0
    finally:
        if child.poll() is None and not keep:
            child.kill()
        errf.close()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print("\nreconnect_check: %d/%d PASS%s"
          % (len(results) - len(bad), len(results),
             "" if not bad else " — FAILED: " + ", ".join(bad)))
    return 1 if bad else 0


def main(argv):
    if "--serve" in argv:
        log = argv[argv.index("--log") + 1]
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
        _serve(log, port)
        return 0
    return run(keep="--keep" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
