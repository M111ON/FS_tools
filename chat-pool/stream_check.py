"""stream_check.py — live end-to-end check of /stream (SSE) and of the
disconnect path it has to survive.

Written because the failure it guards was invisible to every other check: the
server kept answering /health and pool.py's self-check kept passing while three
things were broken at once —

  * the SSE cursor was a line COUNT while the window was capped at n, so once
    the log passed n lines the stream went permanently silent (no error, just
    a chat that never updated again);
  * every (re)connect re-sent the whole window, which is how one session's read
    arrived at every card as the entire chat;
  * socketserver printed a full ConnectionAbortedError traceback per tab close,
    reload or preview detach (WinError 10053), burying the terminal in phantom
    failures — while the one real question (is the server still serving?) was
    never asked.

So this does not inspect code, it starts a real server on an ephemeral port
against a throwaway log and drives it over a real socket, including a hard RST
in the middle of a live stream. The live log is never touched: the child gets
its own log.jsonl + apps.json in a temp dir.

    python stream_check.py          # start server, run checks, clean up
    python stream_check.py --keep   # leave the server up and print its port

Exit 0 = all checks passed. Requires nothing but stdlib; no node, no browser.
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
SEED = 30          # baseline lines: comfortably more than the SSE window
WINDOW = 5         # n= on /stream, so the window is SMALLER than the log


# ---------------------------------------------------------------- child mode
def _serve(log_path, port):
    """Run the real server on a throwaway log. Same code path as production:
    pool_server.PoolHandler + PoolServer, only pool.LOG/APPS/STATE repointed.
    Bound to loopback so no token is needed and no LAN port is opened."""
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


# --------------------------------------------------------------- seed (parent)
def _seed(log_path, n):
    """Baseline history, written offline through pool.append so the chain is
    real (hash-linked) before the server ever starts."""
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


def _post_status(port, path, raw, timeout=5):
    """POST a RAW body and report (status, json-or-None) *including* errors.
    urlopen raises HTTPError on 4xx — which is precisely the answer these checks
    ask for — so it is caught instead of escaping. Status 0 means no response at
    all, which is the failure being guarded against."""
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=raw,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return e.code, None
    except (urllib.error.URLError, OSError):
        return 0, None


def _say(port, app, text):
    """Append the production way — through the server's own POST /append,
    process lock and hash chain included. A test that pokes the file directly
    would not exercise the path sessions actually use."""
    st, body = _post(port, "/append", {"app": app, "kind": "chat", "text": text})
    assert st == 200 and body.get("ok"), "POST /append failed: %s" % body
    return body["hash"]


# -------------------------------------------------------------- raw sse client
def sse_open(port, n=WINDOW, timeout=5):
    """Returns (sock, buf, status_line). buf holds whatever body bytes came in
    with the header (they must not be dropped — the first frame can ride along
    with the response head)."""
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
    """Wait for `needle` (bytes) or a timeout. Returns (found, buf)."""
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


def sse_quiet(sock, buf, seconds=2.0):
    """Read for `seconds` and return everything that arrived. Used to prove the
    stream sends NOTHING on connect (no backfill) — a single line is the whole
    point of that fix, so 'roughly silent' is not good enough."""
    got = b""
    deadline = time.time() + seconds
    while time.time() < deadline:
        sock.settimeout(max(0.1, deadline - time.time()))
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, TimeoutError):
            break
        except OSError:
            break
        if not chunk:
            break
        got += chunk
    return buf + got, got


def abort(sock):
    """Abort for real: SO_LINGER 0 makes close() send RST instead of FIN, which
    is the shape Windows reports as WinError 10053 on the server side. A clean
    close() would let the server notice politely and never reach the broken
    write we are testing."""
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                        struct.pack("ii", 1, 0))
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def frames(buf):
    """Parse complete SSE frames out of a buffer."""
    out = []
    for block in buf.split(b"\n\n"):
        if not block.startswith(b"data: "):
            continue
        try:
            out.append(json.loads(block[6:].decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            pass
    return out


# --------------------------------------------------------------------- driver
def run(keep=False):
    tmp = tempfile.mkdtemp(prefix="stream-check-")
    log = os.path.join(tmp, "log.jsonl")
    err = os.path.join(tmp, "stderr.txt")
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok), detail))
        print("  %s %-22s %s" % ("PASS" if ok else "FAIL", name, detail))

    _seed(log, SEED)
    errf = open(err, "w+", encoding="utf-8", errors="replace")
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--serve",
         "--log", log, "--port", "0"],
        cwd=DIR, stdout=subprocess.PIPE, stderr=errf, text=True)
    try:
        ready = child.stdout.readline().strip()
        if not ready.startswith("READY "):
            print("stream_check: server did not start — %r" % ready)
            print(errf.read() if hasattr(errf, "read") else "")
            return 1
        port = int(ready.split()[1])
        print("stream_check: server on 127.0.0.1:%d  (log %s)" % (port, log))

        st, body = _get(port, "/health")
        check("health", st == 200 and body.get("lines") == SEED,
              "lines=%s (seeded %d)" % (body.get("lines"), SEED))

        # A write whose body cannot be parsed must still ANSWER. json.loads used
        # to be unguarded in all four write endpoints, so a bad body raised out
        # of do_POST and the socket closed with no response at all (status 0
        # here). The caller then cannot tell "refused" from "server gone" —
        # which on the input path is the difference between retry and give up.
        st, b = _post_status(port, "/append", b"{broken json")
        check("bad-body-answers", st == 400 and isinstance(b, dict) and b.get("error"),
              "status=%s error=%r" % (st, (b or {}).get("error")))

        st, b = _post_status(port, "/append", b"")
        check("empty-body-answers", st == 400 and isinstance(b, dict),
              "status=%s" % st)

        st, b = _post_status(port, "/append", b"[1,2,3]")
        check("non-object-answers", st == 400, "status=%s" % st)

        # the label has to name the real cause: a malformed body that came back
        # as "? not registered" would send the caller to fix the registry
        st, b = _post_status(port, "/append", b"{broken json")
        check("bad-body-blames-body", st == 400 and "JSON object" in (b or {}).get("error", ""),
              "error=%r" % (b or {}).get("error"))

        # ...and all three left the handler thread serving (a raise would not)
        st, b = _get(port, "/health")
        check("serving-after-bad-body", st == 200 and b.get("ok"),
              "lines=%s" % b.get("lines"))

        # 1. connect, n=5 but 30 lines on disk, nothing new → silence.
        a, buf, status = sse_open(port, WINDOW)
        check("stream-up", status.startswith("HTTP/1.0 200") or "200" in status,
              status)

        buf, got = sse_quiet(a, buf, 2.0)
        check("no-backfill", b"data: " not in got,
              "%d bytes in 2.0s of idle" % len(got))

        # 2. one append must arrive even though the log is longer than n.
        #    This is the count-cursor bug: with a count cursor the window
        #    length stops changing at n and the stream goes quiet forever.
        _say(port, "alice", "live-1 past the window cap")
        ok, buf = sse_expect(a, buf, b"live-1 past the window cap")
        check("live-past-window", ok, "n=%d, %d lines on disk" % (WINDOW, SEED + 1))

        ms = [m for m in frames(buf) if "live-1" in m.get("text", "")]
        check("frame-has-tick", bool(ms) and isinstance(ms[0].get("seq"), int),
              "seq=%s (feed chunks by seq//8)" % (ms[0].get("seq") if ms else None))

        # 3. hard RST, then keep talking. The server must write to the dead
        #    socket, swallow it, and keep serving everyone else.
        abort(a)
        for i in (2, 3, 4):
            _say(port, "bob", "after-rst-%d" % i)
            time.sleep(1.2)
        time.sleep(1.0)

        alive = child.poll() is None
        check("server-survives-rst", alive,
              "exit code %s" % child.poll())

        st2, body2 = _get(port, "/health")
        check("health-after-rst", st2 == 200 and body2.get("ok"),
              "lines=%s" % body2.get("lines"))

        # 4. a fresh stream still gets new lines: proves the handler thread
        #    ended (daemon thread + dropped socket) instead of wedging.
        b, buf2, status2 = sse_open(port, WINDOW)
        _say(port, "carol", "reconnected-after-rst")
        ok2, buf2 = sse_expect(b, buf2, b"reconnected-after-rst")
        check("reconnect-after-drop", ok2, status2)
        abort(b)
        time.sleep(0.6)

        errf.flush()
        errf.seek(0)
        errtext = errf.read()
        check("no-traceback", "Traceback" not in errtext,
              "%d bytes of stderr" % len(errtext))
        if "went away" in errtext:
            print("       note: also hit PoolServer.handle_error (pre-header drop)")

        if keep:
            print("\nstream_check: --keep — server still on http://127.0.0.1:%d/" % port)
            print("              log: %s" % log)
            print("              stop it with: taskkill //F //PID %d" % child.pid)
            return 0
    finally:
        if child.poll() is None and not keep:
            child.kill()
        errf.close()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)

    bad = [n for n, ok, _ in results if not ok]
    print("\nstream_check: %d/%d PASS%s"
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
