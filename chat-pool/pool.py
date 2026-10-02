"""B-slice: cross-app group-chat pool. Append-only JSONL outside app dirs.
Line = {t, app, kind, text, prev, hash} hash-chained. Hourly flush: sha256
changed -> emit new lines payload, else skip. Multi-app: O_APPEND single-line
writes (atomic for small lines); single flush owner (see LOCKED spec).
ponytail: stdlib only, no server, no schema beyond chat+snippet."""
import atexit
import hashlib
import json
import os
import re
import sys
import textwrap
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


def app_phase(app: str) -> int:
    """Deterministic phase for an app: same name → same phase always.
    Used by app_rkey() to rotate the base rkey so two apps writing identical
    text land on different cells.  16 phases gives good spread."""
    return int.from_bytes(hashlib.sha1(app.encode()).digest()[:2], "big") % 16


def app_rkey(data: bytes, app: str) -> int:
    """RDH address rotated per-app.  Same content from different apps lands
    on different cells.  Uses rkey(data) as base, then rotates (col, row)
    by app_phase(app).  17 and 7 are coprime with 144 so rotations are
    well-spread."""
    base = rkey(data)
    ph = app_phase(app)
    if ph == 0:
        return base
    col, row = base % 144, base // 144
    return ((row + ph * 7) % 144) * 144 + ((col + ph * 17) % 144)


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
    return [m for m in tail(None)   # unread stays unread however old it is
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
            key0 = _log_key()          # cache state before OUR write
            prev = _prev()
            raw_bytes = (app + kind + text).encode("utf-8")
            body = {"v": 3, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "to": to,
                    "tags": _extract_tags(text), "prev": prev,
                    "rkey": app_rkey(raw_bytes, app),
                    "enc": app_rkey(raw_bytes, app) % 1440}
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
            _derived_absorb(body, raw, key0)
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
            key0 = _log_key()          # cache state before OUR write
            prev = _prev()
            raw_bytes = (app + kind + text).encode("utf-8")
            body = {"v": 3, "t": time.time(), "app": app, "kind": kind,
                    "text": text, "to": to,
                    "tags": _extract_tags(text), "prev": prev,
                    "rkey": app_rkey(raw_bytes, app),
                    "enc": app_rkey(raw_bytes, app) % 1440}
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
            _derived_absorb(body, raw, key0)
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

# ── index diet (2026-09-20): what the semantic index actually needs ──
# Raw lines carry 11 envelope keys per message (prev/hash 64-hex, rkey, enc,
# tags, v...) and machine kinds (bond/pointer) that drown the vector in
# noise — a 16MB hourly file embedded as ONE chunk. The searchable index
# keeps 5 keys; the raw log + outbox .pushed files stay the full record.
INDEX_KEEP = ("app", "kind", "t", "to", "text")
INDEX_SKIP_KINDS = ("bond", "pointer")  # graph data: query via /explore, not search
INDEX_CHUNK_BYTES = 16384  # max ~16KB per chunk, split on line boundaries


def index_pieces(msgs):
    """Normalize raw pool lines into embed-ready pieces (pure, no I/O).
    Drops envelope keys + machine kinds, splits on line boundaries."""
    comp = []
    if isinstance(msgs, dict):
        msgs = [msgs]
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        if m.get("kind") in INDEX_SKIP_KINDS:
            continue
        text = m.get("text") or ""
        if not str(text).strip():
            continue
        comp.append({"app": m.get("app", "?"), "kind": m.get("kind", "chat"),
                     "t": m.get("t", 0), "to": m.get("to", "all"),
                     "text": text})
    pieces, cur, size = [], [], 0
    for m in comp:
        s = len(json.dumps(m, ensure_ascii=False)) + 1
        if cur and size + s > INDEX_CHUNK_BYTES:
            pieces.append(cur)
            cur, size = [], 0
        cur.append(m)
        size += s
    if cur:
        pieces.append(cur)
    return pieces


def _embed_piece(piece, session, topic):
    """Embed one normalized piece via the pool CLI. Returns stderr or ''."""
    import subprocess
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json",
        dir=os.path.join(DIR, "outbox"), delete=False)
    try:
        json.dump(piece, tmp, ensure_ascii=False)
        tmp.close()
        py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
        r = subprocess.run(
            [py, CLI, "embed", "--session", session,
             "--topic", topic, "--file", tmp.name],
            capture_output=True, text=True, timeout=300)
        return "" if r.returncode == 0 else r.stderr.strip()[-300:]
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass


def push_outbox(session=SESSION):
    """Push pending outbox payloads via existing pool CLI, then archive.
    Each file is normalized first (index_pieces: envelope stripped, machine
    kinds dropped, split to ~16KB) so the vector sees chat, not schema.
    Raw JSON stays the record (log.jsonl + outbox/*.pushed); rendering to a
    readable chat log is view()'s job, not storage's.
    Returns (pushed, failed). Cloud/searchable afterwards via session-pool."""
    pushed, failed = [], []
    for path in outbox_list():
        try:
            with open(path, encoding="utf-8") as f:
                msgs = json.load(f)
        except Exception as e:
            failed.append((path, f"unreadable: {e}"[-300:]))
            continue
        pieces = index_pieces(msgs)
        if not pieces:  # all machine noise — nothing to index, still archive
            done = path + ".pushed"
            os.replace(path, done)
            pushed.append(done)
            continue
        err = ""
        for piece in pieces:
            err = _embed_piece(piece, session, "hourly")
            if err:
                break
        if not err:
            done = path + ".pushed"
            os.replace(path, done)
            pushed.append(done)
        else:
            failed.append((path, err))
    return pushed, failed


def _visible(m, as_app=None):
    """v3 visibility: to="all" = everyone; to=X = only X itself
    (plus the sender). as_app=None (human/admin view) sees all —
    the dashboard uses that; readers pass their own identity."""
    to = m.get("to", "all")
    if to == "all" or as_app is None:
        return True
    return as_app == to or as_app == m.get("app")


def _iter_lines(reverse=False):
    """Stream the log line by line — nothing materializes the whole file.
    `reverse` reads backwards in chunks, so tail(n) stops paying for the whole
    history (measured: tail(20) used to cost the same as tail(1000), both
    parsing every line, because the old tail() read the file and sliced)."""
    if not os.path.exists(LOG):
        return
    if not reverse:
        with open(LOG, encoding="utf-8") as f:
            for raw in f:
                if raw.strip():
                    yield json.loads(raw)
        return
    CHUNK = 65536
    with open(LOG, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        carry = b""
        while pos > 0:
            step = min(CHUNK, pos)
            pos -= step
            f.seek(pos)
            parts = (f.read(step) + carry).split(b"\n")
            carry = parts[0]        # may be a partial line — glue it to the next chunk
            for raw in reversed(parts[1:]):
                if raw.strip():
                    yield json.loads(raw.decode("utf-8"))
        if carry.strip():
            yield json.loads(carry.decode("utf-8"))


def tail(n=10, as_app=None):
    """Newest n lines this reader may see.
    **n=None means the whole history** — use that whenever completeness
    matters (maps, bonds, the memory bridge). A capped read is a silent
    truncation: at 1,000 bonds the cap returned 200 edges whose ends could
    not even be resolved. The log is append-only, so "all" has one meaning."""
    if not os.path.exists(LOG):
        return []
    if n is None:
        return [m for m in _iter_lines() if _visible(m, as_app)]
    out = []
    for m in _iter_lines(reverse=True):
        if _visible(m, as_app):
            out.append(m)
            if len(out) >= n:
                break
    out.reverse()
    return out


_DERIVED = {"key": None}


def _log_key():
    try:
        st = os.stat(LOG)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


# ── story/method BLOCKS ────────────────────────────────────────────────────
# The pool already stores what was SAID (chat, notes, handoffs) and how lines
# RELATE (bonds) — but not what was LEARNED: the story of a piece of work and
# the method that produced it. That is what another platform has to read to
# understand this one without replaying the whole chat, and it is the memory
# the rest of the system is built on. A block is therefore an ordinary chained
# line (kind="block") and inherits every guarantee the pool already has:
# append-only, sha256-chained, addressed on the 144x144 plane, visible in the
# feed, and safe from the curator (only kind="chat" is ever hidden).
#
# The canonical text is LINE-oriented and KEY-ordered on purpose. "ทุก platform
# เข้าใจตรงกัน" is a format property, not a hope: there is exactly one way to
# write a block and exactly one way to read it back, so two readers that never
# spoke to each other still agree. And because the format lives inside `text`,
# it lives inside `hash` — a block cannot claim content its chain does not
# cover. The hash stays sha256(prev+app+kind+to+text) like every other line; no
# new field is introduced, so nothing rides outside the chain.
BLOCK_MARK = "⟦block⟧"
BLOCK_KEYS = ("title", "story", "method", "evidence", "refs", "status")
BLOCK_STATUS = ("open", "closed", "parked")
BLOCK_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,47}$")
BLOCK_HEAD = re.compile(re.escape(BLOCK_MARK) + r" slug=([^ ]+) rev=(\d+)$")
BLOCK_REF = re.compile(r"^[0-9a-f]{6,64}$")


def _flat(s):
    """Values are ONE physical line. A newline would break the format, and a
    block that silently swallowed one would be storing something other than
    what it claims — so collapse whitespace instead of escaping it: a reader
    should never have to be a parser of escapes."""
    return " ".join(str("" if s is None else s).replace("\r", " ").split())


def render_block(slug, rev, fields):
    """The single canonical encoding — writer and every reader share it.
    status is normalized to "open" rather than left blank, so that
    render(parse(render(x))) == render(x) for EVERY input: the format has one
    normal form, which is what lets two readers agree without talking."""
    out = ["%s slug=%s rev=%d" % (BLOCK_MARK, slug, rev)]
    out += ["%s: %s" % (k, _flat(fields.get(k, "") or ("open" if k == "status" else "")))
            for k in BLOCK_KEYS]
    return "\n".join(out)


def parse_block(text):
    """Canonical text -> dict, or None when it is not a well-formed block.
    Strict on purpose: a wrong header, a key outside BLOCK_KEYS, a repeated key,
    or a missing title all mean "not a block" rather than a last-wins guess —
    two readers disagreeing is exactly what blocks exist to prevent."""
    if not isinstance(text, str) or not text.startswith(BLOCK_MARK):
        return None
    lines = text.split("\n")
    m = BLOCK_HEAD.match(lines[0].rstrip())
    if not m:
        return None
    slug, rev = m.group(1), int(m.group(2))
    if not BLOCK_SLUG.match(slug) or rev < 1:
        return None
    seen, fields = set(), {}
    for ln in lines[1:]:
        if not ln.strip():
            continue
        if ":" not in ln:
            return None
        k, v = ln.split(":", 1)
        k = k.strip()
        if k not in BLOCK_KEYS or k in seen:
            return None
        seen.add(k)
        fields[k] = v.strip()
    if not fields.get("title"):
        return None                 # without a title it is not readable memory
    d = {"slug": slug, "rev": rev}
    d.update({k: fields.get(k, "") for k in BLOCK_KEYS})
    d["status"] = d["status"] or "open"
    return d if d["status"] in BLOCK_STATUS else None


def _refs_of(refs):
    """refs = the checkable part of a block: hash prefixes of the chained lines
    the story was built from. Reject non-hashes loudly — a ref that cannot
    resolve is worse than no ref, because it looks like evidence."""
    items = list(refs) if isinstance(refs, (list, tuple, set)) else \
        re.split(r"[,\s]+", str(refs or ""))
    out = []
    for x in items:
        x = str(x).strip().lower()
        if not x:
            continue
        if not BLOCK_REF.match(x):
            raise ValueError("ref %r is not a hash prefix (6..64 hex)" % x)
        if x not in out:
            out.append(x)           # a ref list is a set that keeps its order
    return out


def _block_row(m, d):
    """Chain facts + block fields in one row: a block is never detached from
    the line that carries it (hash, time, author, address, tick)."""
    row = {"slug": d["slug"], "rev": d["rev"], "kind": m.get("kind"),
           "hash": m.get("hash"), "prev": m.get("prev"),
           "t": m.get("t"), "app": m.get("app"),
           "to": m.get("to", "all"), "rkey": m.get("rkey"),
           "enc": m.get("enc"),
           # the stored payload verbatim: a reader must be able to hand back
           # exactly what was written, not a re-rendering that merely matches
           "text": m.get("text", "")}
    row.update({k: d[k] for k in BLOCK_KEYS})
    return row


def _block_visit(idx, m):
    """Fold one kind="block" line into the slug index. Latest revision wins for
    the FIELDS (the log is append-ordered, so "last line wins" is the same rule
    the plane already uses for cells), while `revs` counts how many times the
    slug was written and `apps` remembers every platform that contributed —
    "shared memory" is only visible if the reading side says who took part."""
    d = parse_block(m.get("text", ""))
    if not d:
        return
    row = _block_row(m, d)
    cur = idx.get(row["slug"])
    if cur is None:
        row["revs"], row["apps"], row["first_t"] = 1, [m.get("app")], m.get("t")
        idx[row["slug"]] = row
        return
    cur.update(row)
    cur["revs"] += 1
    if m.get("app") not in cur["apps"]:
        cur["apps"].append(m.get("app"))


def block(app, slug, title, story="", method="", evidence="", refs=(), status="open"):
    """Append the next revision of a block — the story AND the method that
    produced it.

    Append-only like every other write: a revision is a NEW line with
    rev = latest + 1, and the older revisions stay in the chain (block_revs() is
    the history, and "เรื่องราว" includes how it changed). Nothing is ever
    edited, so the chain keeps meaning what it means everywhere else in this
    log. Raises ValueError on a malformed slug/status/ref/title rather than
    writing a line that half the readers would have to reject."""
    slug = str(slug or "").strip().lower()
    if not BLOCK_SLUG.match(slug):
        raise ValueError("slug must be [a-z0-9][a-z0-9._-]{0,47} — it is the address, keep it stable")
    if status not in BLOCK_STATUS:
        raise ValueError("status must be one of %s" % (BLOCK_STATUS,))
    if not _flat(title):
        raise ValueError("title required — it is the one line the next platform reads first")
    prev = block_get(slug)
    anchor = _refs_of(refs)
    if not anchor and prev:
        # evidence anchors belong to the BLOCK, not to one revision: a revision
        # that does not restate them inherits them, because silently dropping
        # the refs would turn a verifiable memory into an unfalsifiable claim
        anchor = _refs_of(prev.get("refs", ""))
    fields = {"title": title, "story": story, "method": method,
              "evidence": evidence, "refs": ",".join(anchor),
              "status": status}
    rev = (prev["rev"] + 1) if prev else 1
    body = append(app, "block", render_block(slug, rev, fields))
    return _block_row(body, parse_block(body["text"]))


def block_revs(slug):
    """Every revision of one block, oldest first — the story of the block
    itself. Streamed, because revisions are read rarely and a cached copy of a
    rarely-read thing is just another thing that can be stale.
    (No app filter: a block is shared memory by definition, and `to` is the
    chat DM mechanism, not a classification for knowledge.)"""
    out = []
    for m in _iter_lines():
        if m.get("kind") != "block":
            continue
        d = parse_block(m.get("text", ""))
        if d and d["slug"] == slug:
            out.append(_block_row(m, d))
    return out


def block_get(slug):
    """The live revision of one block, or None. Reads the derived index (one
    fold over the log, cached by size+mtime) rather than rescanning."""
    row = derived()["blocks"].get(slug)
    return dict(row) if row is not None else None


def blocks(status=None, query=None):
    """One row per block: the live revision's fields plus revs / apps / first_t.
    Newest work first, like the feed. A copy — callers must not mutate the
    index."""
    rows = [dict(r) for r in derived()["blocks"].values()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if query:
        q = _flat(query).lower()
        rows = [r for r in rows
                if q in _flat(" ".join([r["slug"], r["title"], r["story"],
                                        r["method"], r["evidence"]])).lower()]
    rows.sort(key=lambda r: -(r.get("t") or 0))
    return rows


def verify_block(row):
    """Do this block's refs still resolve to chained lines?
    A block is a claim about work; refs are the part of it that is checkable
    offline, against the same hash index the plane uses."""
    idx = derived()["hash_rkey"]
    refs = _refs_of(row.get("refs", ""))
    missing = [r for r in refs if not any(h.startswith(r) for h in idx)]
    return {"slug": row.get("slug"), "refs": refs, "ok": not missing,
            "missing": missing, "lines": len(idx)}


def block_timeline():
    """Timeline data: all blocks sorted by time with similarity links.

    Returns {nodes: [...], links: [...]} where:
      nodes = [{slug, app, title, status, t, rev, evidence}] sorted by time
      links = [{from, to, sim, via}] — similarity edges (dedup pairs)

    A 'link' means 'these two blocks are about the same work'.
    The dashboard draws these as arrows between nodes on the vertical timeline.
    """
    rows = blocks()  # already sorted newest-first
    nodes = []
    for r in rows:
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("t") or 0))
        nodes.append({
            "slug": r["slug"],
            "app": r.get("app", "?"),
            "title": r.get("title", ""),
            "status": r.get("status", "open"),
            "t": r.get("t"),
            "ts": ts,
            "rev": r.get("rev", 1),
            "evidence": r.get("evidence", ""),
        })
    # compute similarity links: each pair of blocks from different apps
    # (or same app, different slug) checked for overlap
    links = []
    for i, a in enumerate(nodes):
        for b in nodes[i + 1:]:
            # same slug = same block (rev history), skip
            if a["slug"] == b["slug"]:
                continue
            # title-only check first (fast path)
            t_sim = _similarity(a["title"], b["title"])
            if t_sim >= 0.4:
                links.append({"from": a["slug"], "to": b["slug"],
                              "sim": round(t_sim, 3), "via": "title"})
                continue
            # combined check
            combined_a = a["title"] + " " + a["slug"].split("-")[0]
            combined_b = b["title"] + " " + b["slug"].split("-")[0]
            s = _similarity(combined_a, combined_b)
            if s >= DEDUP_SIMILARITY:
                links.append({"from": a["slug"], "to": b["slug"],
                              "sim": round(s, 3), "via": "combined"})
    return {"nodes": nodes, "links": links}


def block_card(row, width=76):
    """Fixed-width card: every block prints the same shape, so the eye learns it
    once and a narrow phone screen gets a wrapped value instead of a mangled
    one. (Width, not content, is the display problem this pool keeps solving.)"""
    ts = time.strftime("%m-%d %H:%M", time.localtime(row.get("t") or 0))
    apps = ",".join(a for a in (row.get("apps") or [row.get("app")]) if a)
    out = ["▣ %s  rev %d · %s · %s · %s"
           % (row.get("slug"), row.get("rev", 1), row.get("status", "open"), apps, ts)]
    for k in BLOCK_KEYS:
        if k == "status":
            continue
        v = _flat(row.get(k, ""))
        if not v:
            continue
        out.append(textwrap.fill(v, width=max(24, width - 12),
                                 initial_indent="  %-9s " % (k + ":"),
                                 subsequent_indent=" " * 12))
    return "\n".join(out)


