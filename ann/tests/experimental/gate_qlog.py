#!/usr/bin/env python3
"""Gates for the ANN query log (post-audit 2026-10-29).

G1-G6 run against the LIVE server on 8096 and read its own log; G7 starts a throwaway
instance with an unwritable log dir. Keeping the fast gates on the live process makes the
whole run ~35 s instead of two 30 s index loads, and the lines it adds are honest receipt
traffic in the real log.

  python gate_qlog.py
Exit 0 = every gate passed.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ANN = Path(r"I:\tools\ann")
LIVE = 8096
LIVE_LOG = ANN / "data" / "query_log"
TMP = Path(os.environ.get("TEMP", ".")) / ("qlog_gate_%d" % os.getpid())
BAD_EXE_PORT = 8098
EXE = ANN / "build" / "ann_search.exe"

results = []


def gate(name, ok, detail=""):
    results.append((name, ok, detail))
    print("%-48s %s  %s" % (name, "PASS" if ok else "FAIL", detail))


def post(path, body, port=LIVE, timeout=120):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def flush(port=LIVE):
    req = urllib.request.Request("http://127.0.0.1:%d/v1/qlog/flush" % port)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"raw": raw[:200]}


def lines(logdir=LIVE_LOG):
    out = []
    for f in sorted(logdir.glob("ann_query-*.jsonl")):
        out += [l for l in f.read_text(encoding="utf-8", errors="replace").splitlines() if l.strip()]
    return out


def vecs(n=3):
    out = []
    with open(ANN / "data" / "memcore" / "ann_index.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d.get("vec"):
                out.append(d["vec"][:384])
                if len(out) >= n:
                    break
    return out


def main():
    v = vecs()
    base = len(lines())

    # G1 the 1-second timer: one search, then silence. No second request may be needed.
    post("/v1/state/search", {"vec": v[0], "topk": 3, "coord": "ses_gate0001#1"})
    time.sleep(2.5)
    got = lines()[base:]
    gate("G1 timer flushes without another request", len(got) == 1,
         "%d line(s) after 2.5 s idle" % len(got))

    # G2 the one unescaped field: a backslash/quote/newline in sid must not split a line
    before = len(lines())
    post("/v1/state/search", {"vec": v[0], "topk": 2, "sid": 'a\\b"c\nd'})
    st, body = flush()
    got = lines()[before:]
    ok = bool(got) and st == 200
    for l in got:
        try:
            json.loads(l)
        except ValueError:
            ok = False
    gate("G2 sid_filter injection cannot split a line",
         ok and len(got) == 1, "%d line(s), flush=%d, dropped=%s"
         % (len(got), st, body.get("dropped_total")))

    # G3 batch flag must not be sticky (the endpoint takes text queries: {"queries":[...]})
    before = len(lines())
    post("/v1/state/search-batch", {"queries": ["tesspack capo load", "stride 37 field"], "k": 2})
    post("/v1/state/search", {"vec": v[2], "topk": 2, "coord": "ses_gate0002#2"})
    flush()
    flags = [json.loads(l).get("batch") for l in lines()[before:]]
    gate("G3 batch flag resets per request", flags == [1, 1, 0], "batch flags %s" % flags)

    # G4 the widest allowed topk with a real scope must still produce a parseable line
    before = len(lines())
    post("/v1/state/search", {"vec": v[0], "topk": 32, "scope": "tag:chatpool",
                              "coord": "ses_gate0003#3"})
    st, body = flush()
    got = [json.loads(l) for l in lines()[before:]]
    gate("G4 topk=32 + real scope is not dropped",
         len(got) == 1 and got[0]["topk"] == 32,
         "flush=%d lines=%d" % (st, len(got)))

    # G4b a REFUSED request must still leave a line (mode=reject:*) — no invisible traffic
    before = len(lines())
    post("/v1/state/search", {"vec": v[0], "topk": 3, "scope": "no_such_tag_xyz",
                              "coord": "ses_gate0006#6"})
    flush()
    got = [json.loads(l) for l in lines()[before:]]
    gate("G4b refused request is logged as reject",
         len(got) == 1 and got[0]["mode"] == "reject:scope_not_found",
         "lines=%d mode=%s" % (len(got), got[0]["mode"] if got else "-"))

    # G5 ts_ms must be the request clock, not the answer clock
    t0 = int(time.time() * 1000)
    post("/v1/state/search", {"vec": v[0], "topk": 3, "coord": "ses_gate0004#4"})
    t1 = int(time.time() * 1000)
    flush()
    rec = json.loads(lines()[-1])
    answer_clock_would_be = rec["ts_ms"] + rec["latency_us"] / 1000.0
    gate("G5 ts_ms is the request clock",
         t0 - 100 <= rec["ts_ms"] <= t1 + 100,
         "ts_ms in [%d,%d] window; latency=%.0f us (answer clock would be +%.0f ms)"
         % (t0, t1, rec["latency_us"], rec["latency_us"] / 1000.0))

    # G6 line size stays inside the buffer
    mx = max(len(l) for l in lines())
    gate("G6 every line fits the 2048-byte cap", mx < 2048, "max %d bytes" % mx)

    # G7 an unwritable log dir must answer with an error, never a silent ok
    ensure_free(BAD_EXE_PORT)
    TMP.mkdir(parents=True, exist_ok=True)
    blocker = TMP / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")   # a path under a FILE can never be created
    env = dict(os.environ)
    env.update({
        "ANN_INDEX_PATH": r"I:\tools\ann\data\memcore\ann_index.jsonl",
        "ANN_ANCH_PATH": r"I:\tools\ann\data\memcore\ann_anchors.bin",
        "ANN_ORDER_PATH": r"I:\tools\ann\data\memcore\ann_index.order",
        "ANN_QUERY_LOG_DIR": str(blocker / "logs"),
        "ANN_QUERY_LOG_TIMER_MS": "0",
    })
    p = subprocess.Popen([str(EXE), "--serve", str(BAD_EXE_PORT)], cwd=str(ANN), env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ready = False
    for _ in range(60):
        time.sleep(1.5)
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/health" % BAD_EXE_PORT, timeout=4) as r:
                if json.loads(r.read().decode()).get("routed") == 1:
                    ready = True
                    break
        except Exception:
            pass
    try:
        if not ready:
            gate("G7 unwritable log dir reports an error", False, "instance did not start")
        else:
            post("/v1/state/search", {"vec": v[0], "topk": 2, "coord": "ses_gate0005#5"},
                 port=BAD_EXE_PORT)
            st, body = flush(port=BAD_EXE_PORT)
            # The request itself flushes once the server has been up >1 s, so the failed write
            # has usually already happened and been counted before this flush call. The gate is
            # "the blindness is visible", not "this particular call writes": either the flush
            # answers error, or the sticky last_error names the unwritable path.
            visible = (st != 200 and body.get("status") == "error") or bool(body.get("last_error"))
            gate("G7 unwritable log dir reports an error",
                 visible and body.get("dropped_total", 0) >= 1,
                 "HTTP %d status=%s lost=%s dropped_total=%s last_error=%s"
                 % (st, body.get("status"), body.get("lost"), body.get("dropped_total"),
                    body.get("last_error")))
    finally:
        p.terminate()
        try:
            p.wait(15)
        except subprocess.TimeoutExpired:
            p.kill()

    bad = [r for r in results if not r[1]]
    print("\n%d/%d gates passed" % (len(results) - len(bad), len(results)))
    return 0 if not bad else 1


def ensure_free(port):
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, timeout=30).stdout
    except Exception:
        return
    for line in out.splitlines():
        f = line.split()
        if len(f) >= 5 and f[1].endswith(":%d" % port) and f[3] == "LISTENING":
            if f[4].isdigit():
                subprocess.run(["taskkill", "/F", "/PID", f[4]], capture_output=True)
                time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
