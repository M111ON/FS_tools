"""pool_wake.py — dreamer-style sleep/wake runner for chat-pool.

Like Magic Context's dreamer: no permanent agent process. A Windows
scheduled task wakes this script on a cron (default: every 15 min).
It peeks the pool inbox for pending mentions/DMs; when there is work,
it spawns `opencode run` one-shot in the target project root, feeding
the message; the agent does the job and exits. Otherwise: exit fast
(slept). Cost = one process spawn per wake, zero when idle.

Usage:
  python pool_wake.py                 # check + run if work (for schtasks)
  python pool_wake.py --peek          # show what is pending, do nothing
  python pool_wake.py --root I:/x     # override target root
  python pool_wake.py --app NAME      # identity (default opencode-agent)
"""
import argparse
import json
import subprocess
import sys
import urllib.request

POOL = "http://localhost:8390"
APP = "opencode-agent"
DEFAULT_ROOT = r"I:/tools/chat-pool"  # pinned root when mention has no /root
IDLE_EXIT_SECONDS = 5


def _get(path):
    req = urllib.request.Request(POOL + path)
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


def _register(app):
    req = urllib.request.Request(
        POOL + "/register", data=json.dumps({"app": app}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=8).read())


def peek(app):
    """Pending inbox without marking read."""
    return _get(f"/inbox?app={app}&mark=0")


def _parse_target(text):
    """'@opencode-agent/root text' -> (root_hint, body). None -> no mention."""
    prefix = "@" + APP
    if not text.startswith(prefix):
        return None, text
    rest = text[len(prefix):]
    if rest.startswith("/"):
        root, _, body = rest[1:].partition(" ")
        return (root or None), body.strip()
    return None, rest.strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=APP)
    ap.add_argument("--root", default=None, help="override target project root")
    ap.add_argument("--peek", action="store_true", help="show pending only")
    ap.add_argument("--model", default=None, help="provider/model for opencode run")
    args = ap.parse_args()

    _register(args.app)
    items = peek(args.app)
    if not items:
        print("sleep: inbox empty")
        return
    print(f"wake: {len(items)} pending")
    if args.peek:
        for m in items:
            print(f"  [{m['app']}] {m['text'][:80]}")
        return

    # choose root: --root > mention /root hint > DEFAULT_ROOT
    first = items[0]
    _, body = _parse_target(first.get("text", ""))
    root = args.root or DEFAULT_ROOT

    # mark read BEFORE run: if opencode crashes mid-job, the work prompt
    # lives in its own session log; avoids re-running forever on failure.
    _get(f"/inbox?app={args.app}&mark=1")

    cmd = ["opencode", "run", body]
    if args.model:
        cmd += ["--model", args.model]
    print(f"-> opencode run in {root}")
    r = subprocess.run(cmd, cwd=root, capture_output=True, text=True,
                       timeout=1800)
    out = (r.stdout or r.stderr or "").strip()
    print(out[-1500:] if out else "(no output)")
    sys.exit(r.returncode or 0)


if __name__ == "__main__":
    main()
