"""B-slice: cross-app group-chat pool. Append-only JSONL outside app dirs.
Line = {t, app, kind, text, prev, hash} hash-chained. Hourly flush: sha256
changed -> emit new lines payload, else skip. Multi-app: O_APPEND single-line
writes (atomic for small lines); single flush owner (see LOCKED spec).
ponytail: stdlib only, no server, no schema beyond chat+snippet."""
import hashlib
import json
import os
import re
import sys
import time

DIR = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(DIR, "log.jsonl")
STATE = os.path.join(DIR, ".flush_state.json")
APPS = os.path.join(DIR, "apps.json")  # dynamic identity registry
CTX_DB = r"I:\cortexkit\magic-context\context.db"  # living memory
BRIDGE_STATE = os.path.join(DIR, ".bridge_state.json")

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


TAG_RE = re.compile(r"@([A-Za-z0-9_\-]+)")


def _extract_tags(text):
    """@mentions in text become notification targets (feed model:
    everyone may see the post; tagged apps are the ones affected)."""
    return list(dict.fromkeys(TAG_RE.findall(text)))


# ── dynamic identity registry (apps.json) ──────────────────────────
def _load_apps():
    if not os.path.exists(APPS):
        return {}
    try:
        return json.load(open(APPS, encoding="utf-8"))
    except Exception:
        return {}


def _save_apps(reg):
    json.dump(reg, open(APPS, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def register_app(name):
    """First-come registration. Idempotent: re-register touches last_seen.
    Returns (registered: bool, created: bool)."""
    if not name or not re.fullmatch(r"[A-Za-z0-9_\-]{1,32}", name):
        raise ValueError("invalid app name (1-32 chars: a-z 0-9 _ -)")
    reg = _load_apps()
    created = name not in reg
    now = time.time()
    if created:
        reg[name] = {"registered_at": now, "last_seen": now, "last_read": 0}
    else:
        reg[name]["last_seen"] = now
    _save_apps(reg)
    return True, created


def is_registered(name):
    return name in _load_apps()


def touch_app(name):
    reg = _load_apps()
    if name in reg:
        reg[name]["last_seen"] = time.time()
        _save_apps(reg)


def list_apps():
    return _load_apps()


def inbox(app, mark_read=True):
    """Unread messages that affect `app`: @app mentioned, or to==app.
    Feed model — broadcasts stay visible to everyone on the feed;
    the inbox is the actionable subset. Marks last_read cursor."""
    reg = _load_apps()
    since = reg.get(app, {}).get("last_read", 0)
    reg.setdefault(app, {})["last_read"] = time.time() if mark_read else reg.get(app, {}).get("last_read", 0)
    if mark_read:
        _save_apps(reg)
    return [m for m in tail(500)
            if m.get("t", 0) > since
            and (app in m.get("tags", []) or m.get("to") == app)]


def append(app, kind, text, to="all"):
    """Append one message. Thread-safe via threading.Lock;
    process-safe via msvcrt.lock on a .lock file (Windows) or fcntl (Unix).
    Feed model: to="all" (default) posts to the shared feed; to=X also
    restricts visibility to X (DM). @name in text tags apps — they get
    the message in their inbox (the actionable effect). tags derive
    from text, which is inside the hash — immutable."""
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
                    "text": text, "to": to,
                    "tags": _extract_tags(text), "prev": prev,
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
                    "text": text, "to": to,
                    "tags": _extract_tags(text), "prev": prev,
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


# ── living memory bridge (pool → Magic Context) ───────────────────
BRIDGE_RE = re.compile(
    r"^(SESSION|RECOVERY|HANDOFF|DECISION|VERDICT)\b", re.IGNORECASE)
BRIDGE_CATEGORIES = {
    "HANDOFF": "HANDOFF",
    "SESSION": "HANDOFF",
    "RECOVERY": "HANDOFF",
    "DECISION": "DECISION",
    "VERDICT": "DECISION",
}


def _bridge_load():
    if not os.path.exists(BRIDGE_STATE):
        return 0
    try:
        return json.load(open(BRIDGE_STATE, encoding="utf-8")).get("last_t", 0)
    except Exception:
        return 0


def _bridge_save(last_t):
    json.dump({"last_t": last_t}, open(BRIDGE_STATE, "w", encoding="utf-8"))


def memory_bridge(dry_run=False):
    """Living memory: scan pool lines newer than the bridge cursor,
    lift handoff/decision/verdict messages into Magic Context's
    memories (context.db) so every OpenCode session recalls them.
    Mirrors ctx_memory's TS-authority write path (direct insert with
    FTS trigger). Returns list of (kind, text, memory_id)."""
    import sqlite3
    since = _bridge_load()
    lifted = []
    last_t = since
    for m in tail(500):
        t = m.get("t", 0)
        if t <= since:
            continue
        last_t = max(last_t, t)
        text = (m.get("text") or "").strip()
        mkey = BRIDGE_RE.match(text)
        if not mkey:
            continue
        kind = mkey.group(1).upper()
        category = BRIDGE_CATEGORIES.get(kind)
        if not category:
            continue
        content = f"[chat-pool/{m.get('app', '?')}] {text}"
        nhash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:32]
        if dry_run:
            lifted.append((category, content, None))
            continue
        now = int(time.time() * 1000)
        db = sqlite3.connect(CTX_DB)
        try:
            cur = db.execute(
                "INSERT INTO memories(project_path, category, content, "
                "normalized_hash, importance, source_type, first_seen_at, "
                "created_at, updated_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("git:174753f2375c19a1c0ce1388ef592bfa02d2b056", category,
                 content, nhash, 60, "chat-pool", now, now, now, now))
            db.commit()
            lifted.append((category, content, cur.lastrowid))
        except sqlite3.IntegrityError:
            pass  # duplicate memory — bridge is idempotent
        finally:
            db.close()
    if not dry_run and last_t > since:
        _bridge_save(last_t)
    return lifted


