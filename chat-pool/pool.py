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


def rkey(data: bytes, phase: int = 0):
    """RDH capture — exact port of RDH_CAPTURE_144 (S=1). ADDRESS only.
    `phase` rotates the walk start: the SAME 144x144 plane, an independent
    axis over the same bytes. phase=0 is the locked capture, byte-identical
    to before. This is the stacking axis (§5.4): a plane is never scaled
    (RDH_CAPTURE_SCALE would move every existing address = break the one
    shared plane), an overflowing cell is split by later phases instead."""
    if not data:
        return 0
    F = 144
    ax = ay = 0
    steps = max(len(data), 48)
    for i in range(steps):
        n = data[(i + phase) % len(data)] & 0x0F
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


# --- bonds (§5.2 decision 2026-09-17) ---------------------------------
# A bond is ordinary data: kind:"bond", text "<src> <rel> <dst>" where
# src/dst are full 64-hex hashes of existing lines. It is hashed, keyed
# and chained by append() exactly like any other line — zero special
# mechanics. Unbind = tombstone line with rel "unbinds" and dst = the
# hash of the bond line being revoked.
BOND_RELS = ("uses", "produces", "validates", "decides", "follows", "contradicts")


def bond(src, rel, dst, app="system"):
    """Declare one edge between two lines, as a line. Returns the body.
    src/dst must be line hashes (64 hex); rel is a closed verb or x-prefixed."""
    if not re.fullmatch(r"[0-9a-f]{64}", src or "") or not re.fullmatch(r"[0-9a-f]{64}", dst or ""):
        raise ValueError("src/dst must be full 64-hex line hashes")
    if rel != "unbinds" and rel not in BOND_RELS and not rel.startswith("x-"):
        raise ValueError("rel must be one of %s, 'unbinds', or x-<free>" % ", ".join(BOND_RELS))
    return append(app, "bond", "%s %s %s" % (src, rel, dst))


def unbind(bond_hash, app="system"):
    """Tombstone a bond: write a new line pointing at the bond's hash.
    The original line stays — append-only, never edit."""
    if not re.fullmatch(r"[0-9a-f]{64}", bond_hash or ""):
        raise ValueError("bond_hash must be a full 64-hex line hash")
    return append(app, "bond", "%s unbinds %s" % (bond_hash, bond_hash))


def bonds_for(hash8_or_full, as_app=None):
    """Read edges touching a line (either end), latest state wins:
    a later 'unbinds' line hides its target bond. Returns dicts
    {hash, src, rel, dst, app, t} — walk blast radius by pointing
    at each dst next; that is the caller's next explicit read."""
    want = hash8_or_full.lower()
    all_lines = tail(1000, as_app=as_app)
    hidden = set()
    for m in all_lines:
        if m.get("kind") == "bond":
            p = m.get("text", "").split()
            if len(p) == 3 and p[1] == "unbinds" and p[0] == p[2]:
                hidden.add(p[0])
    out = []
    for m in all_lines:
        if m.get("kind") != "bond" or m["hash"] in hidden:
            continue
        p = m.get("text", "").split()
        if len(p) == 3 and p[1] != "unbinds" and (p[0].startswith(want) or p[2].startswith(want)):
            out.append({"hash": m["hash"], "src": p[0], "rel": p[1],
                        "dst": p[2], "app": m.get("app"), "t": m.get("t")})
    return out


