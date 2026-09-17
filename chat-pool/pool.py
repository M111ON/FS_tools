"""B-slice: cross-app group-chat pool. Append-only JSONL outside app dirs.
Line = {t, app, kind, text, prev, hash} hash-chained. Hourly flush: sha256
changed -> emit new lines payload, else skip. Multi-app: O_APPEND single-line
writes (atomic for small lines); single flush owner (see LOCKED spec).
ponytail: stdlib only, no server, no schema beyond chat+snippet."""
import hashlib
import json
import os
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DIR, "log.jsonl")
STATE = os.path.join(DIR, ".flush_state.json")

# Exact port of DWGLS core/rdh_capture.h (RDH_CAPTURE_144, S=1).
# Role: ADDRESS only (routing hint). NOT integrity: walk sees low nibbles
# only -> 1800/6072 collisions + 9.04% flip-blindness measured 2026-09-16.
# Integrity stays sha256 (0/0). Never promote rkey to link.
_RDX = ( (1,0),(1,1),(0,1),(1,-1),(-1,0),(-1,-1),(0,-1),(-1,1),
         (2,0),(1,2),(-1,2),(-2,0) )


def rkey(data: bytes):
    if not data:
        return 0
    F = 144
    ax = ay = 0
    steps = max(len(data), 48)
    for i in range(steps):
        n = data[i % len(data)] & 0x0F
        dx, dy = _RDX[n] if n < 12 else (0, 0)
        ax += dx
        ay += dy
    return ((ay % F) * F + (ax % F))  # ring*144+wedge, [0,20736)


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(65536), b""):
            h.update(b)
    return h.hexdigest()


def _prev():
    if not os.path.exists(LOG) or os.path.getsize(LOG) == 0:
        return "GENESIS"
    with open(LOG, "rb") as f:
        f.seek(max(0, os.path.getsize(LOG) - 4096))
        last = f.read().decode("utf-8", "ignore").strip().split("\n")[-1]
    return json.loads(last)["hash"]


def append(app, kind, text):
    """Append one message. Thread-safe via threading.Lock;
    process-safe via msvcrt.lock on a .lock file (Windows) or fcntl (Unix)."""
    # Atomic: compute prev + body + write under process lock
    lock_path = LOG + ".lock"
    if sys.platform == 'win32':
        import msvcrt
        lfd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            os.lseek(lfd, 0, os.SEEK_SET)
            msvcrt.locking(lfd, msvcrt.LK_LOCK, 1)
            prev = _prev()
            raw_bytes = (app + kind + text).encode("utf-8")
            body = {"v": 2, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "prev": prev,
                    "rkey": rkey(raw_bytes),
                    "enc": rkey(raw_bytes) % 1440}
            body["hash"] = hashlib.sha256(
                (prev + app + kind + text).encode("utf-8")).hexdigest()
            line = json.dumps(body, ensure_ascii=False)
            raw = (line + "\n").encode("utf-8")
            fd = os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
            try:
                assert os.write(fd, raw) == len(raw), "short write"
                os.fsync(fd)
            finally:
                os.close(fd)
            return body
        finally:
            os.lseek(lfd, 0, os.SEEK_SET)
            msvcrt.locking(lfd, msvcrt.LK_UNLCK, 1)
            os.close(lfd)
    else:
        import fcntl
        fd_lock = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd_lock, fcntl.LOCK_EX)
            prev = _prev()
            raw_bytes = (app + kind + text).encode("utf-8")
            body = {"v": 2, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "prev": prev,
                    "rkey": rkey(raw_bytes),
                    "enc": rkey(raw_bytes) % 1440}
            body["hash"] = hashlib.sha256(
                (prev + app + kind + text).encode("utf-8")).hexdigest()
            line = json.dumps(body, ensure_ascii=False)
            raw = (line + "\n").encode("utf-8")
            fd = os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
            try:
                assert os.write(fd, raw) == len(raw), "short write"
                os.fsync(fd)
            finally:
                os.close(fd)
            return body
        finally:
            fcntl.flock(fd_lock, fcntl.LOCK_UN)
            os.close(fd_lock)


def outbox_list():
    out = os.path.join(DIR, "outbox")
    if not os.path.isdir(out):
        return []
    return sorted(os.path.join(out, x) for x in os.listdir(out)
                  if x.endswith(".json"))


CLI = r"I:\tools\cloud-workspace\hermes_pool_cli.py"
VENV_PY = r"I:\tools\cloud-workspace\.venv\Scripts\python.exe"
SESSION = "chat-pool"


