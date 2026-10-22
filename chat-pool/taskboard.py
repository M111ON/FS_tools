"""taskboard.py — task layer over pool blocks.

A task is a block (kind="block", slug "task-<id8>") on the SAME hash-chained
log every platform already writes to — append-only, never edited, a status
change is a new rev. Any app that can append to the pool can therefore create
and claim tasks, and "ใครกำลังทำอยู่" is a fact of the chain, not a promise.

Encoding rides inside the existing block format (pool.BLOCK_KEYS), so every
reader that already understands blocks can read tasks with zero new code:

    title:  [<status>] <title> — owner:<app>
    story:  description (+ optional "refs: <hash,...>" line)
    method: path:<a/b/c> | parent: <slug> | due: <iso> | tags: x,y

status vocabulary: pending → in_progress → done | blocked | cancelled
(the inbox board's todo maps onto pending; block's "open" is not used here).
That list is a FLOOR, not a ceiling: any bareword in the brackets is a status,
so `notice`, `icebox` or `wontfix` are statuses the moment somebody writes one
and every other platform reads them without being told first. The vocabulary
is derived from the log (`statuses()`), never from a registry that can disagree
with it.

Tags ride the same rev. `src:` is the bridge's dedup key and is never removed;
everything else can be added or dropped, which is what lets a card be filed
away from the working set without being deleted.

Small things are kept, not shown: `task_open()` is the board's main page —
pending / in_progress / blocked, minus anything tagged `crumb`. A finished
card, a custom status, or a crumb is still one click away; it is just not
mixed in with the work.

Holds: a task that is in_progress is HELD by its owner. First come first
served — the platform that moved it there may write, everyone else may read
and nothing more. The hold is derived, never stored: it lapses on its own
HOLD_IDLE_SEC after the last revision anyone wrote about that task, so "idle
for 15 minutes" is a fact readable off the chain rather than a promise.

Text lives inside text, so it is covered by the sha256 chain like everything
else. task_get()/task_list() read pool.derived()'s block index — one fold,
cached by (size, mtime), no second walk. refs inherit across revs for free
(pool.block already does that), so a task keeps pointing at its evidence.
"""
import hashlib
import re
import time

import pool

TASK_STATUS = ("pending", "in_progress", "done", "blocked", "cancelled")
ACTIVE_STATUS = ("pending", "in_progress", "blocked")   # the board's main page
ARCHIVE_STATUS = ("done", "cancelled")                 # history, folded away
NOTE_TAG = "crumb"           # "small enough to keep but not to think about"
HOLD_IDLE_SEC = 900          # 15 minutes of silence releases a hold
STATUS_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")
TAG_OK = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,31}$")


class Held(ValueError):
    """Someone else holds this task. A ValueError, so old callers still catch
    it, but a distinct type: read-only is not a malformed request, and the
    server answers 409 instead of 400 — the message says who holds it and for
    how much longer, which is the only thing the caller can act on."""
STATUS_LINE = re.compile(
    r"^\[([a-z0-9][a-z0-9_-]{0,23})\]\s*(.+?)\s*(?:—\s*owner:(\S+))?\s*$",
    re.IGNORECASE)
KV = re.compile(r"(?<!\S)(\w+):(\S+)")   # bareword keys only — "I:/x" is not a kv


def _title_of(status, title, owner):
    return "[%s] %s%s" % (status, title, (" — owner:%s" % owner) if owner else "")


def task_new_id(prefix="t"):
    """A short id for a new task: 8 hex chars from a hash of now — same idea
    as the pool's own refs, copy-pasteable, collision window is tiny."""
    return prefix + hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:8]


def _method_kv(parent="", due="", tags="", path="/"):
    parts = ["path:%s" % (path or "/")]
    if parent:
        parts.append("parent:%s" % parent)
    if due:
        parts.append("due:%s" % due)
    if tags:
        parts.append("tags:%s" % tags)
    return " ".join(parts)


def valid_status(s):
    """A status is a bareword: [a-z0-9][a-z0-9_-]*, ≤24 chars. Anything wider
    would break the `[status] title` header the next reader parses, so this is
    a loud no rather than a value that renders as something else."""
    return bool(STATUS_OK.match(str(s or "").strip().lower()))


