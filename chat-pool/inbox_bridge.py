"""inbox_bridge.py — move the inbox-manager board into the pool, gradually.

WHERE THE BOARD LIVES NOW
    The inbox board is a FILE: I:\\.vault\\DWGLS-native-fs\\.inbox_state.json
    (its dashboard used to be a web page on :5000 — that server is gone). This
    bridge is the phase that moved the board onto the pool: cards become task
    blocks on the shared hash-chained log, so every other platform can see
    them, claim them (@owner), and report status from anywhere.

    You do NOT need anything running on :5000. read_board() reads the file
    first; INBOX_STATE_URL (below) is only a fallback for when the file is
    absent, and its :5000 default is a leftover — override it if you ever point
    the bridge at a live inbox dashboard.

    Read the board on the pool now at http://127.0.0.1:8390/tasks/ui .

Two directions, one log:
    inbox → pool   apply()  every card becomes a task block, deduped by src:
                          tag, owner-aware. inbox is the source of record.
    pool → inbox   pull()   a task somebody moved on another platform goes
                          back onto its card, with the three guards below.

The guards are the whole design, because two boards that can both write each
other will either stomp or oscillate:
    HOLD  an in_progress task is HELD by whoever moved it there. First come
          first served: everyone else may read it, nobody else writes it, and
          the hold lapses by itself after taskboard.HOLD_IDLE_SEC of silence.
    TIE   if the card's updated_at is not older than the task's last revision,
          BOTH sides moved and inbox is not behind. inbox wins (it owns its
          cards), the forward sync carries the change, and pull steps aside and
          reports the conflict.
    FRESH only a task that moved since our last pull is a candidate, and the
          baseline is written back after every run — so an unchanged board
          costs nothing and the two directions cannot feed each other a loop.

Writes go to .inbox_state.json directly (tmp + os.replace, under a lock file,
with a compare-and-swap on the card's updated_at). The inbox MCP re-reads that
file on every tool call, so board_list/board_handoff see the result at once —
and unlike the MCP's own write_text, os.replace is atomic: a reader never
sees half a board.

Usage:
    python inbox_bridge.py plan        # dry-run: what WOULD be imported
    python inbox_bridge.py apply       # write task blocks into the pool log
    python inbox_bridge.py pull        # dry-run: pool → inbox (nothing written)
    python inbox_bridge.py pull apply  # actually write the cards back
    python inbox_bridge.py holds       # who is holding what, and for how long
    python inbox_bridge.py status      # summary: counts + who is working
    python inbox_bridge.py tasks       # one line per task
    python inbox_bridge.py demo        # self-check (temp dirs, no live data)

Environment:
    INBOX_STATE_FILE  path to .inbox_state.json (default: the live DWGLS one —
                      this FILE is the source; no server needed)
    INBOX_STATE_URL   fallback URL, used only when the file is absent
                      (default http://127.0.0.1:5000/api/state — a leftover;
                       the old :5000 dashboard is gone, so leave it unset unless
                       you are running an inbox dashboard somewhere)
    PULL_STATE_FILE   baseline of "what both sides last agreed on" (pool dir)
    POOL_DIR          where pool.py lives (default: next to this script)
"""
import contextlib
import datetime
import json
import os
import sys
import time

INBOX_STATE_FILE = os.environ.get(
    "INBOX_STATE_FILE", r"I:\.vault\DWGLS-native-fs\.inbox_state.json")
INBOX_STATE_URL = os.environ.get("INBOX_STATE_URL", "http://127.0.0.1:5000/api/state")
POOL_DIR = os.environ.get("POOL_DIR", os.path.dirname(os.path.abspath(__file__)))
PULL_STATE_FILE = os.environ.get(
    "PULL_STATE_FILE", os.path.join(POOL_DIR, ".inbox_pull_state.json"))

sys.path.insert(0, POOL_DIR)
import pool            # noqa: E402
import taskboard       # noqa: E402

BRIDGE_APP = "inbox-bridge"
INBOX_STATUS = {"todo": "pending", "in_progress": "in_progress", "done": "done",
                "blocked": "blocked", "cancelled": "cancelled"}
