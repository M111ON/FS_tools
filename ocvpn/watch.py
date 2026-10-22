#!/usr/bin/env python3
r"""ocvpn watch — สลับ IP อัตโนมัติเมื่อ 9router ชน rate limit จริงๆ

poll    : อ่าน 9router SQLite (providerConnections.data JSON) หา errorCode=429
classify: แยก "IP-bound" (สลับ VPN ช่วยได้) ออกจาก "account-quota" (สลับไม่ช่วย — ห้ามแตะ)
probe   : เฉพาะตอนเจอ 429 ที่ IP-bound เท่านั้น ค่อยยิง probe ผ่าน 9router
unlock  : หลัง swap สำเร็จ -> PATCH /api/providers/{id} {"testStatus":"active"}
          ซึ่ง h() ใน build จะ clear errorCode/rateLimitedUntil/backoffLevel
          + modelLock_* ทุกตัว -> ไม่ต้องรอ backoff หมดอายุเอง

หลักการของเจ้าของเครื่อง: สลับ IP เมื่อชน rate limit เท่านั้น ห้ามสลับทุก prompt
(มิฉะนั้น connection reuse / cache พัง) -> จึง probe ก่อน hunt เสมอ

ดู log: Get-Content I:\tools\ocvpn\watch.log -Tail 40
"""
import json
import os
import sqlite3
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hunt  # reuse http(), probe(), egress_ip(), new_session(), log()

HERE = os.path.dirname(os.path.abspath(__file__))
LOGF = os.path.join(HERE, "watch.log")
WLOCK = os.path.join(HERE, "watch.lock")
WSTATE = os.path.join(HERE, "watch_state.json")

ROUTER_DB = os.path.join(os.environ.get("APPDATA", ""), "9router", "db", "data.sqlite")
MACHINE_ID = os.path.join(os.environ.get("APPDATA", ""), "9router", "machine-id")
CLI_SECRET = os.path.join(os.environ.get("APPDATA", ""), "9router", "auth", "cli-secret")
ROUTER = "http://127.0.0.1:20128"

# IP-bound = สลับ IP แล้วมีโอกาสหายได้ (วัดแล้ว: 5 VPN Gate node ได้ 429 เหมือนกันหมด
# แต่ SoftEther exit ได้ 200 -> ผูกกับ IP จริง)
IP_SENSITIVE_PROVIDERS = {"opencode", "opencode-zen", "opencode-go", "oc", "ocz", "ocg"}
IP_SENSITIVE_ERR = ("FreeUsageLimitError", "free tier", "FreeTierError")

# account-quota = เปลี่ยน IP ก็ยัง 429 เหมือนเดิม ปล่อย 9router backoff เอง
QUOTA_ERR = ("Credits exhausted", "credits remain", "free allowance",
             "Model usage limit", "request illegal", "11128")

COOLDOWN_SEC = 480          # ห้าม hunt ถี่กว่า 8 นาที
POLL_SEC = 120              # poll ทุก 2 นาที
PROBE_CACHE_SEC = 60        # probe result ใช้ซ้ำได้ภายใน 1 นาที


def log(*a):
    line = " ".join(str(x) for x in a)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (ts, line))
    except Exception:
        pass
    hunt.log(*a)


def token():
    import hashlib
    mid = open(MACHINE_ID, encoding="utf-8").read().strip()
    sec = open(CLI_SECRET, encoding="utf-8").read().strip()
    return hashlib.sha256((mid + "9r-cli-auth" + sec).encode()).hexdigest()[:16]


