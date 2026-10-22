#!/usr/bin/env python3
"""ocvpn — เลือก VPN Gate node ที่ opencode ยังใช้ได้ให้อัตโนมัติ

check : เช็ค IP ปัจจุบัน + สถานะ opencode ผ่าน 9router (ตอนนี้ยังใช้ได้ไหม)
list  : ดึงรายการ node จาก VPN Gate API ใหม่ทุกครั้ง (ไม่ต้องโหลดมือ)
hunt  : connect -> probe -> classify -> disconnect วนทุกตัว (หรือค้างตัวแรกที่ FRESH)
dis   : ตัด tunnel ทั้งหมด (kill openvpn.exe ทุกตัว + ล้าง state)
clean : ล้าง openvpn ค้างโดยไม่สน state (ใช้เมื่อ route ชนกัน)

probe ใช้ 9router เป็น oracle (POST 127.0.0.1:20128) เพราะยิงตรงไป opencode.ai
ด้วย header/UA/session อะไรก็โดน 403 FreeTierError ("free tier can only be used
from within OpenCode") — สูตรจาก build ก็ยังไม่ผ่าน จึงต้องผ่าน client path จริง
"""
import base64
import csv
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state.json")
CFGDIR = os.path.join(HERE, "cfg")
CREDS = os.path.join(HERE, "creds.txt")
ROUTER = "127.0.0.1:20128"
ROUTER_KEY = "sk-a0217d0a6dc1f88b-olr10e-3d11735c"
PROBE_MODEL = "oc/mimo-v2.6-flash-free"
GATE = "https://www.vpngate.net/api/iphone/"
PREF = ("JP", "SG", "KR", "US", "TW", "HK")
PROBE_TIMEOUT = 25
CONNECT_TIMEOUT = 40
OPENVPN = r"C:\Program Files\OpenVPN\bin\openvpn.exe"
LOCK = os.path.join(HERE, "hunt.lock")


def log(*a):
    print(*a, flush=True)


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"nodes": {}, "active": None}


def save_state(st):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def http(url, data=None, headers=None, timeout=20, bearer=None):
    h = dict(headers or {})
    if bearer:
        h["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:500]
    except Exception as e:
        return 0, str(e)


def openvpn_pids():
    """pids ของ openvpn.exe ทุกตัวที่รันอยู่ (ไม่สนว่าใครเป็นคนเปิด)"""
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq openvpn.exe", "/FO", "CSV", "/NH"],
            creationflags=subprocess.CREATE_NO_WINDOW).decode("utf-8", "replace")
    except Exception:
        return []
    pids = []
    for ln in out.splitlines():
        parts = [p.strip('"') for p in ln.strip().split('","')]
        if len(parts) >= 2 and parts[0].lower() == "openvpn.exe":
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