# The pool's vocabulary is open (notice, icebox, …); the inbox board's is five
# fixed columns. A task parked under a word the board has no column for is
# something the forward sync has NO OPINION about — and "no opinion" must never
# be turned into "reset it to pending", which is what a plain status comparison
# would do to every custom status the moment a card was edited on the board.
POOL_KNOWN = frozenset(INBOX_STATUS.values())
POOL_STATUS = {v: k for k, v in INBOX_STATUS.items()}   # pending → todo, …
LOCK_STALE_SEC = 30      # a lock older than this belongs to a dead process
TIE_SEC = 2# a card touched this close to the task's last revision is a
#                       tie, not a win: inbox takes ties. Guessing the other way
#                       destroys somebody's edit, guessing this way only costs
#                       one more sync.

# ── content sync: title/body/tags ride both directions, not just status ──
# A status-only sync leaves two boards that AGREE on the column but DISAGREE
# on what the card says — the pool page (tasks_ui.html) edits title/desc/tags
# in one rev, so the bridge has to carry those fields too, or an edit on one
# side is silently a different card on the other.
#
# The loop guard: each direction appends its own audit line (forward "sync
# from inbox card #N" into the task desc, backward "pool task task-… …" into
# the card body). A naive body comparison would see those lines as a real edit
# and ping-pong forever. Both are stripped before comparing, so only what a
# person actually wrote counts as a difference.
_AUTO_LINES = ("sync from inbox card #", "pool task task-")


def _strip_auto(text):
    """Drop the bridge's own audit lines — what is left is what a person wrote."""
    out = []
    for ln in str(text or "").splitlines():
        s = ln.strip()
        if s.startswith(_AUTO_LINES):
            continue
        out.append(ln)
    return "\n".join(out).strip()


def _card_tags(c):
    """Tags on an inbox card (the field is absent on older cards)."""
    tags = c.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return [str(t) for t in tags]


def _task_user_tags(t):
    """Tags on a pool task minus the bridge's own src: dedup key."""
    return [x for x in (t.get("tags") or []) if not x.startswith("src:")]


def _content_diff(card, task):
    """Which content fields disagree (title/body/tags) — status is separate.

    Returns a list like ["title", "tags"]. Body is compared with the audit
    lines stripped on both sides (see _strip_auto)."""
    fields = []
    if (card.get("title") or "").strip() != (task.get("title") or "").strip():
        fields.append("title")
    if _strip_auto(card.get("body") or "") != _strip_auto(task.get("desc") or ""):
        fields.append("body")
    if {x.lower() for x in _card_tags(card)} != {x.lower() for x in _task_user_tags(task)}:
        fields.append("tags")
    return fields


def read_board():
    """Board cards from the inbox state file (or dashboard HTTP fallback)."""
    path = INBOX_STATE_FILE
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return (json.load(f).get("board") or []), None
        except Exception as e:
            return [], str(e)
    import urllib.request
    try:
        with urllib.request.urlopen(INBOX_STATE_URL, timeout=3) as r:
            return (json.loads(r.read().decode("utf-8")).get("board") or []), None
    except Exception as e:
        return [], str(e)


def _existing_by_src():
    out = {}
    for t in taskboard.task_list():
        for tag in t.get("tags", []):
            if tag.startswith("src:"):
                out[tag] = t
    return out