def api(method, path, body=None, timeout=15):
    url = ROUTER + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("x-9r-cli-token", token())
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def load_wstate():
    try:
        with open(WSTATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_hunt": 0, "last_probe": None, "last_probe_at": 0}


def save_wstate(st):
    with open(WSTATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def conns_429():
    """อ่านจาก DB ตรงๆ -> ได้ modelLock_* ครบ (API flatten ทิ้ง)"""
    c = sqlite3.connect("file:%s?mode=ro" % ROUTER_DB, uri=True)
    try:
        rows = c.execute("SELECT id, name, provider, data FROM providerConnections").fetchall()
    finally:
        c.close()
    out = []
    for cid, name, prov, data in rows:
        d = json.loads(data or "{}")
        if d.get("errorCode") != 429:
            continue
        err = d.get("lastError") or ""
        locks = {k: v for k, v in d.items() if k.startswith("modelLock_") and v}
        out.append({
            "id": cid, "name": name, "provider": prov,
            "err": err, "backoff": d.get("backoffLevel"),
            "testStatus": d.get("testStatus"), "locks": locks,
        })
    return out


def classify(row):
    """ -> 'ip' | 'quota' | 'unknown'"""
    p = (row["provider"] or "").lower()
    el = (row["err"] or "").lower()
    if any(q.lower() in el for q in QUOTA_ERR):
        return "quota"
    if p in IP_SENSITIVE_PROVIDERS and any(s.lower() in el for s in IP_SENSITIVE_ERR):
        return "ip"
    if p in IP_SENSITIVE_PROVIDERS:
        return "ip"
    return "unknown"


def probe_now():
    """probe ผ่าน 9router ด้วย session ใหม่ -> (status, note)"""
    sess = hunt.new_session()
    st, ms, note = hunt.probe(sess)
    return st, "%dms %s" % (ms, note)


def do_unlock(cid):
    # PUT ไม่ใช่ PATCH — route [id] exports DELETE/GET/PUT เท่านั้น (PATCH = 405)
    # PUT handler รับ testStatus แล้วเรียก updateProviderConnection -> h()
    # ซึ่ง clear errorCode/rateLimitedUntil/backoffLevel + modelLock_* ทุกตัว
    st, body = api("PUT", "/api/providers/" + cid, {"testStatus": "active"})
    return st, body


def watch_once(allow_hunt=True, no_write=False):
    """รอบเดียว -> (action, detail)
    no_write=True -> อ่านอย่างเดียว ห้าม unlock/hunt (ใช้ตอน dry test)"""
    rows = conns_429()
    if not rows:
        return "idle", "ไม่มี 429"

    ip_rows = [r for r in rows if classify(r) == "ip"]
    quota_rows = [r for r in rows if classify(r) == "quota"]
    unknown = [r for r in rows if classify(r) == "unknown"]

    log("429 ทั้งหมด: ip=%d quota=%d unknown=%d"
        % (len(ip_rows), len(quota_rows), len(unknown)))
    for r in rows:
        log("  [%s] %s/%s backoff=%s locks=%d :: %s"
            % (classify(r), r["provider"], r["name"], r["backoff"],
               len(r["locks"]), r["err"][:90].replace("\n", " ")))

    if not ip_rows:
        # ไม่มีตัวที่สลับ IP ช่วยได้ -> ห้ามแตะ (ปล่อย 9router backoff เอง)
        log("ไม่มี 429 ที่ IP-bound -> ไม่ทำอะไร (account-quota ต้องรอเอง)")
        return "skip", "quota-only"

    st = load_wstate()

    # probe ก่อนเสมอ -> กันสลับ IP โดยไม่จำเป็น (cache จะพัง)
    if time.time() - st.get("last_probe_at", 0) < PROBE_CACHE_SEC:
        pstatus, pnote = st.get("last_probe") or (0, "cached")
        log("probe (cached):", pstatus, pnote)
    else:
        pstatus, pnote = probe_now()
        st["last_probe"] = [pstatus, pnote]
        st["last_probe_at"] = time.time()
        save_wstate(st)
        log("probe:", pstatus, pnote)

    if pstatus == 200:
        # IP ปัจจุบันใช้ได้ -> 429 ค้างอยู่เฉยๆ ปลดล็อกให้ retry ได้ทันที
        if no_write:
            log("probe ผ่าน -> (dry) จะ unlock ทุกตัว:", [r["name"] for r in ip_rows])
            return "would-unlock", ",".join(r["name"] for r in ip_rows)
        results = []
        for r in ip_rows:
            s, b = do_unlock(r["id"])
            results.append("%s=%s" % (r["name"], s))
            log("unlock (probe ผ่าน -> 429 ค้าง):", r["id"], "->", s, str(b)[:120])
        st["last_hunt"] = time.time()
        save_wstate(st)
        return "unlock-stale", ",".join(results)

    # probe 429 จริง -> ต้องสลับ IP
    if no_write:
        return "would-hunt", "probe 429 (dry — ยังไม่ hunt)"
    if not allow_hunt:
        return "need-hunt", "probe 429 (ยังไม่ hunt)"
    if time.time() - st.get("last_hunt", 0) < COOLDOWN_SEC:
        left = int(COOLDOWN_SEC - (time.time() - st["last_hunt"]))
        log("อยู่ใน cooldown อีก %ds -> ยังไม่ hunt" % left)
        return "cooldown", "%ds left" % left

    log("probe 429 -> hunt node ใหม่ (keep)")
    h = hunt.acquire_lock()
    if h is None:
        log("มี hunt อีกตัวรันอยู่ -> ข้ามรอบนี้")
        return "busy", "another hunt running"
    try:
        rc = hunt.cmd_hunt(6, True, True)
    finally:
        hunt.release_lock(h)

    if rc != 0:
        log("hunt ไม่ได้ node ใหม่ -> ปล่อย lock ค้างไว้ (9router backoff เอง)")
        return "hunt-fail", "rc=%s" % rc

    # ได้ node ใหม่ที่ probe ผ่านแล้ว -> ปลดล็อกทุกตัวที่ IP-bound
    st = load_wstate()
    st["last_hunt"] = time.time()
    save_wstate(st)
    results = []
    for r in ip_rows:
        s, b = do_unlock(r["id"])
        results.append("%s=%s" % (r["name"], s))
        log("unlock:", r["id"], "->", s, str(b)[:120])

    log("egress ใหม่:", hunt.egress_ip())
    return "swapped", ",".join(results)


def watch_lock():
    f = open(WLOCK, "a+")
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        f.close()
        return None
    return f


def watch_unlock(f):
    if not f:
        return
    try:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        f.close()
    except Exception:
        pass


def main():
    args = sys.argv[1:]
    loops = 1
    allow_hunt = True
    dry = False
    if "--loop" in args:
        i = args.index("--loop")
        loops = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 0
    if "--dry" in args:
        allow_hunt = False
        dry = True
    if "--no-hunt" in args:
        allow_hunt = False

    n = 0
    while True:
        f = watch_lock()
        if f is None:
            log("มี watch อีกตัวรันอยู่ -> ออก")
            return 0
        try:
            action, detail = watch_once(allow_hunt=allow_hunt, no_write=dry)
            log("=> %s %s" % (action, detail))
        except Exception as e:
            import traceback
            log("ERROR:", e)
            log(traceback.format_exc())
            action = "error"
        finally:
            watch_unlock(f)

        n += 1
        if dry:
            return 0
        if loops and n >= loops:
            return 0
        time.sleep(POLL_SEC)


if __name__ == "__main__":
    sys.exit(main())