def _coarse_map(rkey0, block=12):
    """L1 vision at one point: the containing 12x12 super-cell.
    Answers with a MAP ONLY — fine-cell addresses that hold lines,
    each with a count. No content ever crosses the read point.
    Want content? Point at a fine rkey from the map: new read."""
    base = (rkey0 // block) * block
    cells = {}
    for m in tail(1000):
        rk = m.get("rkey")
        if rk is not None and base <= rk < base + block * block:
            cells[rk] = cells.get(rk, 0) + 1
    return {"level": 1, "base": base, "block": block,
            "fine_cells": dict(sorted(cells.items()))}


VISION_BUDGET = 64  # lines one cell may hold before a read needs stacking


def _plane_map():
    """L2 vision: the whole 144x144 plane read as one MAP.
    Spatial depth stops here — 20,736 = 144x144 has exactly three useful
    views (20736 cells / 144 blocks / 1 plane); deeper levels add nothing.
    Cells over budget are only FLAGGED, never expanded: expanding one is
    the caller's next explicit read. Addresses + counts, no content."""
    cells = {}
    for m in tail(1000):
        rk = m.get("rkey")
        if rk is not None:
            cells[rk] = cells.get(rk, 0) + 1
    return {"level": 2, "plane": 144 * 144, "budget": VISION_BUDGET,
            "fine_cells": dict(sorted(cells.items())),
            "over_budget": sorted(k for k, c in cells.items()
                                  if c > VISION_BUDGET)}


def _axis(line, depth):
    """Sub-axis of a line inside its own cell (§5.4 stacking).
    Recomputed from the line's own content at read time — the same bytes
    append() captured — so stacking needs ZERO storage change and every
    line already written gets it retroactively. Never stored, never in
    the hash: deterministic replay gives the same bucket every time."""
    raw = (line.get("app", "") + line.get("kind", "")
           + line.get("text", "")).encode("utf-8")
    return rkey(raw, phase=depth) % 144


def sub_map(rkey0, depth=1):
    """Stacked read: ONE overflowing cell split by axis `depth`.
    Same rule as L1 — a MAP only (sub-addresses + counts), no content.
    Depth 1 gives 144 sub-cells; every further depth multiplies by 144,
    so a cell absorbs 144^d x budget lines without the plane growing."""
    cells = {}
    for m in tail(1000):
        if m.get("rkey") == rkey0:
            k = _axis(m, depth)
            cells[k] = cells.get(k, 0) + 1
    return {"level": "0.%d" % depth, "cell": rkey0, "depth": depth,
            "budget": VISION_BUDGET, "sub_cells": dict(sorted(cells.items()))}


def _needed_depth(lines):
    """Cheapest depth at which every bucket of a cell reads under budget.
    Answered by reading the lines — the data still knows nothing."""
    depth = 1
    while depth < 6:
        counts = {}
        for m in lines:
            k = _axis(m, depth)
            counts[k] = counts.get(k, 0) + 1
        if max(counts.values()) <= VISION_BUDGET:
            return depth
        depth += 1
    return depth


# --- pointer (§5.3 decision 2026-09-17) -------------------------------
# Iron law 5: no central pointer. A pointer is born at one read and dies
# with it, so the system keeps NO pointer state anywhere — not in a file,
# not in the server. What CAN be shared is not a live cursor but a
# *sighting*: an ordinary immutable line declaring that a read happened
# at a point. Awareness comes from data; the read stays per-caller.

def sighting(rkey0, level=0, app="system"):
    """Declare a read at one point, as data. kind:"pointer", text
    "point=<rkey> level=<n>". Goes through append() like anything else,
    so it is chained, hashed and addressed for free — and a sighting that
    'ends' is simply the next sighting, never an edit or a delete."""
    return append(app, "pointer", "point=%d level=%d" % (rkey0, level))


def pointers_at(rkey0=None, level=None, as_app=None):
    """Who declared a read where — derived from lines, nothing stored.
    Latest sighting per app wins. Kill the server mid-read and this still
    answers the same, because there is no server state to lose."""
    out = {}
    for m in tail(1000, as_app=as_app):
        if m.get("kind") != "pointer":
            continue
        p = (m.get("text") or "").split()
        if len(p) != 2 or not p[0].startswith("point="):
            continue
        try:
            pt = int(p[0][6:])
            lv = int(p[1].split("=", 1)[1])
        except (ValueError, IndexError):
            continue
        if rkey0 is not None and pt != rkey0:
            continue
        if level is not None and lv != level:
            continue
        a = m.get("app")
        if a not in out or m.get("t", 0) >= out[a]["t"]:
            out[a] = {"app": a, "point": pt, "level": lv,
                      "t": m.get("t"), "hash": m.get("hash")}
    return sorted(out.values(), key=lambda x: -x["t"])


def open_pointer(rkey0, level=0, app="system", record=False, depth=None,
                 as_app=None):
    """Open ONE pointer at ONE point and close it (§5.3).
    L0 = content of that cell only · L1 = containing 12x12 block as a map ·
    L2 = whole plane as a map. If the pointed cell is over budget the
    answer carries a stacked sub-map instead of drowning the reader.
    Nothing about the read is kept unless the caller asks for a sighting,
    and no other caller shares it — that is iron law 5 exactly.
    Maps are address-only, so they carry no content and need no visibility
    filter; only L0 content is filtered by as_app."""
    if level == 2:
        vis = _plane_map()
    elif level == 1:
        vis = _coarse_map(rkey0)
    else:
        lines = [m for m in tail(1000, as_app=as_app)
                 if m.get("rkey") == rkey0]
        vis = {"level": 0, "point": rkey0, "count": len(lines),
               "lines": lines}
        if len(lines) > VISION_BUDGET:
            d = depth or _needed_depth(lines)
            vis["stacked"] = sub_map(rkey0, d)
            vis["over_budget"] = True
    if record:
        vis["sighting"] = sighting(rkey0, level=level, app=app)["hash"]
    return vis


def demo():
    """Self-check. Runs against a TEMP copy of the module directory —
    a crashed demo used to wipe the live log mid-assert (it happened);
    the real LOG/registry/outbox must be untouchable even on failure."""
    import shutil, tempfile
    tmp = tempfile.mkdtemp(prefix="pool-demo-")
    for f in ("pool.py",):
        src = os.path.join(DIR, f)
        if os.path.exists(src):
            shutil.copy(src, os.path.join(tmp, f))
    sys.path.insert(0, tmp)
    try:
        demo_mod = __import__("pool")  # fresh module, DIR=tmp
        demo_mod.DIR = tmp
        demo_mod.LOG = os.path.join(tmp, "log.jsonl")
        demo_mod.STATE = os.path.join(tmp, ".flush_state.json")
        demo_mod.APPS = os.path.join(tmp, "apps.json")
        demo_mod.BRIDGE_STATE = os.path.join(tmp, ".bridge_state.json")
        demo_mod._demo_body()
    finally:
        sys.path.remove(tmp)
        shutil.rmtree(tmp, ignore_errors=True)


def _demo_body():
    for p in (LOG, STATE, LOG + ".lock"):
        if os.path.exists(p):
            os.remove(p)
    if os.path.exists(APPS):
        # keep the real registry — back it up and restore after the demo
        import shutil
        shutil.copy(APPS, APPS + ".demo-bak")
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
    # bonds: ordinary lines, closed verbs, tombstone unbind, L1 coarse map
    h1, h2 = tail(2)[0]["hash"], tail(1)[0]["hash"]
    b = bond(h1, "uses", h2, app="opencode")
    assert b["kind"] == "bond" and b["prev"] == h2, "bond is a chained line"
    assert b["rkey"] == rkey(("opencode" + "bond" + b["text"]).encode("utf-8")), "bond has own rkey from content"
    fb = bonds_for(h1[:8])
    assert len(fb) == 1 and fb[0]["rel"] == "uses" and fb[0]["dst"] == h2, "bond readable from both ends"
    assert bonds_for(h2[:8])[0]["src"] == h1, "edge visible from dst side too"
    try:
        bond(h1, "bogus", h2); assert False, "open verb must be rejected"
    except ValueError:
        pass
    ub = unbind(fb[0]["hash"], app="opencode")
    assert ub["kind"] == "bond", "tombstone is a bond line"
    assert bonds_for(h1[:8]) == [], "unbinded bond hidden, original line intact"
    b2 = bond(h1, "produces", h2, app="opencode")
    assert len(bonds_for(h1[:8])) == 1 and bonds_for(h1[:8])[0]["rel"] == "produces", "new bond after tombstone"
    cm = _coarse_map(b2["rkey"])
    assert cm["level"] == 1 and cm["base"] <= b2["rkey"] < cm["base"] + 144, "L1 map contains the pointed fine cell"
    expect = sum(1 for m in tail(1000)
                 if m.get("rkey") is not None and cm["base"] <= m["rkey"] < cm["base"] + 144)
    assert expect >= 1 and sum(cm["fine_cells"].values()) == expect, "map counts exactly the lines living in this super-cell"
    assert all(isinstance(k, int) for k in cm["fine_cells"]), "map is addresses only — no content"
    # living memory bridge (dry run — no db write in demo)
    append("opencode", "chat", "DECISION: pool stays file-first, server optional")
    lifted = memory_bridge(dry_run=True)
    assert any("DECISION" in c for c, _, _ in lifted), "bridge lifts decisions"
    # multi-pointer vision read: at the actual read point only
    r0 = tail(50)[0]["rkey"]
    nv = neighbors(r0)
    assert nv == [(r0, [m for m in tail(1000) if m.get("rkey") == r0])], "vision = exactly the pointed cell"
    # §5.3 pointer: per-read and ephemeral; awareness only as data
    p0 = tail(50)[0]["rkey"]
    assert pointers_at(p0) == [], "no pointer state exists before any read"
    v0 = open_pointer(p0, level=0, app="codex")
    assert v0["count"] == len([m for m in tail(1000) if m.get("rkey") == p0]), "L0 = content of the pointed cell only"
    assert pointers_at(p0) == [], "a read leaves no trace unless the caller declares one"
    sg = sighting(p0, level=0, app="codex")
    assert sg["kind"] == "pointer", "sighting is data, not server state"
    assert sg["rkey"] == rkey(("codex" + "pointer" + sg["text"]).encode("utf-8")), "sighting chained/address like any line"
    seen = pointers_at(p0)
    assert len(seen) == 1 and seen[0]["app"] == "codex" and seen[0]["level"] == 0, "awareness derived from lines alone"
    assert pointers_at(0) == [], "points nobody read stay silent"
    assert pointers_at(p0, level=1) == [], "level is part of the point"
    sighting(p0, level=0, app="hermes")
    assert {s["app"] for s in pointers_at(p0)} == {"codex", "hermes"}, "each pointer stays its own — no central pointer"
    assert len(pointers_at(p0)) == 2, "two readers, two independent pointers at the same point"
    # §5.4 RDH 144: one fixed leaf plane; overflow STACKS by phase axis
    pm = _plane_map()
    assert pm["level"] == 2 and pm["plane"] == 20736, "L2 = whole plane, the spatial ceiling"
    assert sum(pm["fine_cells"].values()) == len([m for m in tail(1000) if m.get("rkey") is not None]), "plane map counts every addressed line"
    assert pm["over_budget"] == [], "nothing over budget here — stacking stays lazy"
    assert rkey(b"same bytes", phase=1) != rkey(b"same bytes", phase=0), "phase is an independent axis in the same plane"
    assert rkey(b"same bytes") == rkey(b"same bytes", phase=0), "phase 0 = the locked capture, unchanged"
    assert _axis({"app": "a", "kind": "k", "text": "t"}, 1) == rkey(b"akt", phase=1) % 144, "sub-axis recomputed from the line itself"
    pcell = tail(50)[0]["rkey"]
    sub = sub_map(pcell, depth=1)
    assert sub["level"] == "0.1" and sum(sub["sub_cells"].values()) == len([m for m in tail(1000) if m.get("rkey") == pcell]), "stacked read splits exactly that one cell, addresses only"
    assert all(isinstance(k, int) for k in sub["sub_cells"]), "stacked read is a map, never content"
    load = [{"app": "load", "kind": "chat", "text": "line %d" % i} for i in range(200)]
    d = _needed_depth(load)
    counts = {}
    for m in load:
        k = _axis(m, d)
        counts[k] = counts.get(k, 0) + 1
    assert max(counts.values()) <= VISION_BUDGET, "stacking splits until every bucket reads under budget"
    big = open_pointer(pcell, level=0, app="load")
    assert "stacked" not in big, "a cell under budget is never split — breathing only when needed"
    import shutil
    ep1 = flush_emit()
    assert ep1 is not None, "first emit fires"
    assert len(json.load(open(ep1, encoding="utf-8"))) == 10, "emit 10 lines (4 chat incl DM + 1 code + 3 bonds + 2 pointers)"
    assert flush_emit() is None, "emit skips when unchanged"
    append("codex", "chat", "third msg")
    ep3 = flush_emit()
    assert ep3 is not None, "emit fires on new line"
    assert len(json.load(open(ep3, encoding="utf-8"))) == 1, "emit only new"
    shutil.rmtree(os.path.join(DIR, "outbox"), ignore_errors=True)
    for p in (BRIDGE_STATE, APPS):
        if os.path.exists(p):
            if p == APPS and os.path.exists(APPS + ".demo-bak"):
                os.replace(APPS + ".demo-bak", APPS)  # restore real registry
            else:
                os.remove(p)
    print("POOL-SELFCHECK: 45/45 PASS")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()  # self-check in a temp dir: the real log is untouchable
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
        try:  # heartbeat: flusher liveness, visible on the dashboard registry
            register_app("flusher")
        except Exception:
            pass
        pushed, failed = push_outbox()
        if failed:
            print("PUSH-FAILED", failed)
        try:  # cloud backup: best-effort mirror, never blocks the flush
            import pool_cloud
            pool_cloud.push()
        except Exception as e:
            print("cloud-push ERROR", e)
        print(path if path else "SKIP", f"pushed={len(pushed)}")