def plan():
    """Diff the inbox board against the pool: import/update/skip per card.
    Same shape as the server's POST /tasks/sync dry-run."""
    board, err = read_board()
    if err:
        return {"ok": False, "error": err, "actions": []}
    existing = _existing_by_src()
    actions, imported, updated, skipped = [], 0, 0, 0
    for c in board:
        cid = str(c.get("id", ""))
        src = "src:inbox-%s" % cid
        st = INBOX_STATUS.get(c.get("status", ""))
        title = (c.get("title") or "").strip()[:80]
        if not cid or not title:
            skipped += 1
            actions.append({"card": cid, "op": "skip", "why": "missing id/title"})
            continue
        if st is None:
            skipped += 1
            actions.append({"card": cid, "op": "skip",
                            "why": "unknown status %r" % c.get("status")})
            continue
        cur = existing.get(src)
        if cur is not None and cur["status"] not in POOL_KNOWN:
            # parked under a word the inbox board has no column for. The board
            # has no opinion about it, and no opinion must not be executed.
            skipped += 1
            actions.append({"card": cid, "op": "skip", "id": cur["id"],
                            "why": "pool status %r has no inbox column — "
                                   "forward sync leaves it alone" % cur["status"]})
            continue
        if cur is None:
            actions.append({"card": cid, "op": "import", "status": st, "title": title})
            imported += 1
        elif cur["status"] == st and not _content_diff(c, cur):
            actions.append({"card": cid, "op": "same", "id": cur["id"]})
        else:
            fields = _content_diff(c, cur)
            if cur["status"] != st:
                fields = ["status"] + fields
            actions.append({"card": cid, "op": "update", "id": cur["id"],
                            "from": cur["status"], "to": st, "fields": fields})
            updated += 1
    return {"ok": True, "cards": len(board), "imported": imported,
            "updated": updated, "skipped": skipped, "applied": False,
            "actions": actions}


def apply():
    """Execute the plan: register inbox-bridge, import new cards, update moved
    ones. Returns the same shape as plan() with applied:true. Idempotent —
    running it twice imports nothing new (src: tag dedup)."""
    board, err = read_board()
    if err:
        return {"ok": False, "error": err, "actions": []}
    try:
        if not pool.is_registered(BRIDGE_APP):
            pool.register_app(BRIDGE_APP)
    except Exception as e:
        return {"ok": False, "error": "register failed: %s" % e, "actions": []}
    existing = _existing_by_src()
    actions, imported, updated, skipped = [], 0, 0, 0
    for c in board:
        cid = str(c.get("id", ""))
        src = "src:inbox-%s" % cid
        st = INBOX_STATUS.get(c.get("status", ""))
        title = (c.get("title") or "synced from inbox card #%s" % cid).strip()[:80]
        desc = (c.get("body") or "").strip()
        if not cid or not title:
            skipped += 1
            actions.append({"card": cid, "op": "skip", "why": "missing id/title"})
            continue
        if st is None:
            skipped += 1
            actions.append({"card": cid, "op": "skip",
                            "why": "unknown status %r" % c.get("status")})
            continue
        cur = existing.get(src)
        if cur is not None and cur["status"] not in POOL_KNOWN:
            skipped += 1
            actions.append({"card": cid, "op": "skip", "id": cur["id"],
                            "why": "pool status %r has no inbox column — "
                                   "forward sync leaves it alone" % cur["status"]})
            continue
        if cur is None:
            row = taskboard.task_create(
                BRIDGE_APP, title, desc=desc,
                tags=",".join([src] + _card_tags(c)), status=st,
                owner="inbox-manager" if st == "in_progress" else "")
            actions.append({"card": cid, "op": "import", "id": row["id"],
                            "status": st, "title": title})
            imported += 1
        else:
            fields = _content_diff(c, cur)
            status_need = cur["status"] != st
            if not status_need and not fields:
                actions.append({"card": cid, "op": "same", "id": cur["id"]})
                continue
            # guard=False: this direction is the card talking about itself. A
            # hold exists to stop PLATFORMS stomping on each other, and the
            # inbox is where the card lives, not a peer of it — refusing here
            # would mean a card can never record what it already says. The Held
            # catch is the safety net if that ever changes: report the conflict
            # rather than take the whole sync down.
            #
            # task_update() drops `note` when `desc` is given (replace wins),
            # so a body-carrying update embeds its own audit line instead.
            new_title = c.get("title", "").strip()[:80] if "title" in fields else ""
            if "body" in fields:
                new_desc = (c.get("body") or "").strip()
                if status_need:
                    new_desc = (new_desc + "\nsync from inbox card #%s" % cid).strip()
            else:
                new_desc = None
            want_tags = {t.lower() for t in _card_tags(c)} | {src}
            have_tags = {t.lower() for t in cur.get("tags", [])}
            tag_add = sorted(want_tags - have_tags) if "tags" in fields else ()
            tag_del = sorted(have_tags - want_tags) if "tags" in fields else ()
            owner = ("inbox-manager"
                     if status_need and st == "in_progress" and not cur["owner"] else "")
            try:
                if new_desc is not None:
                    taskboard.task_update(
                        BRIDGE_APP, cur["id"],
                        status=(st if status_need else ""), owner=owner,
                        title=new_title, desc=new_desc,
                        add_tags=tag_add, remove_tags=tag_del, guard=False)
                else:
                    taskboard.task_update(
                        BRIDGE_APP, cur["id"],
                        status=(st if status_need else ""), owner=owner,
                        title=new_title,
                        note=("sync from inbox card #%s" % cid if status_need else ""),
                        add_tags=tag_add, remove_tags=tag_del, guard=False)
            except taskboard.Held as e:
                skipped += 1
                actions.append({"card": cid, "op": "conflict", "id": cur["id"],
                                "why": str(e)})
                continue
            done_fields = (["status"] if status_need else []) + fields
            actions.append({"card": cid, "op": "update", "id": cur["id"],
                            "from": cur["status"], "to": st,
                            "fields": done_fields})
            updated += 1
    return {"ok": True, "applied": True, "cards": len(board), "imported": imported,
            "updated": updated, "skipped": skipped, "actions": actions}


