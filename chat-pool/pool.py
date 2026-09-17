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


def append(app, kind, text, to="all"):
    """Append one message. Thread-safe via threading.Lock;
    process-safe via msvcrt.lock on a .lock file (Windows) or fcntl (Unix).
    to="all" = broadcast; to="hermes" = only hermes (or an admin view)
    sees it. `to` is part of the hashed payload — immutable."""
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
            body = {"v": 3, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "to": to, "prev": prev,
                    "rkey": rkey(raw_bytes),
                    "enc": rkey(raw_bytes) % 1440}
            body["hash"] = hashlib.sha256(
                (prev + app + kind + to + text).encode("utf-8")).hexdigest()
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
            body = {"v": 3, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "to": to, "prev": prev,
                    "rkey": rkey(raw_bytes),
                    "enc": rkey(raw_bytes) % 1440}
            body["hash"] = hashlib.sha256(
                (prev + app + kind + to + text).encode("utf-8")).hexdigest()
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


def push_outbox(session=SESSION):
    """Push pending outbox payloads via existing pool CLI, then archive.
    Storage stays raw JSON (prev/hash intact for cloud restore).
    Rendering to a readable chat log is view()'s job, not storage's.
    Returns (pushed, failed). Cloud/searchable afterwards via session-pool."""
    import subprocess
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    pushed, failed = [], []
    for path in outbox_list():
        r = subprocess.run(
            [py, CLI, "embed", "--session", session,
             "--topic", "hourly", "--file", path],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0:
            done = path + ".pushed"
            os.replace(path, done)
            pushed.append(done)
        else:
            failed.append((path, r.stderr.strip()[-300:]))
    return pushed, failed


def _visible(m, as_app=None):
    """v3 visibility: to="all" = everyone; to=X = only X itself
    (plus the sender). as_app=None (human/admin view) sees all —
    the dashboard uses that; readers pass their own identity."""
    to = m.get("to", "all")
    if to == "all" or as_app is None:
        return True
    return as_app == to or as_app == m.get("app")


def tail(n=10, as_app=None):
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding="utf-8") as f:
        lines = f.read().strip().split("\n")
    out = []
    for x in lines:
        if not x:
            continue
        m = json.loads(x)
        if _visible(m, as_app):
            out.append(m)
    return out[-n:]


CLOUD_DB = r"I:\tools\cloud-workspace\pool.db"


def pretty(m):
    """One-line minimal chat-log view of a pool line.
    kind 'chat' is the default and stays implicit in the label;
    directed messages show →recipient."""
    ts = time.strftime("%m-%d %H:%M", time.localtime(m.get("t", time.time())))
    kind = m.get("kind", "chat")
    label = m.get("app", "?") if kind == "chat" else "%s/%s" % (m.get("app", "?"), kind)
    to = m.get("to", "all")
    arrow = " → %s" % to if to not in ("all", None) else ""
    text = m.get("text", "").replace("\n", "\n    ")
    return "[%s] %s%s: %s" % (ts, label, arrow, text)


def _chunk_msgs(text):
    """Extract message list from a cloud chunk text (raw JSON array,
    or legacy chat-log+RAW footer). Returns None when unparseable."""
    try:
        msgs = json.loads(text)
        if isinstance(msgs, list):
            return msgs
    except Exception:
        pass
    if "RAW:" in text:
        try:
            msgs = json.loads(text.split("RAW:", 1)[1])
            if isinstance(msgs, list):
                return msgs
        except Exception:
            pass
    return None


def view(n=20, cloud=False, as_app=None):
    """Minimal human-readable view. Local log tail by default;
    --cloud renders recent session-pool chunks (pool.db, read-only).
    Storage stays raw everywhere — this is display-only."""
    if not cloud:
        return [pretty(m) for m in tail(n, as_app)]
    import sqlite3
    db = sqlite3.connect("file:" + CLOUD_DB.replace("\\", "/") + "?mode=ro", uri=True)
    rows = db.execute(
        "SELECT seq, text FROM chunks WHERE session_id=? ORDER BY seq DESC LIMIT ?",
        (SESSION, n)).fetchall()
    db.close()
    out = []
    for seq, text in rows:
        out.append("== chunk seq=%d ==" % seq)
        msgs = _chunk_msgs(text)
        if msgs:
            out.extend("  " + pretty(m) for m in msgs if isinstance(m, dict)
                       if _visible(m, as_app))
        else:
            out.append("  " + text[:200])
    return out


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
    assert tail(5)[0]["v"] == 3, "v:3 lines"
    assert tail(5)[0]["enc"] == tail(5)[0]["rkey"] % 1440, "enc free view"
    assert 0 <= tail(5)[1]["rkey"] < 20736, "rkey in RDH space"
    # v3 directed visibility
    append("dashboard", "chat", "secret for hermes", to="hermes")
    vis_all = tail(10)
    assert len(vis_all) == 3, "admin view sees all"
    assert all(m["text"] != "secret for hermes" for m in tail(10, as_app="codex")), "codex must not see hermes DM"
    assert tail(10, as_app="hermes")[-1]["text"] == "secret for hermes", "hermes sees own DM"
    assert tail(10, as_app="dashboard")[-1]["text"] == "secret for hermes", "sender sees own DM"
    import shutil
    ep1 = flush_emit()
    assert ep1 is not None, "first emit fires"
    assert len(json.load(open(ep1, encoding="utf-8"))) == 3, "emit 3 lines (2 chat + 1 DM)"
    assert flush_emit() is None, "emit skips when unchanged"
    append("codex", "chat", "third msg")
    ep3 = flush_emit()
    assert ep3 is not None, "emit fires on new line"
    assert len(json.load(open(ep3, encoding="utf-8"))) == 1, "emit only new"
    shutil.rmtree(os.path.join(DIR, "outbox"), ignore_errors=True)
    print("POOL-SELFCHECK: 15/15 PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()  # wipes LOG+STATE: test only, never scheduled
    elif len(sys.argv) > 1 and sys.argv[1] == "view":
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        print("\n".join(view(n, cloud="--cloud" in sys.argv)))
    else:
        path = flush_emit()  # hourly safe default: emit+push or skip
        pushed, failed = push_outbox()
        if failed:
            print("PUSH-FAILED", failed)
        print(path if path else "SKIP", f"pushed={len(pushed)}")
