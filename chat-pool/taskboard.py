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
STATUS_LINE = re.compile(
    r"^\[(" + "|".join(TASK_STATUS) + r")\]\s*(.+?)\s*(?:—\s*owner:(\S+))?\s*$",
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


def task_create(app, title, desc="", parent="", owner="", due="", tags="",
                status="pending", refs=""):
    """Create a task. Returns the block row. Raises ValueError on a bad
    status/refs — the same errors pool.block raises, for the same reason."""
    if status not in TASK_STATUS:
        raise ValueError("status must be one of %s" % (TASK_STATUS,))
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


def task_list(status=None, owner=None, parent=None, query=None):
    """Live tasks, newest first. Filters are ANDs; query matches title+desc."""
    out = []
    for row in pool.blocks(query=query):
        t = _parse(row)
        if t is None:
            continue
        if status and t["status"] != status:
            continue
        if owner and t["owner"] != owner:
            continue
        if parent and t["parent"] != (parent if parent.startswith("task-") else "task-" + parent):
            continue
        out.append(t)
    return out


def task_tree(root=None):
    """{id: task} + children lists — the sub-task view. A task whose parent is
    empty is a root (when root=None); with root given, only its subtree's
    parent edges are listed under it."""
    tasks = {t["id"]: t for t in task_list()}
    children = {}
    for t in tasks.values():
        children.setdefault(t["parent"] or "", []).append(t["id"])
    return {"tasks": tasks, "children": children}


def task_update(app, tid, status="", owner="", desc=None, note=""):
    """Advance a task: new rev = new block line (append-only, like everything).
    `note` is appended to desc; owner re-assignment is recorded in the title.
    refs/due/parent/tags inherit from the previous rev."""
    prev = pool.block_get(tid if tid.startswith("task-") else "task-" + tid)
    if prev is None:
        raise ValueError("no such task: %s" % tid)
    cur = _parse(prev)
    if cur is None:
        raise ValueError("block %s is not a task (title is not [<status>] ...)" % tid)
    if status and status not in TASK_STATUS:
        raise ValueError("status must be one of %s" % (TASK_STATUS,))
    new_desc = cur["desc"]
    if note:
        new_desc = (new_desc + "\n" + note).strip() if new_desc else note
    if desc is not None:
        new_desc = desc
    kv = dict(KV.findall(prev.get("method", "") or ""))
    title = _title_of(status or cur["status"], cur["title"], owner or cur["owner"])
    story = new_desc
    if cur["refs"]:
        story = (story + "\nrefs: " + ",".join(cur["refs"])).strip()
    row = pool.block(app, prev["slug"], title, story=story,
                     method=_method_kv(parent=cur["parent"], due=cur["due"],
                                       tags=",".join(cur["tags"]), path=cur["path"]),
                     status="open")
    return _parse(row)


def task_claim(app, tid):
    """Claim a task: sets owner=app and status=in_progress in one rev."""
    return task_update(app, tid, status="in_progress", owner=app)


def task_summary():
    """The one-screen answer: counts per status and who holds each in_progress.
    This is what "ใครกำลังทำอยู่" reads."""
    tasks = task_list()
    counts = {s: 0 for s in TASK_STATUS}
    for t in tasks:
        counts[t["status"]] += 1
    working = [{"id": t["id"], "title": t["title"], "owner": t["owner"],
                "t": t["t"]} for t in tasks if t["status"] == "in_progress"]
    return {"total": len(tasks), "counts": counts, "in_progress": working}


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

    # invalid status is a loud error, not a silent write
    try:
        task_update("hermes", t["id"], status="almost")
        ok.append(False)
    except ValueError:
        ok.append(True)

    task_update("hermes", t["id"], status="done", note="shipped + parity checked")
    fin = task_get(t["id"])
    ok.append(fin["status"] == "done" and fin["owner"] == "hermes")
    ok.append("shipped" in fin["desc"])
    ok.append(fin["revs"] == 3)
    ok.append(fin["refs"] == [line["hash"][:8]])   # refs inherited across revs

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

    # a non-task block is invisible to the board
    pool.block("opencode", "decision-sync", "DECISION: board on pool log")
    ok.append(task_summary()["total"] == 4)

    fails = [i for i, x in enumerate(ok) if not x]
    print("taskboard demo: %d/%d ok%s" % (sum(ok), len(ok),
          "  FAILED at " + ",".join(map(str, fails)) if fails else ""))
    return all(ok)


if __name__ == "__main__":
    import sys
    sys.exit(0 if demo() else 1)