def status():
    return taskboard.task_summary()


# ── pool → inbox: the return direction ─────────────────────────────────
@contextlib.contextmanager
def _state_lock(timeout=5.0):
    """A mutex around the state file so two pulls running at once cannot both
    read the same board and then each write their own answer.

    O_CREAT|O_EXCL is the lock: atomic on every filesystem the pool already
    runs on, and self-expiring — the file exists exactly as long as the holder
    lives, and a lock older than LOCK_STALE_SEC belongs to a process that died
    mid-write and is broken rather than honoured.

    It does NOT lock out the inbox MCP server, which writes the same file with
    no lock at all. That half is covered by the compare-and-swap in
    _write_back: the board is re-read inside the lock and the write is
    abandoned if the card moved while we were planning."""
    lp = INBOX_STATE_FILE + ".lock"
    end = time.time() + timeout
    fd = None
    while True:
        try:
            fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            fd = None
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lp) > LOCK_STALE_SEC:
                    os.remove(lp)
                    continue
            except OSError:
                pass
            if time.time() > end:
                raise TimeoutError("state file is locked by another writer: %s" % lp)
            time.sleep(0.02)
    try:
        yield lp
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(lp)
        except OSError:
            pass


def _save_json(path, data):
    """tmp + os.replace. The MCP server's own _save_state is a bare
    write_text, so a crash mid-write leaves a truncated board; ours cannot."""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)   # same shape the MCP writes
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _write_back(cid, status, detail, expect_updated_at,
                title=None, body=None, tags=None):
    """Set one card exactly the way board_update() would — same status, same
    appended detail, same ISO updated_at — but atomically, and only if the card
    has not moved since we planned against it. Returns (ok, why, new_ts); on a
    conflict it writes nothing at all.

    title/body/tags carry the pool task's content the same way: title replaces,
    body replaces (the detail line is appended after it), tags replace the
    whole list. Pass None for any field that should be left alone."""
    with _state_lock():
        try:
            with open(INBOX_STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError) as e:
            return False, "cannot read state file: %s" % e, ""
        for c in state.get("board", []):
            if str(c.get("id")) != str(cid):
                continue
            if expect_updated_at and c.get("updated_at") != expect_updated_at:
                return False, ("card #%s changed in inbox at %s, we planned "
                               "against %s" % (cid, c.get("updated_at"),
                                               expect_updated_at)), ""
            c["status"] = status
            if title is not None:
                c["title"] = title
            if body is not None:
                c["body"] = body
            if tags is not None:
                c["tags"] = tags
            if detail:
                c["body"] = (c.get("body", "") + "\n" + detail).strip()
            ts = datetime.datetime.now().isoformat()
            c["updated_at"] = ts
            try:
                _save_json(INBOX_STATE_FILE, state)
            except OSError as e:
                return False, "write failed: %s" % e, ""
            return True, None, ts
        return False, "card #%s not found" % cid, ""