def _taglist(x):
    """Tags may arrive as a list (JSON) or as the comma string the CLI and the
    log itself use. A bare string iterated directly would be a list of
    characters, which is a spectacularly quiet way to tag a card seventeen
    times."""
    if not x:
        return []
    if isinstance(x, str):
        return [t for t in re.split(r"[,\s]+", x) if t]
    return list(x)


def _clean_tags(keep=(), add=(), remove=()):
    """Merge/lower/dedupe tags, then drop what was asked to go.

    `src:` survives removal on purpose: it is how the inbox bridge recognises a
    card it already imported. Drop it and the task silently becomes a second
    copy of a card that already exists on the pool."""
    out, seen = [], set()
    for raw in _taglist(keep) + _taglist(add):
        t = str(raw or "").strip().lower()
        if t and TAG_OK.match(t) and t not in seen:
            seen.add(t)
            out.append(t)
    drop = {str(x).strip().lower() for x in _taglist(remove)}
    return [t for t in out if t not in drop or t.startswith("src:")]


def task_create(app, title, desc="", parent="", owner="", due="", tags="",
                status="pending", refs=""):
    """Create a task. Returns the parsed task. Raises ValueError on a malformed
    status/tag/refs — the same loud no pool.block raises, for the same reason:
    a status nobody can parse back is not a status, it is a typo with a life."""
    if not valid_status(status):
        raise ValueError("status %r must match [a-z0-9][a-z0-9_-]{0,23}" % status)
    tags = ",".join(_clean_tags(add=tags))
    full_title = _title_of(status, title, owner)
    row = pool.block(app, "task-%s" % task_new_id(), full_title,
                     story=desc, method=_method_kv(parent=parent, due=due, tags=tags),
                     refs=refs, status="open")
    return _parse(row)


def _parse(row):
    """Block row -> task dict, or None when the block is not a task.
    Reads what render wrote; a hand-written task in the same shape is also
    a task (the format is the contract, not the writer)."""
    if not str(row.get("slug", "")).startswith("task-"):
        return None
    m = STATUS_LINE.match(row.get("title", "") or "")
    if not m:
        return None
    kv = dict(KV.findall(row.get("method", "") or ""))
    refs = [r for r in re.split(r"[,\s]+", row.get("refs", "") or "") if r]
    story = row.get("story", "") or ""
    for part in re.split(r"\s*\|\s*", story):
        rm = re.match(r"^refs:(.+)$", part.strip())
        if rm:
            refs += [r for r in rm.group(1).split(",") if r]
            story = story.replace(part, "").strip(" |")
    return {
        "id": row["slug"],
        "status": m.group(1).lower(),
        "title": m.group(2),
        "owner": m.group(3) or "",
        "desc": story,
        "parent": kv.get("parent", ""),
        "path": kv.get("path", ""),
        "due": kv.get("due", ""),
        "tags": [t for t in (kv.get("tags", "") or "").split(",") if t],
        "refs": refs,
        "app": row.get("app"), "t": row.get("t"), "hash": row.get("hash"),
        "rev": row.get("rev", 1), "revs": row.get("revs", 1),
        "writers": row.get("apps") or [row.get("app")],
    }


def task_get(tid):
    """One task by id (with or without the task- prefix), or None."""
    row = pool.block_get(tid if tid.startswith("task-") else "task-" + tid)
    return _parse(row) if row else None


def task_list(status=None, owner=None, parent=None, query=None, tag=None,
              view=None):
    """Live tasks, newest first. Filters are ANDs; query matches title+desc.
    view="open" is the board's main page (see task_open) and "folded" is the
    other half — the same split the page draws, so ?view= and the screen can
    never disagree about what is on top."""
    out = []
    for row in pool.blocks(query=query):
        t = _parse(row)
        if t is None:
            continue
        if status and t["status"] != status:
            continue
        if owner and t["owner"] != owner:
            continue
        if tag and tag not in t["tags"]:
            continue
        if parent and t["parent"] != (parent if parent.startswith("task-") else "task-" + parent):
            continue
        if view == "open" and not task_open(t):
            continue
        if view == "folded" and task_open(t):
            continue
        out.append(t)
    return out


def task_open(t):
    """True when a task belongs on the board's main page.

    Work is pending / in_progress / blocked. Everything else — done, cancelled,
    a custom status like `notice`, anything tagged `crumb` — is KEPT and stays
    one click away, it is just not mixed in with the work. A board that shows
    all 54 cards is a list nobody reads; a board that hides them is a lie. This
    is the difference."""
    return (t["status"] in ACTIVE_STATUS
            and NOTE_TAG not in (t.get("tags") or []))


