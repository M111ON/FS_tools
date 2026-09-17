"""pool_cloud.py — best-effort mirror of log.jsonl to cloud-memory-worker.

Scope (owner decision 2026-09-17): local-first + cloud BACKUP ONLY.
- The hash chain lives here (forest model); the worker verifies sha256 +
  chain continuity per device before storing, and 409s on any gap.
- Called after flush (never blocks the hourly flush on failure);
  offline / key missing / worker down = skip quietly, retried next flush.
Usage:
    python pool_cloud.py            # push new lines since last ack
    python pool_cloud.py --status   # show cloud head vs local
"""
import json
import os
import sys
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool  # noqa: E402

WORKER = os.environ.get("POOL_CF_URL", "https://cloud-memory-worker.aexid03.workers.dev")
KEY_FILE_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".api_key"),
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cloud_ws", ".api_key"),
]
STATE = os.path.join(pool.DIR, ".cloud_push_state.json")
DEVICE = os.environ.get("POOL_DEVICE", os.environ.get("COMPUTERNAME", "this-pc").lower())


def _key():
    k = os.environ.get("CF_MEMORY_KEY", "")
    if k:
        return k
    for p in KEY_FILE_CANDIDATES:
        if os.path.exists(p):
            return open(p, encoding="utf-8").read().strip()
    return ""


def _load_state():
    if os.path.exists(STATE):
        try:
            return json.load(open(STATE, encoding="utf-8"))
        except Exception:
            pass
    return {"acked": 0}  # number of local lines already mirrored


def _save_state(acked):
    json.dump({"acked": acked}, open(STATE, "w", encoding="utf-8"))


def _local_lines():
    if not os.path.exists(pool.LOG):
        return []
    with open(pool.LOG, encoding="utf-8") as f:
        return [json.loads(x) for x in f.read().strip().split("\n") if x]


def _post(path, payload, key):
    req = urllib.request.Request(
        WORKER + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-API-Key": key,
                 "User-Agent": "chat-pool/1.0"},  # CF 1010-blocks the default python UA
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def _get(path, key):
    req = urllib.request.Request(WORKER + path, headers={"X-API-Key": key,
                                                         "User-Agent": "chat-pool/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def push():
    key = _key()
    if not key:
        print("cloud-push SKIP: no key (CF_MEMORY_KEY / .api_key)")
        return
    lines = _local_lines()
    if not lines:
        print("cloud-push SKIP: empty log")
        return
    st = _load_state()
    acked = min(st.get("acked", 0), len(lines))
    todo = lines[acked:]
    if not todo:
        print("cloud-push SKIP: cloud up to date")
        return
    try:
        code, res = _post("/pool/ingest?device=" + DEVICE,
                          {"device": DEVICE, "lines": todo}, key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        print(f"cloud-push FAIL {e.code}: {body}")
        return
    except Exception as e:
        print(f"cloud-push FAIL (offline?): {e}")
        return
    if code == 200 and res.get("ok"):
        _save_state(len(lines))
        print(f"cloud-push OK: +{res['accepted']} lines (skipped {res['skipped']}), head {res['head'][:8]}")
    else:
        print(f"cloud-push FAIL {code}: {res}")


def status():
    key = _key()
    lines = _local_lines()
    local_head = lines[-1]["hash"][:8] if lines else "-"
    print(f"local : {len(lines)} lines, head {local_head} (device {DEVICE})")
    if not key:
        print("cloud : no key")
        return
    try:
        code, res = _get("/pool/head?device=" + DEVICE, key)
        print(f"cloud : seq {res.get('seq')}, head {str(res.get('head'))[:8]}")
    except Exception as e:
        print(f"cloud : unreachable ({e})")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--status":
        status()
    else:
        push()
