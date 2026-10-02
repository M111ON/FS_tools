"""taskboard_check.py — does the task layer answer over HTTP, on a real log?

taskboard.py's demo proves the library layer. This proves the thing a
platform actually talks to: a server, on a fresh log, answering the four
questions the board exists for —

  * can a platform POST work, and another platform CLAIM it (owner visible)?
  * does /tasks filter by status/owner and count who is doing what?
  * does the inbox bridge import cards (todo->pending, owner on in_progress)?
  * is /tasks/sync a REAL dry-run by default — zero writes until apply=true?

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
          "ws_bridge.py", "pool_cloud.py")


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
        # a fake .inbox_state.json: the board the bridge will import
        fake_board = [
            {"id": 1, "title": "wire BreathingFS into the pool", "status": "in_progress",
             "body": "phase 1 of the migration", "created_at": "2026-09-25T08:00:00"},
            {"id": 2, "title": "port flush cursor", "status": "todo",
             "body": "", "created_at": "2026-09-25T08:01:00"},
            {"id": 3, "title": "gpu parity re-run", "status": "done",
             "body": "0/11520 mismatch", "created_at": "2026-09-25T08:02:00"},
            {"id": 4, "title": "no-status card", "status": "weird",
             "body": "", "created_at": "2026-09-25T08:03:00"},
        ]
        with open(os.path.join(box, "fake_inbox_state.json"), "w", encoding="utf-8") as f:
            json.dump({"project_name": "fake", "board": fake_board}, f)

        port = free_port()
        env = dict(os.environ, POOL_PORT=str(port), PYTHONIOENCODING="utf-8",
                   INBOX_STATE_FILE=os.path.join(box, "fake_inbox_state.json"))
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
        with open(os.path.join(box, "fake_inbox_state.json"), "w", encoding="utf-8") as f:
            fake_board[1]["status"] = "in_progress"
            json.dump({"project_name": "fake", "board": fake_board}, f)
        st, r = req(port, "/tasks/sync", {"apply": True})
        results.append(("moved card → update, not duplicate",
                        r["imported"] == 0 and r["updated"] == 1))
        st, t2 = req(port, "/tasks?status=in_progress")
        results.append(("update took effect", len(t2["tasks"]) == 2 and
                        any(t["id"] == by_src["src:inbox-2"]["id"] for t in t2["tasks"])))

        # 6. /tasks/ui serves the board page
        with urllib.request.urlopen("http://127.0.0.1:%d/tasks/ui" % port, timeout=5) as resp:
            html = resp.read().decode("utf-8")
        results.append(("tasks/ui serves the board page",
                        "task board" in html and "/tasks" in html))
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