def statuses():
    """Every status in use: the five built-ins first, then whatever the log
    actually contains. Derived, never declared — a task written as [notice] by
    one platform is a notice task for every other reader, with no registry to
    update and no second opinion to reconcile with."""
    out = list(TASK_STATUS)
    for t in task_list():
        if t["status"] not in out:
            out.append(t["status"])
    return out


def tags():
    """Every tag in use, alphabetically — what the filter row offers."""
    out = set()
    for t in task_list():
        out.update(t["tags"])
    return sorted(out)


def task_tree(root=None):
    """{id: task} + children lists — the sub-task view. A task whose parent is
    empty is a root (when root=None); with root given, only its subtree's
    parent edges are listed under it."""
    tasks = {t["id"]: t for t in task_list()}
    children = {}
    for t in tasks.values():
        children.setdefault(t["parent"] or "", []).append(t["id"])
    return {"tasks": tasks, "children": children}


def hold_of(t):
    """Who holds task `t` right now, and for how much longer.

    Derived, never stored: in_progress means held by its owner, and the hold
    dies HOLD_IDLE_SEC after the last revision written about that task. That
    makes "idle for 15 minutes" self-enforcing — nobody has to remember to
    release — and leaves the chain as the audit trail of who took it and what
    they did next.
    """
    now = time.time()
    last = t.get("t") or 0
    idle = max(0, now - last)
    claimed = t.get("status") == "in_progress" and bool(t.get("owner"))
    held = claimed and idle < HOLD_IDLE_SEC
    return {"id": t.get("id"), "held": held,
            "holder": (t.get("owner") or "") if held else "",
            "idle": int(idle),
            "expired": claimed and not held,
            "free_in": max(0, int(HOLD_IDLE_SEC - idle)) if held else 0}


