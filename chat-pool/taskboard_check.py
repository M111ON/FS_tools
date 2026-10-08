"""taskboard_check.py — does the task layer answer over HTTP, on a real log?

taskboard.py's demo proves the library layer. This proves the thing a
platform actually talks to: a server, on a fresh log, answering the four
questions the board exists for —

  * can a platform POST work, and another platform CLAIM it (owner visible)?
  * does /tasks filter by status/owner and count who is doing what?
  * does the inbox bridge import cards (todo->pending, owner on in_progress)?
  * is /tasks/sync a REAL dry-run by default — zero writes until apply=true?
  * does a CLAIM take a hold — read-only for everyone else until it lapses?
  * does /tasks/pull write a moved task back onto its card — and refuse to
    write one somebody else holds, or one the inbox has moved past?
  * can the board be WRITTEN to — new, rename, re-describe, retag, move to a
    status nobody declared — and does ?view=open/folded split work from kept?

Run:  python taskboard_check.py
"""
import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
# Everything pool_server.py imports from THIS directory has to travel with it.
# Same list and same reason as prune_check.py: `ws_bridge` was wired into the
# server and this tuple was left behind, so both suites died on a
# ModuleNotFoundError from a child process that named nothing useful.
COPIES = ("pool.py", "pool_server.py", "ctx_plane.py", "taskboard.py",
          "ws_bridge.py", "pool_cloud.py", "inbox_bridge.py", "tasks_ui.html")


def local_imports(start="pool_server.py"):
    """Every module of THIS directory that `start` pulls in, transitively.

    The closure matters, not the first hop: `pool_server` imports `ws_bridge`,
    which imports `pool_cloud`, so a copy list that carries the first and not
    the second still starts a server that cannot boot. `ast` over each local
    file is the only source that cannot go stale — a hand-written tuple drifts
    the moment anyone adds an import, and the failure arrives as a
    ModuleNotFoundError from a child process that names nothing useful.
    """
    seen, want = set(), [start]
    while want:
        name = want.pop()
        stem = os.path.splitext(name)[0]
        if stem in seen or not os.path.exists(os.path.join(HERE, name)):
            continue
        seen.add(stem)
        try:
            tree = ast.parse(open(os.path.join(HERE, name), encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for n in names:
                if os.path.exists(os.path.join(HERE, n + ".py")) and n not in seen:
                    want.append(n + ".py")
    return sorted(seen - {os.path.splitext(c)[0] for c in COPIES})



# ── harness (same shape as prune_check.py) ──────────────────────────────
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def req(port, path, body=None, timeout=8):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "ignore") or "{}")


def wait_health(port, proc, deadline=25.0):
    end = time.time() + deadline
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if req(port, "/health", timeout=2)[1].get("ok"):
                return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.25)
    return False