def _iso_epoch(s):
    """An ISO timestamp as epoch seconds, or 0.0 when it is missing or from a
    card written by something older than datetime.fromisoformat understands."""
    try:
        return datetime.datetime.fromisoformat(str(s)).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _load_pull_state():
    try:
        with open(PULL_STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_pull_state(d):
    try:
        _save_json(PULL_STATE_FILE, d)
    except OSError:
        pass          # a lost baseline costs one extra pull, never a bad write


def _remember(seen, src, card, task):
    """The baseline both sides agreed on, so the next pull can tell "moved"
    from "never moved" and know whether the card was touched in between."""
    seen[src] = {"card_updated_at": card.get("updated_at", ""),
                 "task_t": task.get("t") or 0,
                 "task_status": task.get("status", "")}


def pull(apply=False):
    """Pool → inbox. Diff every mirrored task against its card and write back
    the ones another platform moved. Dry-run by default, same shape as plan():
    {cards, pulled, skipped, conflicts, actions}."""
    board, err = read_board()
    if err:
        return {"ok": False, "error": err, "actions": []}
    if not os.path.exists(INBOX_STATE_FILE):
        return {"ok": False, "error": "pull needs a writable state file; set "
                                     "INBOX_STATE_FILE (got %s)" % INBOX_STATE_FILE,
                "actions": []}
    base = _load_pull_state().get("cards", {})
    existing = _existing_by_src()
    actions, pulled, skipped, conflicts = [], 0, 0, 0
    seen = {}
    for c in board:
        cid = str(c.get("id", ""))
        src = "src:inbox-%s" % cid
        t = existing.get(src)
        if t is None:
            continue                      # never imported — apply()'s job
        target = POOL_STATUS.get(t["status"], "")
        pri = base.get(src)
        card_t, pool_t = _iso_epoch(c.get("updated_at")), (t.get("t") or 0)
        fields = [] if not target else _content_diff(c, t)
        status_need = bool(target) and c.get("status") != target
        if not target:
            op, why = "skip", "pool status %r has no inbox equivalent" % t["status"]
        elif not status_need and not fields:
            op, why = "same", ""          # nothing to write, whatever the history
        elif card_t >= pool_t - TIE_SEC:
            # the card is not behind the task, so inbox wins this one. No
            # baseline needed: the card's own updated_at against the task's last
            # revision is the same question, and it also holds on a first pull.
            near = " (a tie — inbox takes ties)" if card_t - pool_t < TIE_SEC else ""
            op = "conflict"
            why = ("card edited %s, pool task last moved %ds ago — inbox is "
                   "not behind, so inbox wins; run apply to carry it%s"
                   % (c.get("updated_at"), int(max(0, time.time() - pool_t)), near))
            conflicts += 1
        elif pri is not None and pool_t <= (pri.get("task_t") or 0):
            op, why = "skip", "task has not moved since the last pull"
        elif not taskboard.hold_guard(BRIDGE_APP, t["id"])[0]:
            op, why = "skip", taskboard.hold_guard(BRIDGE_APP, t["id"])[1]
        else:
            op, why = "update", ""
        action = {"card": cid, "op": op, "id": t["id"], "from": c.get("status", ""),
                  "to": target, "why": why}
        if op == "update":
            bits = []
            if status_need:
                bits.append("%s → %s" % (t["status"], target))
            for f in fields:
                bits.append({"title": "title", "body": "body",
                             "tags": "tags"}[f])
            detail = "pool task %s %s%s" % (
                t["id"], " ".join(bits),
                (" by @%s" % t["owner"]) if t["owner"] else "")
            action["why"] = detail
            action["fields"] = (["status"] if status_need else []) + fields
            if apply:
                ok, why2, ts = _write_back(
                    cid, target, detail, c.get("updated_at", ""),
                    title=(t["title"] if "title" in fields else None),
                    body=(_strip_auto(t.get("desc") or "") if "body" in fields else None),
                    tags=(list(_task_user_tags(t)) if "tags" in fields else None))
                if not ok:
                    action["op"] = "conflict"
                    action["why"] = why2
                    conflicts += 1
                else:
                    action["updated_at"] = ts
                    pulled += 1
            else:
                pulled += 1
        elif op in ("skip", "conflict"):
            skipped += 1
        actions.append(action)
        # the baseline moves forward only for cards we did not write, so a
        # failed write is retried next run instead of being silently accepted
        if action["op"] == "update":
            c = dict(c, updated_at=action.get("updated_at", c.get("updated_at", "")))
        _remember(seen, src, c, t)
    if apply:
        _save_pull_state({"cards": seen, "pulled_at": datetime.datetime.now().isoformat()})
    return {"ok": True, "applied": bool(apply), "cards": len(actions),
            "pulled": pulled, "skipped": skipped, "conflicts": conflicts,
            "actions": actions}


def _print_plan(p):
    if not p.get("ok"):
        print("err  %s" % p.get("error"))
        return 1
    print("cards %d  →  import %d, update %d, skip %d%s" % (
        p["cards"], p["imported"], p["updated"], p["skipped"],
        "  [applied]" if p.get("applied") else "  [dry-run]"))
    for a in p["actions"]:
        if a["op"] == "import":
            print("  + %-6s %s" % (a.get("status", ""), a.get("title", "")))
        elif a["op"] == "update":
            extra = " +%s" % ",".join(a.get("fields", [])) if a.get("fields") else ""
            print("  ~ #%s task %s: %s → %s%s" % (a["card"], a["id"], a["from"], a["to"], extra))
        elif a["op"] == "conflict":
            print("  ! #%s CONFLICT: %s" % (a.get("card", "?"), a.get("why", "")))
        elif a["op"] == "skip":
            print("  ! #%s skipped: %s" % (a.get("card", "?"), a.get("why", "")))
    return 0


def _print_pull(p):
    if not p.get("ok"):
        print("err  %s" % p.get("error"))
        return 1
    print("cards %d  →  pull %d, skip %d, conflict %d%s" % (
        p["cards"], p["pulled"], p["skipped"], p["conflicts"],
        "  [applied]" if p.get("applied") else "  [dry-run]"))
    for a in p["actions"]:
        if a["op"] == "update":
            print("  ← #%s task %s: %s → %s   (%s)" % (a["card"], a["id"], a["from"],
                                                       a["to"], a.get("why", "")))
        elif a["op"] == "conflict":
            print("  ! #%s CONFLICT: %s" % (a.get("card", "?"), a.get("why", "")))
        elif a["op"] == "skip" and a.get("why"):
            print("  · #%s held/not-moved: %s" % (a["card"], a["why"]))
    return 0


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "plan"
    if cmd == "plan":
        return _print_plan(plan())
    if cmd == "apply":
        return _print_plan(apply())
    if cmd == "pull":
        # `pull` alone is a dry-run; `pull apply` writes. The whole board is
        # re-read either way, so the dry-run is exactly the applied run.
        return _print_pull(pull(apply="apply" in argv[2:]))
    if cmd == "holds":
        hs = taskboard.hold_list()
        if not hs:
            print("no holds — every task is writable by anyone")
        for h in hs:
            print("  @%-12s %-18s %4ds left   %s"
                  % (h["holder"], h["id"], h["free_in"], h.get("title", "")[:60]))
        return 0
    if cmd == "status":
        s = status()
        print("tasks %d  " % s["total"] + "  ".join(
            "%s=%d" % (k, v) for k, v in s["counts"].items() if v))
        for w in s["in_progress"]:
            print("  ▶ @%s  %s  (%s)" % (w["owner"], w["title"][:70], w["id"]))
        if not s["in_progress"]:
            print("  (nobody is working on anything right now)")
        return 0
    if cmd == "tasks":
        rows = taskboard.task_list()
        if not rows:
            print("board is empty")
        for t in rows:
            print(taskboard.render_row(t))
        return 0
    if cmd == "demo":
        return 0 if taskboard.demo() else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