# ── session-end: auto-block on session close ───────────────────────────
# Every platform that uses the pool eventually finishes a work round. When it
# does, it should leave a block — the story of what it did and the method it
# used — so the next platform (or the next session of the same platform) can
# pick up without replaying the chat. This is the hook that makes blocks
# automatic: the platform calls session_end() when it is done, and the rest
# happens by rules.
#
# Rules (designed to avoid noise AND duplicates):
#   1. Minimum 3 messages — a trivial ping/exchange does not produce memory.
#   2. At least one substantive line (>240 chars OR multi-line OR contains
#      DECISION/HANDOFF/VERDICT/SPEC markers OR created a bond). This is the
#      same "is it a real message?" check the curator already uses.
#   3. Dedup by slug: if the last block by this app was written within the
#      same calendar hour AND has the same title, we bump its rev instead of
#      creating a new slug. This prevents platform X from writing 10 blocks
#      titled "session result" every time it restarts.
#   4. Slug is {app}-{YYYYMMDD-HHMMSS} — stable per second per app, human-readable,
#      and the address on the 144×144 plane.

SESSION_END_MIN_LINES = 3       # rule 1
SESSION_END_MIN_SUBSTANTIVE = 1 # rule 2
SESSION_END_DEDUP_WINDOW = 3600 # rule 3: same-hour = same slug candidate (seconds)

# Markers that signal substantive work (same family as CURATOR_MARKERS but
# focused on session-level signals, not per-line protection).
_SUBSTANTIVE_RE = re.compile(
    r"(DECISION|VERDICT|HANDOFF|SESSION|RECOVERY|CORRECTION|TODO|FIXME|SPEC|"
    r"สรุป|ตัดสิน|มติ|ASSUME|结论|方案)", re.IGNORECASE)


def _session_lines(app):
    """All lines by `app` since the last block it wrote, or since the
    beginning if it never wrote one. Streamed, not cached — session_end
    runs once per session close, not on every refresh."""
    last_block_t = 0
    for m in _iter_lines():
        if m.get("kind") == "block" and m.get("app") == app:
            last_block_t = max(last_block_t, m.get("t", 0))
    lines = []
    for m in _iter_lines():
        if m.get("app") == app and m.get("t", 0) > last_block_t:
            lines.append(m)
    return lines


def _is_substantive(m):
    """Is this line worth remembering? Same logic as curator protection
    for "long" and "marker" — a message the curator would never hide is
    a message worth putting in a block."""
    text = m.get("text") or ""
    if len(text) > 240 or "\n" in text:
        return True
    if _SUBSTANTIVE_RE.search(text):
        return True
    if m.get("kind") == "bond":
        return True
    return False


def _auto_title(app, lines):
    """Generate a block title from session content.
    Strategy: find the most "signal-heavy" line (markers + length) and
    extract a short title from it. Falls back to a timestamp-based title."""
    # score each line: markers are worth more than raw length
    best, best_score = None, 0
    for m in lines:
        text = m.get("text") or ""
        score = len(text) / 100  # length贡献
        if _SUBSTANTIVE_RE.search(text):
            score += 5
        if m.get("kind") == "bond":
            score += 2
        if score > best_score:
            best, best_score = m, score
    if best:
        text = (best.get("text") or "").strip()
        # take the first meaningful sentence/line
        first = text.split("\n")[0].strip()
        if len(first) > 80:
            first = first[:77] + "..."
        if first:
            return first
    # fallback: timestamp
    ts = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    return "%s session %s" % (app, ts)


def _session_refs(lines):
    """Pick the most important lines as refs (hash prefixes).
    Priority: bonds > markers > long messages > everything else.
    Returns up to 8 refs (enough for a meaningful evidence trail
    without bloating the block text)."""
    scored = []
    for m in lines:
        h = m.get("hash", "")
        if not h:
            continue
        score = 0
        if m.get("kind") == "bond":
            score += 10
        text = m.get("text") or ""
        if _SUBSTANTIVE_RE.search(text):
            score += 5
        if len(text) > 240:
            score += 3
        if "\n" in text:
            score += 1
        scored.append((score, h[:8], m))
    scored.sort(key=lambda x: -x[0])
    seen = set()
    refs = []
    for _, h8, _ in scored:
        if h8 not in seen:
            seen.add(h8)
            refs.append(h8)
        if len(refs) >= 8:
            break
    return refs


def _tokenize(s):
    """Lowercase + split on non-alphanumeric (Unicode-aware for Thai).
    Returns a set of tokens — order does not matter for similarity."""
    return set(re.findall(r"[0-9A-Za-z\u0e00-\u0e7f]+", _flat(s).lower()))


# similarity threshold: two blocks are "the same work" when the overlap
# of their title+story tokens exceeds this. Title is weighted 3x because
# the same DECISION/topic with a reworded story is still the same work.
DEDUP_SIMILARITY = 0.45