def main():
    missing = local_imports()
    if missing:
        print("taskboard_check: FAILED — the sandbox would start a server that "
              "cannot import:\n  %s\n  pool_server.py imports %s, and COPIES "
              "does not carry it. Add it to COPIES above."
              % (", ".join(m + ".py" for m in missing), ", ".join(missing)))
        return 1
    box = tempfile.mkdtemp(prefix="taskboard-check-")
    results = []
    proc = None
    out = None
    try:
        for name in COPIES:
            shutil.copy2(os.path.join(HERE, name), os.path.join(box, name))
        # a fake .inbox_state.json: the board the bridge will import. The
        # updated_at fields are what the pull direction compares against the
        # task's last revision, so they have to exist and be real ISO.
        fake_board = [
            {"id": 1, "title": "wire BreathingFS into the pool", "status": "in_progress",
             "body": "phase 1 of the migration", "created_at": "2026-09-25T08:00:00",
             "updated_at": "2026-09-25T08:00:00"},
            {"id": 2, "title": "port flush cursor", "status": "todo",
             "body": "", "created_at": "2026-09-25T08:01:00",
             "updated_at": "2026-09-25T08:01:00"},
            {"id": 3, "title": "gpu parity re-run", "status": "done",
             "body": "0/11520 mismatch", "created_at": "2026-09-25T08:02:00",
             "updated_at": "2026-09-25T08:02:00"},
            {"id": 4, "title": "no-status card", "status": "weird",
             "body": "", "created_at": "2026-09-25T08:03:00",
             "updated_at": "2026-09-25T08:03:00"},
        ]
        state_path = os.path.join(box, "fake_inbox_state.json")
        pull_state = os.path.join(box, ".inbox_pull_state.json")

        def save_state():
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"project_name": "fake", "board": fake_board}, f, indent=2)

        def reload_state():
            # The bridge writes this file. Without re-reading before we touch it,
            # a stale in-memory copy quietly undoes a write-back the moment the
            # test edits an unrelated card — and the test passes on a lie.
            try:
                with open(state_path, encoding="utf-8") as f:
                    fake_board[:] = json.load(f)["board"]
            except (OSError, ValueError, KeyError):
                pass

        def add_card(c):
            reload_state()
            fake_board.append(c)
            save_state()

        def card(cid):
            # read from DISK, not from fake_board: a pull writes the real file,
            # so an assertion that looked at our own copy would pass whatever
            # the bridge actually did.
            with open(state_path, encoding="utf-8") as f:
                board = json.load(f)["board"]
            return next(c for c in board if c["id"] == cid)

        def edit(cid, **kw):
            reload_state()
            next(c for c in fake_board if c["id"] == cid).update(kw)
            save_state()

        save_state()

        port = free_port()
        env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8",
                   INBOX_STATE_FILE=state_path, PULL_STATE_FILE=pull_state)
        out = open(os.path.join(box, "server.log"), "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(box, "pool_server.py")],
            cwd=box, env=env, stdout=out, stderr=subprocess.STDOUT)
        if not wait_health(port, proc):
            raise RuntimeError("server never became healthy")

        # register the two platforms that will use the board
        req(port, "/register", {"app": "opencode"})
        req(port, "/register", {"app": "hermes"})

        # 1. create + claim + finish, over HTTP
        st, r = req(port, "/task", {"app": "opencode", "title": "ship the task board",
                                    "desc": "blocks on the shared chain", "tags": "tasks,pool"})
        results.append(("POST /task creates pending", st == 200 and r["status"] == "pending"))
        tid = r["id"]
        st, r = req(port, "/task/update", {"app": "hermes", "id": tid, "action": "claim"})
        results.append(("claim sets owner+in_progress",
                        st == 200 and r["owner"] == "hermes" and r["status"] == "in_progress"))
        st, r = req(port, "/task/update", {"app": "hermes", "id": tid,
                                           "status": "done", "note": "shipped"})
        results.append(("finish writes a new rev", st == 200 and r["status"] == "done" and r["rev"] == 3))

        # 2. sub-task + filters + summary
        st, r = req(port, "/task", {"app": "opencode", "title": "sub: board page",
                                    "parent": tid})
        sub = r["id"]
        st, r = req(port, "/tasks?parent=%s" % tid.replace("task-", ""))
        results.append(("?parent= filter", st == 200 and len(r["tasks"]) == 1 and
                        r["tasks"][0]["id"] == sub))
        st, r = req(port, "/tasks?owner=hermes")
        results.append(("?owner= filter", st == 200 and
                        all(t["owner"] == "hermes" for t in r["tasks"]) and len(r["tasks"]) == 1))
        st, r = req(port, "/tasks/summary")
        results.append(("summary counts + in_progress owner",
                        st == 200 and r["counts"]["done"] == 1 and
                        r["in_progress"] == [] and r["total"] == 2))

        # 3. dry-run: imports planned, ZERO writes
        st, r = req(port, "/tasks/sync", {})
        results.append(("sync dry-run ok", st == 200 and r["ok"] and r["applied"] is False))
        results.append(("dry-run plans 3, skips 1",
                        r["imported"] == 3 and r["skipped"] == 1))
        st, after = req(port, "/tasks")
        results.append(("dry-run wrote nothing", len(after["tasks"]) == 2))

        # 4. apply: cards become pool tasks, owner rides in_progress
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("sync apply ok", st == 200 and r["applied"] and
                        r["imported"] == 3 and r["updated"] == 0))
        st, after = req(port, "/tasks")
        by_src = {}
        for t in after["tasks"]:
            for tag in t["tags"]:
                if tag.startswith("src:inbox-"):
                    by_src[tag] = t
        results.append(("in_progress card got owner",
                        by_src.get("src:inbox-1", {}).get("owner") == "inbox-manager" and
                        by_src.get("src:inbox-1", {}).get("status") == "in_progress"))
        results.append(("todo card → pending", by_src.get("src:inbox-2", {}).get("status") == "pending"))
        results.append(("done card → done", by_src.get("src:inbox-3", {}).get("status") == "done"))

        # 5. idempotency: second apply imports nothing new
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("re-apply imports nothing", r["imported"] == 0))
        # a moved card updates instead of duplicating
        edit(2, status="in_progress")
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("moved card → update, not duplicate",
                        r["imported"] == 0 and r["updated"] == 1))
        st, t2 = req(port, "/tasks?status=in_progress")
        results.append(("update took effect", len(t2["tasks"]) == 2 and
                        any(t["id"] == by_src["src:inbox-2"]["id"] for t in t2["tasks"])))

        # 6. holds: a claim takes the card, everyone else may read and not write
        st, r = req(port, "/task", {"app": "opencode", "title": "a claim worth taking",
                                    "tags": "pool"})
        claim_tid = r["id"]
        req(port, "/task/update", {"app": "hermes", "id": claim_tid, "action": "claim"})
        st, r = req(port, "/holds")
        results.append(("/holds lists a claimed task",
                        st == 200 and r["idle_sec"] == 900 and
                        any(h["id"] == claim_tid and h["holder"] == "hermes"
                            for h in r["holds"])))
        st, r = req(port, "/tasks")
        ch = next(t for t in r["tasks"] if t["id"] == claim_tid)["hold"]
        results.append(("/tasks carries the hold",
                        ch["held"] and ch["holder"] == "hermes" and ch["free_in"] > 0))
        st, r = req(port, "/task/update", {"app": "opencode", "id": claim_tid,
                                           "status": "done", "note": "not mine"})
        results.append(("held task is read-only for others",
                        st == 409 and r.get("held") and
                        "held by @hermes" in r["error"]))
        st, r = req(port, "/task/update", {"app": "opencode", "id": claim_tid,
                                           "action": "claim"})
        results.append(("held task cannot be stolen", st == 409))
        st, r = req(port, "/tasks")
        results.append(("a refused write changed nothing",
                        any(t["id"] == claim_tid and t["status"] == "in_progress" and
                            t["owner"] == "hermes" for t in r["tasks"])))

        # 7. pull: pool → inbox, with the hold in the way
        add_card({"id": 6, "title": "pull me home", "status": "todo",
                 "body": "", "created_at": "2026-09-25T08:05:00",
                 "updated_at": "2026-09-25T08:05:00"})
        req(port, "/tasks/sync", {"apply": True})
        st, r = req(port, "/tasks")
        t6 = next(t for t in r["tasks"] if "src:inbox-6" in t["tags"])
        req(port, "/task/update", {"app": "hermes", "id": t6["id"], "action": "claim"})
        st, r = req(port, "/tasks/pull", {})
        results.append(("pull dry-run writes nothing",
                        st == 200 and r["applied"] is False and r["pulled"] == 0))
        results.append(("a held card is read-only from the inbox too",
                        any(a["card"] == "6" and a["op"] == "skip" and
                            "held by @hermes" in a["why"] for a in r["actions"])))
        st, r = req(port, "/tasks/pull", {"apply": True})
        results.append(("applied pull respects the hold",
                        r["pulled"] == 0 and card(6)["status"] == "todo"))

        # the holder finishes it: free again, and now the pool is ahead
        req(port, "/task/update", {"app": "hermes", "id": t6["id"], "status": "done"})
        st, r = req(port, "/tasks/pull", {})
        results.append(("pull plans the card the pool finished",
                        r["pulled"] == 1 and
                        any(a["card"] == "6" and a["op"] == "update" and
                            a["to"] == "done" for a in r["actions"])))
        results.append(("dry-run pull wrote nothing", card(6)["status"] == "todo"))
        st, r = req(port, "/tasks/pull", {"apply": True})
        results.append(("applied pull writes the card back",
                        r["pulled"] == 1 and card(6)["status"] == "done"))
        results.append(("the write-back names the task that did it",
                        t6["id"] in (card(6).get("body") or "")))
        results.append(("write-back leaves no tmp and no lock",
                        not [n for n in os.listdir(box)
                             if n.startswith("fake_inbox_state.json.")]))

        # 8. no ping-pong: a settled board pulls nothing, forwards nothing
        st, r = req(port, "/tasks/pull", {})
        results.append(("settled board pulls nothing",
                        r["pulled"] == 0 and r["conflicts"] == 0))
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("settled board forwards nothing",
                        r["imported"] == 0 and r["updated"] == 0))

        # 9. conflict: the card moves in the inbox while the task moves on the
        #    pool. The card is not behind, so it wins and pull steps aside.
        add_card({"id": 5, "title": "tug of war", "status": "todo",
                 "body": "", "created_at": "2026-09-25T08:04:00",
                 "updated_at": "2026-09-25T08:04:00"})
        req(port, "/tasks/sync", {"apply": True})
        st, r = req(port, "/tasks")
        t5 = next(t for t in r["tasks"] if "src:inbox-5" in t["tags"])
        req(port, "/task/update", {"app": "hermes", "id": t5["id"], "status": "done"})
        edit(5, status="blocked",                       # the user edits the card...
             updated_at=time.strftime("%Y-%m-%dT%H:%M:%S.000000"))
                                                    # ...after the pool moved
        st, r = req(port, "/tasks/pull", {"apply": True})
        results.append(("inbox wins a two-sided edit",
                        card(5)["status"] == "blocked" and r["conflicts"] == 1 and
                        r["pulled"] == 0))
        results.append(("the conflict is explained",
                        any(a["card"] == "5" and "inbox wins" in a["why"]
                            for a in r["actions"])))
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("apply carries the inbox's version to the pool",
                        r["updated"] == 1))

        # 11b. a custom status the inbox board has no column for must survive
        #      the forward sync — "no opinion" must not become "reset to pending"
        st, r = req(port, "/task/update", {"app": "opencode",
                                           "id": t5["id"], "status": "icebox"})
        results.append(("park a card under a status inbox has no column for",
                        st == 200 and r["status"] == "icebox"))
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("forward sync refuses to flatten it",
                        any(a["card"] == "5" and a["op"] == "skip" and
                            "no inbox column" in a["why"] for a in r["actions"])))
        st, r = req(port, "/tasks")
        results.append(("the custom status is still there",
                        any(t["id"] == t5["id"] and t["status"] == "icebox"
                            for t in r["tasks"])))
        st, r = req(port, "/tasks/pull", {})
        results.append(("pull leaves it alone too",
                        any(a["card"] == "5" and a["op"] == "skip" and
                            "no inbox equivalent" in a["why"] for a in r["actions"])))

        # 11. the board is writable: new, edit, custom status, tags
        st, r = req(port, "/task", {"app": "opencode", "title": "small thing",
                                    "status": "notice", "tags": "crumb, inbox"})
        results.append(("a status nobody declared is still a status",
                        st == 200 and r["status"] == "notice"))
        note_id = r["id"]
        st, r = req(port, "/task", {"app": "opencode", "title": "bad status",
                                    "status": "in progress"})
        results.append(("an unparseable status is a 400, not a card",
                        st == 400 and "must match" in r["error"]))
        st, r = req(port, "/task/update", {"app": "opencode", "id": note_id,
                                           "title": "renamed", "desc": "edited",
                                           "add_tags": ["ui", "notice"],
                                           "remove_tags": ["inbox"]})
        results.append(("title + desc + tags edit in one rev",
                        st == 200 and r["title"] == "renamed" and
                        r["desc"] == "edited" and "ui" in r["tags"] and
                        "inbox" not in r["tags"] and "crumb" in r["tags"]))
        st, r = req(port, "/task/update", {"app": "opencode",
                                           "id": by_src["src:inbox-3"]["id"],
                                           "remove_tags": ["src:inbox-3"]})
        results.append(("the src: dedup tag cannot be removed",
                        st == 200 and "src:inbox-3" in r["tags"]))

        # 12. kept but not on top: the fold the page draws is the API's fold
        st, r = req(port, "/tasks?view=open")
        results.append(("?view=open is the working set only",
                        st == 200 and r["summary"] is None and
                        all(t["status"] in ("pending", "in_progress", "blocked") and
                            "crumb" not in t["tags"] for t in r["tasks"]) and
                        r["tasks"]))
        st, r = req(port, "/tasks?view=folded")
        results.append(("?view=folded is the kept half",
                        st == 200 and r["tasks"] and
                        all(not (t["status"] in ("pending", "in_progress", "blocked")
                                 and "crumb" not in t["tags"]) for t in r["tasks"])))
        st, r = req(port, "/tasks?tag=crumb")
        results.append(("?tag= filter", st == 200 and len(r["tasks"]) == 1 and
                        r["tasks"][0]["id"] == note_id))
        st, r = req(port, "/tasks?view=nope")
        results.append(("?view= is checked, not guessed", st == 400))
        st, r = req(port, "/tasks/summary")
        results.append(("summary knows the custom status",
                        st == 200 and r["counts"].get("notice") == 1 and
                        "notice" in r["statuses"] and "crumb" in r["tags"] and
                        r["open"] + r["folded"] == r["total"]))

        # 13. /tasks/ui serves the writable board
        with urllib.request.urlopen("http://127.0.0.1:%d/tasks/ui" % port, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        results.append(("tasks/ui serves the board page",
                        "task board" in html and "/tasks" in html))
        results.append(("tasks/ui can write and can fold",
                        'id="b-new"' in html and "?view=" not in html and
                        'details class="drawer"' in html and
                        "crumb" in html))
        # 13b. the no-s spelling is the same page, not a 404 — people type it,
        # and an old bookmark may point at it; both must land on the board
        with urllib.request.urlopen("http://127.0.0.1:%d/task/ui" % port, timeout=5) as resp:
            alias = resp.read().decode("utf-8")
        results.append(("/task/ui (no s) is an alias, not a 404",
                        resp.status == 200 and "task board" in alias
                        and alias == html))
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if out is not None:
            out.close()

    fails = [name for name, ok in results if not ok]
    for name, ok in results:
        print("  %-38s %s" % (name, "ok" if ok else "FAIL"))
    print("taskboard_check: %d/%d ok" % (len(results) - len(fails), len(results)))
    if fails:
        log = os.path.join(box, "server.log")
        if os.path.exists(log):
            with open(log, encoding="utf-8", errors="ignore") as f:
                print("--- server.log tail ---\n" + "".join(f.readlines()[-12:]))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
