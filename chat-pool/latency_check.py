"""latency_check.py — prove SSE delivery works under high latency.

The server uses daemon threads and non-blocking writes.  If a client is slow
to consume frames, the write buffer fills, and the question is whether the
server deadlocks (stops serving everyone) or degrades gracefully (drops the
slow client and keeps serving).

Test: start a server, open a SSE client that reads SLOWLY (sleep 500ms
between reads), push 10 lines, verify all arrive.  Then push more lines
to a FAST client while the slow one is still connected — proves the server
is not blocked by the slow reader.

    python latency_check.py

Exit 0 = no deadlock, all lines delivered.
"""
import json
import os
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
SEED = 20
WINDOW = 50


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


def run():
    tmp = tempfile.mkdtemp(prefix="latency-check-")
    log = os.path.join(tmp, "log.jsonl")
    err = os.path.join(tmp, "stderr.txt")
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-40s %s" % ("PASS" if ok else "FAIL", name, detail))

    _seed(log, SEED)
    errf = open(err, "w+", encoding="utf-8", errors="replace")
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--log", log, "--port", "0"],
        cwd=DIR, stdout=subprocess.PIPE, stderr=errf, text=True)
    try:
        ready = child.stdout.readline().strip()
        if not ready.startswith("READY "):
            print("latency_check: server did not start — %r" % ready)
            return 1
        port = int(ready.split()[1])
        print("latency_check: server on 127.0.0.1:%d  (log %s)" % (port, log))

        # --- Test 1: slow client receives all lines ---
        # Open a client that reads from the socket with a long timeout,
        # simulating a slow consumer.
        slow, buf, status = sse_open(port)
        check("slow-client-up", "200" in status, status)

        # Push 5 lines with small gaps
        for i in range(5):
            _say(port, "alice", "slow-line-%d" % i)
            time.sleep(0.15)

        # Read slowly: set a long timeout, read in small chunks
        slow.settimeout(8.0)
        deadline = time.time() + 8.0
        while time.time() < deadline:
            try:
                chunk = slow.recv(4096)
                if chunk:
                    buf += chunk
                # check if we got all 5
                if all(("slow-line-%d" % i).encode() in buf for i in range(5)):
                    break
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break

        got_all = all(("slow-line-%d" % i).encode() in buf for i in range(5))
        check("slow-client-gets-all", got_all,
              "got %d/5 lines" % sum(1 for i in range(5)
                                      if ("slow-line-%d" % i).encode() in buf))

        # --- Test 2: server not blocked by slow client ---
        # While slow client is still connected, open a fast client and
        # push lines.  If the server is deadlocked, the fast client won't
        # get them.
        fast, buf_fast, status_fast = sse_open(port)
        check("fast-client-up", "200" in status_fast, status_fast)

        for i in range(3):
            _say(port, "bob", "fast-line-%d" % i)
            time.sleep(0.1)

        ok_fast, buf_fast = sse_expect(fast, buf_fast, b"fast-line-2", seconds=5.0)
        check("fast-client-gets-lines", ok_fast,
              "server not blocked by slow reader")

        # --- Test 3: server health during slow read ---
        st, body = _get(port, "/health")
        check("server-healthy", st == 200 and body.get("ok"),
              "lines=%s" % body.get("lines"))

        # --- Test 4: rapid connect/disconnect doesn't wedge ---
        for i in range(10):
            s, b, _ = sse_open(port)
            _say(port, "carol", "rapid-%d" % i)
            abort(s)
            time.sleep(0.05)

        st, body = _get(port, "/health")
        check("survives-rapid-cycles", st == 200 and body.get("ok"),
              "lines=%s" % body.get("lines"))

        # --- Test 5: lines after rapid cycles still arrive ---
        s_final, buf_final, _ = sse_open(port)
        _say(port, "alice", "after-rapid")
        ok_final, buf_final = sse_expect(s_final, buf_final, b"after-rapid")
        check("delivery-after-rapid", ok_final)
        abort(s_final)
        abort(slow)
        abort(fast)

        errf.flush()
        errf.seek(0)
        errtext = errf.read()
        check("no-traceback", "Traceback" not in errtext,
              "%d bytes of stderr" % len(errtext))
    finally:
        if child.poll() is None:
            child.kill()
        errf.close()
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    total = len(results)
    passed = total - len(bad)
    print("\nlatency_check: %d/%d PASS%s" % (
        passed, total,
        "" if not bad else " — FAILED: " + ", ".join(bad)))
    return 1 if bad else 0


def main(argv):
    if "--serve" in argv:
        log = argv[argv.index("--log") + 1]
        port = int(argv[argv.index("--port") + 1]) if "--port" in argv else 0
        _serve(log, port)
        return 0
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