def kill_all_openvpn():
    """บังคับปิด openvpn ทุกตัว (taskkill /F /IM) — คืนจำนวนที่ฆ่า"""
    pids = openvpn_pids()
    if not pids:
        return 0
    subprocess.run(["taskkill", "/F", "/IM", "openvpn.exe"],
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(20):
        if not openvpn_pids():
            break
        time.sleep(0.25)
    return len(pids)


def acquire_lock():
    """lock ไฟล์กัน hunt รันพร้อมกัน 2 ตัว (Windows: msvcrt non-blocking)"""
    f = open(LOCK, "a+")
    try:
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except Exception:
        f.close()
        return None
    return f


def release_lock(f):
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


def egress_ip(timeout=12):
    st, body = http("https://api.ipify.org?format=json", headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
    if st == 200:
        try:
            return json.loads(body)["ip"]
        except Exception:
            pass
    return None


def new_session():
    """ses_<12hex><14base62> ตาม format ของ 9router/opencode (6249.js s())"""
    import secrets
    b62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    t = int(time.time() * 1000)
    hexpart = (4096 * t + 1) & ((1 << 48) - 1)
    return "ses_%012x%s" % (hexpart, "".join(b62[secrets.randbelow(62)] for _ in range(14)))


def probe(session=None):
    """ยิง 9router (client path จริง) -> (status, latency_ms, note)
    session: ถ้าใส่ จะส่ง x-opencode-session ไปให้ 9router pass-through
             (ใช้เช็คว่า 429 มาจากตัว IP หรือมาจาก session ที่ถูกผูกกับ IP เดิม)"""
    t0 = time.time()
    payload = json.dumps({
        "model": PROBE_MODEL,
        "messages": [{"role": "user", "content": "reply with only: OK"}],
        "max_tokens": 8,
        "stream": False,
    }).encode()
    hdr = {"Content-Type": "application/json"}
    if session:
        hdr["x-opencode-session"] = session
    st, body = http("http://%s/v1/chat/completions" % ROUTER, data=payload,
                    headers=hdr, timeout=PROBE_TIMEOUT, bearer=ROUTER_KEY)
    ms = int((time.time() - t0) * 1000)
    if st == 200:
        return st, ms, "FRESH"
    if st == 429:
        return st, ms, "USED | " + body[:220].replace("\n", " ")
    return st, ms, "UNKNOWN " + body[:120].replace("\n", " ")


def vpngate_rows(n=12):
    st, body = http(GATE, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    if st != 200 or "HostName,IP" not in body:
        raise SystemExit("VPN Gate API ล้ม: %s %s" % (st, body[:120]))
    lines = [ln for ln in body.splitlines() if ln and not ln.startswith("#")]
    reader = csv.reader(io.StringIO("\n".join(lines)))
    rows = []
    for r in reader:
        if len(r) < 15:
            continue
        host, ip, score, ping, speed, ctry, ctry_s, sessions = r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        if not ip or not ip[0].isdigit():
            continue
        try:
            cfg = base64.b64decode(r[14]).decode("utf-8", "replace")
        except Exception:
            continue
        if "remote " not in cfg:
            continue
        def num(x):
            try:
                return int(str(x).strip())
            except Exception:
                return 0
        rows.append({"host": host, "ip": ip, "score": num(score), "ping": num(ping),
                     "speed": num(speed), "country": ctry, "cc": ctry_s,
                     "sessions": num(sessions), "cfg": cfg})
    stt = load_state()["nodes"]

    # penalty ต่อ /24: ถ้า IP อื่นในบล็อกเดิมเคยโดน 429 = บล็อกนี้อิ่มตัวแล้ว โยนไปท้าย
    # (วัดจริง 2026-10-22: ทั้ง 5 node ใน 219.100.37.x ได้ 429 เหมือนกันหมด แต่ IP
    #  บ้านผ่าน SoftEther ได้ 200 — เคย=0 เลยไม่ช่วย เพราะ node ใหม่ในบล็อกเดิมก็โดนอีก)
    # นับเฉพาะ USED ภายใน 24 ชม. กันบล็อกโดน poison ถาวร
    now = time.time()

    def _age_min(s):
        try:
            return (now - time.mktime(time.strptime(s, "%Y-%m-%d %H:%M"))) / 60.0
        except Exception:
            return 0.0

    bad_block = {}
    for k, v in stt.items():
        if v.get("state") == "USED" and _age_min(v.get("at") or "") <= 1440:
            b = ".".join(k.split(".")[:3])
            bad_block[b] = bad_block.get(b, 0) + 1

    def rank(x):
        seen = stt.get(x["ip"], {}).get("state")
        never = 0 if seen is None else (1 if seen == "FRESH" else 2)
        pref = PREF.index(x["cc"]) if x["cc"] in PREF else 9
        blk = min(bad_block.get(".".join(x["ip"].split(".")[:3]), 0), 3)
        return (blk, never, pref, -x["score"], x["ping"] or 9999)
    rows.sort(key=rank)
    return rows[:n]


def ensure_dirs():
    os.makedirs(CFGDIR, exist_ok=True)
    with open(CREDS, "w") as f:
        f.write("vpn\nvpn\n")


def mgmt_free_port(start=7501):
    for p in range(start, start + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return 0


def start_node(node, idx):
    ensure_dirs()
    cfg = os.path.join(CFGDIR, "node_%s_%s.ovpn" % (idx, node["ip"].replace(".", "_")))
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(node["cfg"])
    port = mgmt_free_port(7501 + idx * 3)
    if not port:
        return None, "mgmt port หมด"
    logpath = os.path.join(CFGDIR, "ovpn_%s.log" % node["ip"].replace(".", "_"))
    cmd = [r"C:\Program Files\OpenVPN\bin\openvpn.exe", "--config", cfg,
           "--management", "127.0.0.1", str(port),
           "--auth-user-pass", CREDS, "--log", logpath,
           "--route-delay", "5", "--verb", "3"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    # รอ management accept + Initialization Sequence Completed
    sock = None
    deadline = time.time() + CONNECT_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, "openvpn ตายเร็ว (code=%s)" % proc.returncode
        if sock is None:
            s = socket.socket()
            s.settimeout(3)
            try:
                s.connect(("127.0.0.1", port))
                s.settimeout(None)
                try:
                    s.recv(512)  # banner
                except Exception:
                    pass
                sock = s
            except OSError:
                s.close()
        try:
            with open(logpath, encoding="utf-8", errors="replace") as f:
                if "Initialization Sequence Completed" in f.read():
                    return (proc, sock, port, cfg, logpath), "up"
        except Exception:
            pass
        time.sleep(1.0)
    stop_node((proc, sock, port, cfg, logpath), hard=True)
    return None, "connect timeout %ss" % CONNECT_TIMEOUT


def stop_node(h, hard=False):
    if not h:
        return
    proc, sock, port, cfg, logpath = h
    try:
        if sock and not hard:
            sock.sendall(b"signal SIGTERM\n")
            for _ in range(80):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
    except Exception:
        pass
    if proc.poll() is None:
        try:
            proc.terminate()
            time.sleep(2)
        except Exception:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    # hard kill เฉพาะ pid นี้ — กัน taskkill /IM ไปโดนตัวอื่นที่ hunt ถืออยู่
    if proc.poll() is None:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass
    try:
        if sock:
            sock.close()
    except Exception:
        pass
    # รอให้ process ตายจริงก่อนค่อยคืน route
    for _ in range(40):
        if proc.poll() is not None:
            break
        time.sleep(0.25)
    # รอ route คืน
    for _ in range(30):
        ip = egress_ip(timeout=6)
        if ip:
            break
        time.sleep(1)


def cmd_check():
    st = load_state()
    ip = egress_ip()
    s, ms, note = probe()
    log("egress   :", ip)
    log("opencode :", s, "%dms" % ms, "->", note)
    if st.get("active"):
        log("tunnel   : ค้างไว้ (pid=%s, node=%s) — สั่ง `dis` ได้" % (st["active"].get("pid"), st["active"].get("ip")))
    else:
        log("tunnel   : ไม่มี (ตรง ISP)")


def cmd_list(n):
    rows = vpngate_rows(n)
    stt = load_state()["nodes"]
    log("%-18s %-4s %-16s %8s %6s %7s %s" % ("IP", "CC", "host", "score", "ping", "state", "speed"))
    for r in rows:
        seen = stt.get(r["ip"], {}).get("state") or "-"
        log("%-18s %-4s %-16s %8d %6d %7s %s" % (r["ip"], r["cc"], r["host"][:16], r["score"], r["ping"], seen, r["speed"]))


def cmd_hunt(n, keep, fresh=False):
    lock = acquire_lock()
    if not lock:
        log("มี hunt อีกตัวกำลังรันอยู่ — ออกจากตัวนี้")
        return 1
    try:
        return _hunt_locked(n, keep, fresh)
    finally:
        release_lock(lock)


def _hunt_locked(n, keep, fresh=False):
    st = load_state()
    if st.get("active"):
        log("มี tunnel ค้างอยู่ — ตัดก่อน")
        cmd_dis()
        st = load_state()
    left = kill_all_openvpn()
    if left:
        log("ล้าง openvpn ค้าง %d ตัว ก่อนเริ่ม (กันชนกัน)" % left)
    base_ip = egress_ip() or "?"
    log("IP เดิม:", base_ip)
    rows = vpngate_rows(n)
    log("ตัวเลือก %d node (เรียง: ไม่เคยใช้ > ชาติที่ชอบ > score):" % len(rows))
    best = None
    for i, node in enumerate(rows):
        seen = st["nodes"].get(node["ip"], {}).get("state") or "-"
        log("\n[%d/%d] %s (%s, score=%d, ping=%d) เคย=%s" % (i + 1, len(rows), node["ip"], node["cc"], node["score"], node["ping"], seen))
        h, why = start_node(node, i)
        if not h:
            log("   dead:", why)
            st["nodes"][node["ip"]] = {"state": "DEAD", "at": time.strftime("%Y-%m-%d %H:%M"), "host": node["host"], "cc": node["cc"]}
            save_state(st)
            continue
        ip2 = egress_ip()
        log("   egress:", ip2, "(เปลี่ยนแล้ว)" if ip2 and ip2 != base_ip else "(ไม่เปลี่ยน!)")
        sess = new_session() if fresh else None
        if sess:
            log("   session: fresh", sess)
        s, ms, note = probe(sess)
        log("   probe :", s, "%dms" % ms, "->", note)
        fresh = (s == 200)
        st["nodes"][node["ip"]] = {"state": "FRESH" if fresh else "USED", "at": time.strftime("%Y-%m-%d %H:%M"),
                                   "host": node["host"], "cc": node["cc"], "ms": ms, "egress": ip2}
        save_state(st)
        if fresh and keep:
            st["active"] = {"pid": h[0].pid, "ip": node["ip"], "host": node["host"], "cfg": h[3], "at": time.strftime("%Y-%m-%d %H:%M")}
            save_state(st)
            log("\nค้าง node นี้ไว้ (FRESH %dms) — สั่ง `dis` เพื่อตัด" % ms)
            return 0
        best = best or (node, s, ms, note)
        stop_node(h)
        if fresh and not keep:
            log("   (disconnect — แค่พิสูจน์)")
    if best and best[1] == 200:
        log("\nสรุป:", best[0]["ip"], "ผ่าน probe (%dms) — รันซ้ำด้วย --keep เพื่อค้างไว้" % best[2])
        return 0
    log("\nสรุป: node ที่ลองไปไม่มีตัวไหนผ่าน probe")
    return 1


def cmd_dis():
    st = load_state()
    a = st.get("active")
    if a:
        try:
            os.kill(int(a["pid"]), 9)
            log("killed pid", a["pid"])
        except Exception as e:
            log("kill pid เดิมไม่ได้:", e)
    n = kill_all_openvpn()
    if n:
        log("ล้าง openvpn ค้างทั้งหมด %d ตัว" % n)
    st["active"] = None
    save_state(st)
    time.sleep(2)
    log("egress ตอนนี้:", egress_ip())
    log("openvpn ที่เหลือ:", openvpn_pids() or "ไม่มี")
    return 0


def main():
    if len(sys.argv) < 2:
        log(__doc__)
        return 2
    c = sys.argv[1]
    if c == "check":
        cmd_check()
    elif c == "probe":
        sess = new_session() if "--fresh" in sys.argv else None
        log("session :", sess or "(sticky ของ 9router)")
        st, ms, note = probe(sess)
        log("egress  :", egress_ip())
        log("result  :", st, "%dms" % ms, "->", note)
        return 0 if st == 200 else 1
    elif c == "list":
        cmd_list(int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 12)
    elif c == "hunt":
        n = int(sys.argv[sys.argv.index("--n") + 1]) if "--n" in sys.argv else 6
        return cmd_hunt(n, "--keep" in sys.argv, "--fresh" in sys.argv)
    elif c == "dis":
        return cmd_dis()
    elif c == "clean":
        st = load_state()
        if st.get("active"):
            st["active"] = None
            save_state(st)
            log("ล้าง state ที่ค้าง (pid เดิมไม่มีแล้ว)")
        n = kill_all_openvpn()
        log("kill openvpn ทุกตัว:", n, "ตัว")
        log("egress ตอนนี้:", egress_ip())
        return 0
    else:
        log("unknown:", c)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