def _render_push_text(new):
    """Readable chat-log header + RAW JSON footer.
    Search/embedding sees human lines; the RAW block after the 'RAW:'
    marker keeps prev/hash intact for cloud restore (proven by the
    2026-09-17 RECOVERY). Archives stay raw JSON — only the pushed
    text is rendered."""
    lines = []
    for m in new:
        ts = time.strftime("%m-%d %H:%M", time.localtime(m.get("t", time.time())))
        lines.append("[%s] %s (%s): %s" % (
            ts, m.get("app", "?"), m.get("kind", "chat"), m.get("text", "")))
    return "\n".join(lines) + "\nRAW:" + json.dumps(new, ensure_ascii=False)


def push_outbox(session=SESSION):
    """Push pending outbox payloads via existing pool CLI, then archive.
    Pushed text = rendered chat log + RAW footer; archive stays raw JSON.
    Returns (pushed, failed)."""
    import subprocess
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    pushed, failed = [], []
    for path in outbox_list():
        src = path
        tmp = None
        try:
            batch = json.load(open(path, encoding="utf-8"))
        except Exception:
            batch = None
        if isinstance(batch, list) and batch:
            tmp = path + ".push"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(_render_push_text(batch))
            src = tmp
        r = subprocess.run(
            [py, CLI, "embed", "--session", session,
             "--topic", "hourly", "--file", src],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            if tmp:
                os.remove(tmp)
            done = path + ".pushed"
            os.replace(path, done)
            pushed.append(done)
        else:
            if tmp:
                os.remove(tmp)
            failed.append((path, r.stderr.strip()[-300:]))
    return pushed, failed


def tail(n=10):
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    return [json.loads(x) for x in lines[-n:] if x]


def flush_check():
    """Returns new-lines payload or None when nothing changed."""
    if not os.path.exists(LOG) or os.path.getsize(LOG) == 0:
        return None  # empty pool: nothing to emit, never crash
    cur = _sha(LOG)
    off = 0
    if os.path.exists(STATE):
        st = json.load(open(STATE, encoding="utf-8"))
        if st.get("sha") == cur:
            return None  # no change -> skip
        off = st.get("offset", 0)
    with open(LOG, encoding="utf-8") as f:
        lines = [json.loads(x) for x in f.read().strip().split("\n") if x]
    new = lines[off:]
    json.dump({"sha": cur, "offset": len(lines)},
              open(STATE, "w", encoding="utf-8"))
    return new


def flush_emit():
    """Hourly entry: flush_check -> outbox/<ts>.json payload or None.
    Cloud push reads outbox (MCP side); never pushes when unchanged."""
    new = flush_check()
    if not new:
        return None
    out = os.path.join(DIR, "outbox")
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f"{int(time.time())}.json")
    json.dump(new, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    return path


def demo():
    for p in (LOG, STATE, LOG + ".lock"):
        if os.path.exists(p):
            os.remove(p)
    append("opencode", "chat", "hello pool")
    append("hermes", "code", "print(1+1)")
    assert len(tail(5)) == 2, "tail must see both apps"
    assert tail(5)[0]["prev"] == "GENESIS", "chain starts at GENESIS"
    assert tail(5)[1]["prev"] == tail(5)[0]["hash"], "chain linked"
    assert tail(5)[0]["v"] == 2, "v:2 lines"
    assert tail(5)[0]["enc"] == tail(5)[0]["rkey"] % 1440, "enc free view"
    assert 0 <= tail(5)[1]["rkey"] < 20736, "rkey in RDH space"
    import shutil
    ep1 = flush_emit()
    assert ep1 is not None, "first emit fires"
    assert len(json.load(open(ep1, encoding="utf-8"))) == 2, "emit 2 lines"
    assert flush_emit() is None, "emit skips when unchanged"
    append("codex", "chat", "third msg")
    ep3 = flush_emit()
    assert ep3 is not None, "emit fires on new line"
    assert len(json.load(open(ep3, encoding="utf-8"))) == 1, "emit only new"
    shutil.rmtree(os.path.join(DIR, "outbox"), ignore_errors=True)
    print("POOL-SELFCHECK: 12/12 PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()  # wipes LOG+STATE: test only, never scheduled
    else:
        path = flush_emit()  # hourly safe default: emit+push or skip
        pushed, failed = push_outbox()
        if failed:
            print("PUSH-FAILED", failed)
        print(path if path else "SKIP", f"pushed={len(pushed)}")