def _similarity(a, b):
    """Weighted Jaccard: title tokens count 3x. Returns 0.0-1.0.
    A pure function of two strings, so the same two blocks always agree."""
    sa, sb = _tokenize(a), _tokenize(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _is_duplicate(app, title, story):
    """Check if a recent block by this app is about the same work.

    Two-phase check:
      1. Title-only Jaccard >= 0.4: same topic (DECISION about mmap = same topic)
      2. Combined title+story Jaccard >= DEDUP_SIMILARITY: same overall content

    Either match is enough — title is the strongest signal for "same work"
    because a reworded story about the same decision is still a duplicate.

    Returns the most-recent matching block row, or None.
    Within the dedup window (1 hour); outside it, everything is new."""
    now = time.time()
    best, best_sim = None, 0.0
    for row in blocks():
        if row.get("app") != app:
            continue
        if now - (row.get("t") or 0) > SESSION_END_DEDUP_WINDOW:
            continue
        # phase 1: title-only check (strong signal)
        row_title = _flat(row.get("title", ""))
        cand_title = _flat(title)
        title_sim = _similarity(row_title, cand_title)
        if title_sim >= 0.4:
            return row
        # phase 2: combined check
        existing = row_title + " " + _flat(row.get("story", ""))
        candidate = cand_title + " " + _flat(story)
        sim = _similarity(existing, candidate)
        if sim > best_sim:
            best, best_sim = row, sim
    if best and best_sim >= DEDUP_SIMILARITY:
        return best
    return None


def session_end(app, force=False):
    """Auto-write a block when a platform session ends.

    Called by the platform (or by POST /session-end, or CLI `pool.py session-end`).
    Returns the block row, or None when the session was too short / too trivial
    to produce memory. `force=True` bypasses the minimum-line and substantive
    checks — useful for testing or when the platform knows it did real work
    even though the signals are thin.

    Dedup: if a block with the same app + title + story was written within
    the last hour, the existing block is bumped (rev+1, updated refs/evidence)
    instead of creating a new slug."""
    if not is_registered(app):
        raise ValueError("%s not registered — POST /register first" % app)
    lines = _session_lines(app)
    # rule 1: minimum line count
    if not force and len(lines) < SESSION_END_MIN_LINES:
        return None
    # rule 2: at least one substantive line
    substantive = [m for m in lines if _is_substantive(m)]
    if not force and len(substantive) < SESSION_END_MIN_SUBSTANTIVE:
        return None
    title = _auto_title(app, lines)
    # build story from the substantive lines
    story_parts = []
    for m in substantive[:10]:  # cap at 10 to keep the block readable
        text = (m.get("text") or "").strip().split("\n")[0]
        if len(text) > 120:
            text = text[:117] + "..."
        story_parts.append(text)
    story = "; ".join(story_parts) if story_parts else "session closed"
    # method: summarize by kind
    kinds = {}
    for m in lines:
        k = m.get("kind", "chat")
        kinds[k] = kinds.get(k, 0) + 1
    method_parts = ["%d %s" % (v, k) for k, v in sorted(kinds.items(), key=lambda x: -x[1])]
    method = "session composed of: %s" % ", ".join(method_parts)
    refs = _session_refs(lines)
    evidence = "%d lines, %d substantive" % (len(lines), len(substantive))
    # dedup: same title + story within the hour → bump rev
    dup = _is_duplicate(app, title, story)
    if dup:
        # bump the existing block instead of creating a new slug
        slug = dup["slug"]
        status = "closed"
    else:
        # new slug: {app}-{YYYYMMDD-HHMMSS} — seconds prevent collision
        # when the same app ends two different sessions within a minute.
        # If the slug already exists (same-second edge case), append a
        # numeric suffix so genuinely different work gets its own address.
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        slug = "%s-%s" % (app, ts)
        if block_get(slug) is not None:
            # slug collided — find a free suffix
            for seq in range(1, 100):
                candidate = "%s-%s" % (slug, seq)
                if block_get(candidate) is None:
                    slug = candidate
                    break
        status = "closed"
    return block(app, slug, title, story=story, method=method,
                 evidence=evidence, refs=refs, status=status)


# ── atexit guard: auto-call session_end on process exit ─────────────
# Each platform calls pool_session_guard(app) once at startup. When the
# process exits (normal exit, SIGTERM, unhandled exception — anything
# atexit covers), session_end(app) fires automatically. This is the
# "1 line" that makes blocks automatic: no cleanup code in the app,
# no forgotten sessions, no developer discipline required.
#
# Guard rails:
#   - Only fires if the app is registered (avoids writing on import)
#   - Only fires if there are ≥1 lines by this app (avoids empty blocks)
#   - Exceptions inside session_end are swallowed — a memory write must
#     never crash the platform's own shutdown
#   - Idempotent: calling guard(app) twice does not double-fire

_GUARDED_APPS = set()  # apps that have a guard registered

def pool_session_guard(app, force=False):
    """Register an atexit hook that calls session_end(app) on process exit.
    Call once at startup. `force` is passed through to session_end.

    Usage (one line per platform):
        import pool; pool.register_app("freebuff"); pool.pool_session_guard("freebuff")
    """
    if app in _GUARDED_APPS:
        return  # idempotent — same guard does not stack
    _GUARDED_APPS.add(app)

    def _on_exit():
        try:
            if is_registered(app):
                session_end(app, force=force)
        except Exception:
            pass  # never crash the platform's own shutdown

    atexit.register(_on_exit)


def derived():
    """One streaming pass keeping only what the plane reads often: cell counts,
    hash→rkey, the live bond list, sightings and the tick timeline.
    Cached against (size, mtime) — an append-only file changes one of those or
    nothing at all, so the cache can never serve a stale plane.
    Deliberately NOT persisted: ~10 MB at 100k lines, and the place a real
    index belongs is pool.db — the materialized view the hourly flush already
    rebuilds. See bond_scale.py for the numbers behind that call."""
    key = _log_key()
    if _DERIVED.get("key") == key and "cells" in _DERIVED:
        return _DERIVED
    cells, hash_rkey, bonds, hidden, pointers, timeline, blocks_idx = {}, {}, [], set(), [], [], {}
    # Last writer per cell, and the ordinal of the line that did it. Two extra
    # maps, not two extra passes: the plane can then be coloured by WHO owns an
    # address and by WHEN it last changed. Counting per-cell per-app totals
    # instead would cost a dict per cell to answer "who is the majority here" —
    # "who wrote here last" is the same question about the live edge of the log
    # and is exact after every single append.
    cell_app, cell_seq = {}, {}
    # The same question about the last line instead of about its writer: what
    # KIND of line landed here last. Free, for the same reason `cell_app` was —
    # the live edge of the log answers it exactly after every append, and
    # counting per kind would cost a dict per cell to say less.
    cell_kind = {}
    # The WHOLE map, split by kind: {kind: {rkey: count}}. This one costs a
    # dict per kind and is the only layer that cannot be derived from a cell's
    # last line — "how many BONDS live here" is a different question from "what
    # landed here last", and a reader filtering to one kind needs the first.
    # Measured on the live corpus: 13,210 occupied cells become 16,750
    # (cell,kind) pairs = 1.27x, because 9,919 cells hold exactly one kind and
    # only 3,291 are mixed. It rides the same fold, so it costs dicts and not
    # a second walk over an already-streamed log.
    by_kind = {}
    # The same split by WHO: {app: {rkey: count}}. One more line in the fold we
    # are already making — a dict per app — and the answer to "what did this
    # corpus look like when only chatdrop was writing", which is the one axis
    # the plane shows nothing of: it sums every era into one number per cell.
    # Measured on the live corpus this is the COARSER of the two partitions:
    # 13,210 occupied cells become 18,327 (cell,app) pairs = 1.39x, against
    # 16,751 = 1.27x for kinds. Kinds cluster (index writes 5.12 lines into
    # every cell it touches) and apps spread out, so the same two facts cost
    # more to index by writer than by meaning. The 26% quoted in §20.7 before
    # this was measured was low.
    #
    # `m.get("app") or "?"` because a line with no writer has to land SOMEWHERE:
    # a None key is a JSON "null" and a dropdown entry that reads "null".
    by_app = {}
    # The plane prunes what the feed merely hides (see CURATOR_PRUNE): a line
    # the curator calls noise contributes NO count to its cell, so an address
    # that lived on nothing but tests stops being occupied and the cell leaves
    # the map. Both maps fall out of the one pass we are already making, so
    # pruning costs a few dicts, not a second walk over an already-streamed log.
    cells_clean, cell_app_clean, cell_seq_clean = {}, {}, {}
    cell_kind_clean = {}
    by_kind_clean = {}
    by_app_clean = {}
    # Which 12x12 blocks still hold a clean line. A sighting can be made at
    # level 1, which names a BLOCK rather than a cell — answering "does that
    # address live" for it would otherwise mean scanning every clean cell.
    blocks_clean = set()
    # Which LINES were pruned, not just how many. A cell count answers "is this
    # address occupied"; an edge needs "is this exact line still there" — a bond
    # names two hashes, and one pruned end makes it an edge into nothing.
    pruned_hashes = set()
    pruned, pruned_by, _seen = 0, {}, {}
    # A short ring of the last (ordinal, text) pairs. The full pass judges
    # `repeat` from the text->ordinal map, but the absorb path folds ONE line
    # and has no map — it reads this instead, so a rebuild and a fold can never
    # disagree about which copy of a repeated line is the original.
    recent = []
    for m in _iter_lines():
        rk, h, kind = m.get("rkey"), m.get("hash"), m.get("kind")
        if h:
            hash_rkey[h] = rk
        # Verdict first: `repeat` needs this line's append ordinal, and the
        # ordinal is its position in `timeline` before it lands there. Called
        # for EVERY line so the repeat window matches curator_verdicts exactly.
        _ps, _pr, _pp = _prune_verdict(m, _seen, len(timeline))
        _text = (m.get("text") or "").strip()
        if _text:
            recent.append((len(timeline), _text))
            if len(recent) > REPEAT_WINDOW + 1:
                recent.pop(0)
        is_noise = _ps >= CURATOR_PRUNE and not _pp
        if rk is not None:
            cells[rk] = cells.get(rk, 0) + 1
            _bk = by_kind.setdefault(kind, {})
            _bk[rk] = _bk.get(rk, 0) + 1
            _ba = by_app.setdefault(m.get("app") or "?", {})
            _ba[rk] = _ba.get(rk, 0) + 1
            cell_app[rk] = m.get("app")
            cell_kind[rk] = kind
            cell_seq[rk] = len(timeline)      # this line's ordinal, before it lands
            if is_noise:
                pruned += 1
                if h:
                    pruned_hashes.add(h)
                for _r in _pr:
                    pruned_by[_r] = pruned_by.get(_r, 0) + 1
            else:
                cells_clean[rk] = cells_clean.get(rk, 0) + 1
                _bk = by_kind_clean.setdefault(kind, {})
                _bk[rk] = _bk.get(rk, 0) + 1
                _ba = by_app_clean.setdefault(m.get("app") or "?", {})
                _ba[rk] = _ba.get(rk, 0) + 1
                cell_app_clean[rk] = m.get("app")
                cell_kind_clean[rk] = kind
                cell_seq_clean[rk] = len(timeline)
                if len(blocks_clean) < 144:     # 12x12 blocks tile the plane
                    blocks_clean.add(((rk % 144) // 12, (rk // 144) // 12))
        to, tags = m.get("to", "all"), tuple(m.get("tags") or ())
        # index 6 is the FULL hash: the feed's tick layer needs an exact
        # hash -> ordinal map, and a 8-hex prefix collides once a log is big
        # index 7 is the WRITER, and it is NORMALISED to a string the plane can
        # compare against: by_app keys on `app or "?"`, so a scrub filter that
        # reads this column has to agree. A raw None here is the same "?" after
        # a Python `or` and an `undefined` in JavaScript — two languages holding
        # the same rule as two copies of a string literal. Normalised once, at
        # the fold, it is one fact instead of a contract.
        timeline.append((m.get("t"), rk, kind, (h or "")[:8], to, tags, h,
                         m.get("app") or "?"))
        if kind == "bond":
            p = (m.get("text") or "").split()
            if len(p) == 3:
                if p[1] == "unbinds" and p[0] == p[2]:
                    hidden.add(p[0])           # tombstone: hide, never delete
                else:
                    bonds.append({"hash": h, "src": p[0], "rel": p[1], "dst": p[2],
                                  "app": m.get("app"), "t": m.get("t")})
        elif kind == "pointer":
            pointers.append(m)
        elif kind == "block":
            # folded in the pass we are already making: a block index must not
            # cost a second walk over a log that is already streamed here
            _block_visit(blocks_idx, m)
    _DERIVED.update({"key": key, "cells": cells, "hash_rkey": hash_rkey,
                     "cell_app": cell_app, "cell_seq": cell_seq,
                     "cell_kind": cell_kind,
                     "by_kind": by_kind,
                     "by_app": by_app,
                     # the pruned view of the same pass: what the plane draws
                     "cells_clean": cells_clean,
                     "cell_app_clean": cell_app_clean,
                     "cell_seq_clean": cell_seq_clean,
                     "cell_kind_clean": cell_kind_clean,
                     "by_kind_clean": by_kind_clean,
                     "by_app_clean": by_app_clean,
                     "blocks_clean": blocks_clean,
                     "pruned": pruned, "pruned_by": pruned_by,
                     "pruned_hashes": pruned_hashes,
                     # Every rebuild invalidates the resolved-edge cache already
                     # (bonds_rev), so the revision only has to move when a FOLD
                     # pruned a line — that is the one case an edge set can
                     # change without the edge set changing (§dangling).
                     "prune_rev": _DERIVED.get("prune_rev", 0) + 1,
                     # a cell is "pruned" when every line in it was noise, so it
                     # held nothing else to show. That is exactly the gap
                     # between the two maps — no third count to keep in step
                     "pruned_cells": len(cells) - len(cells_clean),
                     "recent_texts": recent,
                     "blocks": blocks_idx,
                     "bonds": [b for b in bonds if b["hash"] not in hidden],
                     "pointers": pointers, "timeline": timeline,
                     # bump so the resolved-edge cache below knows the edge
                     # set changed — a chat append must not rebuild it
                     "bonds_rev": _DERIVED.get("bonds_rev", 0) + 1})
    _DERIVED.pop("seq_of", None)   # a tick is a POSITION: a rebuild renumbers it
    return _DERIVED


def _last_line_is(body):
    """Is the line on the end of the log the one we just wrote?

    The absorb guard cannot use byte arithmetic, and not only because another
    process might write in between: on Windows `os.open` without O_BINARY gives
    a TEXT-mode fd, so the CRT expands our "\\n" to "\\r\\n" and the file grows
    by one byte more than we handed the kernel. (The live log is CRLF for that
    reason; the chain never saw it, because the hash covers content and
    `_prev()` parses JSON, which ignores trailing whitespace.) Reading the tail
    back and matching the hash is exact on both platforms and needs no such
    assumption."""
    try:
        size = os.path.getsize(LOG)
        with open(LOG, "rb") as f:
            f.seek(max(size - 4096, 0))
            tailb = f.read()
    except OSError:
        return False
    ends = [ln for ln in tailb.split(b"\n") if ln.strip()]
    if not ends:
        return False
    try:
        return json.loads(ends[-1].decode("utf-8")).get("hash") == body.get("hash")
    except (ValueError, UnicodeDecodeError):
        return False


def _derived_absorb(body, raw, key0):
    """Feed one just-written line into the index instead of dropping it.

    Append-only means the index is a fold over the log, so a new line is an
    update, not a rebuild. That distinction is the whole cost curve: at 55k
    lines a rebuild is ~460 ms (bond_scale.py), and paying it on the refresh
    after every message is the only part of this that actually hurts today.

    Guarded on purpose. The cache is only updated when it matched the file
    exactly before our own write AND the line now on the end of the log is the
    one we wrote (_last_line_is). If another process slipped a line in either
    side of us, the cache would claim coverage it does not have — so it is
    dropped and the next read rebuilds it. Lying is worse than being slow."""
    d = _DERIVED
    if d.get("key") is None or "cells" not in d:
        return                               # nothing cached to keep in step
    if d["key"] != key0 or not key0 or not _last_line_is(body):
        d["key"] = None                      # unaccounted-for write: rebuild
        return
    rk, h, kind = body.get("rkey"), body.get("hash"), body.get("kind")
    if h:
        d["hash_rkey"][h] = rk
    if rk is not None:
        d["cells"][rk] = d["cells"].get(rk, 0) + 1
        _bk = d.setdefault("by_kind", {}).setdefault(kind, {})
        _bk[rk] = _bk.get(rk, 0) + 1
        _ba = d.setdefault("by_app", {}).setdefault(body.get("app") or "?", {})
        _ba[rk] = _ba.get(rk, 0) + 1
        d["cell_app"][rk] = body.get("app")
        d.setdefault("cell_kind", {})[rk] = kind
        d["cell_seq"][rk] = len(d["timeline"])
        # The pruned view must fold WITH the raw one. Absorbing only `cells`
        # left cells_clean/pruned describing the previous log, and because the
        # fold also re-stamps d["key"] the stale clean map looked current — the
        # plane would keep drawing a line that had already been pruned.
        _text = (body.get("text") or "").strip()
        _ring = d.setdefault("recent_texts", [])
        _ps, _pr, _pp = _prune_verdict_recent(body, _ring, len(d["timeline"]))
        if _ps >= CURATOR_PRUNE and not _pp:
            d["pruned"] = d.get("pruned", 0) + 1
            if h:
                d.setdefault("pruned_hashes", set()).add(h)
            # A new pruned line can STRAND an edge that already exists, so the
            # resolved-edge cache has to notice even though the edge set did
            # not change — this is the one case bonds_rev cannot see.
            d["prune_rev"] = d.get("prune_rev", 0) + 1
            _by = d.setdefault("pruned_by", {})
            for _r in _pr:
                _by[_r] = _by.get(_r, 0) + 1
        else:
            _bk = d.setdefault("by_kind_clean", {}).setdefault(kind, {})
            _bk[rk] = _bk.get(rk, 0) + 1
            _ba = d.setdefault("by_app_clean", {}).setdefault(body.get("app") or "?", {})
            _ba[rk] = _ba.get(rk, 0) + 1
            _cc = d.setdefault("cells_clean", {})
            _cc[rk] = _cc.get(rk, 0) + 1
            d.setdefault("cell_app_clean", {})[rk] = body.get("app")
            d.setdefault("cell_kind_clean", {})[rk] = kind
            d.setdefault("cell_seq_clean", {})[rk] = len(d["timeline"])
            d.setdefault("blocks_clean", set()).add(
                ((rk % 144) // 12, (rk // 144) // 12))
        if _text:
            _ring.append((len(d["timeline"]), _text))
            if len(_ring) > REPEAT_WINDOW + 1:
                _ring.pop(0)
        d["pruned_cells"] = len(d["cells"]) - len(d.get("cells_clean", {}))
    d["timeline"].append((body.get("t"), rk, kind, (h or "")[:8],
                          body.get("to", "all"), tuple(body.get("tags") or ()), h,
                          body.get("app") or "?"))
    if d.get("seq_of") is not None:
        d["seq_of"][h] = len(d["timeline"]) - 1   # a fold, so the ordinal is known
    if kind == "bond":
        p = (body.get("text") or "").split()
        if len(p) == 3:
            d["bonds_rev"] = d.get("bonds_rev", 0) + 1
            if p[1] == "unbinds" and p[0] == p[2]:
                d["bonds"] = [b for b in d["bonds"] if b["hash"] != p[0]]
            else:
                d["bonds"].append({"hash": h, "src": p[0], "rel": p[1],
                                   "dst": p[2], "app": body.get("app"),
                                   "t": body.get("t")})
    elif kind == "pointer":
        d["pointers"].append(body)
    elif kind == "block":
        # same fold, one line, no rebuild — setdefault keeps an older cache
        # shape (built before blocks existed) from raising instead of healing
        _block_visit(d.setdefault("blocks", {}), body)
    d["key"] = _log_key()                    # the state we just verified


def _cell_lines(rkey0, as_app=None):
    """The lines living in ONE cell — streamed and filtered, so a point read
    never materializes the history (and never truncates it either)."""
    return [m for m in _iter_lines()
            if m.get("rkey") == rkey0 and _visible(m, as_app)]


CLOUD_DB = r"I:\tools\cloud-workspace\pool.db"


def pretty(m):
    """One-line minimal chat-log view of a pool line.
    kind 'chat' is the default and stays implicit in the label;
    directed messages show →recipient."""
    ts = time.strftime("%m-%d %H:%M", time.localtime(m.get("t", time.time())))
    kind = m.get("kind", "chat")
    if kind == "block":
        # a block is six canonical lines; the feed shows the two that identify it
        # and the card (block_card) shows the rest — otherwise one block would
        # push six screens of the conversation off the phone
        bd = parse_block(m.get("text", ""))
        if bd:
            return "[%s] %s/block ▣ %s rev%d · %s: %s" % (
                ts, m.get("app", "?"), bd["slug"], bd["rev"], bd["status"], bd["title"])
    label = (m.get("app", "?") if (kind or "chat").strip().lower() == "chat"
             else "%s/%s" % (m.get("app", "?"), kind))
    # The case-insensitive compare above is not decoration. This line compared
    # `kind == "chat"` exactly, and the corpus writes "CHAT" — 13,746 lines of
    # it — so the plain-chat label was dead code and every one of those lines
    # rendered as `hermes/CHAT` instead of `hermes`. It is the same defect §6
    # already paid for in curator_protect(): a kind is a value, never a
    # spelling, and comparing it as a spelling silently disables the branch.
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


def view(n=20, cloud=False, as_app=None, clean=False):
    """Minimal human-readable view. Local log tail by default;
    --cloud renders recent session-pool chunks (pool.db, read-only).
    Storage stays raw everywhere — this is display-only.
    `clean` applies the curator here and only here: the raw read is the one
    callers that need completeness keep using."""
    if not cloud:
        return [pretty(m) for m in curate(tail(n, as_app), mode="noise" if clean else "raw")]
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


# ── curator: what the feed shows (hide, never delete) ──────────────────
# The log is append-only and hash-chained, so nothing here can remove a line —
# a curator only decides what the FEED shows. Three laws keep that honest.
#
#   1. Storage, the index and the graph stay untouched. tail()/derived()/
#      bonds() keep every line, and this runs at the display edge only
#      (the feed, view(), /tail?clean=1). A filter that reached the index
#      would silently shorten a completeness read — the exact bug tail(None)
#      exists to prevent.
#   2. A verdict is a function of the line, so the same line is judged the
#      same way on every machine and forever. The single exception is
#      `repeat`, the only rule that needs a neighbour, and it says so.
#   3. It never decides alone. Every hidden line keeps its reasons, the report
#      prints them with examples, and mode='raw' is a first-class switch: a
#      filter nobody can turn off is a filter nobody can trust.
CURATOR_HIDE = int(os.environ.get("POOL_CURATOR_HIDE", 2))
# Hiding a line and pruning a line are different questions about the same
# noise. A hidden line still HAPPENED — it keeps its bytes, its hash and its
# place in the chain, and the reader can reveal it. A pruned line stops
# OCCUPYING AN ADDRESS: it contributes no count to its cell, so a cell that
# lived on nothing but tests vanishes from the plane. Same rules, same order;
# only the threshold and the consequence differ. Raise it when pruning feels
# too eager: POOL_CURATOR_PRUNE=3 keeps every two-point doubt.
CURATOR_PRUNE = int(os.environ.get("POOL_CURATOR_PRUNE", str(CURATOR_HIDE)))
# How far back `repeat` looks. One number, shared by the full pass, the
# incremental absorb and curator_verdicts: a rule that disagreed with itself
# between a rebuild and a fold would make the plane flicker after every append.
REPEAT_WINDOW = 20
_WORD = re.compile(r"[0-9A-Za-z\u0e00-\u0e7f]")


def _cur_any(*pats):
    """A rule that fires on any of these patterns (case-insensitive)."""
    rx = [re.compile(p, re.IGNORECASE) for p in pats]
    return lambda b: any(r.search(b) for r in rx)


def _cur_run_ge(b, n, max_len):
    """One character repeated n+ times, in a SHORT line: '========', '!!!!!!'.
    Length-gated so a markdown rule inside a real message is not this."""
    if not b or len(b) > max_len:
        return False
    best = run = 1
    for i in range(1, len(b)):
        run = run + 1 if b[i] == b[i - 1] else 1
        if run > best:
            best = run
    return best >= n


# (name, tag, weight, test) — weight is how sure the rule is that a line is
# noise; the caller picks the threshold, so a rule can be listed without being
# strong enough to hide anything on its own.
CURATOR_RULES = (
    # a line that IS a test word, or a one-word probe — not a message that
    # merely mentions testing
    ("test-word", "test", 3, _cur_any(
        r"^[\s\[\(>#*-]*(t+e+s+t+\w*|ping|pong|probe|echo|asdf+|aaa+|foo|bar|baz|"
        r"hello|hi+|hey|yo|lorem ipsum)[\s!.\u2026\-]*$")),
    ("test-probe", "test", 3, _cur_any(
        r"^[\s\[\(>#*-]*(test|probe|smoke ?test|health ?check|dry ?run)\s*[:=]\s*\S{0,40}$")),
    # placeholder: nothing but a stub, so there is no content to lose
    ("stub", "stub", 2, lambda b: len(b) <= 3 and not b.strip("\u2026.!-–—")),
    ("ack", "stub", 2, _cur_any(
        r"^[\s]*(ok+|okay|k|y|yes|no|n|thx|thanks|ty|got ?it|noted|รับทราบ|โอเค|ครับ|ค่ะ|ได้)[\s!.\u2026]*$")),
    # machine noise: an encoded or generated token with no words in it
    ("sess-id", "blob", 2, _cur_any(r"\bses_[0-9a-z]{12,}\b")),
    ("hex-blob", "blob", 2, _cur_any(r"^[0-9a-f]{32,}$")),
    ("b64-blob", "blob", 2, _cur_any(r"^[A-Za-z0-9+/]{60,}={0,2}$")),
    ("punct-soup", "noise", 2, lambda b: len(b) >= 4 and not _WORD.search(b)),
    ("loud", "noise", 2, lambda b: _cur_run_ge(b, 8, 60)),
)


# The short list where a line can never be noise. Every entry is a case where
# the line is structurally load-bearing, not merely well-written — which is why
# the list is about shape (kind, recipient, mention) before it is about words.
CURATOR_MARKERS = re.compile(
    r"(HANDOFF|DECISION|VERDICT|SESSION|RECOVERY|CORRECTION|TODO|FIXME|SPEC|"
    r"traceback|Error\b|commit|สรุป|ตัดสิน|มติ)", re.IGNORECASE)


def curator_protect(m):
    """Why this line may never be hidden, or None."""
    text = m.get("text") or ""
    # Case-insensitive on purpose. Kind is whatever the writer typed, and
    # platforms have written both 'chat' and 'CHAT' for the same thing; a
    # case-sensitive compare silently promoted every 'CHAT' to a protected
    # kind, which switched the whole curator off for those writers. What the
    # rule means is "not chatter" (bonds/pointers/blocks carry the graph),
    # and that is a question about the kind's value, not its spelling.
    if (m.get("kind") or "chat").strip().lower() != "chat":
        return "kind"          # bonds/pointers/code carry the graph, not chatter
    if m.get("to") not in (None, "all"):
        return "dm"            # aimed at one reader: their business, not ours
    if m.get("tags") or re.search(r"@[A-Za-z0-9_\-]{1,32}", text):
        return "mention"       # actionable: someone is being asked to do a thing
    if len(text) > 240 or "\n" in text:
        return "long"          # a real message, whatever it happens to look like
    if CURATOR_MARKERS.search(text):
        return "marker"        # the bridge lifts these; never eat a handoff
    return None


def curator_hits(m):
    """(score, [rule names]) for one line — a pure function of that line."""
    body = (m.get("text") or "").strip()
    score, tags = 0, []
    for name, _tag, weight, test in CURATOR_RULES:
        try:
            hit = test(body)
        except TypeError:
            hit = False
        if hit:
            score += weight
            tags.append(name)
    return score, tags


def _prune_verdict(m, seen, idx, repeat_window=REPEAT_WINDOW):
    """The PLANE's verdict for one line, computed inline: (score, rules, protected).

    Identical judgement to curator_verdicts — a line the feed calls noise is
    the line the plane drops — but it takes the `repeat` window as a mutable
    `seen` dict instead of a materialized list, so derived() can prune cells
    inside the single pass it already makes. `idx` is the line's append
    ordinal, the same number curator_verdicts counts as its enumerate index,
    so both agree on which copy is the original."""
    text = (m.get("text") or "").strip()
    score, rules = curator_hits(m)
    if text and text in seen and (idx - seen[text]) <= repeat_window:
        score += 2
        rules.append("repeat")
    if text:
        seen[text] = idx
    return score, rules, curator_protect(m)


def _prune_verdict_recent(m, recent, idx, repeat_window=REPEAT_WINDOW):
    """The same verdict, judged from the short ring instead of a full map.

    The absorb path folds one appended line into the cached index, so it has no
    text->ordinal map to ask. `recent` is the (ordinal, text) ring derived()
    keeps for exactly this call — same rule, same window, same answer."""
    text = (m.get("text") or "").strip()
    score, rules = curator_hits(m)
    if text:
        for oi, ot in recent:
            if ot == text and (idx - oi) <= repeat_window:
                score += 2
                rules.append("repeat")
                break
    return score, rules, curator_protect(m)


def curator_verdicts(msgs, repeat_window=REPEAT_WINDOW):
    """Verdict per line, in order. `repeat` is the one context rule: the same
    text twice inside `repeat_window` lines is a flood, and the second copy is
    worth hiding with the first one still visible as the original."""
    out, seen = [], {}
    for i, m in enumerate(msgs):
        text = (m.get("text") or "").strip()
        score, rules = curator_hits(m)
        if text and text in seen and (i - seen[text]) <= repeat_window:
            score += 2
            rules.append("repeat")
        if text:
            seen[text] = i
        out.append({"hash": m.get("hash"), "score": score, "rules": rules,
                    "protected": curator_protect(m)})
    return out


def curate(msgs, mode="noise", hide_at=None, dropped=False):
    """The feed's filter. Returns the kept lines, or (kept, cut) when
    `dropped` is set — the cut carries the reasons, because a silent filter
    teaches nobody anything."""
    hide_at = CURATOR_HIDE if hide_at is None else hide_at
    if mode in (None, "off", "raw"):
        return (list(msgs), []) if dropped else list(msgs)
    keep, cut = [], []
    for m, v in zip(msgs, curator_verdicts(msgs)):
        if v["score"] >= hide_at and not v["protected"]:
            cut.append({"hash": v["hash"], "app": m.get("app"),
                        "score": v["score"], "rules": v["rules"],
                        "text": (m.get("text") or "")[:120]})
        else:
            keep.append(m)
    return (keep, cut) if dropped else keep


def stamp_cur(msgs, hide_at=None):
    """Stamp each line with the curator's verdict, the same decoration idea as
    stamp_seq: the line on disk does not change and the hash never covered this
    field, so the feed can decide locally (and the 🧹 switch is instant instead
    of a refetch). Short keys: this rides on every line of every window.
        s = score   r = rules that fired   p = why it is protected (or None)
        h = hide it (score over the threshold AND nothing protects it)
    `repeat` is judged inside the window it is given — the verdict is about the
    lines you are actually looking at, not about history you cannot see."""
    hide_at = CURATOR_HIDE if hide_at is None else hide_at
    for m, v in zip(msgs, curator_verdicts(msgs)):
        m["cur"] = {"s": v["score"], "r": v["rules"], "p": v["protected"],
                    "h": v["score"] >= hide_at and not v["protected"]}
    return msgs


def curator_report(msgs=None, n=200, hide_at=None):
    """What the curator would hide, by rule, with examples — run this before
    trusting it. Read-only: nothing is filtered out of `msgs` here."""
    hide_at = CURATOR_HIDE if hide_at is None else hide_at
    msgs = tail(n) if msgs is None else list(msgs)
    by_rule, examples, hidden, protected = {}, [], [], {}
    for m, v in zip(msgs, curator_verdicts(msgs)):
        for r in v["rules"]:
            by_rule[r] = by_rule.get(r, 0) + 1
        if v["protected"]:
            protected[v["protected"]] = protected.get(v["protected"], 0) + 1
        if v["score"] >= hide_at and not v["protected"]:
            hidden.append(m.get("hash"))
            if len(examples) < 20:
                examples.append({"app": m.get("app"), "score": v["score"],
                                 "rules": v["rules"],
                                 "text": (m.get("text") or "")[:140]})
    return {"lines": len(msgs), "hide_at": hide_at, "hidden": len(hidden),
            "kept": len(msgs) - len(hidden), "hidden_hashes": hidden,
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "protected": dict(sorted(protected.items(), key=lambda kv: -kv[1])),
            "examples": examples}


def curator_prune_report(msgs=None, prune_at=None, limit=20):
    """What the PLANE prunes, by rule, with examples — curator_report's
    stronger sibling and the same honesty contract. Read-only: it removes
    nothing from anything. `msgs=None` reads the WHOLE log, because the plane
    is a map of the whole log and not of the last window the feed shows.

    `by_rule` counts rules among PRUNED lines only: the question this answers
    is not "which rules fire" but "which rule did the removing", so it also
    tells you which threshold to move."""
    prune_at = CURATOR_PRUNE if prune_at is None else prune_at
    msgs = list(msgs) if msgs is not None else [m for m in _iter_lines()]
    by_rule, examples, pruned, protected = {}, [], 0, {}
    seen = {}
    for i, m in enumerate(msgs):
        score, rules, prot = _prune_verdict(m, seen, i)
        if prot:
            protected[prot] = protected.get(prot, 0) + 1
            continue
        if score >= prune_at:
            pruned += 1
            for r in rules:
                by_rule[r] = by_rule.get(r, 0) + 1
            if len(examples) < limit:
                examples.append({"app": m.get("app"), "score": score,
                                 "rules": rules, "rkey": m.get("rkey"),
                                 "text": (m.get("text") or "")[:140]})
    return {"lines": len(msgs), "prune_at": prune_at, "pruned": pruned,
            "kept": len(msgs) - pruned,
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "protected": dict(sorted(protected.items(), key=lambda kv: -kv[1])),
            "examples": examples}


def plane_prune_stats():
    """What the plane ACTUALLY pruned on its last build: lines dropped, cells
    that stopped being occupied, the edges and sightings that lost an end, and
    the rules responsible. Read from the same cache the map is drawn from, so
    the number can never disagree with the picture next to it."""
    d = derived()
    cells_all, cells_clean = d["cells"], d.get("cells_clean", d["cells"])
    # prune reaches past the map: an edge or a sighting can lose the end it
    # points at while its own line is untouched — so the report counts them,
    # not just the cells, or "pruned 3 lines" would undersell what changed
    edges = all_bonds(raw=True)
    sightings = pointers_at(raw=True)
    return {"lines": len(d["timeline"]), "prune_at": CURATOR_PRUNE,
            "pruned": d.get("pruned", 0),
            "kept": len(d["timeline"]) - d.get("pruned", 0),
            "cells_all": len(cells_all), "cells_clean": len(cells_clean),
            "cells_pruned": len(cells_all) - len(cells_clean),
            "bonds_all": len(edges),
            "dangling_bonds": sum(1 for e in edges if e["dangling"]),
            "sightings_all": len(sightings),
            "dangling_sightings": sum(1 for s in sightings if s["dangling"]),
            "by_rule": dict(sorted(d.get("pruned_by", {}).items(),
                                   key=lambda kv: -kv[1]))}


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
    for m in tail(None):   # a capped read here loses old lines silently
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
    return [(rkey0, _cell_lines(rkey0))]


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


def bonds_for(hash8_or_full, as_app=None, raw=False):
    """Read edges touching a line (either end), latest state wins:
    a later 'unbinds' line hides its target bond. Returns dicts
    {hash, src, rel, dst, app, t, dangling} — walk blast radius by pointing
    at each dst next; that is the caller's next explicit read.

    Goes through all_bonds() so "which edges exist" has ONE answer: a bond
    stranded by a pruned endpoint is out unless raw=True, and a tombstoned
    bond is out either way."""
    want = (hash8_or_full or "").lower()
    return [b for b in all_bonds(raw=raw)
            if b["src"].startswith(want) or b["dst"].startswith(want)]


def _block_of(rkey0, block=12):
    """Spatial super-cell holding this point, as (wedge0, ring0).
    rkey = ring*144 + wedge, so a 12x12 block is a 2-D square of
    (wedge, ring) pairs. 144 *consecutive* addresses are NOT a square —
    they run diagonally across rows, which is what the heatmap made
    obvious: L1 must be a square or "zoom out" would not frame the point."""
    wedge, ring = rkey0 % 144, rkey0 // 144
    return (wedge // block) * block, (ring // block) * block


def _in_block(rk, wedge0, ring0, block=12):
    return (wedge0 <= rk % 144 < wedge0 + block
            and ring0 <= rk // 144 < ring0 + block)


def all_bonds(as_app=None, raw=False):
    """Every live edge in one read — same tombstone rule as bonds_for, but
    with both ends resolved to the rkey they sit in, so a caller can draw
    the edge on the plane without a second lookup. Addresses are not
    content, so edges are not visibility-filtered (same rule as maps).

    A bond is `kind='bond'`, so prune never removes the LINE — but an edge
    whose far end was pruned leads somewhere the plane no longer draws. Those
    are marked `dangling` and held back by default: a relationship is only
    shown between addresses that exist. `raw=True` returns every edge WITH
    its mark, so the call stays inspectable instead of silently filtered.

    Resolving 5,000 ends costs ~10 ms, which is 5% of a refresh budget spent
    on a value that only changes when the edge SET changes — so it is cached.
    The key carries prune_rev too: pruning a line can strand an edge without
    the edge set changing at all, and a cache that missed that would keep
    drawing into a vanished address. Treat the result as read-only."""
    d = derived()
    pruned = d.get("pruned_hashes") or ()
    key = (d["bonds_rev"], d.get("prune_rev", 0))
    if d.get("edges_key") != key:
        d["edges"] = [
            {"hash": b["hash"], "src": b["src"], "rel": b["rel"],
             "dst": b["dst"], "src8": b["src"][:8], "dst8": b["dst"][:8],
             "app": b["app"], "t": b["t"],
             "src_rkey": d["hash_rkey"].get(b["src"]),
             "dst_rkey": d["hash_rkey"].get(b["dst"]),
             "dangling": b["src"] in pruned or b["dst"] in pruned}
            for b in d["bonds"]]
        d["edges_key"] = key
    return d["edges"] if raw else [e for e in d["edges"] if not e["dangling"]]


# --- explore: walk the graph, name the shape (§21) --------------------
# The plane answers "what is WHERE". This answers "what IS this thing in
# relation to the rest" — and that question is two questions, the ones a reader
# actually asks: what came out of it, and what points back at it. Both are
# already on disk as bonds; nothing here invents a relation, it only reports
# which ones the data wrote down.
BRIEF = 150        # chars of a line kept for a walk preview
REACH_CAP = 14     # nodes shown per hop (a hub has 273 edges — never dump them)
HUB_AT = 5         # in-degree at which a line stops being prose and becomes a sink

# A shape is a claim about the WHOLE local picture, so it needs both degrees:
# 0/0 is an island, and 0-out vs 0-in are opposite ends of a conversation.
SHAPES = {
    "isolated": ("○", "เกาะ", "ไม่มีเส้นเลย — ยังไม่มีอะไรอ้างถึง และมันไม่อ้างใคร"),
    "source":   ("▶", "ต้นน้ำ", "ไปข้างหน้าได้ แต่ไม่มีใครชี้กลับมาที่มัน"),
    "sink":     ("◀", "ปลายน้ำ", "มีคนอ้างถึง แต่ต่อจากนี้ไม่มีอะไรออกไป"),
    "chain":    ("⇄", "กลางสาย", "มีทั้งก่อนหน้าและต่อจากนี้ — มันคือช่วงกลางของเรื่อง"),
    "edge":     ("—", "เส้นเชื่อม", "บรรทัดนี้คือเส้นเอง ไม่ใช่ปลายทาง — การมี/ไม่มีเส้นของมันไม่ใช่รูปร่าง"),
}


def _brief():
    """hash -> (text, rkey, app, kind) for the walk's one-line previews.

    derived() keeps the bond list but not the text, and a walk has to SHOW
    where it landed — an 8-hex handle tells a reader nothing about whether a
    hop is worth taking. Built lazily and cached on the log key: exploring is
    human-paced, so one pass per append is cheaper than holding 25k texts for
    readers who never walk anything."""
    d = derived()
    if d.get("brief_key") == d.get("key"):
        return d.get("brief") or {}
    b = {}
    for m in _iter_lines():
        h = m.get("hash")
        if h:
            b[h] = ((m.get("text") or "")[:BRIEF], m.get("rkey"),
                    m.get("app"), m.get("kind"))
    d["brief"], d["brief_key"] = b, d.get("key")
    return b


def _adj():
    """The bond graph as two lookup maps (out of a line, into a line).

    Edges are stored as a LIST because everything so far wanted to draw them
    all. A walk wants the opposite question — "from THIS line" — and scanning
    13,572 edges per hop turns a 3-hop walk into 40k comparisons. Cached on
    (bonds_rev, prune_rev), the same pair the edge cache uses: pruning can
    strand an edge without the edge set changing at all, and a walk into a
    vanished address is exactly the mistake that cache key was invented for."""
    d = derived()
    key = (d["bonds_rev"], d.get("prune_rev", 0))
    if d.get("adj_key") != key:
        out, inn = {}, {}
        for e in all_bonds(raw=True):
            if e["dangling"]:
                continue                       # leads somewhere not drawn
            out.setdefault(e["src"], []).append(e)
            inn.setdefault(e["dst"], []).append(e)
        d["adj"], d["adj_key"] = (out, inn), key
    return d["adj"]


def _deg(h):
    out, inn = _adj()
    return len(out.get(h) or ()), len(inn.get(h) or ())


def _rel_hist(edges):
    """{rel: n} for one side of a line — the WHY, not just the count.
    "cited by 273" says nothing; "uses-term 218, same-hint 55" says the line
    is a term everyone defines their prose against."""
    h = {}
    for e in edges:
        h[e["rel"]] = h.get(e["rel"], 0) + 1
    return dict(sorted(h.items(), key=lambda kv: -kv[1]))


def motif(h):
    """The shape ONE line has in the bond graph, with the evidence for it.

    Degrees alone do not name a shape: a definition cited by 273 lines and a
    sentence with one reader both have in > 0. So the shape comes from the two
    degrees and the SIZE of `in` is reported separately as `hub` — one is
    geometry, the other is weight, and collapsing them would call a lone
    backlink a hub."""
    h = (h or "").lower()
    # A bond line IS an edge. Asking whether the edge has neighbours confuses
    # "nothing links to it" with "it is a link" — and 13,572 lines here are
    # bonds, so that mistake would file half the corpus as islands.
    if (_brief().get(h) or ("", None, None, None))[3] == "bond":
        icon, label, why = SHAPES["edge"]
        b = _brief().get(h) or ("", None, None, None)
        return {"hash": h, "hash8": h[:8], "shape": "edge", "icon": icon,
                "label": label, "why": why, "in": 0, "out": 0, "hub": False,
                "cross": 0, "rels_in": {}, "rels_out": {}, "apps_in": {},
                "apps_out": {}, "rkey": b[1], "app": b[2], "kind": b[3],
                "text": b[0]}
    n_out, n_in = _deg(h)
    if not n_out and not n_in:
        shape = "isolated"
    elif not n_out:
        shape = "sink"                            # a well: things land here
    elif not n_in:
        shape = "source"
    else:
        shape = "chain"
    icon, label, why = SHAPES[shape]
    out, inn = _adj()
    oe, ie = out.get(h) or [], inn.get(h) or []
    b = _brief().get(h) or ("", None, None, None)
    brief = _brief()
    apps_in, apps_out = {}, {}
    for e in ie:
        # the writer that MATTERS is the far end's own line, not the bond's
        # author: a bond written by the dashboard can still carry a hermes line
        a = (brief.get(e["src"]) or (None, None, None, None))[2] or e.get("app") or "?"
        apps_in[a] = apps_in.get(a, 0) + 1
    for e in oe:
        a = (brief.get(e["dst"]) or (None, None, None, None))[2] or e.get("app") or "?"
        apps_out[a] = apps_out.get(a, 0) + 1
    # cross = an edge whose two ends were written by different platforms. The
    # graph is mostly same-app (7,614 opencode↔opencode vs 8 across), so a
    # cross edge is the rare case that actually joins two islands and is worth
    # naming apart from the 2,000 edges beside it.
    cross = 0
    for e in ie:
        if (brief.get(e["src"]) or (None, None, None, None))[2] not in (None, b[2]):
            cross += 1
    for e in oe:
        if (brief.get(e["dst"]) or (None, None, None, None))[2] not in (None, b[2]):
            cross += 1
    return {"hash": h, "hash8": h[:8], "shape": shape, "icon": icon,
            "label": label, "why": why, "in": n_in, "out": n_out,
            "hub": n_in >= HUB_AT, "cross": cross,
            "rels_in": _rel_hist(ie), "rels_out": _rel_hist(oe),
            "apps_in": dict(sorted(apps_in.items(), key=lambda kv: -kv[1])),
            "apps_out": dict(sorted(apps_out.items(), key=lambda kv: -kv[1])),
            "rkey": b[1], "app": b[2], "kind": b[3], "text": b[0]}


def _hop_nodes(edges, side, cap):
    """Turn the edges of one hop into placeable nodes, biggest fan-out first.

    Sorted by the far end's own degree, not by time: at hop 2 the useful thing
    to see next is the node that leads somewhere, and that is knowable before
    the reader clicks it."""
    seen, nodes = set(), []
    for e in sorted(edges, key=lambda e: -(_deg(e[side])[0] + _deg(e[side])[1])):
        far = e[side]
        if far in seen:
            continue
        seen.add(far)
        t, rk, app, kind = _brief().get(far, ("", None, None, None))
        o, i = _deg(far)
        nodes.append({"hash": far, "hash8": far[:8], "rel": e["rel"],
                      "dir": "out" if side == "dst" else "in",
                      "rkey": e["dst_rkey"] if side == "dst" else e["src_rkey"],
                      "app": app, "kind": kind, "text": t,
                      "out": o, "in": i, "via": e.get("app")})
        if len(nodes) >= cap:
            break
    return nodes, len(seen)


def _reach_from(seeds, hops, cap):
    """BFS out of a SEED SET in both directions, one level at a time.

    A seed set and not a single node because the plane's unit of reading is an
    ADDRESS, and a cell holds many lines: "what is around this point" has to
    start from all of them or it answers a question the click did not ask.

    Both directions are walked separately, never merged: a line that leads
    somewhere and a line that cites you are opposite facts, and a walk that
    pooled them would let a reader mistake an outgoing hop for a backlink."""
    out, inn = _adj()
    res = {"out": [], "in": []}
    for side, table in (("dst", out), ("src", inn)):
        frontier, visited = set(seeds), set(seeds)
        for depth in range(1, max(1, hops) + 1):
            edges = [e for n in frontier for e in (table.get(n) or ())]
            nodes, distinct = _hop_nodes(edges, side, cap)
            visited.update(n["hash"] for n in nodes)
            res["out" if side == "dst" else "in"].append(
                {"depth": depth, "nodes": nodes, "total": distinct})
            frontier = {n["hash"] for n in nodes}
            if not frontier:
                break
    return res


def reach(h, hops=2, cap=REACH_CAP, raw=False):
    """Walk out from one line in BOTH directions and report each hop.

    Every hop is an edge somebody wrote down, so a level is a claim the DATA
    makes — never a guess the viewer invented. A hop is deduped by LINE, not by
    edge: two verbs pointing at the same line are one neighbour with two
    reasons, and showing it twice teaches a reader nothing the second time."""
    h = (h or "").lower()
    if h in (derived().get("pruned_hashes") or ()) and not raw:
        return {"hash": h, "hash8": h[:8], "gone": True, "hops": hops}
    return dict({"hash": h, "hash8": h[:8], "gone": False, "hops": hops,
                 "shape": motif(h)}, **_reach_from({h}, hops, cap))


def explore_cell(rkey, hops=2, cap=REACH_CAP):
    """The same walk for a whole ADDRESS — the unit the plane actually clicks.

    Reports two altitudes at once, because a cell raises a question a single
    line cannot: the shapes of the lines INSIDE it (a cell holding five plain
    chats and a cell holding a definition everything cites look identical as
    one count) and the edges LEAVING the cell as one thing.

    Empty cells still answer: `shape` is null and the hop lists are empty, which
    is the honest report of an address nothing has been written to yet."""
    brief = _brief()
    seeds = [h for h, (_t, rk, _a, _k) in brief.items() if rk == rkey]
    shapes = {}
    for h in seeds:
        s = motif(h)["shape"]
        shapes[s] = shapes.get(s, 0) + 1
    inside = []
    for h in sorted(seeds, key=lambda h: -sum(_deg(h)))[:cap]:
        t, rk, app, kind = brief[h]
        o, i = _deg(h)
        inside.append({"hash": h, "hash8": h[:8], "rkey": rk, "app": app,
                       "kind": kind, "text": t, "out": o, "in": i})
    # The cell's own shape. Two mistakes are possible here and both were made:
    #
    # 1. Leaving `edge` out of the order. 13,572 lines are bonds, so an address
    #    holding only edges is common — and those were reported as "empty"
    #    while `count` beside them said 9. A shape label disagreeing with the
    #    count printed next to it is the one thing it must never do.
    # 2. Letting a node shape win over a MAJORITY of edges. A cell of 32 edge
    #    lines and 2 plain ones is an edge store; calling it "เกาะ" (nothing
    #    links here) is a claim the address does not support — it IS the linking.
    #
    # So: majority rules. Edges only decide the address when they outnumber the
    # lines that have a shape of their own.
    edges_n = shapes.get("edge", 0)
    nodes = {k: v for k, v in shapes.items() if k != "edge"}
    if edges_n > sum(nodes.values()):
        cell_shape = "edge"
    else:
        cell_shape = next((s for s in ("sink", "chain", "source", "isolated")
                           if nodes.get(s)), None)
    icon, label, why = SHAPES[cell_shape] if cell_shape else ("·", "ว่าง", "ไม่มี line อยู่ตรงนี้")
    return dict({"rkey": rkey, "point": rkey, "count": len(seeds),
                 "shape": cell_shape, "icon": icon, "label": label, "why": why,
                 "shapes": shapes, "lines": inside,
                 # over ALL seeds, not the displayed ones: a cross-platform count
                 # that quietly depended on `cap` would move when the reader
                 # changed the display, which is not a fact about the data
                 "cross": sum(motif(h)["cross"] for h in seeds)},
                **_reach_from(seeds, hops, cap))


def pattern_census():
    """The whole plane's link shape in one read.

    This is the view that makes the corpus legible at a glance: how many lines
    are islands, how many are springs, how many are wells — and which few lines
    everything else hangs off. None of those facts is visible from any single
    cell, which is why reading the plane cell by cell never reveals them.

    Counted from the adjacency maps so it is O(lines + edges), and cached with
    them: the shape of the graph only moves when an edge or a prune does."""
    out, inn = _adj()
    brief = _brief()
    shapes, by_app = {}, {}
    for h, (t, rk, app, kind) in brief.items():
        if rk is None:
            continue                      # not on the plane: nothing to place
        a = by_app.setdefault(app or "?", {"lines": 0, "edges": 0, "out": 0,
                                          "in": 0, "isolated": 0})
        if kind == "bond":
            # the edge, not a node: counted in its own bucket per app, so "เกาะ"
            # keeps meaning "nothing links to this" instead of "this is a link"
            # and by_app can still tell you which platform WROTE the graph
            shapes["edge"] = shapes.get("edge", 0) + 1
            a["edges"] += 1
            continue
        o, i = len(out.get(h) or ()), len(inn.get(h) or ())
        s = ("isolated" if not o and not i else "sink" if not o
             else "source" if not i else "chain")
        shapes[s] = shapes.get(s, 0) + 1
        a["lines"] += 1
        a["out"] += o
        a["in"] += i
        if s == "isolated":
            a["isolated"] += 1
    hubs = sorted(((n, h) for h, n in
                   ((h, len(inn.get(h) or ())) for h in brief) if n >= HUB_AT),
                  reverse=True)[:REACH_CAP]
    rels, cross = {}, 0
    for e in all_bonds():
        rels[e["rel"]] = rels.get(e["rel"], 0) + 1
        sa = (brief.get(e["src"]) or (None, None, None, None))[2]
        da = (brief.get(e["dst"]) or (None, None, None, None))[2]
        if sa and da and sa != da:
            cross += 1
    top = []
    for n, h in hubs:
        t, rk, app, kind = brief.get(h, ("", None, None, None))
        top.append({"hash": h, "hash8": h[:8], "in": n,
                    "out": len(out.get(h) or ()), "rkey": rk,
                    "app": app, "kind": kind, "text": t})
    return {"lines": len([1 for v in brief.values() if v[1] is not None]),
            "shapes": dict(sorted(shapes.items(), key=lambda kv: -kv[1])),
            "shape_meta": {k: {"icon": v[0], "label": v[1], "why": v[2]}
                           for k, v in SHAPES.items()},
            "by_app": dict(sorted(by_app.items(), key=lambda kv: -kv[1]["lines"])),
            "rels": dict(sorted(rels.items(), key=lambda kv: -kv[1])),
            "cross": cross, "edges": sum(len(v) for v in out.values()),
            "hubs": top, "hub_at": HUB_AT}


def timeline(as_app=None):
    """The plane's own tick: `seq` is a line's ordinal in append order,
    never wall clock — so replaying a tick gives exactly the same plane
    (iron law 7). `t` rides along only as a human-readable label.
    Ordinals are counted inside the reader's own view."""
    vis = [r for r in derived()["timeline"]
           if as_app is None or as_app in r[5] or r[4] == as_app]
    # app rides along as index 5 so a timeline strip can colour each tick by who
    # was writing: the tick layer only reads as "who, when" if it carries both
    return {"unit": "seq", "count": len(vis),
            "lines": [[i, r[0], r[1], r[2], r[3], r[7]] for i, r in enumerate(vis)]}


def _seq_of():
    """hash (full) -> append ordinal, built once per index rebuild.

    Only the feed asks for this. An ordinal is a POSITION, so it has to come
    from the log's own order — and derived() already walked that order, so the
    map costs one pass over a list that is already in memory. Lazily built
    because nothing else needs it, and dropped whenever the index rebuilds:
    a stale ordinal would number a chunk wrongly, which is a lie about where
    a line sits in the log."""
    d = derived()
    if d.get("seq_of") is None:
        d["seq_of"] = {r[6]: i for i, r in enumerate(d["timeline"])}
    return d["seq_of"]


def stamp_seq(msgs):
    """Stamp each line with its tick (`seq`) in place, for the feed.

    Decoration only: the line on disk is untouched (the hash never covered a
    `seq` field) and the map is derived, never stored. Because the number is
    append order and not wall clock, a fixed-width chunk (seq // CHUNK) keeps
    its number however much the log grows after it — the same property the
    plane's scrubber relies on.
    """
    seq = _seq_of()
    for m in msgs:
        m["seq"] = seq.get(m.get("hash"))
    return msgs


def _coarse_map(rkey0, block=12, raw=False):
    """L1 vision at one point: the containing 12x12 SPATIAL super-cell.
    Answers with a MAP ONLY — fine-cell addresses that hold lines, each
    with a count. No content ever crosses the read point. Want content?
    Point at a fine rkey from the map: new read.

    Same pruning as L2 (`raw=True` to see the noise) — L1 is the same plane
    coarser, so the two levels would lie about each other if they disagreed."""
    bw, br = _block_of(rkey0, block)
    src = derived()["cells"] if raw else derived()["cells_clean"]
    cells = {rk: n for rk, n in src.items() if _in_block(rk, bw, br, block)}
    return {"level": 1, "block": block, "origin": {"wedge": bw, "ring": br},
            "base": br * 144 + bw, "raw": bool(raw),
            "fine_cells": dict(sorted(cells.items()))}


VISION_BUDGET = 64  # lines one cell may hold before a read needs stacking


def _plane_map(raw=False):
    """L2 vision: the whole 144x144 plane read as one MAP.
    Spatial depth stops here — 20,736 = 144x144 has exactly three useful
    views (20736 cells / 144 blocks / 1 plane); deeper levels add nothing.
    Cells over budget are only FLAGGED, never expanded: expanding one is
    the caller's next explicit read. Addresses + counts, no content.

    Clean by default: lines the curator calls noise are PRUNED, so their
    cells are absent rather than faint. `raw=True` is the honest escape
    hatch — the plane as it was before pruning, for when you want to see
    what got removed and judge the threshold yourself."""
    d = derived()
    cells = dict(d["cells"] if raw else d["cells_clean"])
    return {"level": 2, "plane": 144 * 144, "budget": VISION_BUDGET,
            "fine_cells": dict(sorted(cells.items())),
            "over_budget": sorted(k for k, c in cells.items()
                                  if c > VISION_BUDGET),
            "raw": bool(raw), "pruned": 0 if raw else d.get("pruned", 0),
            "pruned_cells": 0 if raw else d.get("pruned_cells", 0)}


def _axis(line, depth):
    """Sub-axis of a line inside its own cell (§5.4 stacking).
    Recomputed from the line's own content at read time — the same bytes
    append() captured — so stacking needs ZERO storage change and every
    line already written gets it retroactively. Never stored, never in
    the hash: deterministic replay gives the same bucket every time.

    It CANNOT be `rkey(raw, phase=depth)`, and the reason is the whole point
    of this function. The capture walks `steps = max(len(data), 48)` bytes and
    reads `data[(i + phase) % len(data)]`; for any input of 48 bytes or more
    that visits EVERY byte exactly once, so `phase` changes only the order of
    the sum, not the sum. The sub-axis therefore came back identical at every
    depth and the stack never divided anything — measured on the live log, a
    132-line cell reported 132/1 at depths 1, 2 and 3 alike with `still_over`
    stuck at 1, so a cell past VISION_BUDGET could not be opened at all.

    A cell holds lines whose captures COLLIDED, i.e. whose sums are equal. No
    function of that same sum can separate them, at any phase. The sub-axis
    must use what the sum threw away — POSITION — so each byte is weighted by
    where it sits and by which depth is being asked. Two lines that summed
    alike still differ byte by byte, and that difference is what splits them."""
    raw = (line.get("app", "") + line.get("kind", "")
           + line.get("text", "")).encode("utf-8")
    if not raw:
        return 0
    F = 144
    ax = ay = 0
    for i, ch in enumerate(raw):
        n = ch & 0x0F
        dx, dy = _RDX[n] if n < 12 else (0, 0)
        w = 1 + ((i + depth) * depth) % 13
        ax += dx * w
        ay += dy * w
    return ((ay % F) * F + (ax % F)) % F


def plane_meta(raw=False):
    """The plane's second map: per occupied cell, who wrote there last, what
    KIND of line landed there last, and at which tick. Deliberately NOT folded
    into _plane_map() — a reader that only wants addresses and counts should
    not pay for a layer it will not draw. Addresses and labels only: no line
    content reaches this.

    Follows the same pruning as the counts it is drawn over: with `raw=False`
    the who/what/when layer describes the pruned plane, so a colour mode can
    never attribute a cell to a writer whose only line in it was noise."""
    d = derived()
    if raw:
        return {"level": 2, "last_app": d["cell_app"],
                "last_kind": d["cell_kind"],
                "by_kind": d["by_kind"], "by_app": d["by_app"],
                "last_seq": d["cell_seq"], "seq_max": len(d["timeline"]),
                "raw": True, "pruned": 0}
    return {"level": 2, "last_app": d["cell_app_clean"],
            "last_kind": d["cell_kind_clean"],
            "by_kind": d["by_kind_clean"], "by_app": d["by_app_clean"],
            "last_seq": d["cell_seq_clean"], "seq_max": len(d["timeline"]),
            "raw": False, "pruned": d.get("pruned", 0)}


def sub_map(rkey0, depth=1, path=()):
    """Stacked read: ONE overflowing cell split by axis `depth`.
    Same rule as L1 — a MAP only (sub-addresses + counts), no content.
    Depth 1 gives 144 sub-cells; every further depth multiplies by 144,
    so a cell absorbs 144^d x budget lines without the plane growing.
    `path` = sub-cells already chosen at shallower depths — a point
    inside a point. Each step down is the caller's own explicit read."""
    path = tuple(path or ())
    cells = {}
    for m in _cell_lines(rkey0):
        if any(_axis(m, i + 1) != v for i, v in enumerate(path)):
            continue
        k = _axis(m, depth)
        cells[k] = cells.get(k, 0) + 1
    over = sorted(k for k, c in cells.items() if c > VISION_BUDGET)
    return {"level": "0.%d" % depth, "cell": rkey0, "depth": depth,
            "path": list(path), "budget": VISION_BUDGET,
            # a map is only drillable if its keys live at ONE known depth,
            # so the stack advances one axis per read and says whether the
            # reader still needs another step (§5.4: walk, don't teleport)
            "still_over": len(over), "next_depth": depth + 1,
            "sub_cells": dict(sorted(cells.items()))}


def _depth_for(lines, start=1):
    """Cheapest depth at which every bucket reads under budget.
    Answered by reading the lines — the data still knows nothing.
    `start` is the first fresh axis: once depth 1 was chosen as a step,
    splitting again on depth 1 would only repeat the same bucket."""
    depth = max(1, start)
    while depth < 8:
        counts = {}
        for m in lines:
            k = _axis(m, depth)
            counts[k] = counts.get(k, 0) + 1
        if max(counts.values()) <= VISION_BUDGET:
            return depth
        depth += 1
    return depth


def _needed_depth(lines):
    return _depth_for(lines, 1)


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


def sighting_once(rkey0, level=0, app="system"):
    """Declare a read, but only when it ADDS information.

    `pointers_at()` keeps the latest sighting per app, so re-declaring the
    address an app is already reading writes a line that changes nothing —
    and a view that re-reads on every render would write one per render.
    That is the difference between a record of where people looked and a
    log of how often a page was drawn.

    Returns None when the read already stands, else the new line's body.
    A read at a NEW point always writes, because it supersedes."""
    for s in pointers_at(raw=True):
        if s["app"] == app:
            # one current read per app: only this app's OWN latest matters
            return None if (s["point"] == rkey0 and s["level"] == level) else sighting(rkey0, level, app)
    return sighting(rkey0, level, app)


def _address_lives(pt, lv, d):
    """Does the address a sighting names still hold anything the plane draws?

    A sighting is `kind='pointer'`, so prune never removes the LINE — but the
    address it names is a different thing from the line that named it. Level 0
    names a cell, level 1 its 12x12 block, level 2 the whole plane, so the
    question is asked at the same grain the read was declared at: a sighting
    into an emptied cell points at nothing even though its own line is intact."""
    clean = d["cells_clean"]
    if lv == 0:
        return pt in clean
    if lv == 1:
        return ((pt % 144) // 12, (pt // 144) // 12) in d.get("blocks_clean", ())
    return bool(clean)


def pointers_at(rkey0=None, level=None, as_app=None, raw=False):
    """Who declared a read where — derived from lines, nothing stored.
    Latest sighting per app wins. Kill the server mid-read and this still
    answers the same, because there is no server state to lose.

    Sightings into an address that no longer holds a clean line are marked
    `dangling` and held back by default: a sighting is drawn where someone
    read, and drawing it over a vanished address would put a mark on empty
    space. `raw=True` returns them WITH the mark rather than silently."""
    d = derived()
    # ONE PASS, and the order of the two steps is the whole contract: reduce
    # each app to its LATEST read first, then filter to the point asked for.
    # Filtering first would walk back through an app's history and resurrect
    # reads it has already moved on from — a filtered read would then claim
    # somebody is at a cell they left, and the plane would keep their circle
    # there. A sighting supersedes its app's previous one wherever it was.
    latest = {}
    for m in d["pointers"]:
        if not _visible(m, as_app):
            continue
        p = (m.get("text") or "").split()
        if len(p) != 2 or not p[0].startswith("point="):
            continue
        try:
            pt = int(p[0][6:])
            lv = int(p[1].split("=", 1)[1])
        except (ValueError, IndexError):
            continue
        a = m.get("app")
        if a not in latest or m.get("t", 0) >= latest[a]["t"]:
            latest[a] = {"app": a, "point": pt, "level": lv,
                         "t": m.get("t"), "hash": m.get("hash")}
    out = {a: r for a, r in latest.items()
           if (rkey0 is None or r["point"] == rkey0)
           and (level is None or r["level"] == level)}
    for r in out.values():
        r["dangling"] = not _address_lives(r["point"], r["level"], d)
    rows = sorted(out.values(), key=lambda x: -x["t"])
    return rows if raw else [r for r in rows if not r["dangling"]]


def open_pointer(rkey0, level=0, app="system", record=False, depth=None,
                 path=None, as_app=None, raw=False):
    """Open ONE pointer at ONE point and close it (§5.3).
    L0 = content of that cell only · L1 = containing 12x12 block as a map ·
    L2 = whole plane as a map. If the pointed cell is over budget the
    answer carries a stacked sub-map instead of drowning the reader, and
    `path` walks one sub-cell down at a time (a point inside a point) —
    each step still answers a map until the reader asks for content.
    Nothing about the read is kept unless the caller asks for a sighting,
    and no other caller shares it — that is iron law 5 exactly.
    Maps are address-only, so they carry no content and need no visibility
    filter; only L0 content is filtered by as_app."""
    path = tuple(path or ())
    if level == 2:
        vis = _plane_map(raw=raw)
    elif level == 1:
        vis = _coarse_map(rkey0, raw=raw)
    else:
        lines = _cell_lines(rkey0, as_app)
        for i, v in enumerate(path):
            lines = [m for m in lines if _axis(m, i + 1) == v]
        vis = {"level": 0, "point": rkey0, "count": len(lines),
               "lines": lines}
        if path:
            vis["path"] = list(path)
        if len(lines) > VISION_BUDGET:
            vis["stacked"] = sub_map(rkey0, depth or len(path) + 1, path=path)
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
    assert b["rkey"] == app_rkey(("opencode" + "bond" + b["text"]).encode("utf-8"), "opencode"), "bond has own rkey from content"
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
    bw, br = cm["origin"]["wedge"], cm["origin"]["ring"]
    assert cm["level"] == 1 and cm["block"] == 12, "L1 = exactly one 12x12 super-cell"
    assert bw <= b2["rkey"] % 144 < bw + 12, "the square frames the pointed cell in wedge"
    assert br <= b2["rkey"] // 144 < br + 12, "the square frames the pointed cell in ring"
    expect = sum(1 for m in tail(None) if m.get("rkey") is not None
                 and _in_block(m["rkey"], bw, br))
    # exact correspondence is the RAW map's property. The pruned map answers a
    # narrower question — "which lines here are worth an address" — so it may
    # only ever count fewer, and the difference must be the prune total.
    cmr = _coarse_map(b2["rkey"], raw=True)
    assert expect >= 1 and sum(cmr["fine_cells"].values()) == expect, "the raw map counts exactly the lines in this super-cell"
    assert sum(cm["fine_cells"].values()) <= expect, "the pruned map never counts more than the raw one"
    assert all(_in_block(k, bw, br) for k in cm["fine_cells"]), "every address in the map lies inside the square"
    assert all(isinstance(k, int) for k in cm["fine_cells"]), "map is addresses only — no content"
    # bonds + timeline as plane data: every edge places both ends itself
    edges = all_bonds()
    assert len(edges) == 1 and edges[0]["rel"] == "produces", "all_bonds gives every live edge"
    src_line = next(m for m in tail(None) if m["hash"] == edges[0]["src"])
    assert edges[0]["src_rkey"] == src_line["rkey"], "edge resolves src to the rkey it lives in"
    assert edges[0]["dst_rkey"] == next(m for m in tail(None) if m["hash"] == edges[0]["dst"])["rkey"], "edge resolves dst too — both ends place themselves"
    assert edges[0]["dst8"] == edges[0]["dst"][:8] and len(edges[0]["dst"]) == 64, "edge carries short hashes for display"
    tl = timeline()
    assert tl["unit"] == "seq" and tl["count"] == len(tail(None)), "timeline counts every line the reader can see"
    assert [r[0] for r in tl["lines"]] == list(range(tl["count"])), "ticks are dense ordinals in append order"
    assert tl["lines"][-1][2] == tail(1)[0]["rkey"], "the last tick is the newest line"
    assert tl["lines"][-1][4] == tail(1)[0]["hash"][:8], "each tick carries its line's short hash"
    # the feed's chunk layer: a line's tick is its ordinal, never wall clock, so
    # a fixed-width chunk keeps its number however much the log grows after it
    st = stamp_seq(tail(3))
    assert [m["seq"] for m in st] == [tl["count"] - 3, tl["count"] - 2, tl["count"] - 1], "seq is the append ordinal"
    assert [m["hash"] for m in st] == [m["hash"] for m in tail(None)][-3:], "tick N is the Nth line from the start of the log"
    assert stamp_seq([]) == [], "an empty window stamps nothing"
    assert _seq_of() is _seq_of(), "the ordinal map is built once per rebuild"
    _DERIVED["key"] = None
    assert stamp_seq(tail(1))[0]["seq"] == len(tail(None)) - 1, "a rebuild renumbers ordinals identically"
    assert _DERIVED["seq_of"][tail(1)[0]["hash"]] == len(tail(None)) - 1, "and the rebuilt map is exact, not a prefix match"
    # the plane may not prune what the feed was told not to hide, and vice
    # versa: one rule table, two consequences (see CURATOR_PRUNE)
    # ---- curator: the feed's filter, and the lines it may not touch -------
    # Judged on explicit dicts, not on whatever the demo log happens to hold:
    # a rule table tested through the log would only ever test the log.
    _noise = {"app": "probe", "kind": "chat", "text": "test", "to": "all"}
    _real = {"app": "opencode", "kind": "chat", "to": "all",
             "text": "test passed 100/100 after fixing the SSE cursor"}
    assert len({r[0] for r in CURATOR_RULES}) == len(CURATOR_RULES), "every rule has its own name — the report groups by name"
    assert len(curate([_noise])) == 0, "a bare test word is noise"
    assert curator_hits(_noise)[0] >= CURATOR_HIDE, "and it says so in points"
    assert len(curate([_real])) == 1, "a sentence about testing is a message, not a test"
    assert curate([_noise], hide_at=5) == [_noise], "the threshold is the caller's: raise it and nothing is hidden"
    assert curate([_noise], mode="raw") == [_noise], "raw mode hides nothing, ever"
    assert len(curate([{"app": "a", "kind": "chat", "text": "ping", "tags": ["hermes"]}])) == 1, "a mention is never hidden, however noisy"
    assert curator_protect({"app": "a", "text": "test", "to": "hermes"}) == "dm", "a DM is the reader's business, not the curator's"
    assert curator_protect({"app": "a", "kind": "bond", "text": "test"}) == "kind", "a bond carries the graph: never hidden"
    assert curator_protect({"app": "a", "text": "test\nsecond line"}) == "long", "a multi-line message is content whatever it contains"
    assert curator_protect({"app": "a", "text": "test: DECISION here"}) == "marker", "a handoff/decision is never eaten, even in probe form"
    assert len(curate([{"app": "a", "text": "test: DECISION here"}])) == 1, "and that protection beats a real rule hit"
    _rep = [{"app": "a", "kind": "chat", "text": "same"}] * 3
    _kept, _cut = curate(_rep, dropped=True)
    assert len(_kept) == 1 and len(_cut) == 2, "a repeated line is hidden from the second copy on"
    assert "repeat" in _cut[0]["rules"], "and the reason is on the cut, not implied"
    assert _cut[0]["text"] == "same" and _cut[0]["hash"] is None, "the cut lists what it removed"
    _mixed = [_noise, _real, {"app": "a", "text": "ses_f486eb1c4ffeZK7E2ycq8LZ8qF"}]
    assert len(curate(_mixed)) == len(curate(_mixed)), "the same window is judged the same way twice"
    assert _mixed[0] == _noise, "a verdict never mutates the line it judged"
    _r = curator_report(_mixed, hide_at=CURATOR_HIDE)
    assert _r["lines"] == 3 and _r["hidden"] + _r["kept"] == 3, "the report accounts for every line in the window"
    assert _r["hidden"] == 2 and _r["examples"], "and shows examples, so a human can disagree with it"
    assert "sess-id" in _r["by_rule"] and "test-word" in _r["by_rule"], "counted per rule, not as one opaque number"
    assert len(_r["hidden_hashes"]) == _r["hidden"], "every hidden line is listed by its own address"
    _wire = stamp_cur([dict(_noise), dict(_real)])
    assert _wire[0]["cur"]["h"] is True and _wire[0]["cur"]["s"] >= CURATOR_HIDE, "the wire carries the verdict, not just the text"
    assert _wire[1]["cur"]["h"] is False and "cur" in _wire[1], "and every line carries one — the client never has to ask"
    assert "cur" not in _noise, "stamping decorates the window it was given, nothing else"
    # ---- prune: the SAME rules, applied to the plane's ADDRESSES ----------
    # A hidden line keeps its cell; a pruned line gives the cell up. The
    # verdict is one function, so these assertions are about the consequence.
    _pv = {"app": "probe", "kind": "chat", "text": "test", "to": "all"}
    _s1 = _prune_verdict(dict(_pv), {}, 0)
    assert _s1[0] >= CURATOR_PRUNE and _s1[2] is None, "a bare test word is prunable"
    assert _s1[1] == curator_hits(_pv)[1], "the plane judges with the feed's rules, not a copy of them"
    assert _prune_verdict({"app": "a", "kind": "chat", "text": "test", "to": "hermes"}, {}, 0)[2] == "dm", "a DM is pruned by neither"
    assert _prune_verdict({"app": "a", "kind": "bond", "text": "test"}, {}, 0)[2] == "kind", "a bond keeps its address whatever it says"
    # kind is matched by VALUE, never by spelling: 'CHAT' is chat. The old
    # case-sensitive compare promoted every 'CHAT' to a protected kind and
    # silently switched the whole curator off for the writers that used it.
    assert curator_protect({"app": "a", "kind": "CHAT", "text": "test"}) is None, "'CHAT' is the same kind as 'chat'"
    assert curator_protect({"app": "a", "kind": "BOND", "text": "test"}) == "kind", "and 'BOND' is still a bond"
    _rseen = {}
    _first = _prune_verdict({"app": "a", "kind": "chat", "text": "same"}, _rseen, 0)
    _second = _prune_verdict({"app": "a", "kind": "chat", "text": "same"}, _rseen, 1)
    assert "repeat" not in _first[1] and "repeat" in _second[1], "the original keeps the address, the echo gives it up"
    # an empty line is a stub by the same rule that catches '...' — it carries
    # nothing, so it has no address to hold (and a heartbeat is exactly that)
    assert _prune_verdict({"app": "a", "kind": "chat", "text": ""}, {}, 0)[0] >= CURATOR_PRUNE, "a line with no content holds no address"
    assert _prune_verdict({"app": "a", "kind": "chat", "text": ""}, {}, 0)[2] is None, "and nothing protects it"
    # the report is read-only, covers the whole log, and names the rule that
    # did the removing — the question "should we prune?" needs that answer
    _prep = curator_prune_report(
        [dict(_pv), {"app": "a", "kind": "chat", "text": "HANDOFF: real work"}])
    assert _prep["lines"] == 2 and _prep["pruned"] == 1 and _prep["kept"] == 1, "the report accounts for every line"
    assert "test-word" in _prep["by_rule"], "and says WHICH rule removed it"
    assert _prep["examples"][0]["text"] == "test", "with the text, so a human can disagree"
    assert [m for m in _prep["examples"]][0]["rules"], "and the rules on that example"
    # the plane map is clean by default and raw on request — and never richer
    append("probe", "chat", "ping")
    append("probe", "chat", "DECISION: the plane keeps this address")
    _pm, _pmr = _plane_map(), _plane_map(raw=True)
    assert _pm["pruned"] >= 1 and _pm["raw"] is False, "the plane prunes by default and reports how much"
    assert _pmr["raw"] is True and _pmr["pruned"] == 0, "raw is the unpruned plane, and says so"
    assert len(_pmr["fine_cells"]) >= len(_pm["fine_cells"]), "pruning removes cells, never invents them"
    assert _coarse_map(0, raw=True)["raw"] is True, "L1 follows the same switch as L2 — the levels cannot disagree"
    assert plane_meta(raw=True)["pruned"] == 0 and plane_meta()["raw"] is False, "the who/when layer prunes with the counts it describes"
    # the kind map is a layer like the other two, so it inherits the same
    # obligation: with the prune on it must not describe a cell whose only
    # lines were removed, or a colour mode paints the kind of a line that is
    # no longer on the plane.
    _pk_all, _pk_clean = plane_meta(raw=True)["last_kind"], plane_meta()["last_kind"]
    assert set(_pk_clean) <= set(_pk_all), "the pruned kind layer names a SUBSET of the raw one — never a cell the raw plane does not have"
    assert set(_pk_clean) == set(_pm["fine_cells"]), "and exactly the cells the pruned counts kept — the layer follows the map it is drawn over"
    _pstats = plane_prune_stats()
    assert _pstats["cells_all"] >= _pstats["cells_clean"], "the stats report both maps, not just the pretty one"
    assert _pstats["kept"] + _pstats["pruned"] == _pstats["lines"], "and account for every line"
    # The index is BOTH a fold (one line per append) and a rebuild. Two code
    # paths, one answer: absorbing only the raw map left the pruned one
    # describing the previous log while the key said it was current, so the
    # plane kept drawing a line it had already pruned. This is that bug.
    append("probe", "chat", "pong")
    _folded = plane_prune_stats()
    _DERIVED["key"] = None                 # force the other code path
    _rebuilt = plane_prune_stats()
    assert _folded == _rebuilt, "the fold and the rebuild agree on every prune number"
    assert _folded["pruned"] == _pstats["pruned"] + 1, "and the fold really counted the new line"
    # ---- prune reaches the EDGES, not just the cells ----------------------
    # A bond and a sighting are `kind='bond'`/`'pointer'`, so prune never
    # removes their LINE. But the address they name is a different thing from
    # the line that names it, and an edge drawn into a vanished address is the
    # bug this closes: the map was pruned while its edges were not.
    _sig = append("opencode", "chat", "DECISION: an edge case is its own endpoint")
    _bad = append("probe", "chat", "ping")
    append("opencode", "bond", "%s uses %s" % (_sig["hash"], _bad["hash"]))
    append("hermes", "bond", "%s produces %s" % (_sig["hash"], _sig["hash"]))
    sighting(_bad["rkey"], 0, "edge_src")
    sighting(_sig["rkey"], 0, "edge_dst")
    _live_e, _all_e = all_bonds(), all_bonds(raw=True)
    _stray = [e for e in _all_e if e["dangling"]]
    assert _stray, "a bond into a pruned line is marked dangling"
    assert all(_bad["hash"] in (e["src"], e["dst"]) for e in _stray), "and only the edge whose endpoint was pruned"
    assert len(_live_e) < len(_all_e), "the default edge read holds the stranded one back"
    assert all(not e["dangling"] for e in _live_e), "every edge it returns has both ends intact"
    assert len(bonds_for(_sig["hash"][:8], raw=True)) >= 2, "bonds_for sees the stranded edge when asked"
    assert len(bonds_for(_sig["hash"][:8])) < len(bonds_for(_sig["hash"][:8], raw=True)), "and drops it when not"
    _live_p, _all_p = pointers_at(), pointers_at(raw=True)
    assert len(_live_p) < len(_all_p), "a sighting into an emptied cell is held back by default"
    assert any(p["dangling"] for p in _all_p) and not any(p["dangling"] for p in _live_p), "the mark is what separates them"
    assert any(p["app"] == "edge_dst" for p in _live_p), "a sighting into a live cell is kept"
    _st3 = plane_prune_stats()
    assert _st3["dangling_bonds"] >= 1 and _st3["dangling_sightings"] >= 1, "the stats count stranded edges, not just cells"
    assert _st3["bonds_all"] >= _st3["dangling_bonds"], "and never report more stranded than exist"
    # The edge layer is cached against (bonds_rev, prune_rev) because pruning can
    # strand an edge WITHOUT the edge set changing — a key that watched only
    # bonds_rev would keep drawing into a vanished address after a fold. Both
    # code paths must agree about that.
    _anchor = append("hermes", "chat", "DECISION: anchor for the edge-cache fold test")
    append("hermes", "bond", "%s produces %s" % (_anchor["hash"], _anchor["hash"]))
    append("probe", "chat", "ping")          # a fold that prunes
    _fold_edges = [(e["hash"], e["dangling"]) for e in all_bonds(raw=True)]
    _DERIVED["key"] = None                    # force the other code path
    _new_edges = [(e["hash"], e["dangling"]) for e in all_bonds(raw=True)]
    assert _fold_edges == _new_edges, "the edge cache survives a fold that prunes"
    assert len(_fold_edges) == len(_all_e) + 1, "and the fold really saw the new edge"
    # the filter must not have followed us into the reads that need completeness
    append("probe", "chat", "test")
    assert tail(1)[0]["text"] == "test", "tail() stays raw — the curator is a display edge"
    assert view(3, clean=True)[-1] != view(3)[-1], "view(clean=True) hides it, the raw view still shows it"
    assert len(view(3, clean=True)) <= len(view(3)), "cleaning removes lines, never invents them"
    # A kind is a VALUE, never a spelling. The corpus writes CHAT/DECISION in
    # caps and bond/pointer/block in lower, so `kind == "chat"` was a compare
    # that never matched anything: every plain chat line printed as
    # `hermes/CHAT` instead of `hermes`. Both spellings must read as chat.
    _pl_chat = pretty({"app": "hermes", "kind": "CHAT", "text": "hi", "t": 0})
    _pl_low = pretty({"app": "hermes", "kind": "chat", "text": "hi", "t": 0})
    assert "hermes/CHAT" not in _pl_chat and "hermes/chat" not in _pl_low, "a plain chat line is labelled by its writer alone, whatever the spelling"
    assert _pl_chat == _pl_low, "the two spellings of the same kind produce the same line"
    _pl_dec = pretty({"app": "hermes", "kind": "DECISION", "text": "x", "t": 0})
    assert "hermes/DECISION" in _pl_dec, "and a kind that is NOT chat still says what it is"
    # living memory bridge (dry run — no db write in demo)
    append("opencode", "chat", "DECISION: pool stays file-first, server optional")
    lifted = memory_bridge(dry_run=True)
    assert any("DECISION" in c for c, _, _ in lifted), "bridge lifts decisions"
    # multi-pointer vision read: at the actual read point only
    r0 = tail(50)[0]["rkey"]
    nv = neighbors(r0)
    assert nv == [(r0, [m for m in tail(None) if m.get("rkey") == r0])], "vision = exactly the pointed cell"
    # §5.3 pointer: per-read and ephemeral; awareness only as data
    p0 = tail(50)[0]["rkey"]
    assert pointers_at(p0) == [], "no pointer state exists before any read"
    v0 = open_pointer(p0, level=0, app="codex")
    assert v0["count"] == len([m for m in tail(None) if m.get("rkey") == p0]), "L0 = content of the pointed cell only"
    assert pointers_at(p0) == [], "a read leaves no trace unless the caller declares one"
    sg = sighting(p0, level=0, app="codex")
    assert sg["kind"] == "pointer", "sighting is data, not server state"
    assert sg["rkey"] == app_rkey(("codex" + "pointer" + sg["text"]).encode("utf-8"), "codex"), "sighting chained/address like any line"
    seen = pointers_at(p0)
    assert len(seen) == 1 and seen[0]["app"] == "codex" and seen[0]["level"] == 0, "awareness derived from lines alone"
    assert pointers_at(0) == [], "points nobody read stay silent"
    assert pointers_at(p0, level=1) == [], "level is part of the point"
    sg_hermes = sighting(p0, level=0, app="hermes")
    assert {s["app"] for s in pointers_at(p0)} == {"codex", "hermes"}, "each pointer stays its own — no central pointer"
    assert len(pointers_at(p0)) == 2, "two readers, two independent pointers at the same point"
    # sighting_once: a view re-reading the same address must not write a line
    # per render. Only a NEW address supersedes, because pointers_at() keeps
    # the latest read per app.
    _before_once = len(tail(None))
    assert sighting_once(p0, 0, "codex") is None, "re-declaring the address an app already reads adds nothing"
    assert len(tail(None)) == _before_once, "and writes no line — the count is proof"
    _moved = sighting_once(p0 + 1, 0, "codex")
    assert _moved is not None and _moved["kind"] == "pointer", "a read at a NEW point supersedes, so it writes"
    # raw=True because this asserts IDENTITY, not visibility: p0+1 may well be
    # an empty cell, and a sighting into one is marked dangling and held back
    _now = {s["app"]: s for s in pointers_at(raw=True)}
    assert _now["codex"]["point"] == p0 + 1, "the newer read is the one that stands"
    _left = pointers_at(p0, raw=True)
    assert [s["app"] for s in _left] == ["hermes"], "codex's move leaves another app's pointer alone"
    assert _left[0]["hash"] == sg_hermes["hash"], "and it is still the same line it always was"
    # §5.4 RDH 144: one fixed leaf plane; overflow STACKS by phase axis
    pm = _plane_map()
    assert pm["level"] == 2 and pm["plane"] == 20736, "L2 = whole plane, the spatial ceiling"
    # completeness is the RAW map's promise (no window, no 1000-line cap);
    # the pruned map may only ever count fewer, and it must say by how much
    _pmr = _plane_map(raw=True)
    assert sum(_pmr["fine_cells"].values()) == len([m for m in tail(None) if m.get("rkey") is not None]), "the raw plane map counts every addressed line — no 1000-line cap"
    assert sum(pm["fine_cells"].values()) + pm["pruned"] == sum(_pmr["fine_cells"].values()), "the pruned map plus what it pruned is the raw map, exactly"
    assert pm["over_budget"] == [], "nothing over budget here — stacking stays lazy"
    assert rkey(b"same bytes", phase=1) != rkey(b"same bytes", phase=0), "phase is an independent axis in the same plane"
    assert rkey(b"same bytes") == rkey(b"same bytes", phase=0), "phase 0 = the locked capture, unchanged"
    # the sub-axis must be a real axis: rkey(raw, phase=depth) was NOT one,
    # because the capture visits every byte once for any input >= 48 bytes,
    # so the displacement sum — and therefore the sub-cell — was identical at
    # every depth and a cell past budget could never be opened
    _ln = {"app": "load", "kind": "chat", "text": "same text 7"}
    assert len({_axis(_ln, d) for d in range(1, 6)}) > 1, "depth must change the sub-axis"
    assert all(0 <= _axis(_ln, d) < 144 for d in range(1, 6)), "sub-axis stays inside a cell"
    def _buckets(n, depth):
        c = {}
        for i in range(n):
            k = _axis({"app": "load", "kind": "chat",
                       "text": "same text %d" % i}, depth)
            c[k] = c.get(k, 0) + 1
        return c

    _spread = _buckets(200, 1)
    assert max(_spread.values()) < VISION_BUDGET, "one depth splits an over-budget pile under budget"
    # 400 lines that differ only by a counter — the worst case for a
    # position-weighted sum, and it still opens under budget in one step.
    # NOT uniform: near-identical lines keep colliding more than distinct
    # ones (max 40 here against max 2 on real cells). A spread, not a hash.
    _worst = _buckets(400, 1)
    assert max(_worst.values()) < VISION_BUDGET, "even counter-only siblings open under budget"
    assert len(_worst) > 20, "and spread across buckets instead of one pile"
    # app_phase: different apps scatter the same text to different cells
    assert app_phase("opencode") != app_phase("hermes"), "different apps get different phases"
    assert 0 <= app_phase("test") < 16, "phase in valid range"
    assert app_rkey(b"DECISION: use mmap", "opencode") != app_rkey(b"DECISION: use mmap", "hermes"), "same text, different apps -> different cells"
    pcell = tail(50)[0]["rkey"]
    sub = sub_map(pcell, depth=1)
    assert sub["level"] == "0.1" and sum(sub["sub_cells"].values()) == len([m for m in tail(None) if m.get("rkey") == pcell]), "stacked read splits exactly that one cell, addresses only"
    assert all(isinstance(k, int) for k in sub["sub_cells"]), "stacked read is a map, never content"
    load = [{"app": "load", "kind": "chat", "text": "line %d" % i} for i in range(200)]
    d = _needed_depth(load)
    counts = {}
    for m in load:
        k = _axis(m, d)
        counts[k] = counts.get(k, 0) + 1
    assert max(counts.values()) <= VISION_BUDGET, "stacking splits until every bucket reads under budget"
    assert _needed_depth(load) == d, "depth is deterministic — same lines, same split axis"
    b1 = {}
    for m in load:
        b1.setdefault(_axis(m, 1), []).append(m)
    pick = b1[sorted(b1)[0]]
    assert _depth_for(pick, 2) == 2, "after choosing axis 1, the next split opens a fresh axis"
    sm_path = sub_map(pcell, 2, path=(sorted(sub["sub_cells"])[0],))
    assert sum(sm_path["sub_cells"].values()) <= sum(sub["sub_cells"].values()), "a path narrows the read, never widens it"
    big = open_pointer(pcell, level=0, app="load")
    assert "stacked" not in big, "a cell under budget is never split — breathing only when needed"
    import shutil
    ep1 = flush_emit()
    assert ep1 is not None, "first emit fires"
    # every line the log holds, whatever this demo appended — a hardcoded
    # count here only measured how many appends the test happened to make
    assert len(json.load(open(ep1, encoding="utf-8"))) == len(tail(None)), "the first emit carries the whole log"
    assert flush_emit() is None, "emit skips when unchanged"
    append("codex", "chat", "third msg")
    ep3 = flush_emit()
    assert ep3 is not None, "emit fires on new line"
    assert len(json.load(open(ep3, encoding="utf-8"))) == 1, "emit only new"
    # ---- scale: the bug this fixed (measured by bond_scale.py) -------------
    # 1,200 lines with 40 edges spread through them, written straight to the
    # log (append() would fsync 1,200 times). The old capped read returned
    # 200 of 1,000 edges and could not resolve the ends of the ones it kept,
    # silently — so completeness is what gets asserted here, at a size where
    # a cap would actually bite.
    import json as _json
    n_chat, n_bond = 1200, 40
    step = n_chat // n_bond
    made, first_bond, first_end = 0, None, None
    with open(LOG, "w", encoding="utf-8") as fh:
        prev, t, hs = "GENESIS", 1.0, []
        for i in range(n_chat):
            # a bond needs two lines to point at, so the first few are chats
            kind = "bond" if (hs and i % step == 0 and made < n_bond) else "chat"
            text = ("%s uses %s" % (hs[len(hs) // 2], hs[-1])) if kind == "bond" else "l%d" % i
            raw = ("bench" + kind + text).encode("utf-8")
            h = hashlib.sha256((prev + "bench" + kind + "all" + text).encode("utf-8")).hexdigest()
            fh.write(_json.dumps({"v": 3, "t": t, "app": "bench", "kind": kind,
                                  "text": text, "to": "all", "tags": [], "prev": prev,
                                  "rkey": app_rkey(raw, "bench"), "enc": app_rkey(raw, "bench") % 1440, "hash": h},
                                 ensure_ascii=False) + "\n")
            if kind == "bond":
                made += 1
                if first_bond is None:
                    first_bond, first_end = h, hs[len(hs) // 2]
            else:
                hs.append(h)
            prev, t = h, t + 0.001
    assert n_chat > 1000, "the fixture must exceed the old cap for this to mean anything"
    assert len(tail(None)) == n_chat == len(tail(10 ** 9)), "asking for more than exists still reads the whole log"
    assert tail(5) == tail(None)[-5:], "the backwards reader returns the newest lines in the same order"
    assert made >= n_bond - 1, "the fixture wrote the edges it meant to write"
    big = all_bonds()
    assert len(big) == made, "every edge is in the graph — the capped read returned a fifth of them"
    assert all(e["src_rkey"] is not None and e["dst_rkey"] is not None for e in big), "and every end resolves to its cell"
    # query by an ENDPOINT (that is what bonds_for takes) that sits near the
    # start of the log — the very edge a 1000-line window used to hide
    near_start = [e for e in bonds_for(first_end[:8]) if e["hash"] == first_bond]
    assert near_start, "the oldest edge is still visible from its own endpoint"
    assert near_start[0]["rel"] == "uses", "and it is still the edge that was written"
    assert sum(_plane_map()["fine_cells"].values()) == n_chat, "the plane map counts every line, not the newest 1000"
    assert timeline()["count"] == n_chat, "the tick timeline covers the whole log"
    assert len(derived()["hash_rkey"]) == n_chat, "the derived index sees every line"
    _DERIVED["key"] = None
    assert len(all_bonds()) == made, "the derived index rebuilds from scratch and agrees"
    # ---- the index is a fold: a new line is an update, not a rebuild ------
    # Measured in bond_scale.py — a rebuild at 55k lines is ~460 ms, and paying
    # it on the refresh after every message is the only real cost left here.
    d0 = derived()
    n0, cells0 = len(d0["timeline"]), sum(d0["cells"].values())
    fresh_rk = app_rkey(("bench" + "chat" + "absorbed line").encode("utf-8"), "bench")
    append("bench", "chat", "absorbed line")
    d1 = derived()
    assert d1 is d0, "an append feeds the cached index instead of discarding it"
    assert len(d1["timeline"]) == n0 + 1, "and the new line is in the index at once"
    assert sum(d1["cells"].values()) == cells0 + 1, "counted on the plane too"
    assert d1["cells"].get(fresh_rk, 0) >= 1, "in the cell its rkey says it is in"
    assert d1["cell_app"].get(fresh_rk) == "bench", "and the plane remembers WHO wrote here last"
    assert d1["cell_kind"].get(fresh_rk) == "chat", "and WHAT KIND of line landed here last — the same question about the same edge of the log"
    assert d1["cell_seq"].get(fresh_rk) == len(d1["timeline"]) - 1, "and at which tick — recency is derived, never tracked"
    assert len(plane_meta()["last_app"]) == len(d1["cells"]), "one owner per occupied cell, no cell left unowned"
    assert set(plane_meta()["last_kind"]) == set(d1["cells"]), "and one kind per cell from the same fold — a map that names some cells and not others is a map with a hole in it"
    assert set(plane_meta()["last_seq"]) == set(d1["cells"]), "and one tick per cell from the same fold"
    want_cells, want_tl = dict(d1["cells"]), len(d1["timeline"])
    want_app, want_seq = dict(d1["cell_app"]), dict(d1["cell_seq"])
    want_kind = dict(d1["cell_kind"])
    want_kind_c = dict(d1["cell_kind_clean"])
    want_bk, want_bk_c = ({k: dict(v) for k, v in d1["by_kind"].items()},
                          {k: dict(v) for k, v in d1["by_kind_clean"].items()})
    want_ba, want_ba_c = ({k: dict(v) for k, v in d1["by_app"].items()},
                          {k: dict(v) for k, v in d1["by_app_clean"].items()})
    # The per-kind maps must PARTITION the plane: every occupied cell appears
    # under at least one kind, and no cell appears that the counts do not have.
    # A per-kind map that loses a cell is a filter that hides a real address
    # with nothing on screen saying so — the §18 failure in a new place.
    _union = {rk for m_ in d1["by_kind"].values() for rk in m_}
    assert _union == set(d1["cells"]), "every occupied cell appears under some kind, and none that is not occupied"
    assert sum(sum(m_.values()) for m_ in d1["by_kind"].values()) == sum(d1["cells"].values()), "and the per-kind counts add up to the plane they were folded from"
    _uc = {rk for m_ in d1["by_kind_clean"].values() for rk in m_}
    assert _uc == set(d1["cells_clean"]), "the pruned per-kind map covers exactly the pruned cells"
    _pk_raw, _pk_cl = plane_meta(raw=True)["by_kind"], plane_meta()["by_kind"]
    assert {rk for m_ in _pk_cl.values() for rk in m_} <= {rk for m_ in _pk_raw.values() for rk in m_}, "the pruned per-kind map never names a cell the unpruned one does not"
    # The per-app map has to hold the same three promises, and it is a different
    # partition: an address is a PAIR of (cell, writer) facts, and there is no
    # reason for the two partitions to cover the same set of cells — index
    # crowds 13,572 lines into 2,651 cells while chatdrop spreads 7,115 over
    # 5,961. A map that only covered the biggest writer would be the §18 failure
    # again, so the union is asserted against the plane, not against by_kind.
    _ua = {rk for m_ in d1["by_app"].values() for rk in m_}
    assert _ua == set(d1["cells"]), "every occupied cell appears under some writer, and none that is not occupied"
    assert sum(sum(m_.values()) for m_ in d1["by_app"].values()) == sum(d1["cells"].values()), "and the per-writer counts add up to the plane they were folded from"
    _uac = {rk for m_ in d1["by_app_clean"].values() for rk in m_}
    assert _uac == set(d1["cells_clean"]), "the pruned per-writer map covers exactly the pruned cells"
    _pa_raw, _pa_cl = plane_meta(raw=True)["by_app"], plane_meta()["by_app"]
    assert {rk for m_ in _pa_cl.values() for rk in m_} <= {rk for m_ in _pa_raw.values() for rk in m_}, "the pruned per-writer map never names a cell the unpruned one does not"
    _DERIVED["key"] = None
    assert derived()["cells"] == want_cells, "incremental index equals a from-scratch rebuild"
    assert derived()["cell_app"] == want_app and derived()["cell_seq"] == want_seq, "the who/when maps rebuild to the same thing too"
    assert derived()["cell_kind"] == want_kind, "the kind map rebuilds to the same thing too"
    # the fold and the rebuild must ALSO agree on the PRUNED kind map: a fold
    # that only filled the raw one leaves the pruned one describing the previous
    # log while `key` claims it is current, and a colour mode then paints the
    # kind of a line that is no longer on the plane (§6, lesson 2).
    assert derived()["cell_kind_clean"] == want_kind_c, "and so does its pruned view — otherwise the kind layer outlives the prune that removed it"
    assert derived()["by_kind"] == want_bk, "the per-kind map rebuilds to the same thing too"
    assert derived()["by_kind_clean"] == want_bk_c, "and so does its pruned view — a filter must not outlive the prune"
    assert derived()["by_app"] == want_ba, "the per-writer map rebuilds to the same thing too"
    assert derived()["by_app_clean"] == want_ba_c, "and so does its pruned view — a filter must not outlive the prune"
    assert len(derived()["timeline"]) == want_tl, "timeline length agrees too"
    bhash = append("bench", "bond", "%s uses %s" % (hs[0], hs[1]))["hash"]
    assert any(e["hash"] == bhash for e in all_bonds()), "a fresh edge is in the graph without a rebuild"
    assert len(all_bonds()) == made + 1, "only the edge set changing re-resolves the edges"
    b2 = append("bench", "bond", "%s unbinds %s" % (bhash, bhash))["hash"]
    assert not any(e["hash"] == bhash for e in all_bonds()), "a tombstone takes the edge out of the graph"
    assert not any(e["hash"] == b2 for e in all_bonds()), "and the tombstone is not itself an edge"
    # a writer this process cannot account for: trust the file, not the cache
    _DERIVED["key"] = None
    n2 = len(derived()["timeline"])      # an int, not the dict: derived() updates in place
    foreign = {"v": 3, "t": 9.0, "app": "bench", "kind": "chat", "text": "foreign",
               "to": "all", "tags": [], "prev": "x" * 64, "rkey": app_rkey(b"benchchatforeign", "bench"),
               "enc": app_rkey(b"benchchatforeign", "bench"), "hash": "y" * 64}
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(foreign, ensure_ascii=False) + "\n")
    assert len(derived()["timeline"]) == n2 + 1, "another writer's line invalidates by (size, mtime)"
    before = dict(derived()["cells"])
    _derived_absorb({"hash": "z" * 64, "rkey": fresh_rk, "kind": "chat",
                     "t": 0, "to": "all", "tags": []}, b"nothing\n", (0, 0))
    assert _DERIVED["key"] is None, "a key we cannot verify drops the cache instead of trusting it"
    assert derived()["cells"] == before, "and the rebuild agrees with what was already there"
    # the guard identifies the tail line by hash, not by counting bytes — the
    # log is CRLF on Windows (text-mode fd), so byte arithmetic would be off by
    # one per line and the absorb would never fire
    assert _last_line_is({"hash": "0" * 64}) is False, "the tail is not a line we did not write"
    assert _last_line_is(tail(1)[0]) is True, "and it is the line we did write"
    assert open(LOG, "rb").read().endswith(b"\n"), "lines end in a newline on every platform"
    # ── blocks: the story/method layer ─────────────────────────────────
    # refs must point at lines that exist NOW: the synthetic bench log above
    # rewrote this file (with fake hashes), so the bond lines are gone and the
    # two source lines this block cites are written here, for real
    _r1 = append("opencode", "chat", "block demo: the line this was distilled from")["hash"][:8]
    _r2 = append("hermes", "note", "block demo: and the second source line")["hash"][:8]
    bk = block("opencode", "demo-block", "a thing we learned",
               story="found while wiring the demo", method="1) try 2) measure 3) keep",
               evidence="pool.py demo", refs=[_r1], status="open")
    assert bk["kind"] == "block", "a block is an ordinary chained line"
    assert bk["text"].startswith(BLOCK_MARK) and bk["rev"] == 1, "first write is rev 1"
    assert tail(1)[0]["hash"] == bk["hash"], "the block is the newest line, in the same chain"
    assert 0 <= bk["rkey"] < 20736 and bk["enc"] == bk["rkey"] % 1440, "a block is addressed like anything else"
    assert parse_block(bk["text"])["title"] == "a thing we learned", "canonical text parses back"
    assert render_block("s", 2, parse_block(render_block("s", 2, {"title": "t"}))) \
        == render_block("s", 2, {"title": "t"}), "render -> parse -> render is the identity"
    assert blocks()[0]["revs"] == 1 and blocks()[0]["apps"] == ["opencode"], "one write, one contributor"
    # a revision is a NEW line — the old one stays readable, which is the point
    h3 = bk["hash"]
    bk2 = block("hermes", "demo-block", "a thing we learned (revised)",
                story="hermes picked it up", method="1) try 2) measure 3) keep 4) ship",
                refs="%s,%s" % (_r1, _r2), status="closed")
    assert bk2["rev"] == 2, "a revision increments instead of editing"
    assert bk2["prev"] == h3, "revision 2 chains onto revision 1"
    assert len(block_revs("demo-block")) == 2, "both revisions live in the chain"
    live = block_get("demo-block")
    assert live["title"] == "a thing we learned (revised)", "latest revision wins"
    assert live["status"] == "closed" and live["revs"] == 2, "status and write count come from the fold"
    assert live["apps"] == ["opencode", "hermes"], "every platform that touched it is remembered"
    assert live["first_t"] <= live["t"], "first_t is the oldest revision, not the latest"
    assert [r["slug"] for r in blocks()] == ["demo-block"], "one row per slug"
    assert len(blocks(status="closed")) == 1 and blocks(status="open") == [], "status filters"
    assert blocks(query="measure") and blocks(query="ship") and not blocks(query="nope"), "query filters"
    assert verify_block(live)["ok"] and len(verify_block(live)["refs"]) == 2, "refs resolve to chained lines"
    assert verify_block({"refs": "deadbeef"})["missing"] == ["deadbeef"], "a dangling ref is reported, not ignored"
    assert [r["rev"] for r in block_revs("demo-block")] == [1, 2], "history is oldest-first"
    card = block_card(live)
    assert card.startswith("▣ demo-block") and "revised" in card and "method:" in card, "card shows the block"
    assert len(max(card.split("\n"), key=len)) <= 88, "the card is fixed-width, so it wraps instead of overflowing"
    assert curator_protect(bk) == "kind", "the curator never hides a block"
    assert "/block ▣ demo-block rev1" in pretty(bk), "the feed shows slug+rev+title, not six lines"
    assert len(pretty(bk).split("\n")) == 1, "and it stays one feed line"
    assert _flat("a\nb\tc") == "a b c" and "\n" not in bk["text"].split("\n")[1], "values stay one physical line"
    try:
        block("opencode", "BAD SLUG", "t")
        assert False, "a bad slug must be refused"
    except ValueError:
        pass
    try:
        block("opencode", "demo-ok", "t", refs="not-hex")
        assert False, "a bad ref must be refused"
    except ValueError:
        pass
    try:
        block("opencode", "demo-ok", "t", status="maybe")
        assert False, "a bad status must be refused"
    except ValueError:
        pass
    try:
        block("opencode", "demo-ok", "   ")
        assert False, "a title-less block must be refused"
    except ValueError:
        pass
    # a revision that restates no refs inherits them, and a third platform joins
    bk3 = block("codex", "demo-block", "a thing we learned (third pass)", status="closed")
    assert bk3["rev"] == 3, "revisions keep counting"
    assert bk3["refs"] == bk2["refs"], "refs are inherited when a revision does not restate them"
    live3 = block_get("demo-block")
    assert live3["apps"] == ["opencode", "hermes", "codex"], "contributors are the union over revisions"
    assert len(block_revs("demo-block")) == 3 and live3["revs"] == 3, "every revision stays in the chain"
    assert blocks(status="closed")[0]["rev"] == 3, "the live revision is the last line written for that slug"
    assert block_get("never-written") is None, "an unknown slug reads as None, not as an error"
    assert parse_block("just a chat line") is None, "chat text is not a block"
    assert parse_block(bk["text"].replace("title:", "bogus:")) is None, "a key outside the format is refused"
    assert parse_block(bk["text"] + "\ntitle: twice") is None, "a repeated key is refused"
    assert parse_block(bk["text"].replace("slug=demo-block rev=1", "demo-block")) is None, "an off-format header is refused"
    assert parse_block(bk["text"].replace("rev=1", "rev=0")) is None, "rev starts at 1"
    # ── session_end: auto-block engine ───────────────────────────────────
    register_app("sesh"); register_app("sesh2"); register_app("sesh3")
    # rule 1: too few lines → skip
    append("sesh", "chat", "hi")
    assert session_end("sesh") is None, "trivial session (< 3 lines) produces no block"
    assert session_end("sesh", force=True) is not None, "force bypasses the minimum"
    # rule 2: enough lines but no substance → skip
    for i in range(4):
        append("sesh2", "chat", "ack %d" % i)
    assert session_end("sesh2") is None, "no substantive line → skip"
    assert session_end("sesh2", force=True) is not None, "force bypasses substance check"
    # rule 3: real session → produces a block
    append("sesh3", "chat", "hello")
    append("sesh3", "chat", "DECISION: use mmap for the read path")
    hlong = append("sesh3", "chat", "a" * 300)["hash"]  # >240 chars
    bse = session_end("sesh3")
    assert bse is not None, "a session with substance produces a block"
    assert bse["kind"] == "block", "session_end writes a block"
    assert bse["slug"].startswith("sesh3-"), "slug starts with app name"
    assert bse["status"] == "closed", "session-end block is closed"
    assert bse["refs"], "refs were collected from session lines"
    assert "DECISION" in bse["story"] or "DECISION" in bse["title"], "marker lines appear in the block"
    assert verify_block(bse)["ok"], "refs resolve to chained lines"
    # dedup: similar title → bump rev (title Jaccard >= 0.4)
    # add lines that produce a similar auto-title (same DECISION topic)
    append("sesh3", "chat", "ok")
    append("sesh3", "chat", "DECISION: mmap read path confirmed")
    append("sesh3", "chat", "a" * 260)
    bse2 = session_end("sesh3")
    assert bse2 is not None, "reworded session produces a block"
    assert bse2["kind"] == "block", "second session_end is still a block"
    assert verify_block(bse2)["ok"], "refs still resolve"
    # similarity dedup: same topic → same slug, bumped rev
    assert bse2["slug"] == bse["slug"], "same-topic session deduplicates by title similarity"
    assert bse2["rev"] == bse["rev"] + 1, "dedup bumps the rev, not a new slug"
    # genuinely different work → new slug
    append("sesh3", "chat", "ok")
    append("sesh3", "chat", "VERDICT: abandon mmap use fread instead")
    append("sesh3", "chat", "b" * 260)
    bse3 = session_end("sesh3")
    assert bse3 is not None, "different work produces a new block"
    assert bse3["slug"] != bse["slug"], "different topic -> different slug"
    # _tokenize + _similarity: pure-function tests
    assert _similarity("hello world", "hello world") == 1.0, "identical = 1.0"
    assert _similarity("hello world", "goodbye earth") == 0.0, "disjoint = 0.0"
    assert 0.0 < _similarity("DECISION use mmap", "DECISION mmap") < 1.0, "partial overlap = between"
    assert _similarity("", "") == 1.0, "both empty = 1.0"
    assert _similarity("hello", "") == 0.0, "one empty = 0.0"
    # _is_duplicate: threshold test
    assert _is_duplicate("sesh3", bse["title"], bse["story"]) is not None, "identical content is duplicate"
    assert _is_duplicate("sesh3", "totally different title", "totally different story") is None, "different content is not duplicate"
    # pool_session_guard: atexit hook
    pool_session_guard("sesh")  # should not raise, idempotent
    pool_session_guard("sesh")  # second call does not stack
    assert "sesh" in _GUARDED_APPS, "guard registers the app"
    # unregistered app → ValueError
    try:
        session_end("ghost-app")
        assert False, "unregistered app must be refused"
    except ValueError:
        pass
    # _is_substantive: tests on explicit dicts
    assert _is_substantive({"text": "DECISION: use mmap", "kind": "chat"}), "marker line is substantive"
    assert _is_substantive({"text": "a" * 300, "kind": "chat"}), "long line is substantive"
    assert _is_substantive({"text": "multi\nline", "kind": "chat"}), "multi-line is substantive"
    assert _is_substantive({"text": "x uses y", "kind": "bond"}), "a bond is substantive"
    assert not _is_substantive({"text": "ok", "kind": "chat"}), "a short ack is not substantive"
    assert not _is_substantive({"text": "test", "kind": "chat"}), "a bare test word is not substantive"
    # ── block_timeline: similarity links ──────────────────────────────
    tl = block_timeline()
    assert "nodes" in tl, "timeline has nodes"
    assert "links" in tl, "timeline has links"
    assert len(tl["nodes"]) >= 3, "we created at least 3 blocks"
    # timeline nodes are sorted newest-first (matches blocks() ordering)
    times = [n["t"] for n in tl["nodes"]]
    assert times == sorted(times, reverse=True), "timeline nodes are sorted newest-first"
    # nodes have expected fields
    for n in tl["nodes"]:
        assert "slug" in n and "app" in n and "ts" in n, "node has required fields"
    # create two blocks with very similar titles from different apps → link
    register_app("tl_sim_a")
    register_app("tl_sim_b")
    append("tl_sim_a", "chat", "DECISION: use mmap for the read path")
    append("tl_sim_a", "chat", "ok")
    append("tl_sim_a", "chat", "a" * 260)
    block("tl_sim_a", "tl-sim-a", "DECISION: mmap read path", story="test", status="closed")
    append("tl_sim_b", "chat", "DECISION: mmap confirmed good")
    append("tl_sim_b", "chat", "ok")
    append("tl_sim_b", "chat", "b" * 260)
    block("tl_sim_b", "tl-sim-b", "DECISION: mmap confirmed good", story="test", status="closed")
    tl2 = block_timeline()
    link_froms = [lk["from"] for lk in tl2["links"]]
    assert "tl-sim-a" in link_froms or "tl-sim-b" in link_froms, "similar-title blocks produce a dedup link"
    # verify link structure
    for lk in tl2["links"]:
        assert "from" in lk and "to" in lk and "sim" in lk and "via" in lk, "link has required fields"
        assert 0 < lk["sim"] <= 1.0, "similarity is in (0, 1]"
    # ── explore: walk the graph, name the shape ────────────────────────
    # The plane answers "what is where". This layer answers the other half of
    # reading anything: what it leads to, what points back, and what SHAPE that
    # makes. Tested against lines this block writes so the degrees are known.
    register_app("walk")
    wa = append("walk", "chat", "the read path uses mmap")["hash"]
    wb = append("walk", "chat", "mmap keeps the page cache warm")["hash"]
    wc = append("walk", "chat", "prefetch the first window")["hash"]
    lone = append("walk", "chat", "nobody links to this yet")["hash"]
    assert motif(lone)["shape"] == "isolated", "a line with no edges is an island"
    assert motif(lone)["in"] == 0 and motif(lone)["out"] == 0, "an island has no degrees"
    bond(wc, "x-next-in-session", wa)
    wb_bond = bond(wa, "x-next-in-session", wb)
    # Shape comes from the two degrees, and they are NOT symmetric: wa is cited
    # by wc and points at wb, which is what "mid-conversation" means in data
    assert motif(wa)["shape"] == "chain", "an in AND an out is a chain"
    assert motif(wc)["shape"] == "source", "nothing points back at a starter"
    assert motif(wb)["shape"] == "sink", "nothing leaves a destination"
    assert motif(wb)["out"] == 0 and motif(wb)["in"] == 1, "shape reports both degrees"
    assert motif(wa)["rels_in"] == {"x-next-in-session": 1}, "the WHY of a backlink rides along"
    # a bond line IS an edge, not a node. Counting 13,572 of them as islands
    # would call half the corpus unlinked when they ARE the linking.
    assert motif(wb_bond["hash"])["shape"] == "edge", "a bond line is the edge, not a node"
    assert motif(wb_bond["hash"])["label"] == SHAPES["edge"][1], "edge shape names itself"

    # hub is WEIGHT on the same geometry: the same shape can mean "a reader" or
    # "what everything leans on", and collapsing them would call one backlink a hub
    whub = append("walk", "chat", "a definition everything leans on")["hash"]
    for i in range(HUB_AT):
        r = append("walk", "chat", "cite %d" % i)["hash"]
        bond(r, "x-uses-term", whub)
    assert motif(whub)["in"] == HUB_AT, "the hub counted every citing line"
    assert motif(whub)["hub"] is True, "in-degree at the threshold IS a hub"
    assert motif(wb)["hub"] is False, "one backlink is not a hub"
    assert motif(whub)["shape"] == "sink", "a hub is still a destination, not a new shape"

    # reach: BFS out of a line, both directions, deduped by LINE
    rch = reach(wa, hops=2)
    assert rch["hash"] == wa and rch["gone"] is False, "reach reports the line it started from"
    assert rch["out"][0]["total"] >= 1, "reach walks forward at least one hop"
    assert rch["in"][0]["total"] >= 1, "reach walks back at least one hop"
    assert any(n["hash"] == wb for n in rch["out"][0]["nodes"]), "hop 1 forward is the bonded line"
    assert rch["out"][0]["nodes"][0]["dir"] == "out", "a forward hop says which way it points"
    assert rch["in"][0]["nodes"][0]["dir"] == "in", "a backlink says which way it points"
    # two verbs into the SAME line is one neighbour with two reasons, and a walk
    # that listed it twice would report an address that does not exist
    w2a = append("walk", "chat", "source one")["hash"]
    w2b = append("walk", "chat", "source two")["hash"]
    tgt = append("walk", "chat", "the one both point at")["hash"]
    bond(w2a, "x-uses-term", tgt)
    bond(w2b, "x-same-hint", tgt)
    hops = [(l["depth"], n["hash"]) for l in reach(tgt, hops=1)["in"] for n in l["nodes"]]
    assert len(hops) == len(set(hops)), "a hop lists each line once even with two rels"
    assert len(hops) == 2, "both citers are still reported"

    # a cell is the unit the plane CLICKS, and it raises a question one line
    # cannot: what is inside it, and what leaves it
    wrk = derived()["hash_rkey"][whub]
    cell = explore_cell(wrk, hops=1)
    assert cell["rkey"] == wrk and cell["point"] == wrk, "the cell walk names its address"
    assert cell["count"] >= 1, "the cell counts the lines inside it"
    assert any(l["hash"] == whub for l in cell["lines"]), "the hub is listed in its own cell"
    assert cell["in"][0]["total"] >= HUB_AT, "the cell's backlinks include the hub's readers"
    assert cell["shape"] is not None, "a cell with lines in it always has a shape"
    # the label must be supportable by the cell's OWN line mix — the whole point
    # of the majority rule is that a shape is never decided by lines not there
    _ce = cell["shapes"].get("edge", 0)
    _cn = sum(v for k, v in cell["shapes"].items() if k != "edge")
    assert (cell["shape"] == "edge") == (_ce > _cn), \
        "the cell's shape follows its majority, edges or nodes"
    # a cell can hold NOTHING but edges — the bond lines live in addresses too —
    # and then its shape is "edge", never "empty", because the count beside it
    # is not zero. The invariant is the one that matters: the label must be
    # supportable by the lines actually in the cell.
    bond_only = append("walk", "chat", "a line to bond from")["hash"]
    bond_only2 = append("walk", "chat", "and one to bond to")["hash"]
    bonded = bond(bond_only, "x-uses-term", bond_only2)["hash"]
    bed = explore_cell(derived()["hash_rkey"][bonded], hops=1)
    assert bed["count"] >= 1, "the cell of a bond line is not empty"
    assert not (bed["count"] and bed["shape"] is None), \
        "a non-empty cell never reports the empty shape"
    _e = bed["shapes"].get("edge", 0)
    _n = sum(v for k, v in bed["shapes"].items() if k != "edge")
    assert bed["shape"] == ("edge" if _e > _n else bed["shape"]), \
        "a majority of edges makes the address an edge store, not a node"
    assert bed["shape"] == "edge", "this cell really is edges-only"
    empty_rk = next(i for i in range(20736)
                    if i not in set(v[1] for v in _brief().values()))
    vacant = explore_cell(empty_rk)
    assert vacant["shape"] is None and vacant["count"] == 0, "an untouched address reports empty"
    assert vacant["icon"] and vacant["label"], "an empty address still answers in words"

    # census: the whole plane's link shape, which no single cell can show
    cen = pattern_census()
    assert sum(cen["shapes"].values()) == cen["lines"], \
        "every line lands in exactly one shape bucket"
    assert cen["edges"] == len(all_bonds()), "the census counts the same edges the plane draws"
    n_bond = len([1 for m in _iter_lines()
                  if m.get("kind") == "bond" and m.get("rkey") is not None])
    assert cen["shapes"].get("edge", 0) == n_bond, \
        "bond lines are counted as edges, never as islands"
    # by_app splits the same bucket by AUTHOR, so its edges must add up to the
    # same total — one number, two readings
    assert sum(v["edges"] for v in cen["by_app"].values()) == n_bond, \
        "by_app credits every edge to the app that wrote it"
    n_walk_bond = len([1 for m in _iter_lines()
                       if m.get("kind") == "bond" and m.get("app") == "walk"])
    assert cen["by_app"]["walk"]["edges"] == n_walk_bond, \
        "this block's own bonds are credited to this block's app"
    assert cen["by_app"]["walk"]["lines"] > 0, "by_app counts nodes separately from edges"
    assert cen["cross"] >= 0 and cen["hub_at"] == HUB_AT, "the census states its own threshold"
    assert any(h["hash"] == whub for h in cen["hubs"]), "the hub we built is in the census"
    assert all({"icon", "label", "why"} <= set(cen["shape_meta"][s])
               for s in cen["shapes"]), "every shape bucket can explain itself"

    shutil.rmtree(os.path.join(DIR, "outbox"), ignore_errors=True)
    for p in (BRIDGE_STATE, APPS):
        if os.path.exists(p):
            if p == APPS and os.path.exists(APPS + ".demo-bak"):
                os.replace(APPS + ".demo-bak", APPS)  # restore real registry
            else:
                os.remove(p)
    # count the checks we actually wrote — a stale total was one more thing
    # that could silently rot, so read it off the source instead
    import ast
    tree = ast.parse(open(__file__, encoding="utf-8").read())
    fn = next(nd for nd in ast.walk(tree)
              if isinstance(nd, ast.FunctionDef) and nd.name == "_demo_body")
    total = sum(1 for nd in ast.walk(fn) if isinstance(nd, ast.Assert))
    print("POOL-SELFCHECK: %d/%d PASS" % (total, total))


def _cli_split(argv):
    """`--flag value` / bare `--flag` / positionals — one tiny parser, so the
    block CLI reads like the rest of this file: no argparse, no surprises."""
    pos, flags, i = [], {}, 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) else None
            if nxt is None or nxt.startswith("--"):
                flags[a[2:]], i = "1", i + 1
            else:
                flags[a[2:]], i = nxt, i + 2
        else:
            pos.append(a)
            i += 1
    return pos, flags


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo()  # self-check in a temp dir: the real log is untouchable
    elif len(sys.argv) > 1 and sys.argv[1] == "bridge":
        dry = "--dry" in sys.argv
        lifted = memory_bridge(dry_run=dry)
        for cat, content, mid in lifted:
            print(f"[{cat}]{' #' + str(mid) if mid else ''} {content[:100]}")
        print(f"bridge: {len(lifted)} lifted" + (" (dry)" if dry else ""))
    elif len(sys.argv) > 1 and sys.argv[1] == "curate":
        n = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 200
        rep = curator_report(n=n)
        if "--clean" in sys.argv:      # the feed, as the curator would show it
            print("\n".join(view(n, clean=True)))
        elif "--json" in sys.argv:
            print(json.dumps(rep, ensure_ascii=False, indent=1))
        else:
            print(f"curator: {len(tail(n))} lines read · hide_at={rep['hide_at']}"
                  f" · would hide {rep['hidden']} · keep {rep['kept']}")
            for rule, c in rep["by_rule"].items():
                print(f"  {rule:<12} {c}")
            if rep["protected"]:
                print("protected (never hidden): "
                      + ", ".join(f"{k} {v}" for k, v in rep["protected"].items()))
            for ex in rep["examples"][:10]:
                print(f"  [{ex['score']}] {ex['app']} "
                      f"({'+'.join(ex['rules'])}): {ex['text']!r}")
            print("  (--clean = show the filtered feed · --json = machine-readable)")
    elif len(sys.argv) > 1 and sys.argv[1] == "prune":
        # What the PLANE drops, and which rule did it — the report to read
        # before trusting the pruning. Read-only: nothing is removed anywhere.
        rep = curator_prune_report()
        st = plane_prune_stats()
        if "--json" in sys.argv:
            print(json.dumps({"prune": rep, "plane": st},
                             ensure_ascii=False, indent=1))
        elif "--reveal" in sys.argv:
            # every line the plane would take, not a sample of them
            full = curator_prune_report(limit=10 ** 9)
            for ex in full["examples"]:
                print(f"  [{ex['score']}] {ex['app']} "
                      f"({'+'.join(ex['rules'])}): {ex['text']!r}")
            print(f"prune: {full['pruned']} lines listed")
        else:
            print(f"plane prune: {rep['lines']} lines · prune_at={rep['prune_at']}"
                  f" · would cut {rep['pruned']} · keep {rep['kept']}")
            print(f"  cells: {st['cells_all']} -> {st['cells_clean']}"
                  f" ({st['cells_pruned']} addresses freed)")
            for rule, c in rep["by_rule"].items():
                print(f"  {rule:<12} {c}")
            if rep["protected"]:
                print("protected (never pruned): "
                      + ", ".join(f"{k} {v}" for k, v in rep["protected"].items()))
            for ex in rep["examples"][:10]:
                print(f"  [{ex['score']}] {ex['app']} "
                      f"({'+'.join(ex['rules'])}): {ex['text']!r}")
            print("  (--reveal = list every pruned line · --json = machine-readable"
                  " · POOL_CURATOR_PRUNE=<n> moves the threshold)")
    elif len(sys.argv) > 1 and sys.argv[1] == "blocks":
        pos, fl = _cli_split(sys.argv[2:])
        rows = blocks(status=fl.get("status"), query=fl.get("q") or (pos[0] if pos else None))
        if "json" in fl:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(block_card(r))
                print()
            print("blocks: %d" % len(rows))
    elif len(sys.argv) > 1 and sys.argv[1] == "block-show":
        pos, fl = _cli_split(sys.argv[2:])
        if not pos:
            print("usage: pool.py block-show <slug> [--revs] [--json]")
            sys.exit(2)
        rows = block_revs(pos[0]) if "revs" in fl else [block_get(pos[0])]
        rows = [r for r in rows if r]
        if not rows:
            print("no such block: %s" % pos[0])
            sys.exit(2)
        if "json" in fl:
            print(json.dumps(rows if "revs" in fl else rows[0], ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(block_card(r, width=88))
                print()
            v = verify_block(rows[-1])
            print("refs: %d/%d resolve against the chain"
                  % (len(v["refs"]) - len(v["missing"]), len(v["refs"])))
            if v["missing"]:
                print("  missing: %s" % ", ".join(v["missing"]))
    elif len(sys.argv) > 1 and sys.argv[1] == "block":
        # python pool.py block <slug> --title T [--story S --method M
        #        --evidence E --refs h1,h2 --status open|closed|parked --as app]
        pos, fl = _cli_split(sys.argv[2:])
        if not pos:
            print("usage: pool.py block <slug> --title \"...\" [--story --method"
                  " --evidence --refs h1,h2 --status open --as app]")
            sys.exit(2)
        try:
            b = block(fl.get("as", "human"), pos[0], fl.get("title", ""),
                      story=fl.get("story", ""), method=fl.get("method", ""),
                      evidence=fl.get("evidence", ""), refs=fl.get("refs", ""),
                      status=fl.get("status", "open"))
        except ValueError as e:
            print("block refused: %s" % e)
            sys.exit(2)
        print("▣ %s rev %d · %s · %s" % (b["slug"], b["rev"], b["hash"][:8], b["status"]))
    elif len(sys.argv) > 1 and sys.argv[1] == "session-end":
        # python pool.py session-end <app> [--force] [--json]
        pos, fl = _cli_split(sys.argv[2:])
        if not pos:
            print("usage: pool.py session-end <app> [--force] [--json]")
            sys.exit(2)
        app = pos[0]
        force = "force" in fl
        try:
            b = session_end(app, force=force)
        except ValueError as e:
            print("session-end refused: %s" % e)
            sys.exit(2)
        if b is None:
            print("session-end: skipped (session too short or no substantive work)")
        elif "json" in fl:
            print(json.dumps(b, ensure_ascii=False, indent=1))
        else:
            print("▣ %s rev %d · %s · %s" % (b["slug"], b["rev"], b["hash"][:8], b["status"]))
            print("  title: %s" % b["title"])
            print("  refs: %s" % b["refs"])
            print("  story: %s" % (b["story"][:120] + "..." if len(b["story"]) > 120 else b["story"]))
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