def neighbors(rkey0):
    """Multi-pointer read at the actual read point — no in-between.
    Open a vision at one point (rkey0), see only that cell's lines.
    Data never moves and knows nothing; every read is a fresh pointer.
    Broader rings are the caller's next explicit read, not ours."""
    lines = [m for m in tail(1000) if m.get("rkey") == rkey0]
    return [(rkey0, lines)]


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
    # dynamic registry + tags + inbox
    register_app("hermes"); register_app("codex")
    assert is_registered("hermes") and not is_registered("ghost"), "registry works"
    append("dashboard", "chat", "@hermes please run the flush")
    ib = inbox("hermes", mark_read=False)
    assert any("run the flush" in m["text"] for m in ib), "mention lands in hermes inbox"
    assert not any("run the flush" in m["text"] for m in inbox("codex", mark_read=False)), "codex inbox clean"
    ib2 = inbox("hermes")  # mark read
    assert inbox("hermes") == [], "inbox drains after read"
    # living memory bridge (dry run — no db write in demo)
    append("opencode", "chat", "DECISION: pool stays file-first, server optional")
    lifted = memory_bridge(dry_run=True)
    assert any("DECISION" in c for c, _, _ in lifted), "bridge lifts decisions"
    # multi-pointer vision read: at the actual read point only
    r0 = tail(50)[0]["rkey"]
    nv = neighbors(r0)
    assert nv == [(r0, [m for m in tail(1000) if m.get("rkey") == r0])], "vision = exactly the pointed cell"
    import shutil
    ep1 = flush_emit()
    assert ep1 is not None, "first emit fires"
    assert len(json.load(open(ep1, encoding="utf-8"))) == 5, "emit 5 lines (4 chat + 1 DM)"
    assert flush_emit() is None, "emit skips when unchanged"
    append("codex", "chat", "third msg")
    ep3 = flush_emit()
    assert ep3 is not None, "emit fires on new line"
    assert len(json.load(open(ep3, encoding="utf-8"))) == 1, "emit only new"
    shutil.rmtree(os.path.join(DIR, "outbox"), ignore_errors=True)
    for p in (APPS, BRIDGE_STATE):
        if os.path.exists(p):
            os.remove(p)
    print("POOL-SELFCHECK: 21/21 PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()  # wipes LOG+STATE: test only, never scheduled
    elif len(sys.argv) > 1 and sys.argv[1] == "bridge":
        dry = "--dry" in sys.argv
        lifted = memory_bridge(dry_run=dry)
        for cat, content, mid in lifted:
            print(f"[{cat}]{' #' + str(mid) if mid else ''} {content[:100]}")
        print(f"bridge: {len(lifted)} lifted" + (" (dry)" if dry else ""))
    elif len(sys.argv) > 1 and sys.argv[1] == "view":
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 20
        print("\n".join(view(n, cloud="--cloud" in sys.argv)))
    else:
        path = flush_emit()  # hourly safe default: emit+push or skip
        memory_bridge()  # living memory: lift handoff/decision into magic context
        pushed, failed = push_outbox()
        if failed:
            print("PUSH-FAILED", failed)
        print(path if path else "SKIP", f"pushed={len(pushed)}")