def hold_guard(app, tid):
    """(True, "") when `app` may write task `tid`, else (False, why).

    Reading is never blocked. Writing is: first come first served, until the
    holder has been silent long enough for the lease to lapse."""
    t = task_get(tid)
    if t is None:
        return False, "no such task: %s" % tid
    h = hold_of(t)
    if h["held"] and h["holder"] != app:
        return False, ("held by @%s — read only for another %ds "
                       "(idle %d min releases it)"
                       % (h["holder"], h["free_in"], HOLD_IDLE_SEC // 60))
    return True, ""


def hold_list():
    """Every live hold: {id, title, holder, free_in}. What a platform reads
    before it tries to touch someone else's work."""
    out = []
    for t in task_list():
        h = hold_of(t)
        if h["held"]:
            h["title"] = t["title"]
            out.append(h)
    return out


def task_update(app, tid, status="", owner="", desc=None, note="", title="",
                add_tags=(), remove_tags=(), guard=True):
    """Edit a task. A new rev is a new block line — append-only, like
    everything. Nothing here mutates history; it says what the task looks like
    NOW and leaves every earlier shape readable in block_revs().

    `note` appends to desc, `desc` replaces it, `title` replaces the text,
    `add_tags`/`remove_tags` edit the tag list (src: is protected). `owner`
    re-assignment and any status change — including a custom one like `notice`
    — ride the same rev. refs/due/parent inherit from the previous rev.

    Raises Held when another platform holds the task (see hold_guard) — a claim
    is therefore first come first served, not last writer wins.

    guard=False is for the ORIGIN of the work, not for a peer of it: the inbox
    bridge reading a card's own status out of the inbox board must not be locked
    out by the board it is mirroring, or a card could never record what the card
    already says. Everything that writes INTO the pool from outside leaves the
    guard on."""
    prev = pool.block_get(tid if tid.startswith("task-") else "task-" + tid)
    if prev is None:
        raise ValueError("no such task: %s" % tid)
    cur = _parse(prev)
    if cur is None:
        raise ValueError("block %s is not a task (title is not [<status>] ...)" % tid)
    ok, why = hold_guard(app, prev["slug"])
    if guard and not ok:
        raise Held(why)
    if status and not valid_status(status):
        raise ValueError("status %r must match [a-z0-9][a-z0-9_-]{0,23}" % status)
    new_title = (title.strip() if title else cur["title"])
    if not new_title:
        raise ValueError("title required — an unnamed card cannot be found again")
    new_desc = cur["desc"]
    if note:
        new_desc = (new_desc + "\n" + note).strip() if new_desc else note
    if desc is not None:
        new_desc = desc
    tags = _clean_tags(cur["tags"], add_tags, remove_tags)
    full_title = _title_of(status or cur["status"], new_title,
                           owner or cur["owner"])
    story = new_desc
    if cur["refs"]:
        story = (story + "\nrefs: " + ",".join(cur["refs"])).strip()
    row = pool.block(app, prev["slug"], full_title, story=story,
                     method=_method_kv(parent=cur["parent"], due=cur["due"],
                                       tags=",".join(tags), path=cur["path"]),
                     status="open")
    return _parse(row)


def task_claim(app, tid):
    """Claim a task: sets owner=app and status=in_progress in one rev, and in
    doing so TAKES the hold. A task somebody else already holds raises Held —
    first come first served, because the hold is the claim."""
    return task_update(app, tid, status="in_progress", owner=app)


def task_summary():
    """The one-screen answer: counts per status and who holds each in_progress.
    This is what "ใครกำลังทำอยู่" reads — the holder is the owner, and the hold
    says how long they have left before it lapses. `open`/`folded` is the split
    the page draws: work on top, the kept-and-parked underneath."""
    tasks = task_list()
    counts = {s: 0 for s in statuses()}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    working = [{"id": t["id"], "title": t["title"], "owner": t["owner"],
                "t": t["t"], "free_in": hold_of(t)["free_in"]}
               for t in tasks if t["status"] == "in_progress"]
    return {"total": len(tasks), "counts": counts, "in_progress": working,
            "held": [h for h in (hold_of(t) for t in tasks) if h["held"]],
            "statuses": statuses(), "tags": tags(),
            "open": sum(1 for t in tasks if task_open(t)),
            "folded": sum(1 for t in tasks if not task_open(t)),
            "idle_sec": HOLD_IDLE_SEC}


def render_row(t):
    """One-line summary for CLI: id, status, owner, age, title."""
    age = time.strftime("%m-%d %H:%M", time.localtime(t.get("t") or 0))
    return "%-18s %-12s @%-10s %s  %s" % (t["id"], t["status"],
                                          t.get("owner") or "-", age,
                                          (t["title"] or "")[:60])


# ── self-check (temp-dir isolated, never touches live data) ─────────────
def demo():
    import os
    import tempfile
    global HOLD_IDLE_SEC
    box = tempfile.mkdtemp(prefix="taskboard-demo-")
    pool.LOG = os.path.join(box, "log.jsonl")
    pool.STATE = os.path.join(box, ".flush_state.json")
    pool.APPS = os.path.join(box, "apps.json")
    pool.DIR = box
    ok = []

    pool.register_app("opencode")
    pool.register_app("hermes")

    # create + claim + finish
    line = pool.append("opencode", "chat", "DECISION: tasks live on the pool log")
    t = task_create("opencode", "port the flush cursor to the new log shape",
                    desc="cursor must survive CRLF growth", tags="flush,log",
                    refs=line["hash"][:8])
    ok.append(t["status"] == "pending" and t["owner"] == "")
    ok.append(t["refs"] == [line["hash"][:8]])
    task_claim("hermes", t["id"])
    cur = task_get(t["id"])
    ok.append(cur["status"] == "in_progress" and cur["owner"] == "hermes")
    ok.append("hermes" in cur["writers"])

    # holds: the claim IS the hold — first come first served, read-only for all
    # other platforms until the holder goes idle for HOLD_IDLE_SEC.
    h = hold_of(cur)
    ok.append(h["held"] and h["holder"] == "hermes" and h["free_in"] > 0)
    ok.append(hold_guard("opencode", cur["id"])[0] is False)
    ok.append("hermes" in hold_guard("opencode", cur["id"])[1])
    try:
        task_claim("opencode", cur["id"])
        ok.append(False)
    except Held:
        ok.append(True)
    ok.append(hold_list()[0]["id"] == cur["id"])
    stale = hold_of(dict(cur, t=time.time() - HOLD_IDLE_SEC - 60))
    ok.append(stale["held"] is False and stale["expired"] is True)
    saved_idle = HOLD_IDLE_SEC
    HOLD_IDLE_SEC = 0            # lapse the lease without waiting 15 real minutes
    ok.append(hold_guard("opencode", cur["id"])[0] is True)
    HOLD_IDLE_SEC = saved_idle
    ok.append(hold_guard("opencode", cur["id"])[0] is False)

    # a status the next reader cannot parse back is a typo, not a status
    try:
        task_update("hermes", t["id"], status="in progress")
        ok.append(False)
    except ValueError:
        ok.append(True)

    task_update("hermes", t["id"], status="done", note="shipped + parity checked")
    fin = task_get(t["id"])
    ok.append(fin["status"] == "done" and fin["owner"] == "hermes")
    ok.append("shipped" in fin["desc"])
    ok.append(fin["revs"] == 3)
    ok.append(fin["refs"] == [line["hash"][:8]])   # refs inherited across revs
    ok.append(hold_guard("opencode", fin["id"])[0] is True)   # released, not held

    # sub-tasks + tree
    parent = task_create("opencode", "sync inbox board into the pool")
    sub1 = task_create("opencode", "read .inbox_state.json board", parent=parent["id"])
    sub2 = task_create("hermes", "map todo->pending, write blocks", parent=parent["id"])
    tree = task_tree()
    kids = tree["children"].get(parent["id"], [])
    ok.append(sorted(kids) == sorted([sub1["id"], sub2["id"]]))
    ok.append(len(task_list(parent=parent["id"])) == 2)

    # filters + summary
    task_claim("hermes", sub2["id"])
    mine = task_list(owner="hermes")
    ok.append(len(mine) == 2 and all(x["owner"] == "hermes" for x in mine))
    open_now = task_list(status="in_progress")
    ok.append(len(open_now) == 1 and open_now[0]["id"] == sub2["id"])
    s = task_summary()
    ok.append(s["total"] == 4 and s["counts"]["done"] == 1)
    ok.append(all(w["owner"] for w in s["in_progress"]))
    ok.append(len(s["held"]) == 1 and s["idle_sec"] == HOLD_IDLE_SEC)

    # a non-task block is invisible to the board
    pool.block("opencode", "decision-sync", "DECISION: board on pool log")
    ok.append(task_summary()["total"] == 4)

    # ── editing: the board is writable, and the vocabulary is not closed ──
    ed = task_create("opencode", "keep the old dashboard around",
                     desc="small enough to park", tags="src:inbox-99,archive")
    ok.append(ed["tags"] == ["src:inbox-99", "archive"])
    ed = task_update("opencode", ed["id"], title="park the old dashboard",
                     desc="still readable, out of the way",
                     add_tags=["notice", "ui"], remove_tags=["archive"])
    ok.append(ed["title"] == "park the old dashboard")
    ok.append(ed["desc"] == "still readable, out of the way")
    ok.append(ed["tags"] == ["src:inbox-99", "notice", "ui"])
    ed = task_update("opencode", ed["id"], remove_tags=["src:inbox-99"])
    ok.append("src:inbox-99" in ed["tags"])     # the dedup key is not removable
    ok.append("archive" not in ed["tags"])

    # a custom status is a status the moment one platform writes it
    ed = task_update("opencode", ed["id"], status="notice")
    ok.append(ed["status"] == "notice")
    ok.append(task_get(ed["id"])["status"] == "notice")   # survives a re-read
    ok.append("notice" in statuses())
    ok.append(task_list(status="notice")[0]["id"] == ed["id"])
    ok.append(task_summary()["counts"]["notice"] == 1)
    try:
        task_update("opencode", ed["id"], title="   ")
        ok.append(False)
    except ValueError:
        ok.append(True)

    # kept but not on top: work stays, the small stuff folds away
    ok.append(task_open(ed) is False)                      # custom status
    crumb = task_create("opencode", "ctrls line 12 still flaky", tags=NOTE_TAG)
    ok.append(task_open(crumb) is False)                  # crumb tag
    ok.append(task_open(parent) is True)                  # plain pending work
    ok.append(task_open(sub1) is True)
    ok.append(len(task_list(view="open")) == 3)
    ok.append(len(task_list(view="folded")) == 3)
    ok.append(len(task_list(tag=NOTE_TAG)) == 1)
    s = task_summary()
    ok.append(s["open"] == 3 and s["folded"] == 3 and s["total"] == 6)
    ok.append("crumb" in s["tags"] and "notice" in s["tags"])

    fails = [i for i, x in enumerate(ok) if not x]
    print("taskboard demo: %d/%d ok%s" % (sum(ok), len(ok),
          "  FAILED at " + ",".join(map(str, fails)) if fails else ""))
    return all(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if demo() else 1)
