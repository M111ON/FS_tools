"""pool_server.py — stdlib HTTP wrapper for pool.py + dashboard UI.
POST /append   {app, kind, text}  → {ok, hash}
GET  /tail?n=10                    → [messages...]
GET  /view?n=20&cloud=0            → rendered chat log (pool.view)
GET  /stream?n=5  (SSE)            → real-time new messages
POST /flush                        → flush_emit + push_outbox now
POST /session-end {app, force?}    → auto-write a block on session close
GET  /health                       → {ok, lines}
GET  /                             → dashboard (single file, no CDN)
"""
import json
import os
import re
import sys
import time
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool
import taskboard
import ws_bridge

PORT = int(os.environ.get("POOL_PORT", 8390))
_lock = threading.Lock()

# ── task board page (the inbox dashboard, rebuilt over /tasks) ───────
# Read-only: a task changes by POST /task /task/update, never from the page.
# The inbox source stays authoritative for what was posted there; this page is
# the shared, owner-aware view every platform can read from the pool directly.
TASKS_PAGE = """<!DOCTYPE html>
<html lang=\"th\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
<title>chat-pool · task board</title>
<style>
  :root { --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3;
          --muted:#8b949e; --accent:#58a6ff; --green:#3fb950; --yellow:#d29922;
          --red:#f85149; --grey:#6e7681; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:'SF Mono','Cascadia Code',monospace; background:var(--bg);
         color:var(--text); padding:20px; }
  .header { display:flex; justify-content:space-between; align-items:baseline; margin-bottom:16px; }
  .header h1 { font-size:16px; }
  .header a { color:var(--accent); font-size:12px; text-decoration:none; }
  .stats { display:grid; grid-template-columns:repeat(6,1fr); gap:10px; margin-bottom:20px; }
  .stat { background:var(--surface); border:1px solid var(--border); border-radius:8px;
          padding:10px 12px; text-align:center; }
  .stat .num { font-size:22px; font-weight:700; }
  .stat .label { font-size:10px; color:var(--muted); text-transform:uppercase; margin-top:2px; }
  .col-pending .num{color:var(--accent)} .col-in_progress .num{color:var(--yellow)}
  .col-done .num{color:var(--green)} .col-blocked .num{color:var(--red)}
  .sec { margin-bottom:22px; }
  .sec h2 { font-size:12px; color:var(--muted); text-transform:uppercase;
            letter-spacing:0.5px; margin-bottom:10px; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:8px;
          padding:12px 14px; margin-bottom:6px; }
  .card .t { font-size:13px; font-weight:600; margin-bottom:4px; }
  .card .d { color:var(--muted); font-size:11px; line-height:1.5; white-space:pre-wrap; }
  .card .meta { display:flex; gap:8px; flex-wrap:wrap; margin-top:8px;
                font-size:10px; color:var(--muted); align-items:center; }
  .pill { padding:1px 8px; border-radius:10px; font-size:10px; font-weight:600;
          text-transform:uppercase; background:#1f2937; }
  .pill-pending{color:var(--accent)} .pill-in_progress{color:var(--yellow);background:#2d1f00}
  .pill-done{color:var(--green);background:#0d2818} .pill-blocked{color:var(--red);background:#2d1214}
  .pill-cancelled{color:var(--grey)}
  .owner { color:var(--text); }
  .sub { border-left:2px solid var(--border); margin-left:14px; padding-left:10px; }
  .muted { color:var(--muted); }
</style></head><body>
<div class=\"header\"><h1>chat-pool · task board</h1>
<a href=\"/\">← pool dashboard</a></div>
<div class=\"stats\" id=\"stats\"></div>
<div id=\"board\">loading…</div>
<script>
const S={pending:'pending',in_progress:'working',done:'done',blocked:'blocked',cancelled:'cancelled'};
function esc(s){return (s||'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]))}
fetch('/tasks').then(r=>r.json()).then(j=>{
  const tasks=j.tasks||[];
  const by={}; tasks.forEach(t=>{(by[t.parent||'']=by[t.parent||'']||[]).push(t)});
  const st=j.summary||{counts:{},in_progress:[]};
  document.getElementById('stats').innerHTML=
    ['pending','in_progress','done','blocked','cancelled'].map(s=>
      `<div class=\"stat col-${s}\"><div class=\"num\">${st.counts[s]||0}</div><div class=\"label\">${S[s]}</div></div>`
    ).join('')+`<div class=\"stat\"><div class=\"num\">${tasks.length}</div><div class=\"label\">total</div></div>`;
  function card(t,sub){
    const who=t.owner?`<span class=\"owner\">@${esc(t.owner)}</span>`:'<span class=\"muted\">unclaimed</span>';
    const kids=(by[t.id]||[]).map(k=>card(k,true)).join('');
    return `<div class=\"card\"><div class=\"t\">${esc(t.title)}</div>`+
      (t.desc?`<div class=\"d\">${esc(t.desc)}</div>`:'')+
      `<div class=\"meta\"><span class=\"pill pill-${t.status}\">${S[t.status]||t.status}</span>${who}`+
      `<span>${esc(t.id)}</span>${t.tags.map(x=>`<span>#${esc(x)}</span>`).join('')}`+
      (t.revs>1?`<span>rev ${t.revs}</span>`:'')+`</div></div>`+(kids?`<div class=\"sub\">${kids}</div>`:'');
  }
  const roots=(by['']||[]);
  document.getElementById('board').innerHTML=
    roots.length?roots.map(t=>card(t,false)).join(''):'<div class=\"muted\">board is empty — POST /task to add one</div>';
}).catch(e=>{document.getElementById('board').textContent='load failed: '+e});
</script></body></html>"""

# A query parameter that could not be read as a number: the 400 answer is
# already written when these come back, so the caller only has to return.
_REFUSED = object()
_THRESHOLD_ERR = "?hide=<int> / ?at=<int>"

# LAN access: bearer token required for non-loopback clients.
# Token file lives next to the log (0600-ish perms on NTFS via profile);
# created on first boot. Loopback (dashboard on this PC) is always trusted.
TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".pool_token")


def _get_token():
    import secrets
    if not os.path.exists(TOKEN_FILE):
        tok = secrets.token_hex(16)
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(tok)
    return open(TOKEN_FILE, encoding="utf-8").read().strip()


# ── inbox → pool task bridge ─────────────────────────────────────────
# The inbox-manager board (.inbox_state.json board, status todo|in_progress|...)
# is a local file one app owns. The pool is where every platform already meets,
# so the bridge reads that file over the inbox dashboard's own /api/state and
# writes each card into the chain as a task — owner-aware, deduped by source id.
# The inbox board stays the source of record; the pool mirrors status so any
# OTHER platform can see and claim work without touching that file.
_INBOX_STATUS = {"todo": "pending", "in_progress": "in_progress", "done": "done",
                 "blocked": "blocked", "cancelled": "cancelled"}


def _inbox_board():
    """Read the live inbox board. INBOX_STATE_FILE (a .inbox_state.json) is
    tried first — no network, works even when the inbox dashboard is down —
    then the dashboard's own /api/state at INBOX_STATE_URL (default
    http://127.0.0.1:5000/api/state). Returns (board, err)."""
    path = os.environ.get("INBOX_STATE_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            return (d.get("board") or []), None
        except Exception as e:
            return [], str(e)
    url = os.environ.get("INBOX_STATE_URL", "http://127.0.0.1:5000/api/state")
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            d = json.loads(r.read().decode("utf-8"))
        return (d.get("board") or []), None
    except Exception as e:
        return [], str(e)


def _src_of(t):
    return (t.get("tags") or [""])[0].startswith("src:")


def _inbox_sync_tasks(apply=False):
    """Diff the inbox board against pool tasks and return the plan; with
    apply=True, execute it. Import rules:
      - card status must map (unknown statuses are skipped, reported)
      - title[:80] becomes the task title, deduped by the src:<id> tag
      - in_progress cards get owner=inbox-manager (ใครกำลังทำอยู่)
      - done/cancelled cards import as their terminal status (history)
    Returns {cards, imported, updated, skipped, actions} — actions carry the
    task id + what would happen, so a dry-run is the same shape as the result."""
    board, err = _inbox_board()
    if err:
        return {"ok": False, "error": err, "imported": 0, "updated": 0, "actions": []}
    # existing mirrored tasks by their src: tag
    existing = {}
    for t in taskboard.task_list():
        for tag in t.get("tags", []):
            if tag.startswith("src:"):
                existing[tag] = t
    actions, imported, updated, skipped = [], 0, 0, 0
    app = "inbox-bridge"
    for c in board:
        cid = str(c.get("id", ""))
        src = "src:inbox-%s" % cid
        st = _INBOX_STATUS.get(c.get("status", ""))
        title = (c.get("title") or "").strip()[:80]
        if not cid or not title:
            skipped += 1
            actions.append({"card": cid, "op": "skip", "why": "missing id/title"})
            continue
        if st is None:
            skipped += 1
            actions.append({"card": cid, "op": "skip", "why": "unknown status %r" % c.get("status")})
            continue
        desc = (c.get("body") or "").strip()
        cur = existing.get(src)
        if cur is None:
            actions.append({"card": cid, "op": "import", "status": st,
                            "title": title,
                            "owner": "inbox-manager" if st == "in_progress" else ""})
            if apply:
                row = taskboard.task_create(app, title, desc=desc,
                                            tags=src, status=st,
                                            owner="inbox-manager" if st == "in_progress" else "")
                actions[-1]["id"] = row["id"]
            imported += 1
        elif cur["status"] != st:
            actions.append({"card": cid, "op": "update", "id": cur["id"],
                            "from": cur["status"], "to": st})
            if apply:
                taskboard.task_update(app, cur["id"], status=st,
                                      note="sync from inbox card #%s" % cid)
            updated += 1
    if apply and (imported or updated):
        try:
            if not pool.is_registered(app):
                pool.register_app(app)
            else:
                pool.touch_app(app)
        except Exception:
            pass
    return {"ok": True, "cards": len(board), "imported": imported,
            "updated": updated, "skipped": skipped,
            "applied": bool(apply), "actions": actions}


def _lan_ips():
    """Real LAN addresses (skip loopback + RFC1918 172.16/12 virtuals).
    getaddrinfo(hostname) alone returns stale/wrong-order entries on
    multi-adapter boxes — cross-check with a UDP connect to the LAN
    gateway side and prefer the adapter the default route actually uses."""
    import socket
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127.") or ip.startswith("169.254."):
                continue
            if ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31:
                continue  # WSL/Hyper-V virtual adapters
            ips.add(ip)
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 80))  # home gateway — picks the live route
        ips.discard(s.getsockname()[0])
        ips = {s.getsockname()[0]} | ips
        s.close()
    except Exception:
        pass
    return sorted(ips)


def _qr_svg(url):
    """QR as inline SVG string — no image files, no JS lib needed."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=1)
        qr.add_data(url)
        qr.make(fit=True)
        m = qr.get_matrix()
        n = len(m)
        cells = "".join(
            f"<rect x='{x}' y='{y}' width='1' height='1'/>"
            for y, row in enumerate(m) for x, on in enumerate(row) if on)
        return (f"<svg viewBox='0 0 {n} {n}' shape-rendering='crispEdges'"
                f" style='width:130px;height:130px;background:#fff'>"
                f"<g fill='#0d1117'>{cells}</g></svg>")
    except Exception:
        return ""


def _qr_panel():
    ips = _lan_ips()
    tok = _get_token()
    if not ips:
        return ""
    links = "".join(
        f"<div style='margin:4px 0'><a href='http://{ip}:{PORT}/?token={tok}' "
        f"style='color:var(--acc2);font:11px var(--mono)'>http://{ip}:{PORT}</a></div>"
        for ip in ips)
    # token as a query param, not a #fragment: many QR scanners (LINE,
    # some phone cameras) drop the fragment, and the fragment is also
    # invisible to the server — a query param survives both
    qr = _qr_svg(f"http://{ips[0]}:{PORT}/?token={tok}")
    hint = ("<div style='font:10px var(--mono);color:var(--dim);padding-top:4px'>"
            "สแกนแล้วเบราว์เซอร์จะจำรหัสเข้าใช้ให้เอง ไม่ต้องพิมพ์</div>")
    return ("<div style='display:flex;gap:10px;align-items:flex-start;padding-top:6px'>"
            f"{qr}<div>{links}{hint}</div></div>")

DASHBOARD = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chat-pool</title>
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<meta http-equiv="Pragma" content="no-cache">
<style>
:root{
  --bg:#0d1117; --panel:#161b22; --panel2:#1c2330; --line:#2d333b;
  --fg:#e6edf3; --dim:#8b949e; --acc:#7ee787; --acc2:#79c0ff; --warn:#ffa657;
  --mono:'Cascadia Code',Consolas,'Courier New',monospace;
}
*{box-sizing:border-box;margin:0}
html{height:100%}
/* portrait app: header · tabs · tick rail · feed · composer, top to bottom.
   The feed is the only thing that scrolls, and the composer never leaves the
   bottom of the screen — on a phone that is the whole point. */
body{background:var(--bg);color:var(--fg);font:15px/1.55 -apple-system,'Segoe UI',system-ui,sans-serif;height:100dvh;display:flex;flex-direction:column;overflow:hidden;-webkit-text-size-adjust:100%}
header{display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap;flex:0 0 auto}
header h1{font-size:14px;font-weight:600;letter-spacing:.4px}
header h1 b{color:var(--acc)}
.dot{width:9px;height:9px;border-radius:50%;background:#f85149;transition:.3s;flex:0 0 auto}
.dot.on{background:var(--acc);box-shadow:0 0 8px var(--acc)}
#chain{font:11px var(--mono);color:var(--dim)}
#chain.ok{color:var(--acc)}
/* one tab per writer: a long session reads alone without hiding the rest */
#tabs{display:flex;gap:6px;padding:7px 12px;border-bottom:1px solid var(--line);background:var(--panel);overflow-x:auto;flex:0 0 auto;scrollbar-width:none}
#tabs::-webkit-scrollbar{display:none}
.tab{font:12px var(--mono);padding:5px 12px;border-radius:999px;border:1px solid var(--line);background:var(--panel2);color:var(--dim);cursor:pointer;white-space:nowrap;flex:0 0 auto}
.tab.on{background:var(--acc);border-color:var(--acc);color:#0d1117;font-weight:700}
.tab .n{opacity:.65}
/* the tick rail is the chunk layer's scrollbar: a tick counts LINES in append
   order, so jumping to tick 12 lands on the same lines every time */
#tickbar{display:flex;align-items:center;gap:7px;padding:6px 12px;border-bottom:1px solid var(--line);background:var(--bg);flex:0 0 auto;flex-wrap:wrap}
#tick-label{font:10px var(--mono);color:var(--dim);white-space:nowrap}
#tick-range{flex:1;min-width:88px;accent-color:var(--acc)}
main{flex:1;display:flex;min-height:0}
#feed{flex:1;overflow-y:auto;padding:10px 12px 16px;display:flex;flex-direction:column;gap:9px;min-width:0;overscroll-behavior:contain}
#feed::-webkit-scrollbar{width:8px}#feed::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
.empty{color:var(--dim);text-align:center;padding:40px 12px;flex:0 0 auto}
/* a tick chunk: closed it is one scannable line, open it is the lines itself */
/* flex:0 0 auto is load-bearing. #feed is a flex column, and an item whose
   overflow is hidden has an automatic minimum size of ZERO — so every chunk
   was being shrunk to fit the viewport instead of overflowing it. Result: no
   scrollbar, and expanding a card clipped its own text (416px of content in a
   320px box) with no way to scroll to it. Never shrink a chunk here. */
.tick{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden;flex:0 0 auto}
.tick.open{border-color:#3b4657}
/* one row, always: the header is the scanning surface, so it truncates
   rather than wrapping to look like another message */
.thead{display:flex;align-items:center;gap:8px;padding:8px 11px;cursor:pointer;user-select:none;overflow:hidden}
.thead:hover{background:var(--panel2)}
.tno{font:11px var(--mono);color:var(--acc);font-weight:600;white-space:nowrap;flex:0 0 auto}
.tmeta{font:10px var(--mono);color:var(--dim);flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tapps{flex:0 1 auto;display:flex;gap:4px;overflow:hidden;justify-content:flex-end;min-width:0}
.tchip{font:10px var(--mono);padding:1px 7px;border-radius:9px;background:var(--panel2);border:1px solid var(--line);color:var(--acc2);white-space:nowrap}
.chev{color:var(--dim);font:11px var(--mono);flex:0 0 auto}
.tbody{display:none;flex-direction:column;gap:7px;padding:0 9px 10px}
.tick.open .tbody{display:flex}
.msg{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 11px;max-width:78ch;animation:in .2s ease}
@media (max-width:900px){.msg{max-width:100%}}
@keyframes in{from{opacity:0;transform:translateY(5px)}to{opacity:1}}
.msg .top{display:flex;gap:7px;align-items:baseline;margin-bottom:3px;flex-wrap:wrap}
.badge{font:10px var(--mono);padding:1px 8px;border-radius:9px;background:var(--panel);color:var(--acc2);border:1px solid var(--line);white-space:nowrap}
.badge.kind{color:var(--warn)}
.time{font:10px var(--mono);color:var(--dim)}
.meta{font:10px var(--mono);color:var(--dim);margin-left:auto;cursor:pointer}
.meta:hover{color:var(--acc2)}
.text{white-space:pre-wrap;word-break:break-word}
.more{margin-top:5px;font:11px var(--mono);background:none;border:1px dashed var(--line);color:var(--acc2);border-radius:8px;padding:3px 9px;cursor:pointer}
.more:hover{border-color:var(--acc2)}
/* noise the curator kept out of the way: dimmed and dashed when revealed, so a
   line you chose to see still reads as one the feed would not have shown */
.msg.quiet{opacity:.6;border-style:dashed}
.badge.quiet{color:var(--warn);border-color:var(--line)}
.tclean{font:10px var(--mono);color:var(--warn);cursor:pointer;border:1px dashed var(--warn);border-radius:9px;padding:1px 7px;white-space:nowrap;flex:0 0 auto}
.tclean:hover{background:var(--warn);color:#0d1117}
.tclean.on{background:var(--warn);color:#0d1117}
#btn-cur.on{color:var(--warn);border-color:var(--warn)}
.curbar{font:11px var(--mono);color:var(--dim);line-height:1.75;white-space:pre-wrap}
.curchip{display:inline-block;padding:1px 7px;border-radius:9px;background:var(--panel2);border:1px solid var(--line);color:var(--warn);margin:2px 4px 0 0}
.curex{border-left:2px solid var(--line);padding-left:6px;margin:6px 0;color:var(--dim)}
aside{width:302px;flex:0 0 302px;border-left:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column;overflow-y:auto}
aside section{padding:12px 14px;border-bottom:1px solid var(--line)}
aside h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:9px}
#q{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:9px;padding:9px 11px;font-size:15px;outline:none}
#q:focus{border-color:var(--acc2)}
.stat{font:11px var(--mono);color:var(--dim);display:flex;justify-content:space-between;padding:2px 0}
.stat b{color:var(--fg)}
#scrim{position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:39;display:none}
#scrim.on{display:block}
#hint{display:flex;align-items:center;gap:8px;padding:5px 12px;border-top:1px solid var(--line);background:var(--panel);font:10px var(--mono);color:var(--dim);flex:0 0 auto;overflow:hidden}
#flushtip{margin-left:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;text-align:right}
#to-sel{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);padding:3px 6px;max-width:130px;flex:0 0 auto}
#sysline{flex:0 0 auto;padding:5px 14px;font:11px var(--mono);color:var(--fg);background:var(--panel);border-bottom:1px solid var(--line)}
#sysline a{color:var(--acc);margin:0 2px}
#sysline a.on{color:var(--bg);background:var(--acc);border-radius:6px;padding:0 5px}
#composer{display:flex;gap:8px;align-items:flex-end;padding:8px 12px calc(8px + env(safe-area-inset-bottom,0px));background:var(--panel);flex:0 0 auto}
#msg-in{flex:1;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:12px;padding:11px 13px;font:15px/1.45 var(--mono);outline:none;resize:none;height:46px;max-height:32dvh;overflow-y:hidden}
#msg-in:focus{border-color:var(--acc)}
button.send{background:var(--acc);color:#0d1117;border:none;border-radius:12px;padding:0 18px;font-weight:700;font-size:15px;cursor:pointer;height:46px;min-width:68px}
button.send:hover{filter:brightness(1.08)}
button.send:disabled{opacity:.55;cursor:progress}
/* phone: the panels become a drawer so the feed keeps the full width, and every
   control grows to a thumb — 16px input text or iOS zooms the page on focus */
@media (max-width:900px){
  #msg-in{font-size:16px}
  #q{padding:11px}
  header{padding:7px 10px}
  #tabs{padding:6px 9px}
  #tickbar{padding:6px 9px}
  #feed{padding:8px 9px 14px}
  .pbtn{min-height:34px}
  aside{position:fixed;top:0;right:0;bottom:0;width:min(88vw,340px);border-left:1px solid var(--line);z-index:40;transform:translateX(102%);transition:transform .22s ease;box-shadow:-14px 0 34px rgba(0,0,0,.5)}
  aside.open{transform:none}
}
@media (min-width:901px){#btn-side{display:none}#side-close{display:none}}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--acc);color:var(--acc);font:12px var(--mono);padding:8px 18px;border-radius:9px;opacity:0;transition:.3s;pointer-events:none}
.toast.show{opacity:1}
/* ── 🚶 the three-step tour — shown once, dismissed forever ─────────────
   The app has five panels and one vocabulary; a first-time reader meets all
   of them at once. Three things make this a tour and not another wall of
   text: it names what to DO (not what a feature is), it ADVANCES on the
   reader doing that thing — not on a Next click alone — and once dismissed
   it stays dismissed, because a guide that reappears is a nag. */
/* The card must never OWN the screen it is teaching: a full-scrim guide over
   the search box it just said to type into is a lock, not a lesson (and shipped
   as one — the backdrop swallowed every click, so the acts it waits for were
   unreachable). pointer-events:none on the shell, auto only on the card, and
   the page stays live underneath; left-aligned so it stays off the canvas. */
#tour{position:fixed;inset:0;z-index:70;display:flex;align-items:flex-end;justify-content:flex-start;padding:0 12px 96px 16px;pointer-events:none}
#tour[hidden]{display:none}
#tour-card{background:var(--panel);border:1px solid var(--acc);border-radius:14px;padding:14px 16px;max-width:430px;width:100%;box-shadow:0 8px 30px rgba(0,0,0,.6);pointer-events:auto}
#tour-card b{color:var(--acc);font-size:14px}
#tour-body{font:12px/1.6 var(--mono);color:var(--fg);margin-top:6px}
#tour-body code{color:var(--warn);background:var(--panel2);border-radius:5px;padding:0 5px}
#tour-row{display:flex;align-items:center;gap:10px;margin-top:12px}
#tour-skip{font:11px var(--mono);color:var(--dim)}
#tour-skip:hover{color:var(--fg)}
#tour-next{margin-left:auto}
#tour-steps{display:flex;align-items:center;gap:6px;margin-bottom:9px}
.tstep{width:22px;height:22px;border-radius:50%;border:1px solid var(--line);color:var(--dim);font:11px var(--mono);display:flex;align-items:center;justify-content:center;flex:0 0 auto}
.tstep.on{border-color:var(--acc);color:var(--acc);font-weight:700}
.tstep.done{border-color:var(--acc);background:var(--acc);color:var(--bg)}
.tbar{height:2px;flex:0 0 26px;background:var(--line)}
.tbar.done{background:var(--acc)}
.cloudwrap{max-height:0;overflow:hidden;transition:max-height .3s}
.cloudwrap.open{max-height:400px;overflow-y:auto}
.clabel{font-size:10px;color:var(--dim);cursor:pointer;user-select:none}
.clabel:hover{color:var(--acc2)}
.pbtn{font:11px var(--mono);padding:4px 11px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);color:var(--dim);cursor:pointer}
.pbtn:hover{color:var(--fg)}
.pbtn.on{color:var(--acc);border-color:var(--acc)}
#plane-modal{position:fixed;inset:0;background:rgba(0,0,0,.74);display:none;align-items:center;justify-content:center;z-index:50;padding:14px}
#plane-modal.open{display:flex}
#plane-box{background:var(--panel);border:1px solid var(--line);border-radius:14px;width:min(1080px,100%);height:min(780px,100%);display:flex;flex-direction:column;overflow:hidden}
#plane-head{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap}
#plane-bar{display:flex;align-items:center;gap:12px;padding:8px 15px;border-bottom:1px solid var(--line);font:10px var(--mono);color:var(--dim);flex-wrap:wrap}
#pv-seq{flex:1;min-width:86px;accent-color:var(--acc)}
#pv-when{min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* The popup and the tooltip live in the TOP LAYER (popover), not inside the
   canvas box. That box is overflow:hidden on a phone and only ~165px tall, so
   anything positioned inside it is clipped BY CONSTRUCTION: measured, the popup
   (164px tall) sat at top=4px in a 165px box and its bottom row of buttons was
   cut off. The old clamp could not fix it — when the popup is taller than its
   container `wr.height - popH - 10` goes NEGATIVE, and `Math.max(4,·)` puts it
   straight back inside. A popup can never be taller than the VIEWPORT, so the
   top layer is the one place the problem does not exist. It also means the
   popup cannot be painted under the side panel or over the map by accident. */
#plane-pop{position:fixed;z-index:6;background:var(--panel2);border:1px solid var(--acc);border-radius:10px;padding:9px 11px;font:11px var(--mono);color:var(--fg);max-width:min(330px,calc(100vw - 24px));max-height:calc(100dvh - 24px);overflow:auto;box-shadow:0 6px 24px rgba(0,0,0,.5)}
#plane-pop .pp{color:var(--dim);white-space:pre-wrap;margin-top:4px}
#plane-pop .prow{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
#plane-body{flex:1;display:flex;min-height:0}
/* overflow must be HIDDEN here, not auto: pstep() sizes the canvas FROM this
   box, so a canvas bigger than the box would raise a scrollbar, the scrollbar
   would shrink the box, the smaller box would shrink the canvas, the scrollbar
   would vanish — a closed feedback loop that oscillates the canvas size every
   frame (measured mid-drag: 1152px -> 576px). The phone rule below already
   clips for the same reason. */
#plane-canvaswrap{flex:1;display:flex;align-items:center;justify-content:center;padding:14px;position:relative;min-width:0;overflow:hidden}
#plane-cv{background:#0a0e14;border:1px solid var(--line);border-radius:10px;cursor:crosshair;image-rendering:pixelated;box-shadow:0 8px 28px rgba(0,0,0,.45),inset 0 0 0 1px rgba(255,255,255,.02)}
/* the timeline strip: one bar per tick (or per bucket of ticks), coloured by who
   was writing — the tick layer with its "when" and "who" both visible at once */
#plane-ribbon{display:flex;align-items:center;gap:9px;padding:7px 12px 0;flex:0 0 auto}
#pv-ribbon{flex:1 1 130px;height:42px;min-width:90px;background:#0a0e14;border:1px solid var(--line);border-radius:9px;cursor:ew-resize}
/* No longer 349px of label: a wrapping row decides its line from the BASIS sum,
   so a label that is as wide as its content pushes everything after it onto a
   second row — which is how the strip and its own scrubber ended up in two
   different rows. Fixed basis, ellipsis, full text in the tooltip. */
#pv-ribbon-info{flex:0 1 118px;min-width:0;overflow:hidden;text-overflow:ellipsis;font:10px var(--mono);color:var(--dim);white-space:nowrap;text-align:right}
/* the scrubber belongs beside the strip it scrubs, not in another row */
#pv-time{display:flex;align-items:center;gap:6px;flex:1 1 222px;min-width:0}
#pv-legend{display:flex;align-items:center;gap:10px;font:10px var(--mono);color:var(--dim);flex-wrap:wrap}
.pvgrad{width:78px;height:8px;border-radius:5px;border:1px solid var(--line);display:inline-block}
#pv-colour{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px;font:10px var(--mono);padding:2px 4px}
#plane-empty{color:var(--dim);font:12px var(--mono);text-align:center;padding:30px 12px}
#plane-side{width:360px;border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
#pv-lensinfo{display:none;color:var(--warn);font:10px var(--mono);white-space:nowrap}
#pv-lensinfo.on{display:inline}
/* ── 🔍 the zoom cluster ───────────────────────────────────────────────────
   Small and ALWAYS VERTICAL: the zoom dial is an up/down ladder (level climbs
   with +), so the column reads the same as the ladder. A compact 30px row of
   buttons — the buttons never move, so thumb accuracy comes from the fixed
   spot, not from the size — and it stays out of the canvas it magnifies. */
#pv-zoom{position:absolute;right:10px;bottom:10px;z-index:3;display:flex;
  flex-direction:column;gap:4px;align-items:center;
  background:rgba(13,17,23,.88);border:1px solid var(--line);border-radius:10px;
  padding:5px;box-shadow:0 4px 16px rgba(0,0,0,.5)}
#pv-zoom button{width:30px;height:30px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel2);color:var(--fg);font:16px/1 var(--mono);cursor:pointer;
  display:flex;align-items:center;justify-content:center;padding:0;flex:0 0 auto}
#pv-zoom button:hover{border-color:var(--acc);color:var(--acc)}
#pv-zoom button:disabled{opacity:.35;cursor:default}
#pv-zoom button:disabled:hover{border-color:var(--line);color:var(--fg)}
#pv-zoom #pv-zreset{font-size:13px;height:26px}
/* The drawing options are worth having and not worth SHOWING: 14 controls in a
   wrapping row measured 115px, above a canvas that then had 175px left — and
   the row held the scrubber too, which belonged with the strip. One button
   away, and the button says what is behind it. */
#plane-bar.off{display:none}
#pv-opt.on{color:var(--acc);border-color:var(--acc)}
#plane-detail{flex:1;overflow-y:auto;padding:12px 15px;font:11px var(--mono);color:var(--dim);line-height:1.6}
/* the tooltip WRAPS: `white-space:nowrap` with no max-width made a 350px box
   that was cut off whenever the pointer was near the right edge (measured:
   left=430, right=780, canvas box=540) */
#plane-tip{position:fixed;pointer-events:none;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:3px 8px;font:10px var(--mono);color:var(--fg);max-width:min(340px,calc(100vw - 24px))}
#subgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:2px;margin-top:7px}
#subgrid div{aspect-ratio:1;border-radius:2px;background:var(--panel2);cursor:pointer}
#subgrid div:hover{outline:1px solid var(--fg)}
.pline{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 8px;margin:6px 0;color:var(--fg)}
.pline .ph{color:var(--dim);cursor:pointer}
.pline .ph:hover{color:var(--acc2)}
/* ── 🧭 walk: what this leads to, what cites it, and what shape it is ──────
   The shape badge is the FIRST thing in the panel because it reframes every-
   thing under it: a line with in=273 is not read the same way as a line with
   in=1, and the two look identical as a bare list of edges. */
.shape{display:inline-flex;align-items:center;gap:5px;font:11px var(--mono);
  padding:2px 9px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);white-space:nowrap}
.shape b{font-size:12px}
.swhy{color:var(--dim);font-size:10px;line-height:1.5;margin-top:4px}
.crumbs{display:flex;flex-wrap:wrap;gap:4px;align-items:center;margin:0 0 8px}
.crumb{font:10px var(--mono);padding:2px 7px;border-radius:8px;border:1px solid var(--line);
  background:var(--panel2);color:var(--acc2);cursor:pointer;max-width:150px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.crumb.here{color:var(--warn);border-color:var(--warn)}
.crumb.x{color:var(--dim)}
.hopsec{margin-top:10px;border-top:1px solid var(--line);padding-top:8px}
.hophead{display:flex;align-items:center;gap:6px;color:var(--fg);font-size:11px;cursor:pointer}
.hophead .n{color:var(--dim);font-weight:400}
.hoplevel{color:var(--dim);font-size:10px;margin:7px 0 3px}
.hop{display:flex;gap:6px;align-items:flex-start;padding:5px 7px;margin:3px 0;border-radius:7px;
  border:1px solid var(--line);background:var(--panel2);cursor:pointer}
.hop:hover{border-color:var(--acc2)}
.hop .deg{color:var(--dim);font:9px var(--mono);white-space:nowrap;margin-left:auto;padding-left:6px}
.hop .tx{color:var(--fg);font-size:10px;line-height:1.45;overflow:hidden;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.relchip{font:9px var(--mono);padding:1px 6px;border-radius:7px;border:1px solid var(--line);color:var(--dim)}
/* census: proportions have to be SEEN, because "3,001 islands" next to
   "13,572 edges" reads as comparable numbers when they are not */
.cen{margin-top:8px}
.cenrow{display:grid;grid-template-columns:16px 78px 1fr 46px;gap:6px;align-items:center;margin:4px 0}
.cenbar{height:9px;border-radius:5px;background:var(--panel2);overflow:hidden}
.cenbar i{display:block;height:100%;border-radius:5px;background:var(--acc2)}
.cennum{color:var(--dim);font:10px var(--mono);text-align:right}
.cenhub{display:flex;gap:6px;align-items:flex-start;padding:5px 7px;margin:4px 0;border-radius:7px;
  border:1px solid var(--line);background:var(--panel2);cursor:pointer}
.cenhub:hover{border-color:var(--warn)}
@media (max-width:900px){
  #plane-modal{padding:0}
  #plane-box{width:100%;height:100%;max-height:100dvh;border-radius:0;border:none}
  #plane-body{flex-direction:column}
  /* Both halves share the remaining height with min-height:0, and the canvas
     box CLIPS instead of growing. That matters more than it looks: pstep()
     sizes cells from the box's clientHeight, so a box that also GROWS to fit
     the canvas it just sized is a feedback loop — it settles wherever it
     starts, which is how a canvas ends up 156px tall after a toolbar row is
     added. A real height every frame means every frame agrees. */
  #plane-canvaswrap{flex:1 1 40%;min-height:0;overflow:hidden;
    align-items:center;justify-content:center;padding:8px}
  #plane-side{width:100%;flex:1 1 60%;min-height:0;border-left:none;
    border-top:1px solid var(--line)}
  #plane-head,#plane-bar{gap:5px;padding:6px 9px}
  #plane-head .pbtn,#plane-bar .pbtn{font-size:10px;padding:3px 7px}
}
/* ▣ ก้อนเรื่องราว — the story/method layer. Cards, not a table, for the same
   reason the feed is cards: a fixed meta line plus wrapped values means a phone
   shows the whole shelf without a sideways scroll, and the six fields of a
   block stay folded until someone asks for them. */
#blocks{display:flex;flex-direction:column;gap:9px;padding:10px 14px;border-bottom:1px solid var(--line);max-height:46dvh;overflow-y:auto}
#blk-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);padding-bottom:6px;z-index:2}
#blk-bar b{font-size:13px;color:var(--acc)}
#blk-q{flex:1;min-width:140px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:9px;font:12px var(--mono);padding:5px 7px}
#blk-status{background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:9px;font:11px var(--mono);padding:5px 6px}
.blk{border:1px solid var(--line);border-radius:11px;padding:9px 10px}
.blk[data-open="1"]{border-color:var(--acc)}
.blk-h{display:flex;gap:7px;align-items:baseline;flex-wrap:wrap}
.blk-h b{font:12px var(--mono);color:var(--acc)}
.blk-m{font:10px var(--mono);color:var(--dim)}
.blk-t{font-size:13px;line-height:1.35;margin-top:5px}
.blk-v{font:11px/1.45 var(--mono);margin-top:6px;white-space:pre-wrap;word-break:break-word}
.blk-v i{color:var(--dim);font-style:normal;display:inline-block;min-width:66px}
.blk-more{cursor:pointer;color:var(--acc2);font:11px var(--mono);margin-top:6px;user-select:none}
.blk-rev{border-left:2px solid var(--line);padding-left:8px;margin-top:5px}

/* ── ⏱ session-block timeline ──────────────────────────────────────── */
#tl-panel{display:flex;flex-direction:column;gap:0;padding:0;border-bottom:1px solid var(--line);max-height:50dvh;overflow-y:auto;background:var(--bg)}
#tl-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);padding:8px 14px 6px;z-index:2;border-bottom:1px solid var(--line)}
#tl-bar b{font-size:13px;color:var(--acc2)}
#tl-body{padding:10px 14px;position:relative}
.tl-row{display:flex;align-items:stretch;gap:0;position:relative;min-height:56px}
.tl-rail{width:28px;flex:0 0 28px;display:flex;flex-direction:column;align-items:center;position:relative}
.tl-dot{width:11px;height:11px;border-radius:50%;border:2px solid var(--acc2);background:var(--bg);z-index:1;flex:0 0 auto;margin-top:14px}
.tl-dot.dup{border-color:var(--warn);background:var(--warn);width:9px;height:9px}
.tl-line{width:2px;flex:1;background:var(--line);min-height:20px}
.tl-card{flex:1;padding:6px 10px 8px;border:1px solid var(--line);border-radius:10px;margin:4px 0;background:var(--panel);min-width:0}
.tl-card:hover{border-color:var(--acc2)}
.tl-card.hi{border-color:var(--warn);background:var(--panel2)}
.tl-h{display:flex;gap:6px;align-items:baseline;flex-wrap:wrap;font:11px var(--mono)}
.tl-h .app{color:var(--acc);font-weight:600}
.tl-h .ts{color:var(--dim)}
.tl-h .rev{color:var(--dim)}
.tl-h .st{color:var(--warn);border:1px dashed var(--warn);border-radius:7px;padding:0 5px;font-size:9px}
.tl-title{font-size:13px;line-height:1.3;margin-top:3px;color:var(--fg)}
.tl-ev{font:10px var(--mono);color:var(--dim);margin-top:2px}
.tl-link{display:inline-block;font:9px var(--mono);color:var(--acc2);border:1px dashed var(--acc2);border-radius:7px;padding:1px 6px;margin-top:3px;cursor:pointer;text-decoration:none}
.tl-link:hover{background:var(--acc2);color:var(--bg)}
.tl-link .sim{opacity:.7}
.tl-empty{color:var(--dim);text-align:center;padding:30px 12px;font:13px var(--mono)}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>chat<b>-pool</b></h1>
  <span id="chain">กำลังตรวจ…</span>
  <button class="pbtn" id="btn-side" title="แผงจัดการ — ค้นข้อความ · ดูว่าแอปไหนเชื่อมต่ออยู่ · ข้อความที่ส่งถึงคุณ · สถิติ">☰ แผง</button>
  <button class="pbtn" id="btn-cur" title="ตัวกรองขยะ: ซ่อนข้อความไร้สาระ/ข้อความทดสอบออกจากฟีด — กดเพื่อปิดตัวกรองและเห็นทุกบรรทัด (ไฟล์ log ไม่ถูกแตะ)">🧹 …</button>
  <button class="pbtn" id="open-plane" title="แผนที่ที่อยู่ 144×144 ช่อง — ทุกบรรทัดมีที่อยู่ของตัวเอง คลิกช่องเพื่อเปิดดูว่ามีอะไรอยู่ตรงนั้น">▦ แผนที่</button>
  <button class="pbtn" id="btn-blk" title="ความจำที่บันทึกไว้ — เรื่องราว + วิธีการ + หลักฐาน ที่ทุกแอปอ่านเข้าใจตรงกัน และตรวจย้อนกับประวัติได้">▣ ความจำ</button>
  <button class="pbtn" id="btn-tl" title="เส้นเวลา — ดูว่าแอปไหนทำงานเสร็จเมื่อไร และความจำชิ้นไหนเชื่อมกับชิ้นไหน">⏱ เส้นเวลา</button>
  <span style="margin-left:auto;font:10px var(--mono);color:var(--dim)" id="clock"></span>
</header>
<div id="tabs"></div>
<div id="tickbar">
  <span id="tick-label">ช่วง…</span>
  <input type="range" id="tick-range" min="0" max="0" value="0" title="ลากเพื่อข้ามไปช่วงนั้น">
  <button class="pbtn" id="tk-prev" title="ช่วงก่อนหน้า">◀</button>
  <button class="pbtn" id="tk-next" title="ช่วงถัดไป">▶</button>
  <button class="pbtn" id="tk-open" title="กางทุกช่วงที่โหลดมา">กาง</button>
  <button class="pbtn" id="tk-close" title="พับทุกช่วง — อ่านภาพรวมยาว ๆ">พับ</button>
</div>
<div id="blocks" hidden>
  <div id="blk-bar">
    <b>▣ ความจำที่บันทึกไว้</b>
    <input id="blk-q" placeholder="ค้นในความจำที่บันทึกไว้ (Enter)">
    <select id="blk-status"><option value="">ทุกสถานะ</option><option value="open">กำลังทำ</option><option value="closed">จบแล้ว</option><option value="parked">พักไว้</option></select>
    <span class="blk-m" id="blk-count">–</span>
    <button class="pbtn" id="blk-close" title="ปิดแผง">✕</button>
  </div>
  <div id="blk-list">…</div>
</div>
<div id="tl-panel" hidden>
  <div id="tl-bar">
    <b>⏱ เส้นเวลา</b>
    <span class="blk-m" id="tl-count">–</span>
    <button class="pbtn" id="tl-close" title="ปิดเส้นเวลา">✕</button>
  </div>
  <div id="tl-body">…</div>
</div>
<main>
  <div id="sysline" hidden>ซ่อนบันทึกระบบไว้ <b id="sys-n">0</b> — <a href="#" data-mach="bond" class="machlink">เส้นเชื่อม <b id="sys-nb"></b></a> · <a href="#" data-mach="pointer" class="machlink">รอยอ่าน <b id="sys-np"></b></a> · <a href="#" data-mach="all" class="machlink">ทั้งหมด</a> · <a href="#" id="sys-hide" hidden>ซ่อนกลับ</a> <span style="color:var(--dim)">ยังถูกใช้บนแผนที่ตามปกติ</span></div>
  <div id="feed"></div>
  <aside id="side">
    <section>
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:9px">
        <h2 style="margin:0;flex:1">แผงจัดการ</h2>
        <button class="pbtn" id="side-close">ปิด ✕</button>
      </div>
      <input id="q" placeholder="กรองข้อความ…">
      <select id="apps-sel" title="กรองตามแอป" style="width:100%;margin-top:8px;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:9px;font:12px var(--mono);padding:6px 7px">
        <option value="">ทุกแอป</option>
      </select>
    </section>
    <section>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font:10px var(--mono);color:var(--dim)">มุมมอง</span>
        <select id="id-sel" title="ดูในมุมของใคร — เลือกแล้วฟีดจะเหลือเฉพาะบรรทัดของคนนั้น" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);padding:4px 6px">
          <option value="admin">ผู้ดูแล (เห็นทุกอย่าง)</option>
        </select>
      </div>
      <div id="regbox" style="margin-top:9px;display:none">
        <div style="font:10px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:1px">แอปที่เชื่อมต่ออยู่</div>
        <div id="reg-list" style="font:11px var(--mono);color:var(--dim);max-height:130px;overflow-y:auto;white-space:pre-wrap;padding-top:4px"></div>
      </div>
      <div id="inboxbox" style="margin-top:9px;display:none">
        <div style="font:10px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:1px">ข้อความถึงคุณ <b id="ib-count" style="color:var(--acc)">0</b></div>
        <div id="ib-list" style="font:11px var(--mono);color:var(--dim);max-height:150px;overflow-y:auto;white-space:pre-wrap;padding-top:4px"></div>
      </div>
    </section>
    <section>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font:10px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:1px">ตัวกรองขยะ</span>
        <button class="pbtn" id="cur-toggle" style="margin-left:auto">เปิด/ปิด</button>
      </div>
      <div class="curbar" id="cur-out" style="padding-top:6px">…</div>
    </section>
    <section>
      <div class="stat"><span>บรรทัดทั้งหมด</span><b id="st-lines">–</b></div>
      <div class="stat"><span>เขียนล่าสุด</span><b id="st-last">–</b></div>
      <div class="stat">
        <span>สำเนาที่ค้นได้</span>
        <label class="clabel"><input type="checkbox" id="cloudck" style="accent-color:var(--acc)"> แสดง</label>
      </div>
      <div class="cloudwrap" id="cloudwrap"><div id="cloudout" style="font:11px var(--mono);color:var(--dim);white-space:pre-wrap;padding-top:6px">…</div></div>
    </section>
  </aside>
</main>
<div id="scrim"></div>
<div id="hint">
  <span>ส่งถึง</span>
  <select id="to-sel" title="ส่งถึงใคร — ทุกคน หรือเฉพาะแอปเดียว"><option value="all">→ ทุกคน</option></select>
  <span id="flushtip" style="margin-left:auto" title="ทุกชั่วโมงระบบจะคัดลอกข้อความใหม่ไปไว้ในสำเนาที่ค้นหาได้เอง โดยไม่ต้องกดอะไร">สำเนาทุกชม. ·  <a href="#" id="flushnow" style="color:var(--acc2)">สำเนาเดี๋ยวนี้</a> · <a href="#" id="tour-again" style="color:var(--acc2)">แนะนำการใช้งาน</a></span>
</div>
<div id="composer">
  <textarea id="msg-in" rows="1" placeholder="พิมพ์ข้อความ… (Enter ส่ง · Shift+Enter ขึ้นบรรทัดใหม่) · @ชื่อ=ติดป้ายให้คนนั้น"></textarea>
  <button class="send" id="send">ส่ง</button>
</div>
<div class="toast" id="toast"></div>
<div id="tour" hidden>
  <div id="tour-card">
    <div id="tour-steps"><span class="tstep" data-s="1">1</span><span class="tbar"></span><span class="tstep" data-s="2">2</span><span class="tbar"></span><span class="tstep" data-s="3">3</span></div>
    <b id="tour-title"></b>
    <div id="tour-body"></div>
    <div id="tour-row">
      <a href="#" id="tour-skip">ข้าม — ไม่ต้องแนะนำแล้ว</a>
      <button class="pbtn" id="tour-next"></button>
    </div>
  </div>
</div>
<div id="plane-modal">
  <div id="plane-box">
    <div id="plane-head">
      <b style="font-size:13px;color:var(--acc)">แผนที่ที่อยู่ 144×144 ช่อง</b>
      <span id="plane-info" style="font:10px var(--mono);color:var(--dim)"></span>
      <span style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
        <button class="pbtn on" id="pv-plane" title="มองทั้งแผนที่ — เห็นแค่ว่าช่องไหนมีบรรทัดอยู่กี่บรรทัด ไม่มีเนื้อหา">▦ ทั้งแผนที่</button>
        <button class="pbtn" id="pv-blocks" title="ย่อแผนที่เหลือตาราง 12×12 — หนึ่งช่องแทน 12×12 ช่องของแผนที่">▩ ย่อ 12×12</button>
        <button class="pbtn" id="pv-block" title="เปิดกรอบเลื่อนบนแผนที่ — ลากกรอบหรือกดลูกศร วนรอบไม่รู้จบ">▢ กรอบเลื่อน</button>
        <select id="pv-win" title="ขนาดกรอบเลื่อน (หน่วย = ช่องบนแผนที่)" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px;font:10px var(--mono);padding:2px 4px">
          <option value="12">12×12</option>
          <option value="24">24×24</option>
          <option value="36">36×36</option>
          <option value="48">48×48</option>
          <option value="72">72×72</option>
        </select>
        <button class="pbtn" id="pv-lens" title="🔍 แว่นขยาย — ถือไว้แล้วลากไปทั่วแผนที่ ช่องใต้แว่นจะขยาย (ลาก = เลื่อนแว่น · ล้อ/ปุ่มมุมขวาล่าง = ซูม · ลูกศร = ขยับทีละช่อง · Enter/คลิก = อ่านช่องนั้น) ขยายได้ถึงความละเอียดจริงเท่านั้น: 1 ช่องเต็มแว่นแล้วหยุด">🔍 แว่นขยาย</button>
        <button class="pbtn" id="pv-opt" title="ตัวเลือกการวาด: ตัดขยะ · เส้นเชื่อม · ความจำ · สีของช่อง · วิธีวาดเส้น · รัศมี · แพทเทิน">⚙ ตัวเลือก</button>
        <button class="pbtn" id="pv-png2" title="บันทึกภาพแผนที่ตรงนี้เป็นไฟล์ PNG">⤓ png</button>
        <button class="pbtn" id="plane-close">ปิด ✕</button>
      </span>
    </div>
    <div id="plane-bar" class="off">
      <label class="clabel" title="ตัดขยะ: บรรทัดที่ระบบตัดสินว่าเป็นข้อความทดสอบ/สัญญาณรบกวน จะไม่กินที่อยู่บนแผนที่ — ช่องที่อยู่ได้เพราะขยะล้วนจะหายไป และเส้นที่ชี้ไปที่นั้นก็ไม่ถูกวาด · ปิด = เห็นแผนที่ดิบก่อนตัด"><input type="checkbox" id="pv-prune" checked style="accent-color:var(--acc2)"> 🧹 ตัดขยะ</label>
      <label class="clabel"><input type="checkbox" id="pv-bonds" checked style="accent-color:var(--acc2)"> เส้นเชื่อม</label>
      <label class="clabel"><input type="checkbox" id="pv-ctx" style="accent-color:var(--acc2)"> ความจำ</label>
      <label class="clabel" title="ให้สีตามความหมายของเส้น (uses/produces/...) — สีประจำความหมายไม่เปลี่ยนระหว่างการวาด"><input type="checkbox" id="pv-rel" checked style="accent-color:var(--acc2)"> สีตามความหมาย</label>
      <select id="pv-colour" title="สีของช่อง = ข้อมูลที่กำลังดูอยู่ (จำนวนบรรทัด / ความร้อน / ใครเขียนล่าสุด / ชนิดของบรรทัด / ล่าสุดเมื่อไร)">
        <option value="density">สี: จำนวนบรรทัด</option>
        <option value="heat">สี: ความร้อน</option>
        <option value="writer">สี: ใครเขียนล่าสุด</option>
        <option value="kind">สี: ชนิดของบรรทัด</option>
        <option value="recent">สี: ล่าสุดเมื่อไร</option>
      </select>
      <select id="pv-kind" title="กรองตามชนิดของบรรทัด — สีและความสว่างของช่องจะเปลี่ยนไปตามชนิดที่เลือก ส่วนที่ไม่ใช่ชนิดนั้นจะจางลง (ยังนับรวมอยู่) · ตัวเลขที่ผู้อ่านเห็นยังนับทั้งคลัง ไม่ใช่เฉพาะที่กรอง · เลือกที่นี่จะแทนที่การกรองด้วยผู้เขียน (กรองได้ทีละแกน)">
        <option value="">ทุกชนิด</option>
      </select>
      <select id="pv-app" title="กรองตามผู้เขียน — ดูคลังข้อมูลในแว่นของแอปหนึ่ง ๆ เช่นตอน chatdrop เป็นคนเขียนอย่างเดียว · เลือกที่นี่จะแทนที่การกรองด้วยชนิด (กรองได้ทีละแกน)">
        <option value="">ทุกผู้เขียน</option>
      </select>
      <select id="pv-style" title="วิธีวาดเส้นเชื่อม" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px;font:10px var(--mono);padding:2px 4px">
        <option value="auto">อัตโนมัติ (วัดแล้ว)</option>
        <option value="density">จางตามความแน่น</option>
        <option value="flat">เส้นตรง</option>
        <option value="bundle">รวมเส้น (คู่เดียวกัน)</option>
        <option value="hub">เฉพาะจุดรวม (N แรก)</option>
        <option value="degree">วงตามจำนวนเส้น (ไม่วาดเส้น)</option>
      </select>
      <select id="pv-hubk" title="กี่ช่องที่ถือว่าเป็นจุดรวม — ยิ่งมาก ยิ่งเห็นเส้นมาก (ก่อนจะกลายเป็นหมึกท่วม)" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:7px;font:10px var(--mono);padding:2px 4px">
        <option value="12">จุดรวม 12</option>
        <option value="30">จุดรวม 30</option>
        <option value="60">จุดรวม 60</option>
        <option value="120">จุดรวม 120</option>
      </select>
      <span id="pv-wininfo" style="color:var(--dim);font:10px var(--mono)"></span>
      <span id="pv-lensinfo" title="ระดับที่กำลังขยายอยู่ · × = ขยายกี่เท่า · L = ความละเอียดจริงของข้อมูล"></span>
      <span style="display:flex;gap:4px;align-items:center;margin-left:auto">
        <button class="pbtn" id="pv-census" title="รูปร่างของกราฟทั้งแผนที่: กี่บรรทัดเป็นเกาะ/ต้นน้ำ/กลางสาย/ปลายน้ำ และบรรทัดไหนที่มีคนอ้างถึงมากสุด">📊 แพทเทิน</button>
        <span style="color:var(--dim)" title="รัศมี = แสดงเฉพาะช่องที่เชื่อมกับจุดที่เลือกภายใน N ทอด — 1 ทอด คือมีเส้นตรงถึงกัน 1 เส้น (ต้องเลือกจุดบนแผนที่ก่อน)">รัศมี</span>
        <button class="pbtn on" id="pv-f0" title="เห็นทั้งแผนที่ ไม่กรองอะไร">ปิด</button>
        <button class="pbtn" id="pv-f1" title="เหลือเฉพาะช่องที่ต่อจากจุดที่เลือก 1 ทอด (มีเส้นตรงถึงกัน 1 เส้น)">1 ทอด</button>
        <button class="pbtn" id="pv-f2" title="เหลือเฉพาะช่องที่ต่อจากจุดที่เลือกภายใน 2 ทอด">2 ทอด</button>
        <button class="pbtn" id="pv-f3" title="เหลือเฉพาะช่องที่ต่อจากจุดที่เลือกภายใน 3 ทอด">3 ทอด</button>
      </span>
    </div>
    <!-- one row for "when": the coloured strip AND the scrubber it corresponds
         to. They used to sit in two different rows — the strip here, the slider
         and replay down in the options bar — which is the half of "controls
         piled on controls" that was not just clutter: two controls for one axis
         in two places reads as two axes. -->
    <div id="plane-ribbon">
      <canvas id="pv-ribbon" title="แถบเวลา: หนึ่งแท่ง = หนึ่งช่วง 8 บรรทัด สูงตามจำนวนบรรทัด สีตามคนที่เขียนมากสุด · ลากเพื่อเลื่อนเวลา"></canvas>
      <span id="pv-ribbon-info">…</span>
      <span id="pv-time">
        <span id="pv-when">ทุกบรรทัด</span>
        <input type="range" id="pv-seq" min="0" max="0" value="0" title="ลากเพื่อข้ามไปบรรทัดนั้น — ปลายขวาสุด = สภาพตอนนี้">
        <button class="pbtn" id="pv-play" title="เล่นย้อนลำดับการเขียนทีละบรรทัด">▶ เล่นย้อน</button>
        <button class="pbtn" id="pv-last" title="กลับไปสถานะตอนนี้ (แผนที่สด)">ล่าสุด</button>
      </span>
    </div>
    <div id="plane-body">
      <div id="plane-canvaswrap">
        <canvas id="plane-cv"></canvas>
        <!-- the +/− start disabled because the magnifier starts OFF: there is
             nothing to zoom until there is a glass to zoom. pvLensInfo owns
             their state from the moment it runs. -->
        <div id="pv-zoom">
          <button id="pv-lens-zin" disabled title="ซูมเข้า — เห็นช่องเล็กลง (หยุดที่ความละเอียดจริง)">+</button>
          <button id="pv-lens-zout" disabled title="ซูมออก — เห็นช่องใหญ่ขึ้น">−</button>
          <button id="pv-zreset" title="รีเซ็ตมุมมอง — กลับไปทั้งแผนที่และความละเอียด ×1 พร้อมวางแว่นที่จุดที่เลือก">⟲</button>
        </div>
        <div id="plane-tip" popover="manual"></div>
        <div id="plane-pop" popover="manual"></div>
      </div>
      <div id="plane-side"><div id="pv-comp"></div><div id="plane-detail"><div id="pv-legend"></div><b style="color:var(--fg)">แผนที่นี้คือที่อยู่ของทุกบรรทัด</b> — หนึ่งช่อง = หนึ่งที่อยู่ ตัวเลขในช่อง = มีกี่บรรทัดอยู่ตรงนั้น<br><br>▸ <b>คลิกช่อง</b> = ดูว่ามีอะไรอยู่ตรงนั้น<br>▸ <b>🔍 แว่นขยาย</b> = ถือไปดูใกล้ ๆ (ลาก = เลื่อนแว่น)<br>▸ ปุ่ม <b>+ −</b> มุมขวาล่าง = ซูมเข้า/ออก · <b>⟲</b> = กลับไปทั้งแผนที่<br>▸ <b>บนจอสัมผัส</b>: ลาก = เลื่อนมุมมอง · บีบสองนิ้วออก = ซูมเข้า · บีบสองนิ้วเข้า = ซูมออก (หยุดที่ทั้งระนาบ และที่กรอบ 12×12)<br>▸ <b>⚙ ตัวเลือก</b> ด้านบน = เปิดตัวกรอง (ตัดขยะ · เส้นเชื่อม · ความจำ · รัศมี · แพทเทิน)<br><br><span style="color:var(--dim)">◌ สีของช่อง = ข้อมูลที่เลือกไว้ด้านบน (จำนวนบรรทัด / ความร้อน / ใครเขียนล่าสุด / ล่าสุดเมื่อไร)<br>◌ วงฟ้า = ช่องที่มีคนบันทึกไว้ว่าอ่านแล้ว<br>◌ เส้นฟ้า = เส้นเชื่อมระหว่างบรรทัด · เส้นขาว = เส้นที่แตะจุดที่เลือกไว้<br>◌ จุดฟ้า = ความจำกลางที่แชร์ไว้ · เส้นม่วงประ = เส้นที่เชื่อมข้ามระบบ<br>◌ แถบเวลาด้านบน = ใครเขียนเมื่อไร · ลากที่แถบหรือลากสไลเดอร์เพื่อย้อนดูแผนที่ค่อย ๆ ประกอบ<br>◌ คีย์บอร์ด: ← → เลื่อนทีละบรรทัด · space เล่น/หยุด · Esc ปิด</span><div id="rel-legend" style="margin-top:7px"></div></div></div>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
// ── display budget ───────────────────────────────────────────────────────────
// CHUNK: how many lines make one tick. A tick is a POSITION in append order —
// the same number the plane scrubs with, never a wall clock — so a chunk keeps
// its number however much the log grows after it, and the newest tick is always
// the last one. CLAMP: how much of one line a card shows while it is closed. A
// handoff that used to fill the screen is now one scannable card, and the rest
// is one tap away.
const CHUNK=8, CLAMP=260;
let msgs=[], filterQ='', filterApps=new Set(), identity='admin', myInbox=[];
let expanded=new Set(), openTicks=new Set(), autoOpen=true, stick=true;
// curator state lives on the client: every line arrives with its verdict (see
// pool.stamp_cur), so 🧹 is a re-render, not a refetch — and the log itself is
// never modified, only what this feed chooses to render
let curatorOn=true, revealTicks=new Set(), curatorRep=null;
// LAN access: token prompt (localhost skips auth server-side, this is harmless there)
const TOKEN=(()=>{const m=location.hash.match(/token=([0-9a-f]+)/);const q=new URLSearchParams(location.search).get('token');const t=m?m[1]:q;if(t){localStorage.setItem('pool_token',t);history.replaceState(null,'',location.pathname)}return t||localStorage.getItem('pool_token')||''})();
const H=TOKEN?{'Authorization':'Bearer '+TOKEN}:{'x-noauth':'1'};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fmt(t){const d=new Date(t*1000);return d.toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'})+(d.getDate()!=new Date().getDate()?' '+d.toLocaleDateString('th-TH',{day:'2-digit',month:'short'}):'')}
function hash8(h){return h?h.slice(0,8):''}
const REL_TH={'x-uses-term':'อ้างถึงศัพท์ของอีกฝั่ง','x-hint-is-term':'หัวเรื่องคือศัพท์ของอีกฝั่ง',
  'x-same-hint':'หัวเรื่องเดียวกัน','x-same-workspace':'พื้นที่ทำงานเดียวกัน',
  'x-next-chunk':'ต่อเนื่องกัน','x-next-in-session':'ต่อเนื่องในเซสชันเดียวกัน',
  'x-answers':'ตอบกลับ','uses':'ใช้','produces':'ผลิต','validates':'ยืนยัน',
  'decides':'ตัดสิน','follows':'ตามหลัง','contradicts':'ขัดแย้ง'};
// bond text `src rel dst` — space- or newline-separated (writers differ);
// → human line; null when not a bond (caller falls back to raw text)
function bondShort(full){
  const p=String(full||'').match(/^\s*([0-9a-f]{64})\s+(\S+)\s+([0-9a-f]{64})\s*$/);
  if(!p)return null;
  return {html:'เชื่อมบันทึกสองชิ้น: '+esc(REL_TH[p[2]]||p[2])
    +' <span style="color:var(--dim)">· '+hash8(p[1])+' → '+hash8(p[3])+'</span>',
    title:String(full||'')};
}
// pointer text `point=N level=L` → human line; null otherwise
function pointerShort(full){
  const p=String(full||'').match(/^point=(\d+) level=(\d+)$/);
  if(!p)return null;
  const lv=['ช่อง','บล็อก 12×12','ทั้งระนาบ'][+p[2]]||('ชั้น '+p[2]);
  return {html:'อ่าน'+esc(lv)+'ที่ '+p[1]
    +' <span style="color:var(--dim)">· จุดวงฟ้าบนแผนที่ = ที่ที่แอปนี้กำลังดูอยู่</span>',
    title:String(full||'')};
}
function tickOf(m){return m.seq==null?null:Math.floor(m.seq/CHUNK)}
// what the filters admit, before the curator is consulted
// a machine line is DATA ABOUT THE SYSTEM, not something someone said — a bond
// ("this line uses that term"), a pointer ("someone read here"), an unbind.
// The feed is for reading the conversation, so these collapse to one quiet row
// per kind per chunk instead of one card each: at 25k lines a bond flood was
// burying every real message. `machineSel` holds the per-kind switches, and a
// search that NAMES a hash must still find its card, which is why the filter
// stays inside inScope() rather than the renderer.
const MACHINE_KINDS=new Set(['bond','pointer','unbind']);
/* ONE table for every kind on screen, read by BOTH the feed badge and the
   plane's colour legend. Two tables for one job is the drift §17.5 already
   paid for with the curator rules: add a kind to one and the other prints the
   wire name, and the symptom is invisible because the page still renders.
   The words are Thai because these are read by people who did not build it;
   the wire keeps its own spelling underneath (§17.2).

   `kindIsChat` is a FUNCTION, not a comparison, for the same reason
   curator_protect() compares a kind by value: the corpus writes CHAT in caps
   and bond/pointer/block in lower, so `kind==='chat'` matched none of the
   13,746 chat lines — every one of them grew a badge reading "CHAT". A kind
   is a value, never a spelling. */
const KIND_TH={CHAT:'แชท',DECISION:'ข้อสรุป',bond:'เส้นเชื่อม',pointer:'รอยอ่าน',
  block:'ความจำ',unbind:'ยกเลิกเส้นเชื่อม',msg:'ข้อความ',bridge:'สะพาน'};
const kindIsChat=k=>String(k||'chat').trim().toLowerCase()==='chat';
const kindLabel=k=>KIND_TH[k]||(kindIsChat(k)?KIND_TH.CHAT:'อื่น ๆ ('+(k||'?')+')');
/* The plane's palette. Kinds are a CLOSED, tiny set — eight across 32,685
   lines — so they get FIXED hues rather than the app hash: a hash is stable
   but not learnable, and nothing would ever tell the reader that one hue means
   "bonds" twice. Hand-spaced so no two sit next to each other: 20 · 60 · 135 ·
   172 · 210 · 280 · 330. An unknown kind falls back to the app hash rather
   than taking a colour that already means something else. */
const KIND_HUE={DECISION:20,block:60,CHAT:135,bridge:172,bond:210,pointer:280,
  msg:330,unbind:210};
// which machine kinds the reader has ASKED to see. `all` is the old "show
// everything": one state variable per question — "show bonds?", "show
// pointers?" — instead of one boolean over both, because wanting to audit who
// read where is not the same as wanting 2,000 bond lines back in the feed.
// A search that names a hash still finds its card regardless of these.
let machineSel={all:false,bond:false,pointer:false};
function machineShown(m){
  const k=m.kind||'';
  if(k==='bond'||k==='unbind')return machineSel.all||machineSel.bond;
  if(k==='pointer')return machineSel.all||machineSel.pointer;
  return false;
}
function inScope(m){
  if(!machineShown(m)&&MACHINE_KINDS.has(m.kind||'')&&!filterQ)
    return false;                    // data about the system: folded away by default
  return (!filterQ||(m.text||'').toLowerCase().includes(filterQ))&&
         (filterApps.size===0||filterApps.has(m.app));
}
// a line the curator would keep out. `revealTicks` is the human overruling it
// for ONE tick — the curator never gets to hide something permanently
function trimmed(m){
  return curatorOn && !!(m.cur&&m.cur.h) && !revealTicks.has(tickOf(m));
}
function visible(){return msgs.filter(m=>inScope(m)&&!trimmed(m))}
// one line, clamped: label row + as much of the text as fits a glance
// the browser reads the SAME canonical text the CLI prints (no second format on
// the wire): a block line in the feed is six lines of payload, so it collapses to
// one line here and the panel shows the rest — the feed is for the conversation,
// not for re-printing a memory in full.
function parseBlockText(t){
  if(!t||!t.startsWith('⟦block⟧'))return null;   /* the marker is 7 chars, not 9 */
  const head=t.split('\n')[0].match(/^⟦block⟧ slug=([^ ]+) rev=(\d+)$/);
  if(!head)return null;
  const d={slug:head[1],rev:+head[2],status:'open'};
  ['title','story','method','evidence','refs','status'].forEach(k=>{
    const ln=t.split('\n').find(x=>x.indexOf(k+':')===0);
    if(ln)d[k]=ln.slice(k.length+1).trim()||d[k];
  });
  return d.title?d:null;
}
function card(m){
  const kind=m.kind||'chat';
  // the two card-shaped machine kinds get names a reader owns, not the wire's:
  // a bond is a connection, a pointer is a record of a read — from the one
  // KIND_TH, so the feed badge and the plane legend cannot disagree on a word
  const kbadge=kindIsChat(kind)?'':`<span class="badge kind" title="${MACHINE_KINDS.has(kind)?'บันทึกของระบบ — เป็นข้อมูลที่แผนที่ใช้วาดเส้น/วง ไม่ใช่ข้อความคุย':''}">${esc(kindLabel(kind))}</span>`;
  const arrow=m.to&&m.to!=='all'?`<span class="badge kind">→ ${esc(m.to)}</span>`:'';
  const quiet=!!(m.cur&&m.cur.h);
  const full=String(m.text==null?'':m.text);
  if(kind==='block'){
    const bd=parseBlockText(full);
    if(bd)return `<div class="msg" data-m="${esc(m.hash||'')}"><div class="top"><span class="badge">${esc(m.app||'?')}</span>${kbadge}`
      +`<span class="time">${fmt(m.t)}</span><span class="meta" data-h="${esc(m.hash||'')}" title="คลิกเพื่อคัดลอก">#${m.seq==null?'–':m.seq}</span></div>`
      +`<div class="text"><b>▣ ${esc(bd.slug)}</b> <span style="color:var(--dim)">แก้ครั้งที่ ${bd.rev} · ${blkStatusTh(bd.status)}</span><br>${esc(bd.title)}</div>`
      +`<button class="more" data-blk="${esc(bd.slug)}">▣ เปิดในแผงก้อน — เรื่อง · วิธีการ · หลักฐาน</button></div>`;
  }
  if(kind==='bond'){
    // `src rel dst` where src/dst are 64-hex: the human line is the RELATION.
    // The hashes stay reachable — the click still copies — but they are no
    // longer the first thing the eye lands on.
    const p=full.match(/^([0-9a-f]{64}) (\S+) ([0-9a-f]{64})$/);
    if(p){
      const rel=REL_TH[p[2]]||p[2];
      return `<div class="msg quiet" data-m="${esc(m.hash||'')}"><div class="top">`
        +`<span class="badge">${esc(m.app||'?')}</span>${kbadge}`
        +`<span class="time">${fmt(m.t)}</span><span class="meta" data-h="${esc(m.hash||'')}" title="คลิกเพื่อคัดลอก">#${m.seq==null?'–':m.seq}</span></div>`
        +`<div class="text" title="คลิกซ้ายเพื่อดูต้นทางบนแผนที่">เชื่อมบันทึกสองชิ้น: ${esc(rel)}`
        +`<span style="color:var(--dim)"> · ${hash8(p[1])} → ${hash8(p[3])} · คลิกที่ #ลำดับเพื่อคัดลอกรหัสเต็ม</span></div></div>`;
    }
  }
  if(kind==='pointer'){
    const p=full.match(/^point=(\d+) level=(\d+)$/);
    if(p){
      const lv=['ช่อง','บล็อก 12×12','ทั้งระนาบ'][+p[2]]||('ชั้น '+p[2]);
      return `<div class="msg quiet" data-m="${esc(m.hash||'')}"><div class="top">`
        +`<span class="badge">${esc(m.app||'?')}</span>${kbadge}`
        +`<span class="time">${fmt(m.t)}</span><span class="meta" data-h="${esc(m.hash||'')}" title="คลิกเพื่อคัดลอก">#${m.seq==null?'–':m.seq}</span></div>`
        +`<div class="text">อ่าน${esc(lv)}ที่ ${p[1]} · <span style="color:var(--dim)">จุดวงฟ้าบนแผนที่ = ที่ที่แอปนี้กำลังดูอยู่</span></div></div>`;
    }
  }
  const lines=full.split('\n').length;
  // a card only earns a clamp if there is something worth hiding: a text that
  // barely crosses the budget reads in full instead of offering +6 ตัวอักษร
  const long=full.length>CLAMP+60, open=expanded.has(m.hash);
  const body=(open||!long)?full:full.slice(0,CLAMP);
  const more=(long&&!open)
    ?`<button class="more" data-h="${esc(m.hash||'')}">▸ อ่านต่อ (อีก ${full.length-CLAMP} ตัวอักษร${lines>1?` · ${lines} บรรทัด`:''})</button>`
    :'';
  // a revealed line says WHY it was kept out, in the line itself: a filter you
  // cannot audit is one you end up not believing
  const rbadge=quiet?`<span class="badge quiet" title="ตัวกรองขยะซ่อนบรรทัดนี้ไว้เพราะเข้าข่ายเหล่านี้ · ตัวเลขคือคะแนนความน่าเบื่อ ยิ่งสูงยิ่งถูกซ่อน">🧹 ${esc((m.cur.r||[]).map(ruleTh).join(' + '))} ${m.cur.s}</span>`:'';
  return `<div class="msg${quiet?' quiet':''}" data-m="${esc(m.hash||'')}"><div class="top"><span class="badge">${esc(m.app||'?')}</span>${kbadge}${arrow}${rbadge}`
    +`<span class="time">${fmt(m.t)}</span>`
    +`<span class="meta" data-h="${esc(m.hash||'')}" title="คลิกเพื่อคัดลอก">#${m.seq==null?'–':m.seq}</span>`
    +`</div><div class="text">${esc(body)}${long&&!open?'…':''}</div>${more}</div>`;
}

function render(){
  const feed=$('#feed');
  // groups are built from what the FILTERS admit, not from what the curator
  // left: a chunk whose every line is noise still keeps its tick in the rail,
  // because a timeline that renumbers itself when you flip 🧹 is not a timeline
  const list=msgs.filter(inScope);
  // the folded machine lines did not vanish — say where they went, and keep a
  // door open: a reader who sees "6 system records" must be able to open them
  // without hunting for a settings panel
  const machineFolded=msgs.filter(m=>MACHINE_KINDS.has(m.kind||'')&&!inScope(m)).length;
  const sys=$('#sysline');
  if(sys){
    // the bar is a PICKER, not a switch: per-kind counts, per-kind doors, and
    // a door that closes. `on` marks what is open so the state is on the bar,
    // never only in the feed
    const nb=msgs.filter(m=>(m.kind==='bond'||m.kind==='unbind')&&!machineShown(m)).length;
    const np=msgs.filter(m=>m.kind==='pointer'&&!machineShown(m)).length;
    sys.hidden=!(nb+np);
    $('#sys-n').textContent=nb+np;
    $('#sys-nb').textContent=nb?nb:'';
    $('#sys-np').textContent=np?np:'';
    sys.querySelectorAll('.machlink').forEach(a=>{
      const w=a.dataset.mach;
      a.classList.toggle('on',machineSel[w]);
      a.onclick=e=>{e.preventDefault();machineSel[w]=!machineSel[w];render()};
      a.title=w==='bond'?'แสดง/ซ่อนเฉพาะเส้นเชื่อม (บันทึกที่บอกว่าอะไรเชื่อมกับอะไร)'
        :w==='pointer'?'แสดง/ซ่อนเฉพาะรอยอ่าน (จุดวงฟ้าบนแผนที่)'
        :'แสดง/ซ่อนบันทึกระบบทุกชนิด';
    });
    const hid=$('#sys-hide');
    hid.hidden=!machineSel.all&&!machineSel.bond&&!machineSel.pointer;
    hid.onclick=e=>{e.preventDefault();machineSel={all:false,bond:false,pointer:false};render()};
    sys.title='เส้นเชื่อมและรอยอ่านเป็นข้อมูลของระบบ (ใช้วาดแผนที่) ไม่ใช่ข้อความคุย — กดเลือกชนิดที่อยากเห็น';
  }
  if(!list.length){
    feed.innerHTML=`<div class="empty">ไม่มีข้อความ${(filterQ||filterApps.size)?' ที่ตรงกับตัวกรอง':''}</div>`;
    paintPickers();paintTicks([]);paintCurator();return;
  }
  // chunk the window by tick: lines are numbered by their own ordinal, so a
  // boundary falls in the same place whether one line arrived or a hundred
  const groups=[];
  list.forEach(m=>{
    const t=tickOf(m);
    if(!groups.length||groups[groups.length-1].tick!==t)groups.push({tick:t,msgs:[]});
    groups[groups.length-1].msgs.push(m);
  });
  if(autoOpen)openTicks.add(groups[groups.length-1].tick);   // the newest tick breathes
  feed.innerHTML=groups.map(g=>{
    const open=openTicks.has(g.tick);
    const shown=g.msgs.filter(m=>!trimmed(m));
    // how many of these lines the curator judges noise, regardless of whether
    // this tick is currently revealing them — the chip has to stay a button in
    // both directions, or revealing a tick would throw away the way back
    const noise=g.msgs.filter(m=>m.cur&&m.cur.h).length;
    const cut=g.msgs.length-shown.length;
    const apps={};shown.forEach(m=>apps[m.app||'?']=(apps[m.app||'?']||0)+1);
    const ent=Object.entries(apps).sort((a,b)=>b[1]-a[1]);
    // three writers named, the rest counted: a header that clips its own tail
    // would hide who is actually in the chunk
    const chips=ent.slice(0,3).map(([a,n])=>`<span class="tchip">${esc(a)}${n>1?' ×'+n:''}</span>`).join('')
      +(ent.length>3?`<span class="tchip">+${ent.length-3}</span>`:'');
    const span=g.msgs[0].seq==null?'':`บรรทัด ${g.msgs[0].seq}–${g.msgs[g.msgs.length-1].seq} · `;
    // the count in the header is what you see; the 🧹 chip is the rest, and it
    // is a button in both directions (hide for this tick / show for this tick)
    const rev=revealTicks.has(g.tick);
    const clean=noise?`<span class="tclean${rev?' on':''}" data-rev="${g.tick}" title="ตัวกรองขยะซ่อนไว้ ${noise} บรรทัดในช่วงนี้ — กดเพื่อดู/ซ่อนเฉพาะช่วงนี้">🧹 ${rev?(noise+' แสดง'):cut}</span>`:'';
    const body=shown.length?shown.map(card).join('')
      :`<div class="empty" style="padding:12px 6px">ช่วงนี้ถูกซ่อนไว้ทั้งช่วง (${cut} บรรทัด)</div>`;
    return `<div class="tick${open?' open':''}" data-tick="${g.tick}">`
      +`<div class="thead"><span class="tno">ช่วงที่ ${g.tick==null?'–':g.tick}</span>`
      +`<span class="tmeta">${span}${fmt(g.msgs[0].t)} – ${fmt(g.msgs[g.msgs.length-1].t)} · ${shown.length} บรรทัด</span>`
      +`<span class="tapps">${chips}${clean}</span><span class="chev">${open?'▾':'▸'}</span></div>`
      +`<div class="tbody">${body}</div></div>`;
  }).join('');
  paintPickers();paintTicks(groups);paintCurator();
  if(stick)feed.scrollTop=feed.scrollHeight;
}

// The curator panel: what it would hide, by rule, with examples — the thing you
// read before trusting it. Counts come from the server (the report over the
// same window), so they do not depend on what this tab happens to be showing.
function paintCurator(){
  const n=msgs.filter(m=>m.cur&&m.cur.h).length;
  const rep=curatorRep||{};
  const btn=$('#btn-cur');
  btn.textContent=curatorOn?`🧹 ซ่อน ${n}`:'🧹 ปิด';
  btn.title=curatorOn
    ? `ตัวกรองขยะกำลังซ่อน ${n} บรรทัดออกจากฟีด — กดเพื่อปิดและเห็นทุกบรรทัด`
    : 'ตัวกรองขยะปิดอยู่ — เห็นทุกบรรทัด กดเพื่อเปิด';
  btn.classList.toggle('on',curatorOn);
  const tog=$('#cur-toggle');
  tog.textContent=curatorOn?'ปิด 🧹':'เปิด 🧹';
  tog.classList.toggle('on',curatorOn);
  const chips=Object.entries(rep.by_rule||{}).slice(0,6)
    .map(([r,c])=>`<span class="curchip">${esc(ruleTh(r))} ${c}</span>`).join('');
  const prot=Object.entries(rep.protected||{}).map(([k,v])=>`${protectTh(k)} ${v}`).join(' · ');
  const ex=(rep.examples||[]).slice(0,3).map(e=>
    `<div class="curex">[${e.score}] ${esc(e.app)} (${esc((e.rules||[]).map(ruleTh).join(' + '))}) ${esc(e.text)}</div>`).join('');
  $('#cur-out').innerHTML=(curatorOn?'':'<b style="color:var(--warn)">ตัวกรองปิดอยู่ — เห็นทุกบรรทัด</b><br>')
    +`อ่านมา ${rep.lines==null?msgs.length:rep.lines} บรรทัด · ซ่อน ${rep.hidden==null?n:rep.hidden}`
    +` · เหลือ ${rep.kept==null?(msgs.length-n):rep.kept} · ตัดเมื่อคะแนน ≥${rep.hide_at==null?'?':rep.hide_at}<br>`
    +(chips?`<div style="margin-top:4px">${chips}</div>`:'')
    +(prot?`<div style="margin-top:4px">ยกเว้นไว้: ${esc(prot)}</div>`:'')
    +(ex?`<div style="margin-top:6px">ตัวอย่างที่ถูกซ่อน:</div>${ex}`:'')
    +`<div style="margin-top:6px;color:var(--dim)">ซ่อนแค่บนจอ — ในไฟล์ log ยังมีครบทุกบรรทัด</div>`
    // the plane answers the SAME question with a different consequence:
    // hide dims a line on the feed, prune stops it occupying an address.
    // Both numbers come from the same rules, so show them side by side.
    +(()=>{
      const p=rep.plane;
      if(!p)return '';
      const pr=Object.entries(p.by_rule||{}).slice(0,6)
        .map(([r,c])=>`<span class="curchip">${esc(ruleTh(r))} ${c}</span>`).join('');
      // prune reaches past the map: an edge or a sighting can lose the end
      // it points at while its own line stays intact. Say so, or "pruned 3
      // lines" reads like nothing else moved.
      const dang=(p.dangling_bonds||0)+(p.dangling_sightings||0);
      return `<div style="margin-top:8px;border-top:1px dashed var(--line);padding-top:6px">`
        +`<b>▦ แผนที่ — การตัดขยะจริง</b> · ช่องที่หายไป ${p.cells_pruned}`
        +` · ตัดออก ${p.pruned} บรรทัด · ตัดเมื่อคะแนน ≥${p.prune_at}`
        +(pr?`<div style="margin-top:4px">${pr}</div>`:'')
        +(dang?`<div style="margin-top:4px">🪢 เส้นที่ชี้ไปยังที่ที่ถูกตัด: เส้นเชื่อม ${p.dangling_bonds} · รอยอ่าน ${p.dangling_sightings}</div>`:'')
        +`<div style="margin-top:6px;color:var(--dim)">บรรทัดที่ถูกตัดจะไม่กินที่อยู่ — และเส้น/รอยอ่านที่ชี้ไปที่นั่นก็ไม่ถูกวาด (ใน log ยังมีครบ)</div></div>`;
    })();
}
function paintPickers(){
  // populate selects (apps filter, view-as, to) + the app tabs
  const apps={};msgs.forEach(m=>apps[m.app||'?']=(apps[m.app||'?']||0)+1);
  const known=[...new Set([...Object.keys(apps),'hermes','codex','human'])].sort();
  const asel=$('#apps-sel');const acur=asel.value;
  asel.innerHTML='<option value="">ทุกแอป</option>'+Object.entries(apps).sort((a,b)=>b[1]-a[1]).map(([a,n])=>`<option value="${esc(a)}">${esc(a)} (${n})</option>`).join('');
  if([...asel.options].some(o=>o.value===acur))asel.value=acur;
  // `identity` is the value the WHOLE page is filtered by, so the control that
  // picks it must (a) contain it and (b) display it. The options are rebuilt
  // from the loaded window, which dropped any identity with no lines in it —
  // the select fell back to "admin" while the view was still filtered as
  // someone else, and picking from it then compared against the wrong value.
  // A picker that disagrees with the thing it picks is worse than no picker.
  const idOpts=[...new Set([...known,identity])].filter(Boolean).sort();
  const isel=$('#id-sel');
  isel.innerHTML='<option value="admin">ผู้ดูแล (เห็นทุกอย่าง)</option>'
    +idOpts.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');
  isel.value=identity;
  const sel=$('#to-sel');const cur=sel.value;
  sel.innerHTML='<option value="all">→ ทุกคน</option>'+known.filter(a=>a!=='human').map(a=>`<option value="${esc(a)}">→ ${esc(a)}</option>`).join('');
  if([...sel.options].some(o=>o.value===cur))sel.value=cur;
  $('#st-lines').textContent=msgs.length;
  $('#st-last').textContent=msgs.length?fmt(msgs[msgs.length-1].t):'–';
  $('#tabs').innerHTML=`<button class="tab${filterApps.size?'':' on'}" data-app="">ทั้งหมด <span class="n">${msgs.length}</span></button>`
    +Object.entries(apps).sort((a,b)=>b[1]-a[1])
      .map(([a,n])=>`<button class="tab${filterApps.has(a)?' on':''}" data-app="${esc(a)}">${esc(a)} <span class="n">${n}</span></button>`).join('');
}
// the rail says what the loaded window really holds: ticks are numbered from the
// start of the log, so the label can never claim more history than it read
function paintTicks(groups){
  const r=$('#tick-range'),label=$('#tick-label');
  const ticks=groups.map(g=>g.tick).filter(t=>t!=null);
  if(!ticks.length){r.min=r.max=0;r.value=0;r.disabled=true;label.textContent='ช่วง —';return}
  r.disabled=false;r.min=Math.min(...ticks);r.max=Math.max(...ticks);r.value=r.max;
  const lines=groups.reduce((a,g)=>a+g.msgs.length,0);
  label.textContent=`ช่วงที่ ${r.min}–${r.max} · ${ticks.length} ช่วง · โหลดมา ${lines} บรรทัด`;
}
// opening a chunk is a view change, not a read: the lines are already here and
// toggling a class keeps the scroll exactly where the finger left it
function toggleTick(t,on){
  const node=$('#feed').querySelector(`.tick[data-tick="${t}"]`);
  if(!node)return;
  const open=on==null?!node.classList.contains('open'):on;
  node.classList.toggle('open',open);
  const chev=node.querySelector('.chev');if(chev)chev.textContent=open?'▾':'▸';
  if(open)openTicks.add(t);else{openTicks.delete(t);autoOpen=false}
}
$('#tabs').onclick=e=>{
  const b=e.target.closest('.tab');if(!b)return;
  const a=b.dataset.app;
  filterApps=(!a||filterApps.has(a))?new Set():new Set([a]);
  stick=true;render();
};
$('#feed').onclick=e=>{
  // reveal/hide the noise inside ONE tick: the curator's verdict is per line,
  // but the human's override is per chunk — that is the unit they are reading
  const rev=e.target.closest('.tclean');
  if(rev){
    const t=+rev.dataset.rev;
    revealTicks.has(t)?revealTicks.delete(t):revealTicks.add(t);
    stick=false;render();
    $('#feed').querySelector(`.tick[data-tick="${t}"]`)?.scrollIntoView({block:'nearest'});
    return;
  }
  const blk=e.target.closest('[data-blk]');
  if(blk){
    // a block in the feed is a pointer to the panel, not a copy of it: one card,
    // one shelf, one place where the story lives
    const slug=blk.dataset.blk;
    blocksOn=true;$('#blocks').hidden=false;blkOpen.add(slug);
    loadBlocks().then(()=>$('#blk-list').querySelector(`.blk[data-slug="${slug}"]`)?.scrollIntoView({block:'nearest'}));
    return;
  }
  const more=e.target.closest('.more');
  if(more&&more.dataset.h){
    const h=more.dataset.h,box=more.closest('.msg'),top=box?box.getBoundingClientRect().top:null;
    expanded.has(h)?expanded.delete(h):expanded.add(h);
    // expanding grows the card under the finger: render() rebuilds the feed, so
    // re-anchor THIS card's top edge where it was instead of jumping (stick
    // would drag the reader to the bottom of the newest tick instead)
    stick=false;render();
    const node=$('#feed').querySelector(`.msg[data-m="${h}"]`);
    if(node&&top!=null)$('#feed').scrollTop+=node.getBoundingClientRect().top-top;
    return;
  }
  const head=e.target.closest('.thead');
  if(head){toggleTick(+head.parentElement.dataset.tick);return}
  const hash=e.target.closest('.meta[data-h]');
  if(hash){const h=hash.dataset.h||'';
    if(navigator.clipboard)navigator.clipboard.writeText(h).catch(()=>{});
    toast('คัดลอกแล้ว · '+hash8(h))}
};
$('#feed').onscroll=()=>{const f=$('#feed');stick=(f.scrollHeight-f.scrollTop-f.clientHeight)<70};
$('#tick-range').oninput=e=>{
  const t=+e.target.value,node=$('#feed').querySelector(`.tick[data-tick="${t}"]`);
  if(!node)return;
  toggleTick(t,true);stick=false;
  node.scrollIntoView({block:'start',behavior:'smooth'});
};
function stepTick(d){
  const r=$('#tick-range');if(r.disabled)return;
  r.value=Math.max(+r.min,Math.min(+r.max,+r.value+d));
  r.oninput({target:r});
}
$('#tk-prev').onclick=()=>stepTick(-1);
$('#tk-next').onclick=()=>stepTick(1);
$('#tk-open').onclick=()=>$('#feed').querySelectorAll('.tick').forEach(n=>toggleTick(+n.dataset.tick,true));
$('#tk-close').onclick=()=>$('#feed').querySelectorAll('.tick').forEach(n=>toggleTick(+n.dataset.tick,false));
// the panels are a drawer, so the feed keeps the full width on a phone
function sideOpen(on){
  const s=$('#side');
  const open=on==null?!s.classList.contains('open'):on;
  s.classList.toggle('open',open);$('#scrim').classList.toggle('on',open);
}
const curatorFlip=()=>{
  curatorOn=!curatorOn;revealTicks.clear();render();
  toast(curatorOn?'🧹 ซ่อนข้อความไร้สาระแล้ว — ในไฟล์ log ยังมีครบ'
                :'🧹 ปิดตัวกรอง — เห็นทุกบรรทัดรวมข้อความทดสอบ');
};
$('#btn-cur').onclick=curatorFlip;
$('#cur-toggle').onclick=curatorFlip;

// ── ▣ ก้อนเรื่องราว: the story/method layer ─────────────────────────────────
// Same source as the CLI (GET /blocks), so a human reading the CLI, a browser
// and an agent posting JSON all see byte-identical text — the format is
// canonical, this is only a renderer. A closed card is meta+title; the rest is
// one tap away, because six unfolded blocks would be a wall of text on a phone.
let blocksOn=false, blkRep=[], blkOpen=new Set(), blkHist=new Map();
function blkCard(r){
  const open=blkOpen.has(r.slug);
  const vals=['story','method','evidence','refs'].filter(k=>r[k])
    .map(k=>`<div class="blk-v"><i>${k}</i>${esc(r[k])}</div>`).join('');
  const hist=(blkHist.get(r.slug)||[]).map(h=>
    `<div class="blk-rev blk-m">แก้ครั้งที่ ${h.rev} · ${esc(h.app||'?')} · ${fmt(h.t)} · ${hash8(h.hash)}</div>`).join('');
  return `<div class="blk" data-open="${open?1:0}" data-slug="${esc(r.slug)}">
    <div class="blk-h"><b>▣ ${esc(r.slug)}</b><span class="blk-m">แก้ครั้งที่ ${r.rev}${r.revs>1?' (แก้มา '+r.revs+' ครั้ง)':''} · ${blkStatusTh(r.status)} · ${esc((r.apps||[]).join(','))} · ${fmt(r.t)} · ที่อยู่ ${r.rkey}/${r.enc}</span></div>
    <div class="blk-t">${esc(r.title)}</div>
    ${open?vals:''}
    <div class="blk-more">${open?'▾ ซ่อน':'▸ กาง'} รายละเอียด${open?` · <span data-hist="${esc(r.slug)}" style="color:var(--acc2)">ประวัติการแก้</span>`:''}</div>
    ${open?hist:''}
  </div>`;
}
function paintBlocks(){
  $('#blk-list').innerHTML=blkRep.map(blkCard).join('')||
    '<div class="empty" style="padding:10px 4px">ยังไม่มีความจำที่บันทึกไว้ — เขียนด้วย <code>python pool.py block &lt;slug&gt; --title "…"</code> หรือ POST /block</div>';
  $('#blk-count').textContent=blkRep.length+' ชิ้น';
}
async function loadBlocks(){
  const q=$('#blk-q').value.trim(), st=$('#blk-status').value;
  const url='/blocks'+(q?'?q='+encodeURIComponent(q):'')+(st?(q?'&':'?')+'status='+encodeURIComponent(st):'');
  try{ blkRep=await fetch(url,{headers:H}).then(r=>r.json()); }catch(e){ blkRep=[]; }
  blkHist.clear(); paintBlocks();
}
$('#blk-list').onclick=async ev=>{
  const hb=ev.target.closest('[data-hist]');
  if(hb){
    const slug=hb.getAttribute('data-hist');
    const row=await fetch('/blocks?slug='+encodeURIComponent(slug)+'&revs=1',{headers:H}).then(r=>r.json()).catch(()=>null);
    if(row&&row.history){ blkHist.set(slug,row.history); paintBlocks(); }
    return;
  }
  const card=ev.target.closest('.blk'); if(!card) return;
  const slug=card.getAttribute('data-slug');
  blkOpen.has(slug)?blkOpen.delete(slug):blkOpen.add(slug);
  paintBlocks();
};
$('#btn-blk').onclick=()=>{ blocksOn=!blocksOn; $('#blocks').hidden=!blocksOn; if(blocksOn) loadBlocks(); };
$('#blk-close').onclick=()=>{ blocksOn=false; $('#blocks').hidden=true; };
$('#blk-q').onkeydown=e=>{ if(e.key==='Enter'){ e.preventDefault(); loadBlocks(); } };
$('#blk-status').onchange=loadBlocks;
setInterval(()=>{ if(blocksOn&&!document.hidden) loadBlocks(); },20000);
$('#btn-side').onclick=()=>sideOpen();
$('#side-close').onclick=()=>sideOpen(false);
$('#scrim').onclick=()=>sideOpen(false);
window.setIdentity=async a=>{
  identity=a;
  // the stream carries the identity in its own `as=` filter, so switching the
  // view has to rebuild it. Without this the header said "hermes" while the
  // live stream kept sending admin's EVERY line — measured: stream.url stayed
  // /stream?n=200 with no `as=`. Re-reading once (refresh) only fixes the past;
  // listen() is what fixes the next line.
  listen();
  refresh().catch(()=>{});
  await registerIdentity();
  loadInbox().catch(()=>{});
};

async function loadInbox(mark){
  const regbox=$('#regbox'), inboxbox=$('#inboxbox');
  try{
    const reg=await fetch('/apps',{headers:H}).then(r=>r.json());
    regbox.style.display='block';
    $('#reg-list').innerHTML=reg.map(a=>{
      const seen=new Date(a.last_seen*1000);
      const live=(Date.now()-seen.getTime())<120000;
      // only the live one earns a word: two dozen rows of "gone" is noise, and
      // the section already says the list is who is connected — the grey dot
      // carries the rest
      return (live?`🟢 กำลังเชื่อมต่อ · `:'⚪ ')+esc(a.app)
        +` · เห็นล่าสุด ${seen.toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'})}`;
    }).join('\n')||'(ยังไม่มีแอปที่เชื่อมต่ออยู่)';
  }catch(e){
    // a failed registry read used to just hide the section, so "cannot reach
    // the server" and "nobody is registered" looked identical — the panel
    // silently disappeared instead of saying why it was empty.
    regbox.style.display='block';
    const rb=$('#reg-list');
    rb.textContent='⚠ โหลดรายชื่อแอปไม่ได้ — ต่อเซิร์ฟเวอร์ไม่ติด';
    rb.title='ข้อความจากเบราว์เซอร์: '+(e&&e.message||e);
  }
  if(identity==='admin'){inboxbox.style.display='none';return}
  inboxbox.style.display='block';
  const res=await fetch('/inbox?app='+identity+(mark===false?'&mark=0':''),{headers:H});
  const r=await res.json().catch(()=>({}));
  if(r.error){
    const box=$('#ib-list');
    box.textContent='⚠ '+sayError(r,res.status);
    box.title=sayRaw(r,res.status);
    $('#ib-count').textContent='?';return
  }
  myInbox=r;
  $('#ib-count').textContent=r.length;
  $('#ib-list').innerHTML=r.length?r.map(m=>`[${fmt(m.t)}] ${esc(m.app)}: ${esc(m.text).slice(0,90)}`).join('\n'):'(ว่าง — ไม่มีข้อความใหม่ถึงคุณ)';
}
// ── errors in words a person can act on ─────────────────────────────────────
// The wire keeps its English label: the CLI prints it, `stream_check` asserts
// on it, and an operator greps for it. So the translation lives HERE — at the
// only place a human ever reads it. An error we do not recognise falls back to
// what its status code means, and the original wording is never thrown away:
// it goes into the toast's tooltip, one hover away from whoever needs it.
const SAY_STATUS={
  400:'ส่งข้อมูลไปไม่ครบหรือรูปแบบไม่ถูกต้อง — ยังไม่มีอะไรถูกบันทึก',
  401:'ยังไม่ได้เข้าสู่ระบบ — ต้องสแกน QR ใหม่จากหน้าจอเซิร์ฟเวอร์',
  403:'แอปนี้ยังไม่ได้ลงทะเบียนไว้กับพูล — ต้องลงทะเบียนก่อนจึงจะเขียนได้',
  404:'ไม่พบสิ่งที่ขอ — อาจถูกลบไปแล้ว หรือที่อยู่ผิด',
  500:'เซิร์ฟเวอร์ทำงานผิดพลาด — ลองอีกครั้ง ถ้ายังไม่หายให้ดู log ที่เซิร์ฟเวอร์',
  503:'ส่วนนี้ยังไม่พร้อมใช้งาน'
};
// Ordered, and the order is load-bearing: a pattern that claims a CAUSE has to
// come before one that only recognises a shape. `?rkey=… and optional ?level=`
// matches both, and "you did not give me an address" is the useful sentence.
// The last line is the widest for the same reason it is the last.
//
// Every entry here was reached by a real request — `error_check.py` drives all
// 39 malformed probes at a live server and fails if any error falls back to its
// HTTP status instead of a sentence of its own.
const SAY_ERRORS=[
  // ── what the request was missing
  [/\bapp=?\s+required/, ()=>'ไม่ได้ระบุชื่อแอป'],
  [/^text required/, ()=>'ไม่มีข้อความให้บันทึก'],
  [/body must be a JSON object/, ()=>'ข้อมูลที่ส่งไปอ่านไม่ได้ — ข้อความนี้ยังไม่ถูกบันทึก'],
  [/\?n=/, ()=>'จำนวนบรรทัดที่ขอไม่ถูกต้อง — ต้องเป็นจำนวนเต็ม'],
  [/\?hide=/, ()=>'คะแนนเกณฑ์ไม่ถูกต้อง — ต้องเป็นจำนวนเต็ม'],
  [/\?rkey=/, ()=>'ไม่ได้ระบุที่อยู่บนแผนที่'],
  [/\?level=/, ()=>'ตัวเลือกระดับความละเอียดไม่ถูกต้อง'],
  [/\?path=/, ()=>'เส้นทางช่องย่อยไม่ถูกต้อง — ต้องเป็นตัวเลขคั่นด้วยจุลภาค'],
  [/\?hops=/, ()=>'จำนวนทอดไม่ถูกต้อง (ต้อง 1–4)'],
  [/\?hash=/, ()=>'ต้องระบุบรรทัดที่จะเปิด'],
  // ── what was named, and whether it exists or is allowed
  [/^no such block: (\S+)/, m=>`ไม่พบบันทึกความจำชื่อ “${m[1]}”`],
  [/^no such task: (\S+)/, m=>`ไม่พบ task ชื่อ “${m[1]}”`],
  [/is not a task \(/, ()=>'บล็อกนี้ไม่ใช่ task — title ต้องขึ้นต้นด้วย [สถานะ]'],
  [/^id required/, ()=>'ต้องระบุ id ของ task ที่จะอัปเดต'],
  [/^(\S+) not registered/, m=>`แอป “${m[1]}” ยังไม่ได้ลงทะเบียนไว้กับพูล — ลงทะเบียนก่อนจึงจะเขียนได้`],
  [/^invalid app name/, ()=>'ชื่อแอปใช้ไม่ได้ — ใช้ได้เฉพาะ a–z 0–9 _ - ยาว 1–32 ตัว'],
  [/^slug must be/, ()=>'ชื่อความจำ (slug) ใช้ไม่ได้ — ต้องขึ้นต้นด้วย a–z หรือ 0–9 แล้วตามด้วย a–z 0–9 . _ - ไม่เกิน 48 ตัว'],
  [/^status must be one of/, ()=>'สถานะไม่ถูกต้อง — ใช้ได้แค่ กำลังทำ / จบแล้ว / พักไว้'],
  [/^title required/, ()=>'ต้องมีชื่อเรื่อง — เป็นบรรทัดแรกที่คนอื่นจะอ่าน'],
  [/is not a hash prefix/, ()=>'รหัสอ้างอิงไม่ถูกต้อง — ต้องเป็น hex 6–64 ตัว'],
  [/^src\/dst must be/, ()=>'ต้องอ้างถึงบรรทัดด้วยรหัสเต็ม 64 ตัว'],
  [/^rel must be one of/, ()=>'ความหมายของเส้นไม่ถูกต้อง'],
  [/^bond_hash must be/, ()=>'ต้องระบุรหัสเส้นเต็ม 64 ตัว'],
  // ── what the server itself could not read
  [/no ctx index/, ()=>'ยังไม่มีข้อมูลความจำกลาง — ต้องรันตัวสแกนที่เซิร์ฟเวอร์ก่อน'],
  [/^ctx_plane unavailable/, ()=>'อ่านข้อมูลความจำกลางไม่ได้ — ลองรันตัวสแกนใหม่ที่เซิร์ฟเวอร์'],
  [/token required/, ()=>'ยังไม่ได้เข้าสู่ระบบ — สแกน QR ใหม่จากหน้าจอเซิร์ฟเวอร์'],
  [/^not found/, ()=>'ไม่พบสิ่งที่ขอ']
];
// r = the server's JSON body, st = its HTTP status (either may be missing)
function sayError(r,st){
  const raw=(r&&r.error)?String(r.error):'';
  for(const [re,f] of SAY_ERRORS){const m=raw.match(re);if(m)return f(m)}
  if(st&&SAY_STATUS[st])return SAY_STATUS[st];
  return raw?'เซิร์ฟเวอร์ปฏิเสธคำขอนี้':'ต่อเซิร์ฟเวอร์ไม่ติด';
}
function sayRaw(r,st){                  // the exact wording, for hovering over
  const raw=(r&&r.error)?String(r.error):'';
  return raw?('ข้อความจากเซิร์ฟเวอร์: '+raw+(st?(' · HTTP '+st):'')):'';
}
// a memory's status is a wire value (open/closed/parked); a person reads a word
const BLK_STATUS={open:'กำลังทำ',closed:'จบแล้ว',parked:'พักไว้'};
const blkStatusTh=s=>BLK_STATUS[s]||s||'?';
// the noise filter names its reasons in code (test-word, hex-blob, …). Those
// names are the AUDIT TRAIL — they stay on the wire and in the report — but the
// screen is read by a person, so the words change here and nowhere else.
const RULE_TH={'test-word':'คำทดสอบ','test-probe':'คำทดสอบ','stub':'สั้นเกินไป',
  'ack':'คำรับทราบ','sess-id':'รหัสรอบทำงาน','hex-blob':'ชุดรหัส','b64-blob':'ชุดรหัส',
  'punct-soup':'เครื่องหมายล้วน','loud':'ตัวอักษรซ้ำมาก','repeat':'ข้อความซ้ำ'};
const ruleTh=r=>RULE_TH[r]||r;
const PROTECT_TH={kind:'ไม่ใช่ข้อความคุย (เป็นข้อมูลของระบบ)',dm:'ส่งถึงคนเดียว',
  mention:'มีการเรียกชื่อ',long:'ข้อความยาว',marker:'มีคำสำคัญอย่าง สรุป/ตัดสิน'};
const protectTh=p=>PROTECT_TH[p]||p;
window.toast=(t,detail)=>{const el=$('#toast');el.textContent=t;el.title=detail||'';el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1600)};
/* ── 🚶 โหมดแนะนำ 3 ขั้น — โผล่ครั้งแรก ปิดได้ จำไว้ใน localStorage ───────
   แอปนี้มีห้าแผงกับศัพท์ชุดหนึ่ง คนแรกเข้าเจอทุกอย่างพร้อมกัน สิ่งที่ทำให้
   อันนี้เป็น "ทัวร์" ไม่ใช่กำแพงตัวหนังสืออีกอัน: มันบอกสิ่งที่ต้อง*ทำ*
   (ไม่ใช่นิยามของฟีเจอร์) · มันคืบหน้าเมื่อคนอ่านทำสิ่งนั้นจริง ไม่ใช่เมื่อ
   กดปุ่มถัดไป · และเมื่อปิดแล้วมันเงียบตลอด — คู่มือที่โผล่ซ้ำคือการรบกวน */
const TOUR_KEY='pool_tour_done';
const TOUR_STEPS=[
  {title:'1 · ค้นข้อความ',body:'พิมพ์ในช่อง <code>กรองข้อความ…</code> ด้านบน — เหลือเฉพาะที่ตรงกัน '
    +'ทันที (ค้นด้วยรหัสยาว 64 ตัวก็เจอ แม้บันทึกนั้นถูกพับไว้)',
    when:()=>tourFlags.searched},
  {title:'2 · ดูบนแผนที่',body:'กดปุ่ม <code>▦ แผนที่</code> ด้านบน — ทุกบรรทัดมีที่อยู่ '
    +'ของตัวเอง ช่องแน่น = บรรทัดเยอะ กดช่องเพื่อเปิดดูว่ามีอะไรอยู่ตรงนั้น',
    when:()=>tourFlags.map},
  {title:'3 · ลากดูเส้นเชื่อม',body:'บนแผนที่: ลาก = เลื่อนมุมมอง · เส้น = บันทึกที่อ้างถึงกัน '
    +'· บีบสองนิ้ว = ซูม — ลองเลือกช่องที่มีเส้นผ่าน แล้วลากดูว่าเส้นวิ่งไปทางไหน',
    when:()=>tourFlags.pan},
];
let tourFlags={searched:false,map:false,pan:false};
let tourStep=0;
function tourPaint(){
  const t=$('#tour');if(!t||t.hidden)return;
  $('#tour-title').textContent=TOUR_STEPS[tourStep].title;
  $('#tour-body').innerHTML=TOUR_STEPS[tourStep].body;
  $('#tour-next').textContent=tourStep>=TOUR_STEPS.length-1?'เริ่มใช้งาน':'ถัดไป ▸';
  t.querySelectorAll('.tstep').forEach(s=>{
    s.classList.toggle('on',+s.dataset.s===tourStep+1);
    s.classList.toggle('done',+s.dataset.s<=tourStep);
  });
  t.querySelectorAll('.tbar').forEach((b,i)=>b.classList.toggle('done',i<tourStep));
}
function tourOpen(){
  const t=$('#tour');if(!t)return;
  tourStep=0;tourFlags={searched:false,map:false,pan:false};
  t.hidden=false;tourPaint();
}
function tourClose(done){
  const t=$('#tour');if(!t)return;
  t.hidden=true;
  if(done)localStorage.setItem(TOUR_KEY,'1');
  toast('แนะนำสั้น ๆ อีกครั้งได้ที่ “☰ แผง → แนะนำการใช้งาน”');
}
function tourAdvance(){
  // advance on the ACT, with a small grace so a single action cannot skip a
  // step the reader has not read — and re-paint only on real movement
  if(tourStep<TOUR_STEPS.length-1&&TOUR_STEPS[tourStep].when()){
    tourStep++;tourPaint();
    if(tourStep===TOUR_STEPS.length-1){
      setTimeout(()=>{if(!$('#tour').hidden)tourClose(true)},20000);
    }
  }
}
$('#tour-next').onclick=e=>{e.preventDefault();
  if(tourStep>=TOUR_STEPS.length-1)tourClose(true);
  else{tourStep++;tourPaint()}
};
$('#tour-skip').onclick=e=>{e.preventDefault();tourClose(true)};
// the acts: one hook each, placed where the event already fires
$('#q').addEventListener('input',()=>{if(tourFlags&&tourStep===0&&$('#q').value)tourFlags.searched=true;tourAdvance()});
$('#open-plane').addEventListener('click',()=>{if(tourFlags){tourFlags.map=true;tourAdvance()}});
// the canvas is defined LATER in this script, so the pan hook waits for it —
// an init-order exception here would have killed every handler wired after
window.addEventListener('load',()=>{
  pcv.addEventListener('pointerdown',()=>{if(tourFlags&&PV.mode!=='plane')tourFlags.pan=true;tourAdvance()});
});
// ☰ panel: come back later — the reader asked for it once, it must exist
$('#tour-again').onclick=e=>{e.preventDefault();sideOpen(false);tourOpen()};
if(!localStorage.getItem(TOUR_KEY))setTimeout(tourOpen,900);   // let the feed breathe first

$('#q').oninput=e=>{filterQ=e.target.value.toLowerCase();render()};
$('#apps-sel').onchange=e=>{filterApps=new Set(e.target.value?[e.target.value]:[]);render()};
$('#id-sel').onchange=e=>setIdentity(e.target.value);
$('#cloudck').onchange=async e=>{
  $('#cloudwrap').classList.toggle('open',e.target.checked);
  if(e.checked){const r=await fetch('/view?n=6&cloud=1',{headers:H}).then(r=>r.json());
    $('#cloudout').textContent=r.lines?.join('\n')||'(ยังไม่มีอะไรถูกคัดลอก)';}
};
// ── the input path: one append in flight, and the draft is dropped only once
// the line is IN the chain. Clearing the box first — what this used to do —
// means any failed send destroys what someone typed (server restarting, phone
// off the LAN, a rejected token) with no toast and an unhandled rejection to
// prove it: measured, the box came back empty and silent. A message that could
// not be sent is recoverable; a message that was erased is not.
let sending=false;
async function send(){
  const el=$('#msg-in'),btn=$('#send');
  const text=el.value.trim();
  if(!text||sending)return;            // Enter held down is still ONE append
  sending=true;btn.disabled=true;      // and the button says so
  try{
    // the STATUS matters for the message: the body says which field was wrong,
    // the code says whether it is worth trying again, and a person needs both
    const res=await fetch('/append',{method:'POST',headers:{'Content-Type':'application/json',...H},
      body:JSON.stringify({app:'human',kind:'chat',text,to:$('#to-sel').value})});
    const r=await res.json().catch(()=>({}));
    if(r.ok){
      el.value='';el.style.height='46px';el.style.overflowY='hidden';stick=true;
      toast('ส่งแล้ว · '+hash8(r.hash));
    }else{
      el.focus();
      toast('ยังไม่ส่ง — '+sayError(r,res.status)+' · ข้อความยังอยู่ในช่อง', sayRaw(r,res.status));
    }
  }catch(e){
    // the server refused, or never answered — either way the draft stays put
    el.focus();
    toast('ส่งไม่ถึงเซิร์ฟเวอร์ — ข้อความยังอยู่ในช่อง (กด Enter อีกครั้ง)');
  }finally{
    sending=false;btn.disabled=false;
  }
}

async function registerIdentity(){
  if(identity==='admin')return;
  const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app:identity})}).then(r=>r.json());
  if(r.error)toast('ลงทะเบียนไม่ผ่าน — '+sayError(r), sayRaw(r));
  else toast(r.created?('ลงทะเบียน “'+identity+'” แล้ว'):('“'+identity+'” ลงทะเบียนไว้ก่อนแล้ว'));
}
$('#send').onclick=send;
$('#msg-in').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}};
// grow with the draft, up to a third of the screen, and only then hand the
// scrolling back to the textarea (a permanent scrollbar on a 46px box is just
// noise, and this is the control the whole UI exists to serve)
$('#msg-in').oninput=e=>{
  const el=e.target,cap=Math.round(window.innerHeight*0.32);
  el.style.height='auto';
  el.style.height=Math.min(Math.max(el.scrollHeight,46),cap)+'px';
  el.style.overflowY=(el.scrollHeight>cap)?'auto':'hidden';
};
$('#flushnow').onclick=async e=>{
  e.preventDefault();
  // same rule as the composer: an action that fails must SAY so. This is the
  // one button whose whole job is "did it reach the cloud", so silence here is
  // the least acceptable silence on the page.
  try{
    const res=await fetch('/flush',{method:'POST',headers:H});
    const r=await res.json().catch(()=>({}));
    if(r.error){toast('คัดลอกสำเนาไม่ผ่าน — '+sayError(r,res.status), sayRaw(r,res.status));return}
    toast(r.pushed?`คัดลอกสำเนาแล้ว ${r.pushed.length} บรรทัด`:(r.emitted?'คัดลอกสำเนาเรียบร้อย':'ไม่มีอะไรใหม่ให้คัดลอก'));
    refresh();
  }catch(err){toast('คัดลอกสำเนาไม่ผ่าน — เซิร์ฟเวอร์ไม่ตอบ', 'ข้อความจากเบราว์เซอร์: '+(err&&err.message||err))}
};

setInterval(()=>$('#clock').textContent=new Date().toLocaleTimeString('th-TH'),1000);
async function refresh(){
  const as=identity==='admin'?'':'&as='+identity;
  const [tail,h,cur]=await Promise.all([
    fetch('/tail?n=200'+as,{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()}),
    fetch('/view?n=500'+as,{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()}),
    fetch('/curator?n=200',{headers:H}).then(r=>r.json()).catch(()=>null)
  ]).catch(()=>{document.getElementById('qrpanel')?.style.setProperty('display','block');throw 'auth'});
  curatorRep=cur;
  document.getElementById('qrpanel')?.style.setProperty('display','none');
  msgs=tail;
  render();
  paintChain(h);
}
let stream=null,streamRetry=0,streamTimer=null;
function listen(){
  const as=identity==='admin'?'':'&as='+identity;
  if(streamTimer){clearTimeout(streamTimer);streamTimer=null}
  if(stream)stream.close();
  stream=new EventSource('/stream?n=200'+as+(TOKEN?'&token='+TOKEN:''));
  // the stream sends no backlog (it starts at the line that was newest when we
  // connected), so re-reading once on open is what closes a reconnect gap —
  // exactly, instead of guessing a bigger window
  stream.onopen=()=>{streamRetry=0;$('#dot').classList.add('on');refresh().catch(()=>{})};
  stream.onerror=async()=>{
    $('#dot').classList.remove('on');stream.close();
    // EventSource never says WHY it failed, so a rejected token looks exactly
    // like a dropped connection — and the old code retried every 3s forever,
    // so a phone with a stale QR link sat there reconnecting in silence with
    // no QR panel and no error. Ask an endpoint that CAN say 401 before
    // deciding: a refusal is permanent, a drop is not.
    let unauth=false;
    try{unauth=(await fetch('/health',{headers:H})).status===401}catch(e){}
    if(unauth){document.getElementById('qrpanel')?.style.setProperty('display','block');return}
    streamRetry=Math.min(streamRetry+1,4);          // 3s → 6 → 12 → 24 → 24 …
    streamTimer=setTimeout(listen,3000*Math.pow(2,streamRetry-1));
  };
  stream.onmessage=ev=>{
    let m;try{m=JSON.parse(ev.data)}catch(e){return}   // one torn frame ≠ dead feed
    if(m&&m.hash&&!msgs.find(x=>x.hash===m.hash)){msgs.push(m);render();syncChain()}
  };
}
function paintChain(h){
  const links=Math.max(h.hashes.length-1,0);
  const el=$('#chain');
  el.textContent=h.chain_ok?`✓ ประวัติครบ · ${links} ข้อต่อ`:'✗ ประวัติถูกแก้';
  el.title=h.chain_ok
    ? `ทุกบรรทัดผูกกับบรรทัดก่อนหน้าด้วยรหัส — ${links} ข้อต่อเรียงกันครบ ไม่มีใครแก้ย้อนหลัง`
    : 'รหัสไม่ต่อกัน — มีบรรทัดถูกแก้หรือถูกสลับที่หลังเขียนลงไป';
  el.className=h.chain_ok?'ok':'';$('#dot').classList.toggle('on',h.chain_ok);
}
// SSE carries new lines but not the chain count, so a line arriving after
// boot used to leave "chain ✓ N links" stale by however many pushed since
let chainT=null;
function syncChain(){
  clearTimeout(chainT);
  chainT=setTimeout(async()=>{
    try{
      const as=identity==='admin'?'':'&as='+identity;
      const [v,c]=await Promise.all([
        fetch('/view?n=500'+as,{headers:H}).then(r=>r.json()),
        fetch('/curator?n=200',{headers:H}).then(r=>r.json()).catch(()=>null)]);
      paintChain(v);
      if(c){curatorRep=c;paintCurator()}
    }catch{}
  },1200);
}
listen();

/* ── RDH plane 144×144 — vision you can see ───────────────────────────
   The plane is a MAP, so this canvas can only ever draw addresses and
   counts: no content reaches it without a pointer. Every click is one
   explicit read (cell → sub-cell → sub-sub-cell), and L1/L2 are just
   later reads at a coarser point — nothing here is cached state. */
// the pinch ladder and its threshold live with the view they change, not with
// the touch code: PV_WINS must equal the options in #pv-win, because a frame
// size the picker cannot show is a frame the reader cannot choose again
const PV_WINS=[12,24,36,48,72],PV_PINCH_OUT=1.45;
const PV={mode:'plane',w:144,h:144,sel:null,path:[],plane:{},cells:{},origin:null,
          budget:64,over:[],sights:[],lines:null,stacked:null,timer:null,
          bonds:[],timeline:[],seqMax:0,seq:null,showBonds:true,playing:null,focus:0,
          style:'auto',ctxOn:false,ctxCells:{},crossEdges:[],
          autoAt:120,hubK:12,relColour:true,   // measured: past ~120 edges a full draw is mostly ink
          colour:'density',cellApp:{},cellKind:{},cellSeq:{},cellSeqMax:0,metaOn:false,rib:null,
          // the FILTER: exactly ONE axis at a time, or none. `filt` says which
          // ({dim:'kind'|'app',key:...}); `kinds` and `apps` are the two whole
          // maps ({name:{rkey:n}}) that ride the meta layer and are only
          // fetched when a filter is actually opened.
          //
          // One axis, not two, and the reason is arithmetic rather than taste:
          // "bond AND chatdrop" is an intersection NEITHER map can answer.
          // by_kind[bond][rk] counts every bond in the cell and
          // by_app[chatdrop][rk] counts every chatdrop line in it; the lines
          // that are both live in a third map nobody has kept, and a second
          // <select> writing a second field would have the status line print a
          // number it cannot compute — the one answer a filter must never give
          // (§20.7). Choosing one axis therefore REPLACES the other, and both
          // controls are a view of this one field.
          kinds:{},apps:{},filt:null,
          // prune: the PLANE drops the keeper's noise instead of dimming it, so
          // a cell that lived on nothing but tests leaves the map. Off = the
          // honest raw plane, refetched with &raw=1, for judging the rules.
          prune:true,pruned:0,prunedCells:0,
          // the wrapping window: PV.win cells across, origin in plane cells
          win:48,panDrag:null,popWant:false,   // the popup's memory: only a deliberate read may open it
          // the block map's own seam, in BLOCK coordinates (0..11). The 12x12
          // map already holds the whole plane, so panning it moves the seam
          // rather than revealing new ground — but it is still a view position,
          // and it lives here so that pvXY (which draws) and pvRkeyAt (which
          // answers clicks) subtract the SAME number.
          bOrig:{x:0,y:0},
          // walk: the trail is the reader's OWN path, kept client-side and
          // never written to the pool — exploring is not a sighting (a
          // sighting says "someone read here"; a trail says "I got lost here"
          // and would drown the real ones)
          walk:null,census:null,showCensus:false,trail:[],cmp:0,
          // 🔍 the magnifier: a lens you CARRY over the plane, not another
          // plane scale you switch to. It is an overlay for two reasons — the
          // base view keeps its landmarks (so you can see WHICH patch is
          // magnified), and the glass reads the SAME PV.cells map, so what is
          // under it can never disagree with the map around it.
          lens:{on:false,wedge:0,ring:0,z:4,drag:null,moved:false}};
const pcv=$('#plane-cv');
// The two legend boxes live INSIDE #plane-detail, and pvDetail rewrites that
// container's innerHTML every time a cell is opened. So the first click of the
// session destroyed them, and their painters begin with `if(!box)return` — which
// is why nothing ever said so: the legend simply stopped existing. Holding the
// nodes here, once, is what lets them be put BACK; a re-query after the write
// would find nothing left to find.
const _pvLegendBox=$('#pv-legend'),_pvRelBox=$('#rel-legend'),_pvCompBox=$('#pv-comp');
// one writer for every path that replaces the panel, so the legends cannot be
// remembered on the main path and forgotten on the census one. The order is the
// original one — colour legend above the prose, relation legend below it — so
// nothing moves on screen the day this was fixed.
//
// The composition strip is deliberately NOT adopted: it is a SIBLING of
// #plane-side, not a child of the panel, so nothing here can destroy it. The
// first version adopted it anyway and put it inside the panel, where
// `insertBefore(strip, legendBox)` throws the moment innerHTML has already
// detached the legend — so the call this file exists to make was itself
// breaking the panel, and the symptom was a legend that had vanished again.
// `el.firstChild` is the only reference node that is always valid, because
// null means "append".
function pvAdoptLegends(el){
  if(_pvRelBox)el.appendChild(_pvRelBox);
  if(_pvLegendBox)el.insertBefore(_pvLegendBox,el.firstChild);
}
// Cell size adapts to the space the panel actually has: a 12x12 map drawn at a
// fixed 36px used a quarter of the canvas and looked broken on a laptop, while
// the same map on a phone needs the cells to be thumb-sized. Everything that
// places a stroke asks this one function, so the picture always agrees with
// itself.
function pstep(){
  const w=PV.w,h=PV.h,wrap=$('#plane-canvaswrap');
  const avail=(wrap&&wrap.clientWidth)
    ?Math.min(wrap.clientWidth-26,Math.max(wrap.clientHeight-26,160)):520;
  // the canvas should fill the space it has at whatever cell count the view
  // needs: 144 cells at 3px and 12 cells at 36px are the same picture at
  // different magnifications, so the size is DERIVED, never hardcoded per mode
  //
  // The divisor is the LONGER side. One PV.n used to serve as "the cell count"
  // for both axes at once, which held only because every mode happens to be
  // square. Naming the two sides makes that an explicit fact instead of a
  // coincidence: a step that fits w columns still has to fit h rows.
  const span=Math.max(w,h);
  //
  // How LARGE a cell may be drawn is a property of the MODE, not of how many
  // cells there happen to be. Reading it off the cell count meant any mode that
  // was not 144 wide silently inherited the plane's 2..8 pitch range — and a
  // future mode that WAS 144 wide would have been told it was the plane. The
  // plane needs cells of 2px precisely because it has 144 of them across; the
  // other two need room to be hit with a finger. PV.mode is the only thing
  // that can answer that, and it is already asked everywhere else in this file.
  const [lo,hi]=PV.mode==='plane'?[2,8]:[8,84];
  return Math.max(lo,Math.min(hi,Math.floor(avail/span)));
}

// the plane at tick S: every line whose append ordinal is <= S, counted
// straight from the log's own order — no wall clock is consulted anywhere
function pvCellsUpTo(S){
  const c={},f=PV.filt,ki=f&&f.dim==='kind'?f.key:null,ap=f&&f.dim==='app'?f.key:null;
  for(let i=0;i<PV.timeline.length&&i<=S;i++){
    const row=PV.timeline[i],rk=row[2];
    if(rk==null)continue;
    // the scrub counts the log's own order, so it has to honour the filter the
    // same way the live plane does — otherwise dragging time back paints a
    // plane the filter is not describing
    if(ki&&row[3]!==ki)continue;
    // the WIRE row is [ordinal,t,rkey,kind,hash8,writer] — six columns, not the
    // eight the fold keeps. row[5] is the writer, and the fold already
    // normalised a missing one to '?' so this is a comparison and not a second
    // copy of that rule. (row[7] is the fold's index; asking for it here reads
    // undefined, which silently filters to "nothing matches")
    if(ap&&row[5]!==ap)continue;
    c[rk]=(c[rk]||0)+1;
  }
  return c;
}
function pvLiveBonds(){
  if(PV.seq===null)return PV.bonds;
  return PV.bonds.filter(b=>b.seq==null||b.seq<=PV.seq);   // an edge exists only after its line
}
// Radius mask: bonds join LINES but the plane only sees CELLS, so a walk
// happens at cell level and a bond inside one cell is a self-loop.
// Returns null when the mask is off (= show everything).
let _geKey=null,_geArr=null;
function pvGraphEdges(){
  // Memoised: this MAPS every bond into a fresh object, and on this corpus that
  // is 13,572 allocations per call. The lens calls it once per wrapped copy of
  // the glass, so a single drag frame was allocating tens of thousands of
  // objects for an array that had not changed since the last frame — measured
  // as the remaining 12.8ms/frame after the base layer was cached.
  const key=[PV.seq,PV.showBonds,PV.ctxOn,PV.prune,
    _idOf(PV.bonds),_idOf(PV.crossEdges)].join('|');
  if(key===_geKey&&_geArr)return _geArr;
  _geKey=key;
  let e=pvLiveBonds().map(b=>({...b,kind:'bond'}));
  if(PV.ctxOn)e=e.concat((PV.crossEdges||[]).filter(x=>x.seq==null||PV.seq===null||x.seq<=PV.seq));
  _geArr=e;
  return e;                                     // one graph, two systems
}
let _keepKey=null,_keepSet=null;
function pvKeep(){
  // Memoised, keyed on the inputs it reads. This walks every edge to build an
  // adjacency map, and pvLensOne calls it once per wrapped copy of the glass —
  // up to four complete walks per frame, for an answer that cannot change
  // between them. The glass redraws on every drag frame, so this was not a
  // corner: it was the per-frame floor.
  if(!PV.focus||PV.sel==null)return null;
  const key=[PV.sel,PV.focus,PV.seq,PV.showBonds,PV.ctxOn,PV.prune,
    _idOf(PV.bonds),_idOf(PV.crossEdges),_idOf(PV.cells)].join('|');
  if(key===_keepKey)return _keepSet;
  _keepKey=key;
  const adj={};
  pvGraphEdges().forEach(b=>{
    if(b.src_rkey==null||b.dst_rkey==null)return;
    (adj[b.src_rkey]=adj[b.src_rkey]||[]).push(b.dst_rkey);
    (adj[b.dst_rkey]=adj[b.dst_rkey]||[]).push(b.src_rkey);
  });
  const seen=new Set([PV.sel]);let frontier=[PV.sel];
  for(let h=0;h<PV.focus&&frontier.length;h++){
    const next=[];
    frontier.forEach(r=>(adj[r]||[]).forEach(x=>{if(!seen.has(x)){seen.add(x);next.push(x)}}));
    frontier=next;
  }
  _keepSet=seen;
  return seen;
}
function pvDerive(){
  const full=PV.seq===null||PV.seq>=PV.seqMax-1;
  const src=full?PV.plane:pvCellsUpTo(PV.seq);
  PV.over=Object.keys(src).filter(k=>src[k]>PV.budget).map(Number);
  if(PV.mode==='plane'){PV.w=144;PV.h=144;PV.cells=src;return}
  if(PV.mode==='window'){
    // the frame size is the reader's choice; the wrapping is not
    PV.w=PV.win;PV.h=PV.win;
    const c={};
    for(const k in src){
      const[dx,dy]=pvXY(+k);
      if(dx>=0&&dy>=0)c[k]=src[k];
    }
    PV.cells=c;return;
  }
  if(PV.mode==='blocks'){
    PV.w=12;PV.h=12;const agg={};
    for(const k in src){
      // keyed by the block's FIRST ADDRESS, like every other mode — pvXY, the
      // edge strokes, the radius mask and pvCountAt all speak cell addresses,
      // and a second numbering understood only by this branch is how the block
      // squares ended up drawn on top of each other in row 0 while the lines
      // around them were placed correctly (nothing on screen says which square
      // is which, so it read as "this map is confusing", not as a bug)
      const rk=+k,bx=Math.floor((rk%144)/12),by=Math.floor(rk/144/12);
      const key=(by*12)*144+bx*12;
      agg[key]=(agg[key]||0)+src[k];
    }
    PV.cells=agg;return;
  }
  // the block view IS a window — the same wrapping frame, entered by zooming
  // into one block instead of by choosing an origin by hand. One code path, or
  // the picture and the hit-test drift apart at the seam.
  PV.mode='window';PV.win=12;PV.w=PV.win;PV.h=PV.win;
  const c={};
  for(const k in src){
    const[dx,dy]=pvXY(+k);
    if(dx>=0&&dy>=0)c[k]=src[k];
  }
  PV.cells=c;
}
function pvCenter(rk){
  const [x,y]=pvXY(rk);
  /* torus: edges may reference ghost-zone coords (-8..151), allow them */
  // mode alone decides: in plane mode pvDerive has already set w and h to
  // T144, so re-testing the count could only ever agree — or, the day a plane
  // mode is not 144 wide, disagree and draw the wrong bound
  const bx=PV.mode==='plane'?T144+8:PV.w,
        by=PV.mode==='plane'?T144+8:PV.h;
  if(x<-8||y<-8||x>=bx||y>=by)return null;
  const step=pstep();
  return[x*step+step/2,y*step+step/2];
}
/* Screen position of a plane-cell coordinate WITHOUT bounds — the caller
   (pvEdgeEnds) has already chosen the torus image, and an end outside the
   canvas is exactly how a seam-crossing edge must be drawn: canvas clips the
   line, and the wrapped copy enters from the far side. */
function pvCenterRaw(x,y){const step=pstep();return[x*step+step/2,y*step+step/2]}
/* nearest torus image of a raw delta: v wrapped into (-T144/2, T144/2] */
function torusNear(v){let d=((v%T144)+T144)%T144;if(d>T144/2)d-=T144;return d}
/* Both ends of an edge in SCREEN pixels, torus-correct.

   The old path dropped any edge whose endpoint fell outside the frame
   (pvCenter -> null), so a line whose far end slipped past the seam simply
   VANISHED — and never re-entered on the other side, which is what the torus
   promises. The fix keeps the edge's true shape instead:
   - the anchor end takes its image nearest the window centre;
   - the far end is placed at anchor + SHORTEST torus delta, so cells that are
     neighbours stay neighbours even when the naive wrap put them on opposite
     sides (a pair 4 cells apart was once drawn 140 cells apart!).
   A segment that cannot touch the canvas at all returns null — the one cheap
   reject, equivalent to the old drop but only for genuinely invisible edges. */
function pvEdgeEnds(src,dst){
  const step=pstep(),px=PV.w*step,py=PV.h*step;
  if(PV.mode==='plane'){
    const ax=src%144,ay=Math.floor(src/144);
    let bx=dst%144,by=Math.floor(dst/144);
    if(Math.abs(bx-ax)>T144/2)bx+=(bx>ax?-T144:T144);
    if(Math.abs(by-ay)>T144/2)by+=(by>ay?-T144:T144);
    const a=pvCenterRaw(ax,ay),z=pvCenterRaw(bx,by);
    if((Math.max(a[0],z[0])<0&&Math.min(a[0],z[0])+step<0)||
       (Math.min(a[0],z[0])>px&&Math.max(a[0],z[0])-step>px))return null;
    if((Math.max(a[1],z[1])<0&&Math.min(a[1],z[1])+step<0)||
       (Math.min(a[1],z[1])>py&&Math.max(a[1],z[1])-step>py))return null;
    return[a,z];
  }
  if(PV.mode==='blocks'){
    const o=PV.bOrig;
    const abx=Math.floor((src%144)/12),aby=Math.floor(src/144/12);
    const ax=pvB(abx-o.x),ay=pvB(aby-o.y);
    // block deltas wrap at TWELVE blocks, not 144 cells: the shortest path
    // between block columns 0 and 11 is one block across the seam
    let ddx=(((dst%144)/12|0)-abx)%12;if(ddx<0)ddx+=12;if(ddx>6)ddx-=12;
    let ddy=((Math.floor(dst/144/12)-aby)%12+12)%12;if(ddy>6)ddy-=12;
    // the block map shows the WHOLE 12-block ring, so when the nearest image
    // lands off-canvas and the opposite one (±12) is the visible rim, flip:
    // block 11 sits ON SCREEN at the right rim — draw to it, not off the left
    let bx=ax+ddx,by=ay+ddy;
    if(bx<0)bx+=12;else if(bx>=12)bx-=12;
    if(by<0)by+=12;else if(by>=12)by-=12;
    const a=pvCenterRaw(ax,ay),z=pvCenterRaw(bx,by);
    if(Math.max(a[0],z[0])<0||Math.min(a[0],z[0])>px||
       Math.max(a[1],z[1])<0||Math.min(a[1],z[1])>py)return null;
    return[a,z];
  }
  const w=PV.w,h=PV.h,o=PV.origin||{wedge:0,ring:0},half=Math.floor(Math.min(w,h)/2);
  const aw=src%144,ar=Math.floor(src/144);
  const ax=half+torusNear(aw-o.wedge-half),ay=half+torusNear(ar-o.ring-half);
  const bx=ax+torusDx(aw,dst%144),by=ay+torusDx(ar,Math.floor(dst/144));
  const a=pvCenterRaw(ax,ay),z=pvCenterRaw(bx,by);
  if(Math.max(a[0],z[0])<0||Math.min(a[0],z[0])>px||
     Math.max(a[1],z[1])<0||Math.min(a[1],z[1])>py)return null;
  return[a,z];
}
function pvDegrees(edges){
  const d={};
  edges.forEach(e=>{
    if(e.src_rkey==null||e.dst_rkey==null)return;
    d[e.src_rkey]=(d[e.src_rkey]||0)+1;
    if(e.dst_rkey!==e.src_rkey)d[e.dst_rkey]=(d[e.dst_rkey]||0)+1;
  });
  return d;
}
// Edges -> strokes. Style decides which strokes exist and how they read, and
// nothing else, so measuring a style measures the real renderer.
//   flat    : every edge the same weight
//   density : alpha falls with the busiest end's degree, so hubs do not blot out
//   bundle  : one stroke per cell PAIR (width by how many edges share it)
// A relation is a closed set, so each one gets a FIXED hue: the same edge looks
// the same on every render and in the side list, and a free `x-<rel>` is allowed
// but hashed into a hue of its own. Nothing here reads the content of a line —
// a rel is a field of the bond line, exactly like the addresses are.
const REL_COLORS={uses:'58a6ff',produces:'7ee787',validates:'d2a8ff',
                  decides:'ffa657',follows:'39c5cf',contradicts:'ff7b72',
                  lifted:'bc8cff'};
function relCSS(rel,alpha){
  const a=Math.max(0,Math.min(1,alpha)).toFixed(3);
  const hex=REL_COLORS[rel];
  if(hex)return 'rgba('+parseInt(hex.slice(0,2),16)+','+parseInt(hex.slice(2,4),16)
    +','+parseInt(hex.slice(4,6),16)+','+a+')';
  let h=7;for(const ch of String(rel||'?'))h=(h*31+ch.charCodeAt(0))%360;
  return 'hsla('+h+',72%,62%,'+a+')';
}
function pvRelOf(e){return e.rel||e.kind||'?'}
// the same hue in the plane and in the side lists — one relation, one colour
function pvRelInk(b){return PV.relColour?relCSS(pvRelOf(b),1)
  :(b.kind==='lifted'?'#bc8cff':'var(--acc2)')}
// which edges the current style actually draws — the legend must be able to say
// how much of each rel it left out, or a hub view looks like a complete graph
let _deKey=null,_deArr=null;
function pvDrawnEdges(){
  // Memoised on the same object identity pvGraphEdges now hands back: the filter
  // and the full degree ranking do not change between frames either, so doing
  // them per wrapped copy was pure repetition.
  const key=[PV.style,PV.autoAt,PV.hubK,_idOf(pvGraphEdges())].join('|');
  if(key===_deKey&&_deArr)return _deArr;
  _deKey=key;
  const edges=pvGraphEdges();
  if(PV.style==='degree'){_deArr=edges;return edges}
  const hubMode=PV.style==='hub'||(PV.style==='auto'&&edges.length>PV.autoAt);
  if(!hubMode){_deArr=edges;return edges}
  const deg=pvDegrees(edges);
  const top=Object.keys(deg).map(Number).sort((a,b)=>deg[b]-deg[a]).slice(0,PV.hubK);
  const set=new Set(top);
  const drawn=edges.filter(e=>set.has(e.src_rkey)||set.has(e.dst_rkey));
  _deArr=drawn;
  return drawn;
}
function pvEdgeStrokes(keep){
  const step=pstep(),edges=pvDrawnEdges();
  const deg=pvDegrees(edges);   // degree is needed by three of the styles
  // Above a few hundred edges the plane is mostly ink and no alpha fixes that
  // (measured: 600 edges cover 67% either way) — so those styles cut STROKES
  // instead: hub shows only the busiest cells' edges, degree draws no lines at
  // all and encodes each cell's edge count as a ring.
  // auto picks from the measurements: every line while that is cheap, the
  // hub view once a full draw would be mostly ink (constant cost instead)
  const hubMode=PV.style==='hub'||(PV.style==='auto'&&edges.length>PV.autoAt);
  if(PV.style==='degree'){
    const mx=Math.max(1,...Object.values(deg)),out=[];
    Object.keys(deg).forEach(rk=>{
      const ctr=pvCenter(+rk);if(!ctr)return;
      const a=0.15+0.85*Math.sqrt(deg[rk]/mx);
      out.push({x1:ctr[0],y1:ctr[1],loop:true,radius:step*0.95,width:1,
                alpha:a,hot:false,step,css:'rgba(121,192,255,'+a.toFixed(3)+')'});
    });
    return out;
  }
  if(hubMode){
    return pvEdgeStrokesOf(edges,keep,'density');
  }
  if(PV.style==='auto')return pvEdgeStrokesOf(edges,keep,'density');
  return pvEdgeStrokesOf(edges,keep,PV.style);
}
function pvEdgeStrokesOf(edges,keep,style){
  const step=pstep();
  const deg=style==='density'?pvDegrees(edges):{};
  const hot=e=>PV.sel!=null&&(e.src_rkey===PV.sel||e.dst_rkey===PV.sel);
  const out=[],groups={};
  const push=(e,a,z,alpha,width)=>{
    const h=hot(e);
    // selection is a shape cue (white, thicker, drawn on top), never a hue:
    // with rel colours on, a hue would collide with a real relation
    out.push({x1:a[0],y1:a[1],x2:z[0],y2:z[1],rel:pvRelOf(e),
              width:h?Math.max(width,2):width,alpha:h?1:alpha,hot:h,step,
              loop:a[0]===z[0]&&a[1]===z[1],
              dash:e.kind==='lifted'?[3,2]:null,
              css:h?'#ffffff':(PV.relColour?relCSS(pvRelOf(e),alpha)
                    :'rgba('+(e.kind==='lifted'?'188,140,255':'121,192,255')+','+alpha.toFixed(3)+')')});
  };
  edges.forEach(e=>{
    const ends=pvEdgeEnds(e.src_rkey,e.dst_rkey);
    if(!ends)return;                           // cannot touch this view at all
    const[a,z]=ends;
    let alpha=0.5;
    if(keep){
      const s=keep.has(e.src_rkey),d=keep.has(e.dst_rkey);
      if(!s&&!d)return;
      if(!s||!d)alpha=0.16;                    // half-way out: hint that it continues
    }
    if(style==='bundle'&&e.src_rkey!==e.dst_rkey&&!hot(e)){
      const k=e.src_rkey<e.dst_rkey?e.src_rkey+':'+e.dst_rkey:e.dst_rkey+':'+e.src_rkey;
      const g=groups[k]||(groups[k]={e,a,z,n:0,alpha});
      g.n++;g.alpha=Math.min(g.alpha,alpha);
      return;
    }
    if(style==='density'&&!hot(e))
      alpha=Math.min(alpha,Math.max(0.08,0.5/Math.sqrt(Math.max(deg[e.src_rkey]||1,deg[e.dst_rkey]||1))));
    push(e,a,z,alpha,1);
    /* seam re-entry: on the FULL plane the rim is a seam, so a line that runs
       off one side must RE-ENTER on the other — that is the torus promise the
       reader is asking about. In plane mode an overhang happens exactly for
       seam-crossing pairs (every other edge sits inside the canvas), so the
       overhang test is the cheap filter for which edges earn a second image.
       Window/block views are a magnified PATCH, not the whole torus: their
       far side is NOT adjacent (96 more cells sit in between), so a re-entry
       there would lie about the geometry — those stay clipped. */
    if(PV.mode==='plane'){
      const px2=PV.w*step,py2=PV.h*step;
      const ox=Math.max(a[0],z[0])>px2?-T144*step:(Math.min(a[0],z[0])<0?T144*step:0);
      const oy=Math.max(a[1],z[1])>py2?-T144*step:(Math.min(a[1],z[1])<0?T144*step:0);
      if(ox||oy)push(e,[a[0]+ox,a[1]+oy],[z[0]+ox,z[1]+oy],alpha,1);
    }
  });
  Object.values(groups).forEach(g=>push(g.e,g.a,g.z,Math.min(0.42,g.alpha),1+Math.min(3,Math.log2(g.n))));
  out.sort((p,q)=>(p.hot?1:0)-(q.hot?1:0));     // the selected cell's edges read last = on top
  return out;
}
function pvStroke(c,s,alphaOverride){
  c.save();
  if(s.dash){c.setLineDash(s.dash);c.lineDashOffset=Math.round(s.step*2)}
  c.strokeStyle=alphaOverride!=null?('rgba(0,0,0,'+alphaOverride+')'):s.css;
  c.lineWidth=s.width;
  c.beginPath();
  if(s.loop)c.arc(s.x1,s.y1,s.radius||(s.step*1.6+2),0,7);   // bond inside ONE cell
  else{c.moveTo(s.x1,s.y1);c.lineTo(s.x2,s.y2)}
  c.stroke();
  if(alphaOverride==null&&(s.step>6||s.width>1)){
    c.fillStyle=s.css;c.beginPath();c.arc(s.x1,s.y1,1.8,0,7);c.fill();
  }
  c.restore();
}
function pvMemoryPass(c,step,keep){
  for(const k in PV.ctxCells){
    const [x,y]=pvXY(+k);if(x<0||y<0||x>=PV.w||y>=PV.h)continue;
    const near=!keep||keep.has(+k),m=Math.max(2,Math.round(step*0.45));
    c.fillStyle='rgba(121,192,255,'+(near?0.9:0.10)+')';
    c.fillRect(x*step+(step-m)/2,y*step+(step-m)/2,m,m);
  }
}
// Measure the three styles on the real renderer, on a synthetic plane with
// hubs (what a busy graph actually looks like). Returns strokes + ink = how
// much of the plane the edges cover, overlap = how many strokes land on the
// same pixel, hot = share of edge pixels that 3+ edges are fighting over.
function pvBench(N,hubCount,hubDeg){
  N=N||200;hubCount=hubCount||8;hubDeg=hubDeg||12;
  const W=432,rnd=()=>Math.floor(Math.random()*20736),edges=[];
  for(let i=0;i<hubCount;i++){const h=rnd();for(let j=0;j<hubDeg;j++)edges.push({src_rkey:h,dst_rkey:rnd(),kind:'bond'})}
  while(edges.length<N)edges.push({src_rkey:rnd(),dst_rkey:rnd(),kind:'bond'});
  const saved={style:PV.style,sel:PV.sel,focus:PV.focus,bonds:PV.bonds,ctxOn:PV.ctxOn,mode:PV.mode,timeline:PV.timeline,seq:PV.seq};
  PV.bonds=edges;PV.sel=null;PV.focus=0;PV.ctxOn=false;PV.timeline=[];PV.seq=null;PV.mode='plane';
  const res={};
  ['flat','density','bundle','hub','degree'].forEach(mode=>{
    PV.style=mode;
    const strokes=pvEdgeStrokes(null);
    const vis=document.createElement('canvas'),acc=document.createElement('canvas');
    vis.width=vis.height=acc.width=acc.height=W;
    const vc=vis.getContext('2d'),ac=acc.getContext('2d');
    vc.fillStyle='#000000';vc.fillRect(0,0,W,W);
    strokes.forEach(s=>{pvStroke(vc,s);pvStroke(ac,s,0.05)});
    const vd=vc.getImageData(0,0,W,W).data,ad=ac.getImageData(0,0,W,W).data;
    let ink=0,hotPx=0,sum=0;
    for(let i=0;i<vd.length;i+=4){
      if(vd[i]+vd[i+1]+vd[i+2]>24){ink++;const n=Math.round(ad[i+3]/13);sum+=n;if(n>=3)hotPx++}
    }
    res[mode]={strokes:strokes.length,inkPct:+(ink/(W*W)*100).toFixed(2),
               meanOverlap:ink?+(sum/ink).toFixed(2):0,
               hotPct:ink?+(hotPx/ink*100).toFixed(1):0};
  });
  Object.assign(PV,saved);pvMode();
  return {edges:N,hubs:hubCount+'x'+hubDeg,planeCells:20736,result:res};
}
window.pvBench=pvBench;
// Does colouring edges by rel stay as readable as hub mode? Flat ink/hot cannot
// answer that — colour does not remove strokes, so the question is whether you
// can still TELL WHICH rel a pixel belongs to. So this measures two more
// numbers per mode: mixed = share of edge pixels where two different rels are
// drawn on top of each other (unreadable either way), pure = share carrying
// exactly one rel (attributable). "rel" = colour only, "rel+hub" = colour with
// the stroke cut auto already switches to.
function pvBenchRel(N,hubK){
  N=N||200;hubK=hubK||12;
  const W=432,rnd=()=>Math.floor(Math.random()*20736),edges=[];
  const mix=['uses','uses','uses','produces','produces','validates','decides','follows','contradicts'];
  const hubs=[];for(let i=0;i<8;i++)hubs.push(rnd());
  for(let i=0;i<N;i++)edges.push({src_rkey:(i%3===0)?hubs[i%hubs.length]:rnd(),
                                  dst_rkey:rnd(),kind:'bond',rel:mix[i%mix.length]});
  const saved={};Object.keys(PV).forEach(k=>saved[k]=PV[k]);
  PV.bonds=edges;PV.sel=null;PV.focus=0;PV.ctxOn=false;PV.timeline=[];PV.seq=null;
  PV.mode='plane';PV.showBonds=true;PV.hubK=hubK;
  const res={};
  // colour on/off on the SAME graph: hue never changes which pixels are inked,
  // so the only honest comparison is what colour does to attribution
  [['rel','flat',true],['plain','flat',false],
   ['rel-hub','hub',true],['hub-plain','hub',false]].forEach(([name,style,colr])=>{
    PV.style=style;PV.relColour=colr;
    const strokes=pvEdgeStrokes(null);
    const mask=(list)=>{const cv=document.createElement('canvas');cv.width=cv.height=W;
      const cx=cv.getContext('2d');list.forEach(s=>pvStroke(cx,s,0.05));
      return cx.getImageData(0,0,W,W).data};
    const vis=document.createElement('canvas'),acc=document.createElement('canvas');
    vis.width=vis.height=acc.width=acc.height=W;
    const vc=vis.getContext('2d'),ac=acc.getContext('2d');
    vc.fillStyle='#000000';vc.fillRect(0,0,W,W);
    strokes.forEach(s=>{pvStroke(vc,s);pvStroke(ac,s,0.05)});
    const vd=vc.getImageData(0,0,W,W).data,ad=ac.getImageData(0,0,W,W).data;
    // group by what the eye can distinguish: with colour off every stroke is one
    // visible group, so `mixed` is 0 by construction and the difference below is
    // the real cost of colouring by rel
    const byRel={};strokes.forEach(s=>{const k=colr?(s.rel||'?'):'all';(byRel[k]=byRel[k]||[]).push(s)});
    const masks=Object.values(byRel).map(mask);
    const owners=new Int16Array(W*W);
    masks.forEach(m=>{for(let p=0,i=3;i<m.length;i+=4,p++)if(m[i]>0)owners[p]++});
    let ink=0,hotPx=0,sum=0,mixed=0;
    for(let i=0;i<vd.length;i+=4){
      if(vd[i]+vd[i+1]+vd[i+2]<=24)continue;
      ink++;const n=Math.round(ad[i+3]/13);sum+=n;if(n>=3)hotPx++;
      if(owners[i/4]>1)mixed++;
    }
    res[name]={strokes:strokes.length,rels:Object.keys(byRel).length,
      inkPct:+(ink/(W*W)*100).toFixed(2),
      hotPct:ink?+(hotPx/ink*100).toFixed(1):0,
      mixedPct:ink?+(mixed/ink*100).toFixed(1):0,
      purePct:ink?+(100-mixed/ink*100).toFixed(1):0};
  });
  // what the colour cost in ambiguity, on identical strokes
  res['cost@'+N]=((res.rel.mixedPct-res.plain.mixedPct).toFixed(1)
    +'pt at '+res.rel.strokes+' edges, '
    +(res['rel-hub'].mixedPct-res['hub-plain'].mixedPct).toFixed(1)
    +'pt at '+res['rel-hub'].strokes+' edges');
  Object.assign(PV,saved);pvMode();
  return {edges:N,rels:9,result:res};
}
window.pvBenchRel=pvBenchRel;
/* ── the window: a wrapping viewport on the torus ─────────────────────
   The plane is a torus (rkey = ring*144 + wedge, both mod 144), so a window
   over it must WRAP: scroll right and what left the frame comes back on the
   left. Without the modulo a window near the seam renders empty space where
   the other side belongs — the field looked finite when it is not.
   Both directions go through one `%144`, so there is no seam logic, no ghost
   copy and nothing to keep in sync: the address IS toroidal, only the
   projection was flat. Cheap by construction — the data never moves, a cell
   is just asked for at its wrapped name. */
function pvWrap(v){return((v%144)+144)%144}
/* ── panning the window ───────────────────────────────────────────────
   Moving the frame is the whole point of a viewport: the reader slides over
   the field and the field slides under them. Because the address is
   toroidal, panning wraps for free — scroll right past 143 and you arrive at
   0, which is the SAME edge, so the picture never jumps.
   Panning is a READ, not stored state: the frame is one caller's position at
   one moment, exactly like a pointer. */
function pvPan(dx,dy){
  if(PV.mode!=='window'||!PV.origin)return;
  PV.origin={wedge:pvWrap(PV.origin.wedge+dx),ring:pvWrap(PV.origin.ring+dy)};
  pvMode();
}
function pvWinInfo(){
  const el=$('#pv-wininfo');if(!el)return;
  if(PV.mode==='blocks'){
    // Say what a drag does HERE before it is tried: the 12x12 map already holds
    // the whole plane, so sliding it moves the seam and not the contents — and
    // a reader who drags, sees the picture shift and is told nothing deserves
    // better than to wonder which part they just visited.
    el.textContent='ผังย่อ 12×12 · ทั้งระนาบอยู่ในจอแล้ว — ลาก = เลื่อนขอบผัง (วนกลับที่เดิมทุก 12 บล็อก)';
    return;
  }
  if(PV.mode!=='window'||!PV.origin){el.textContent='';return}
  const w=PV.win,o=PV.origin;
  const x0=o.wedge,y0=o.ring,x1=pvWrap(o.wedge+w-1),y1=pvWrap(o.ring+w-1);
  // name the seams the frame crosses, or a wrapped edge reads as a glitch
  const sx=o.wedge+w>144?' · ข้ามขอบ x':'';
  const sy=o.ring+w>144?' · ข้ามขอบ y':'';
  el.textContent='กรอบเลื่อน '+w+'×'+w+' · ช่อง ('+x0+','+y0+')→('+x1+','+y1+')'+sx+sy
    +' · ลากเพื่อเลื่อน (ไม่รู้จบ — วนรอบ torus)';
}
/* Block coordinates wrap at 12, never at 144: a block is 12x12 cells, so the
   12x12 grid of blocks is itself a torus and a pan has to come round after
   twelve blocks rather than a hundred and forty-four. */
function pvB(v){return((v%12)+12)%12}
function pvXY(rk){
  if(PV.mode==='plane')return[rk%144,Math.floor(rk/144)];
  if(PV.mode==='blocks'){const o=PV.bOrig;
    return[pvB(Math.floor((rk%144)/12)-o.x),pvB(Math.floor(rk/144/12)-o.y)]}
  const w=PV.w,h=PV.h,o=PV.origin||{wedge:0,ring:0};
  const dx=pvWrap(rk%144-o.wedge),dy=pvWrap(Math.floor(rk/144)-o.ring);
  if(dx>=w||dy>=h)return[-1,-1];          // this address is outside the frame
  return[dx,dy];
}
function pvRkeyAt(x,y){
  if(PV.mode==='plane')return pvWrap(y)*144+pvWrap(x);
  if(PV.mode==='blocks'){const o=PV.bOrig;   // the square's block, in first addresses
    return(pvB(o.y+y)*12)*144+pvB(o.x+x)*12}
  const o=PV.origin||{wedge:0,ring:0};
  return pvWrap(o.ring+y)*144+pvWrap(o.wedge+x);
}
/* ── torus helpers ──────────────────────────────────────────────────── */
const T144=144;
function torusDist(a,b){const d=Math.abs(a-b);return Math.min(d,T144-d)}
function torusDx(a,b){const d=b-a;return Math.abs(d)<=T144/2?d:(d>0?d-T144:d+T144)}
/* ghost offset: if a cell at (cx,cy) is near the edge, return [[dx,dy],...]
   of wrapped copies that should also be drawn.  Only fires within 8 cells
   of any border — beyond that the copy is off-screen and wasted. */
function ghostOffsets(cx,cy){
  const G=8,o=[];
  if(cx<G)o.push([T144,0]);if(cx>=T144-G)o.push([-T144,0]);
  if(cy<G)o.push([0,T144]);if(cy>=T144-G)o.push([0,-T144]);
  // corners: both axes near border
  if(cx<G&&cy<G)o.push([T144,T144]);
  if(cx<G&&cy>=T144-G)o.push([T144,-T144]);
  if(cx>=T144-G&&cy<G)o.push([-T144,T144]);
  if(cx>=T144-G&&cy>=T144-G)o.push([-T144,-T144]);
  return o;
}
/* The value AT AN ADDRESS — the question the glass and the tooltip both ask.
   In blocks mode the entries are the blocks' first addresses, so a cell has to
   borrow the count of the block around it. Without that the glass drew a block
   map as empty and its readout said "0 line" over a cell holding 2,228. */
function pvValAt(rk){
  if(PV.mode!=='blocks')return PV.cells[rk]||0;
  const bx=Math.floor((rk%144)/12),by=Math.floor(rk/144/12);
  return PV.cells[(by*12)*144+bx*12]||0;
}
function pvCountAt(x,y,rk){return pvValAt(rk)}
/* Cell colour is a LAYER, not decoration: the same plane answers four
   different questions and one function decides the hue, so the legend can
   never disagree with the canvas. Slower traffic is the point here — a log is
   heavy-tailed, so one hub would swallow the whole ramp if it were linear. */
const HEAT=[[11,37,69],[29,92,143],[57,197,207],[126,231,135],[255,212,121],[255,123,114]];
function pvRamp(t){
  t=Math.max(0,Math.min(1,t));
  const x=t*(HEAT.length-1),i=Math.min(HEAT.length-2,Math.floor(x)),f=x-i;
  const a=HEAT[i],b=HEAT[i+1];
  return[Math.round(a[0]+(b[0]-a[0])*f),Math.round(a[1]+(b[1]-a[1])*f),Math.round(a[2]+(b[2]-a[2])*f)];
}
const pvHeatCSS=(t,a)=>'rgba('+pvRamp(t).join(',')+','+a.toFixed(2)+')';
const pvRampCSS=n=>HEAT.slice(0,n===undefined?HEAT.length:n).map(c=>'rgb('+c.join(',')+')');
// one app, one hue — hashed and stable, so the same writer is the same colour
// on the plane, on the ribbon and in the legend, on every render
function pvAppHue(app){let h=7;for(const ch of String(app||'?'))h=(h*31+ch.charCodeAt(0))%360;return h}
const pvAppInk=(app,rel)=>'hsla('+pvAppHue(app)+',72%,'+Math.round(38+26*Math.max(0,Math.min(1,rel)))
  +'%,'+Math.min(1,0.34+0.62*rel).toFixed(2)+')';
const pvTopApps=()=>{
  const n={};Object.keys(PV.cellApp).forEach(k=>{const a=PV.cellApp[k]||'?';(n[a]=n[a]||0)});
  return Object.keys(n).length?n:null;
};
/* KIND_TH, KIND_HUE and kindLabel live at the top of the script, next to
   MACHINE_KINDS, because the feed badge needs them too. Only the plane's ink
   is here: the same hue formula pvAppInk uses, so a cell drawn by kind and a
   bar drawn by writer share one ramp and one alpha curve. */
function pvKindHue(k){const h=KIND_HUE[k];return h===undefined?pvAppHue(k):h}
const pvKindInk=(k,rel)=>'hsla('+pvKindHue(k)+',72%,'+Math.round(38+26*Math.max(0,Math.min(1,rel)))
  +'%,'+Math.min(1,0.34+0.62*rel).toFixed(2)+')';
const pvTopKinds=()=>{
  const n={};Object.keys(PV.cellKind).forEach(k=>{const a=PV.cellKind[k]||'?';(n[a]=(n[a]||0)+1)});  // the +1 is the whole function: `n[a]=n[a]||0` reads a count and writes it back, so every kind reported 0 cells and both the filter's list and the legend said "(0)". A label nobody checks is a label that lies quietly.
  return Object.keys(n).length?n:null;
};

/* ── the kind filter ──────────────────────────────────────────────────
   ONE function answers "what is the map drawing", because the base pass, the
   torus ghost pass, the glass and the legend each used to ask their own
   question and were free to answer it differently (§13: one value, one view).

   Two numbers, on purpose, and never confused:

   * the PICTURE is drawn from the filtered counts — otherwise the filter
     tints the map without moving a single shade, and the busiest cell overall
     still owns the whole ramp, so "filter to bonds" looks like nothing happened;
   * the NUMBER a reader is told stays the TOTAL, and both are printed. A cell
     that reports 0 while it holds 34 lines is the §18 failure wearing a filter,
     and nothing on screen would be saying otherwise.

   `null` for a kind with no cells in this view is NOT treated as "no filter":
   the filter stays on and the status line says the kind is empty here, because
   silently drawing the unfiltered map under a filter that claims to be on is the
   one answer this must never give. */
function pvFilt(){return PV.filt?((PV.filt.dim==='app'?PV.apps:PV.kinds)[PV.filt.key]||null):null}
function pvFiltN(rk){const f=pvFilt();return f?(f[rk]||0):0}
// What the reader is being shown, in words. Both controls print these, so the
// status line, the lens, the tooltip and the legend cannot describe two
// different pictures from one click.
function pvFiltLabel(){return PV.filt?(PV.filt.dim==='app'?appLabel(PV.filt.key):kindLabel(PV.filt.key)):null}
function pvFiltWord(){return PV.filt?(PV.filt.dim==='app'?'ผู้เขียนนี้':'ชนิดนี้'):''}
function pvFiltAxisName(){return PV.filt&&PV.filt.dim==='app'?'ผู้เขียน':'ชนิด'}
// app names are already the reader's own words ("chatdrop", "opencode"); the one
// that is not is a whole sentence, and a 40-character dropdown entry is a
// control nobody can scan. The VALUE is never shortened.
function appLabel(a){a=String(a);return a.length>26?a.slice(0,25)+'…':a}
function pvInkAt(rk,rel){
  const f=pvFilt();
  // filtered out: a neutral grey, deliberately NOT a kind hue. A hashed hue for
  // a cell that holds none of the asked-for kind would look like a seventh
  // kind in the legend, which is a claim the data does not make.
  if(f&&!f[rk])return 'rgba(139,148,158,0.13)';
  return pvCellInk(rk,rel);
}
function pvDrawMax(){
  const f=pvFilt(),vals=f?Object.values(f):Object.values(PV.cells);
  return vals.length?Math.max(...vals):1;
}
function pvKindOptions(){
  const sel=$('#pv-kind');if(!sel)return;
  const ks=pvTopKinds();
  // Build the html the reader SHOULD see, then compare. One comparison, and it
  // is correct whether the list is empty, half-built, or already right.
  //
  // Both earlier versions compared the wrong thing and neither was caught. The
  // first asked `ks.join('|')` of a COUNT OBJECT — undefined, so the guard
  // itself threw a TypeError on every path that had kinds to show, and the list
  // was never built at all. The second asked `have[0]!==''`, which is TRUE when
  // the list is empty (have[0] is undefined), so it returned early on the one
  // case it existed to handle. The maths probe stayed green through both: four
  // correct functions behind an unreachable <select> look exactly like a pass to
  // a check that only computes.
  // The count beside each kind is what choosing it will LIGHT UP, not the colour
  // layer's "cells whose last line was this" (pvTopKinds). They are different
  // numbers and they disagreed on screen: the list said bonds (1961), the status
  // line then said สว่าง 2,651 ช่อง. A filter's number has to predict the
  // filter, or the reader is asked to trust two numbers about one click.
  const nLit=k=>PV.kinds&&PV.kinds[k]?Object.keys(PV.kinds[k]).length:0;
  const want=ks
    ?'<option value="">ทุกชนิด</option>'
      +Object.keys(ks).sort((a,b)=>nLit(b)-nLit(a)||ks[b]-ks[a])
        .map(k=>'<option value="'+esc(k)+'">'+esc(kindLabel(k))+' ('+nLit(k)+')</option>').join('')
    // No layer yet: the control must ASK for what it needs. It used to be
    // disabled with a single option, so it could never fire the change handler
    // that loads the very layer it needs — the only way in was to first pick a
    // meta-backed COLOUR, a control the reader has no reason to touch.
    :'<option value="">ทุกชนิด</option><option value="__load">โหลดรายชื่อชนิด… (ครั้งแรก)</option>';
  // No churn per frame: this runs on every mode change, and rebuilding the
  // options resets an open dropdown under the reader's cursor. The VALUE is
  // still synced every call — an early `return` here (the first version) left
  // the control showing yesterday's filter, which the probe caught.
  if(sel._kindHtml!==want){sel.innerHTML=want;sel._kindHtml=want;}
  // TWO controls, ONE state: each shows the filter when it is ITS axis and "no
  // filter" when it is the other one. Two independent <select>s syncing
  // themselves is how a reader ends up looking at "ทุกชนิด" and "ทุกผู้เขียน"
  // while a live filter is dimming half the map.
  //
  // The key is checked against the MAP as well as the axis. A real <select>
  // silently reverts an unknown value to "", so the control would have said
  // "no filter" while PV.filt was still filtering — the browser papering over
  // the disagreement instead of proving there is none. The list probe caught
  // this the day the second axis was added.
  sel.value=(PV.filt&&PV.filt.dim==='kind'&&PV.kinds[PV.filt.key])?PV.filt.key:'';
  sel.disabled=false;
}
// The writer control is the SAME control with a different axis, so it is built
// by the same shape and pinned by the same rule: the number beside each app is
// what choosing it will light up.
function pvAppOptions(){
  const sel=$('#pv-app');if(!sel)return;
  const nLit=a=>PV.apps&&PV.apps[a]?Object.keys(PV.apps[a]).length:0;
  const as=Object.keys(PV.apps||{});
  const want=as.length
    ?'<option value="">ทุกผู้เขียน</option>'
      +as.sort((a,b)=>nLit(b)-nLit(a)||a.localeCompare(b))
        .map(a=>'<option value="'+esc(a)+'">'+esc(appLabel(a))+' ('+nLit(a)+')</option>').join('')
    :'<option value="">ทุกผู้เขียน</option><option value="__load">โหลดรายชื่อผู้เขียน… (ครั้งแรก)</option>';
  if(sel._appHtml!==want){sel.innerHTML=want;sel._appHtml=want;}
  sel.value=(PV.filt&&PV.filt.dim==='app'&&PV.apps[PV.filt.key])?PV.filt.key:'';
  sel.disabled=false;
}
// One picker for both axes, because choosing an axis REPLACES the filter and
// two copies of the __load dance would be two places to get it wrong.
async function pvPickFilt(dim,val){
  if(val==='__load'){
    await loadMeta();pvFilterOptions();pvMode();
    toast(PV.metaOn?'โหลดรายชื่อ'+pvFiltAxisNameFor(dim)+'แล้ว'
                 :'โหลดข้อมูล'+pvFiltAxisNameFor(dim)+'ไม่สำเร็จ — ลองใหม่อีกครั้ง');
    return;}
  const was=PV.filt;
  PV.filt=val?{dim:dim,key:val}:null;
  if(PV.filt)await loadMeta();
  pvMode();
  if(was&&was.dim!==dim)toast('กรองได้ทีละแกน — เปลี่ยนจาก'+pvFiltAxisNameFor(was.dim)+' “'
    +(was.dim==='app'?appLabel(was.key):kindLabel(was.key))+'” เป็น '
    +pvFiltAxisNameFor(dim)+' “'+(val?(dim==='app'?appLabel(val):kindLabel(val)):'ทุกชนิด')+'”');
}
function pvFiltAxisNameFor(dim){return dim==='app'?'ผู้เขียน':'ชนิด'}
function pvFilterOptions(){pvKindOptions();pvAppOptions()}
// rel is the value normalised to the busiest cell: how loud this cell is
function pvCellInk(rk,rel){
  if(PV.colour==='heat')return pvHeatCSS(Math.sqrt(rel),0.95);
  if(PV.colour==='writer')return pvAppInk(PV.cellApp[rk],rel);
  if(PV.colour==='kind')return pvKindInk(PV.cellKind[rk],rel);
  if(PV.colour==='recent'){
    // "when did this address last change": the ordinal of its newest line,
    // straight from the fold — no clock anywhere in the picture
    const s=PV.cellSeq[rk];
    return pvHeatCSS(s==null||!PV.cellSeqMax?0:1-s/PV.cellSeqMax,0.92);
  }
  return'rgba(126,231,135,'+(0.22+0.78*rel).toFixed(2)+')';
}
/* ── 🔍 the magnifier ────────────────────────────────────────────────
   A lens held over the plane. Three rules make it a viewport instead of a
   second plane:

   1. It magnifies the SAME data (PV.cells), so the glass cannot show a cell
      the map disagrees with. There is no second picture to keep in step.
   2. It stops at the REAL SCALE — the zoom cap is where one cell fills the
      lens, because a cell is the finest ADDRESS that exists. Past that the
      glass would only enlarge a single number, and the honest next step is
      that cell's CONTENTS (the panel read), not more magnification.
   3. Its position is stored in PLANE coordinates, so moving the base view or
      changing mode never teleports the glass.
   The seam is free: positions are torus deltas, so a patch near x=143 borrows
   real cells from x=0 and there is no edge case to handle. */
const PV_ZOOM=[1,2,3,4,5,6,8,10,12,16,20,25,32,40,48,64];
function pvLensR(){
  // sized from the canvas, not fixed: 70px is most of the view on a phone and
  // a speck on a laptop, and the cap below is derived from this radius
  const px=Math.max(PV.w,PV.h)*pstep();
  return Math.max(34,Math.min(120,Math.round(px*0.17)));
}
function pvLensZ(){
  const m=pvLensZMax();return Math.max(1,Math.min(PV.lens.z,m));
}
function pvLensPxPerCell(){
  // one PLANE cell in canvas pixels. In block view a block is step px and a
  // block is 12x12 cells, so the glass must move and grow by step/12 there —
  // otherwise the same drag would fly across the plane 12x too fast.
  return pstep()/(PV.mode==='blocks'?12:1);
}
function pvLensZMax(){
  // ⛔ the real scale: the zoom at which one CELL spans the lens. Expressed in
  // px-per-cell, so the cap means the same thing in every view.
  return Math.max(1,Math.floor(2*pvLensR()/pvLensPxPerCell()));
}
function pvLensStops(){
  const m=pvLensZMax(),s=PV_ZOOM.filter(z=>z<=m);
  return s.length?s:[1];
}
function pvLensPos(){
  const step=pstep(),L=PV.lens;
  if(PV.mode==='plane')return[L.wedge*step,L.ring*step];
  if(PV.mode==='blocks')return[pvB(L.wedge/12-PV.bOrig.x)*step,pvB(L.ring/12-PV.bOrig.y)*step];
  const o=PV.origin||{wedge:0,ring:0};
  return[pvWrap(L.wedge-o.wedge)*step,pvWrap(L.ring-o.ring)*step];
}
function pvLensCS(){return pvLensPxPerCell()*pvLensZ()}
function pvLensNudge(dw,dr){
  PV.lens.wedge=pvWrap(PV.lens.wedge+dw);
  PV.lens.ring=pvWrap(PV.lens.ring+dr);
  pvLensFollow();
  pvDraw();pvLensInfo();
}
/* Keep the glass ON SCREEN — only when the reader is NOT holding it.

   The follow used to run on every pointermove of a glass drag too, and that
   was the flicker: the hand moved the glass one way, the band moved the frame
   the other, and pvDraw between them painted a frame that jumped against the
   hand — "flicker, and it snaps back where it was". A reader holding the
   glass wants the patch they are inspecting; a nudge or a zoom may reveal a
   glass that was left at an edge, and then following is the courtesy that
   keeps it visible. */
function pvLensFollow(){
  if(PV.lens.drag)return;
  if(PV.mode!=='window'||!PV.origin)return;
  const m=Math.ceil(pvLensR()/pvLensPxPerCell())+1,win=PV.win;
  if(win<=2*m)return;                        // no band to keep it in
  const L=PV.lens,o=PV.origin;
  let off=pvWrap(L.wedge-o.wedge),adj=0;
  if(off<m)adj=off-m;else if(off>win-m)adj=off-(win-m);
  if(adj)o.wedge=pvWrap(o.wedge+adj);
  off=pvWrap(L.ring-o.ring);adj=0;
  if(off<m)adj=off-m;else if(off>win-m)adj=off-(win-m);
  if(adj)o.ring=pvWrap(o.ring+adj);
}
/* Deliberately NO post-drag frame slide: a reclaim that moved the frame after
   release was measured as "เด้งกลับจุดเดิม" too — the map is a torus, so a
   glass parked near a rim is not clipped, it re-enters from the far side. */
// Name the RESOLUTION, not the zoom factor: ×4 is "cells" on a laptop and
// "blocks" on a phone, so a label in multiples would mean different things
// on different screens. A level means the same thing everywhere.
function pvLensLevel(cs){
  if(cs>=pvLensR()*1.35)return['จริง','สเกลจริง — 1 ช่องเต็มแว่น','หยุดแล้ว: ไม่มีที่อยู่ที่ละเอียดกว่านั้น เนื้อในอ่านที่แผงขวา'];
  if(cs>=7)return['L0','L0 เซลล์','แต่ละช่องแยกออกจากกันแล้ว — เห็นว่าช่องไหนแน่นกว่าช่องไหน'];
  if(cs>=3)return['L1','L1 บล็อก','เห็นกริด 12×12 — ที่อยู่ยังรวมกันอยู่'];
  return['L2','L2 ระนาบ','ยังเป็นแผนที่ความหนาแน่น — ยังแยกเซลล์ไม่ออก'];
}
function pvLensPt(w,r){return[pvWrap(w),pvWrap(r)]}
// Where on the glass a canvas point falls, or null when it is off it. Every
// wrapped copy is tested: the copy the pointer is visibly over is the one that
// must answer, or the cursor would contradict the picture.
function pvLensHit(gx,gy){
  if(!PV.lens.on)return null;
  const [bx,by]=pvLensPos(),R=pvLensR(),px=PV.w*pstep(),py=PV.h*pstep();
  for(const dx of [-px,0,px])for(const dy of [-py,0,py]){
    const mx=gx-(bx+dx),my=gy-(by+dy);
    if(mx*mx+my*my<=R*R)return[mx,my];
  }
  return null;
}
function pvLensPaint(){
  // a drag fires faster than a frame is worth: drawing the lens on every
  // pointermove would spend the whole budget on frames nobody sees
  if(PV.lens.snap)return;
  PV.lens.snap=requestAnimationFrame(()=>{PV.lens.snap=null;pvDraw();pvLensInfo()});
}
// the cluster's elements, found once: pvLensInfo runs on every drag frame, and
// three querySelectors per frame to re-find buttons that never move is exactly
// the kind of cost §14 was about.
let _zEls=null;
function pvZoomEls(){
  if(!_zEls)_zEls={zin:$('#pv-lens-zin'),zout:$('#pv-lens-zout')};
  return _zEls;
}
function pvLensInfo(){
  const el=$('#pv-lensinfo');
  const z=pvLensZ(),m=pvLensZMax(),lv=pvLensLevel(pvLensCS());
  // the cluster mirrors the same numbers the status line reports, and greys out
  // at the same limits. The one place the magnifier PROMISES to stop (§10.1) is
  // the one place a control should stop — a + that keeps working at the real
  // scale would be the UI contradicting the model.
  const zi=pvZoomEls();
  if(zi.zin)zi.zin.disabled=!PV.lens.on||z>=m;
  if(zi.zout)zi.zout.disabled=!PV.lens.on||z<=1;
  if(!el)return;
  el.classList.toggle('on',PV.lens.on);
  if(!PV.lens.on){el.textContent='';return}
  const w=Math.floor(PV.lens.wedge),r=Math.floor(PV.lens.ring);
  const at=m?Math.round(100*z/m):0;
  const f2=pvFilt(),ct=Math.floor(L.wedge)+Math.floor(L.ring)*144;
  el.textContent='🔍 ×'+z+(z>=m?' ⛔':'')+' · '+lv[1]+' · (คอลัมน์ '+w+', แถว '+r+')'
    +' · ขยายแล้ว '+at+'% ของความละเอียดจริง'
    +(f2?(' · '+pvFiltLabel()+' '+pvFiltN(ct)+' จาก '+pvValAt(ct)):'');
  el.title=lv[2]+(f2?(' · กรอง: '+pvFiltLabel()+' — ตัวเลขในแว่นนับทุกชนิด'):'');
}
function pvLensDraw(c){
  const L=PV.lens;if(!L.on)return;
  const px=PV.w*pstep(),py=PV.h*pstep(),r=pvLensR(),[bx,by]=pvLensPos();
  // The glass WRAPS with the plane: near an edge its rim reappears on the far
  // side, exactly like the cells it is magnifying. The seam is not a special
  // case here either — the content is placed by torus deltas, so the copy on
  // the other side is the same glass, not a second one.
  for(const dx of [-px,0,px])for(const dy of [-py,0,py]){
    const ox=bx+dx,oy=by+dy;
    if(ox+r<0||ox-r>px||oy+r<0||oy-r>py)continue;
    pvLensOne(c,ox,oy,r,dx===0&&dy===0);
  }
}
function pvLensOne(c,ox,oy,r,primary){
  const L=PV.lens,step=pstep(),cs=pvLensCS();
  const z=pvLensZ(),lv=pvLensLevel(cs);
  const max=pvDrawMax();
  const keep=pvKeep(),filt=pvFilt(),half=Math.ceil(r/cs)+1;
  c.save();
  c.beginPath();c.arc(ox,oy,r,0,7);c.clip();
  c.fillStyle='#0a0e14';c.fillRect(ox-r,oy-r,2*r,2*r);
  // ── the cells, magnified. At low zoom only OCCUPIED cells are painted:
  // 20k empty rects is a black frame either way, and the loop is the whole
  // cost of a drag at ×2.
  const cw=Math.floor(L.wedge),cr=Math.floor(L.ring),fw=L.wedge-cw,fr=L.ring-cr;
  for(let dy=-half;dy<=half;dy++){
    const py=oy+(dy-fr)*cs;if(py+cs<oy-r||py>oy+r)continue;
    for(let dx=-half;dx<=half;dx++){
      const px=ox+(dx-fw)*cs;if(px+cs<ox-r||px>ox+r)continue;        const P=pvLensPt(cw+dx,cr+dy),rk=P[1]*144+P[0],v=pvValAt(rk);
      if(!v&&cs<7)continue;
      const gh=filt?(filt[rk]||0):v;          // the SHADE follows the filter
      c.globalAlpha=((!keep||keep.has(rk))&&(!filt||gh>0))?1:0.15;
      c.fillStyle=v?(filt&&!gh?'#121821':pvCellInk(rk,gh/max)):'#121821';
      c.fillRect(px+.5,py+.5,cs-1,cs-1);
      // at this size the NUMBER is the data — a colour ramp a reader has to
      // remember is worse than the count written where it happened
      if(cs>=26&&v>0){
        c.fillStyle='#0a0e14';c.font='bold '+Math.min(16,cs*0.42).toFixed(0)+'px monospace';
        c.textAlign='center';c.fillText(v,px+cs/2,py+cs/2+5);c.textAlign='left';
      }
      c.globalAlpha=1;
    }
  }
  // ── the 12x12 grid: the only landmark that survives magnification, and the
  // one that keeps you oriented without reading a coordinate out loud
  if(cs*12>=20){
    c.strokeStyle='rgba(139,148,158,.3)';c.lineWidth=1;
    const k0=Math.floor((L.wedge-half)/12)-1,k1=Math.ceil((L.wedge+half)/12)+1;
    for(let k=k0;k<=k1;k++){
      const gx=ox+torusDx(L.wedge,pvWrap(k*12))*cs;
      const gy=oy+torusDx(L.ring,pvWrap(k*12))*cs;
      if(gx>ox-r&&gx<ox+r){c.beginPath();c.moveTo(gx+.5,oy-r);c.lineTo(gx+.5,oy+r);c.stroke()}
      if(gy>oy-r&&gy<oy+r){c.beginPath();c.moveTo(ox-r,gy+.5);c.lineTo(ox+r,gy+.5);c.stroke()}
    }
  }
  // ── edges, magnified: a graph read without its lines is half the picture
  if(PV.showBonds){
    const edges=pvDrawnEdges(),reach=2*r;
    const at=rk=>rk==null?null:[ox+torusDx(L.wedge,rk%144)*cs,oy+torusDx(L.ring,Math.floor(rk/144))*cs];
    c.lineWidth=1.1;
    edges.forEach(e=>{
      const a=at(e.src_rkey),b=at(e.dst_rkey);
      if(!a||!b)return;
      const far=(p,q)=>Math.abs(p[0]-ox)>reach&&Math.abs(p[1]-oy)>reach
        &&Math.abs(q[0]-ox)>reach&&Math.abs(q[1]-oy)>reach;
      if(far(a,b))return;                    // entirely off the glass
      const hot=PV.sel!=null&&(e.src_rkey===PV.sel||e.dst_rkey===PV.sel);
      c.strokeStyle=hot?'#ffffff':(PV.relColour?relCSS(pvRelOf(e),0.75)
        :'rgba('+(e.kind==='lifted'?'188,140,255':'121,192,255')+',.7)');
      c.globalAlpha=hot?1:0.8;
      c.beginPath();c.moveTo(a[0],a[1])
        ;c.lineTo(b[0],b[1]);c.stroke();c.globalAlpha=1;
    });
  }
  // ── red rings: a sighting under the glass is still a sighting
  PV.sights.forEach(s=>{
    const x=ox+torusDx(L.wedge,s.point%144)*cs;
    const y=oy+torusDx(L.ring,Math.floor(s.point/144))*cs;
    if(Math.abs(x-ox)>r+6||Math.abs(y-oy)>r+6)return;
    c.strokeStyle='#79c0ff';c.lineWidth=1.2;
    c.beginPath();c.arc(x+cs/2,y+cs/2,Math.max(3.5,Math.min(cs*0.7,7)),0,7);c.stroke();
  });
  c.restore();
  // ── the rim, drawn OUTSIDE the clip so it stays crisp at every zoom
  c.strokeStyle='rgba(121,192,255,.9)';c.lineWidth=2;
  c.beginPath();c.arc(ox,oy,r,0,7);c.stroke();
  c.strokeStyle='rgba(10,14,20,.85)';c.lineWidth=1;
  c.beginPath();c.arc(ox,oy,r-2,0,7);c.stroke();
  // crosshair on the exact address being magnified — the glass has a POINT,
  // not just a region, or "what am I looking at" has no answer
  c.strokeStyle='rgba(255,212,121,.95)';c.lineWidth=1;
  c.beginPath();c.moveTo(ox-7,oy);c.lineTo(ox+7,oy);
  c.moveTo(ox,oy-7);c.lineTo(ox,oy+7);c.stroke();
  // At the real scale the glass covers ONE address and there is nothing finer
  // to draw. So it stops magnifying and instead SAYS WHAT IS THERE — more zoom
  // at this point would be enlarging a single number, which is the one thing a
  // viewport must never do.
  if(primary&&cs>=r*1.4){
    const vw=pvWrap(Math.floor(L.wedge)),vr=pvWrap(Math.floor(L.ring));      const vv=pvValAt(vr*144+vw);
    c.textAlign='center';
    c.fillStyle=vv?'#ffd479':'#484f58';c.font='bold 26px monospace';
    c.fillText(vv,ox,oy+3);
    c.fillStyle='#8b949e';c.font='10px monospace';
    c.fillText('line'+(vv===1?'':'s')+' ในที่อยู่นี้',ox,oy+21);
    if(vv)c.fillText('คลิกเพื่ออ่านเนื้อใน',ox,oy+35);
    c.textAlign='left';
  }
  if(primary){
    // the label and the handle belong to the ONE glass, not to every wrapped
    // copy of its rim: nine labels is not nine times the information
    const cw2=pvWrap(Math.floor(L.wedge)),cr2=pvWrap(Math.floor(L.ring));      const v=pvValAt(cr2*144+cw2);
    const txt='×'+z+' '+lv[0]+' · '+cw2+','+cr2+' · '+v+' line'+(v===1?'':'s');
    c.font='11px monospace';
    const tw=c.measureText(txt).width+14,by=oy+r-19;
    c.fillStyle='rgba(10,14,20,.9)';
    c.beginPath();
    if(c.roundRect)c.roundRect(ox-tw/2,by,tw,16,8);else c.rect(ox-tw/2,by,tw,16);
    c.fill();
    c.fillStyle=z>=pvLensZMax()?'#ff9f9f':'#ffd479';c.textAlign='center';
    c.fillText(txt,ox,by+11.5);c.textAlign='left';
  }
  pvLensHandle(c,ox,oy,r);
}
// A visible grab handle, because a lens with no handle reads as a rendering
// artefact and nobody tries to drag it
function pvLensHandle(c,ox,oy,r){
  const a=-Math.PI/4,hx=ox+Math.cos(a)*r,hy=oy+Math.sin(a)*r;
  c.strokeStyle='rgba(121,192,255,.9)';c.lineWidth=4;c.lineCap='round';
  c.beginPath();c.moveTo(hx,hy);c.lineTo(hx+Math.cos(a)*13,hy+Math.sin(a)*13);c.stroke();
  c.lineWidth=1.4;c.strokeStyle='rgba(10,14,20,.75)';
  c.beginPath();c.moveTo(hx,hy);c.lineTo(hx+Math.cos(a)*13,hy+Math.sin(a)*13);c.stroke();
  c.lineCap='butt';
}
function pvGrid(c,w,h,step,px,py){
  // a faint L1 grid: without it a 144x144 plane of 3px cells has no landmarks
  if(step*12<18)return;
  c.strokeStyle='rgba(139,148,158,.16)';c.lineWidth=1;
  for(let i=1;i*12<w;i++){
    const p=i*12*step+.5;
    c.beginPath();c.moveTo(p,0);c.lineTo(p,py);c.stroke();
  }
  for(let i=1;i*12<h;i++){
    const p=i*12*step+.5;
    c.beginPath();c.moveTo(0,p);c.lineTo(px,p);c.stroke();
  }
}
/* ── the base layer: painted once, blitted per frame ────────────────────
   The base picture is a function of data that does NOT move during a gesture,
   so it is rendered into an offscreen canvas and blitted. Two measurements
   forced this:

     * pvDraw assigned canvas.width — and so reallocated the whole backing
       store, and cleared it — on EVERY call: 30 assignments for 30 draws;
     * one plane-mode draw repainted all 10,131 cells in **27.7ms** (max 37ms,
       ~27fps) before the lens had drawn anything.

   A wheel tick and a drag frame therefore each paid for the entire map. Now a
   zoom frame repaints ONLY the glass.

   The key is the exact set of inputs the base reads, taken by OBJECT IDENTITY
   wherever the input is a big structure (cells/bonds/sights are replaced
   wholesale, never edited in place), so a stale cache is not something to
   reason about — it is a key mismatch. `PV.origin` is in the key because in
   window mode it moves every cell without touching PV.cells at all: the glass
   sliding its FRAME (§10.4) would otherwise leave the base behind. */
let _pvUid=0;const _pvIds=new WeakMap();
function _idOf(o){
  if(o==null)return'-';
  let v=_pvIds.get(o);if(!v){v=String(++_pvUid);_pvIds.set(o,v)}
  return v;
}
function pvBaseKey(px,bw){
  const o=PV.origin;
  return[px,bw,PV.w,PV.h,PV.mode,PV.colour,PV.sel,PV.focus,PV.seq,PV.win,
    PV.showBonds,PV.ctxOn,PV.style,PV.hubK,PV.relColour,PV.prune,PV.cellSeqMax,
    o?o.wedge:null,o?o.ring:null,PV.bOrig.x,PV.bOrig.y,
    _idOf(PV.cells),_idOf(PV.bonds),_idOf(PV.crossEdges),_idOf(PV.sights),
    _idOf(PV.cellApp),_idOf(PV.cellSeq),_idOf(PV.cellKind),
    _idOf(PV.kinds),_idOf(PV.apps),PV.filt&&PV.filt.dim,PV.filt&&PV.filt.key].join('|');
}
let _baseKey=null,_baseCv=null;
function pvBase(px,bw){
  const key=pvBaseKey(px,bw);
  if(key===_baseKey&&_baseCv){PV.baseHit=(PV.baseHit||0)+1;return _baseCv}
  _baseKey=key;PV.baseMiss=(PV.baseMiss||0)+1;
  const cv=_baseCv||document.createElement('canvas');
  _baseCv=cv;
  if(cv.width!==bw){cv.width=bw;cv.height=bw}
  const c=cv.getContext('2d');
  c.setTransform(1,0,0,1,0,0);c.clearRect(0,0,bw,bw);
  const scale=bw/px;                    // css px -> device px, exactly
  c.setTransform(scale,0,0,scale,0,0);
  pvBasePaint(c,px);
  return cv;
}
function pvBasePaint(c,px){
  const w=PV.w,h=PV.h,step=pstep();
  c.fillStyle='#0a0e14';c.fillRect(0,0,px,px);
  const max=pvDrawMax();
  const keep=pvKeep(),filt=pvFilt();
  if(!Object.keys(PV.cells).length){
    c.fillStyle='#8b949e';c.font='12px monospace';c.textAlign='center';
    c.fillText('ยังไม่มีบรรทัดในมุมมองนี้',px/2,px/2);c.textAlign='left';
  }
  pvGrid(c,w,h,step,px,px);
  // the three busiest cells get a glow: at plane scale they are the only
  // landmarks, and "where is the weight" is the first question anyone asks
  // the hubs are the busiest cells OF WHAT IS DRAWN: under a filter that is the
  // busiest cell of the filtered kind, or the gold rings would crown cells that
  // hold none of it
  const hk=Object.keys(filt||PV.cells).map(Number);
  const hv=k=>filt?filt[k]:PV.cells[k];
  const hubs=new Set(hk.sort((a,b)=>hv(b)-hv(a)).slice(0,3).filter(k=>hv(k)>1));
  for(const k in PV.cells){
    const [x,y]=pvXY(+k);if(x<0||y<0||x>=w||y>=h)continue;
    const hit=filt?(filt[k]||0):PV.cells[k];
    const near=(!keep||keep.has(+k))&&(!filt||hit>0);
    const rel=hit/max;
    // at plane scale one cell is ~3px, so a single line is nearly invisible;
    // every occupied cell gets a floor of 2px so the map is actually readable
    const cw=Math.max(step,2),o=(cw-step)/2;
    const gap=step>=6?1:0;                      // a hairline between cells
    c.globalAlpha=near?1:0.12;                  // masked = barely there
    c.fillStyle=pvInkAt(+k,rel);
    c.fillRect(x*step-o+gap/2,y*step-o+gap/2,cw-gap,cw-gap);
    if(hubs.has(+k)){
      c.strokeStyle='rgba(255,212,121,.85)';c.lineWidth=1;
      c.strokeRect(x*step-o-.5,y*step-o-.5,cw+1,cw+1);
    }
    if(keep&&near&&+k!==PV.sel){                // in radius: mark it
      c.strokeStyle='rgba(126,231,135,.55)';c.lineWidth=1;
      c.strokeRect(x*step-o-.5,y*step-o-.5,cw+1,cw+1);
    }
    c.globalAlpha=1;
  }
  /* ── torus ghost cells: draw near-border cells on opposite side ── */
  if(PV.mode==='plane'){
    for(const k in PV.cells){
      const rk=+k,cx=rk%144,cy=Math.floor(rk/144);
      const ghosts=ghostOffsets(cx,cy);
      if(!ghosts.length)continue;
      const gh=filt?(filt[k]||0):PV.cells[k];
      const near=(!keep||keep.has(rk))&&(!filt||gh>0),rel=gh/max;
      const cw=Math.max(step,2),o=(cw-step)/2,gap=step>=6?1:0;
      c.globalAlpha=near?0.65:0.08;
      c.fillStyle=pvCellInk(rk,rel);
      for(const[dx,dy]of ghosts){
        const gx=cx+dx,gy=cy+dy;
        c.fillRect(gx*step-o+gap/2,gy*step-o+gap/2,cw-gap,cw-gap);
        if(hubs.has(rk)){
          c.strokeStyle='rgba(255,212,121,.85)';c.lineWidth=1;
          c.strokeRect(gx*step-o-.5,gy*step-o-.5,cw+1,cw+1);
        }
        if(keep&&near&&rk!==PV.sel){
          c.strokeStyle='rgba(126,231,135,.55)';c.lineWidth=1;
          c.strokeRect(gx*step-o-.5,gy*step-o-.5,cw+1,cw+1);
        }
      }
      c.globalAlpha=1;
    }
    /* seam indicators: faint dashed lines at 0/144 boundaries */
    c.save();c.setLineDash([4,6]);c.strokeStyle='rgba(139,148,158,.25)';c.lineWidth=1;
    c.beginPath();c.moveTo(0,0);c.lineTo(0,px);c.stroke();
    c.beginPath();c.moveTo(px,0);c.lineTo(px,px);c.stroke();
    c.beginPath();c.moveTo(0,0);c.lineTo(px,0);c.stroke();
    c.beginPath();c.moveTo(0,px);c.lineTo(px,px);c.stroke();
    c.restore();
  }
  // edges are drawn from the addresses of their two ends — a bond line
  // carries the meaning, the plane only shows where both ends sit
  if(PV.showBonds)pvEdgeStrokes(keep).forEach(s=>pvStroke(c,s));
  if(PV.ctxOn)pvMemoryPass(c,step,keep);
  // Reads go on TOP of the edges: a sighting is a point event and must not be
  // buried under the graph it sits in. A cell rect alone vanishes exactly
  // where the plane is most crowded (a whole 144x144 plane on a phone is ~3px
  // per cell), so the marker is a RING sized in pixels, not in cells — it
  // stays a circle at every zoom, which is what the legend promises.
  PV.sights.forEach(s=>{
    // `n` was a parameter of a signature this function no longer has, so both
    // of these bounds threw a ReferenceError the moment the list ran — and a
    // paint that throws leaves the canvas showing the PREVIOUS frame while
    // everything after the throw (the "you are here" marker, and the caller
    // that was going to open the popup) silently never happened. Found by
    // ui_browser_check clicking a mode button, not by any arithmetic.
    const [x,y]=pvXY(s.point);if(x<0||y<0||x>=w||y>=h)return;
    const cx=x*step+step/2, cy=y*step+step/2;
    c.save();
    c.strokeStyle='#79c0ff';c.lineWidth=1.2;
    if(step>=7)c.strokeRect(x*step+.5,y*step+.5,step-1,step-1);
    const rr=Math.max(3.6,Math.min(step*0.75,7));
    c.beginPath();c.arc(cx,cy,rr,0,7);c.stroke();
    c.fillStyle='#79c0ff';c.beginPath();c.arc(cx,cy,1.4,0,7);c.fill();
    if(PV.sel===s.point){                 // you are here: a second ring
      c.globalAlpha=.45;c.lineWidth=1;
      c.beginPath();c.arc(cx,cy,rr+3,0,7);c.stroke();c.globalAlpha=1;
    }
    c.restore();
  });
  if(PV.sel!=null){
    const [x,y]=pvXY(PV.sel);
    if(x>=0&&y>=0&&x<w&&y<h){
      const o=step>=6?3:1;
      c.strokeStyle='#ffa657';c.lineWidth=2;
      c.strokeRect(x*step+o,y*step+o,step-2*o,step-2*o);
      c.strokeStyle='rgba(255,166,87,.35)';c.lineWidth=1;
      c.strokeRect(x*step-o,y*step-o,step+2*o,step+2*o);
    }
  }
}
function pvDraw(){
  const step=pstep(),px=Math.max(PV.w,PV.h)*step;
  // Device-pixel-exact backing store. A fractional devicePixelRatio (measured
  // 1.232 here) with an integer css pixel size gave a NON-integer backing
  // store, which is the blurriest of all worlds: the size and the transform
  // now agree by construction instead of by luck. And the assignment happens
  // only when the size actually changed — re-assigning width reallocates and
  // clears, which is exactly what we are no longer paying for per frame.
  const dpr=window.devicePixelRatio||1,bw=Math.round(px*dpr);
  if(pcv.width!==bw||pcv.height!==bw){pcv.width=bw;pcv.height=bw}
  pcv.style.width=px+'px';pcv.style.height=px+'px';
  const c=pcv.getContext('2d');
  c.setTransform(1,0,0,1,0,0);c.clearRect(0,0,bw,bw);
  const scale=bw/px;
  c.setTransform(scale,0,0,scale,0,0);
  c.drawImage(pvBase(px,bw),0,0,px,px);
  // the magnifier is drawn LAST and over everything: it is a thing held above
  // the map, so nothing on the map may paint over the glass
  pvLensDraw(c);
}
/* ── the timeline strip ────────────────────────────────────────────────
   The tick layer with both of its axes visible: height = how many lines in
   that tick, colour = who wrote most of them. Past a few hundred ticks one bar
   per tick is a smear, so bars bucket and the label says how many ticks each
   bar stands for — an unlabelled aggregate would be a lie about resolution. */
const rb=$('#pv-ribbon');
function pvRibbon(){
  const rows=PV.timeline,n=rows.length,dpr=window.devicePixelRatio||1;
  const w=Math.max(120,rb.clientWidth||320),h=42;
  rb.width=w*dpr;rb.height=h*dpr;rb.style.width=w+'px';rb.style.height=h+'px';
  const c=rb.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);
  c.fillStyle='#0a0e14';c.fillRect(0,0,w,h);
  if(!n){
    c.fillStyle='#8b949e';c.font='10px monospace';c.fillText('ยังไม่มีบรรทัดให้แสดง',7,24);
    $('#pv-ribbon-info').textContent='ช่วง —';PV.rib=null;return;
  }
  // A bar is a TICK — the same 8-line chunk the feed counts in — so the strip
  // and the feed are the same ruler. Past a few hundred ticks one bar per tick
  // is a smear, so bars bucket and B says how many ticks each one stands for:
  // an unlabelled aggregate would be a lie about resolution.
  const ticks=Math.ceil(n/CHUNK);
  const MAXB=240,B=Math.ceil(ticks/MAXB),bars=Math.ceil(ticks/B),bw=w/bars;
  PV.rib={B,bars,bw,ticks};
  // Two passes because the height is relative to the BUSIEST bar, not to the
  // bar size: normalising by the bucket width would paint every bar full and a
  // picture that cannot show a quiet stretch is not showing activity at all.
  const bucket=[];
  let maxCnt=1;
  for(let i=0;i<bars;i++){
    let cnt=0;const apps={};
    for(let j=i*B*CHUNK;j<Math.min((i+1)*B*CHUNK,n);j++){cnt++;const a=rows[j][5]||'?';(apps[a]=apps[a]||0)}
    const ent=Object.entries(apps).sort((p,q)=>q[1]-p[1]);
    bucket.push({cnt,app:ent.length?ent[0][0]:'?'});
    if(cnt>maxCnt)maxCnt=cnt;
  }
  for(let i=0;i<bars;i++){
    const full=bucket[i].cnt/maxCnt;
    const bh=Math.max(3,(h-7)*full);
    c.fillStyle=pvAppInk(bucket[i].app,full);
    c.fillRect(i*bw+0.5,h-3-bh,Math.max(1,bw-1),bh);
  }
  // where we are in time (the scrub position), and the live edge
  const S=PV.seq===null?n-1:PV.seq;
  const x=Math.round(((Math.floor(S/CHUNK)+0.5)/ticks)*w)+.5;
  c.strokeStyle='#ffa657';c.lineWidth=1.5;
  c.beginPath();c.moveTo(x,0);c.lineTo(x,h);c.stroke();
  const seen=new Set();PV.timeline.slice(0,S+1).forEach(r=>seen.add(r[5]||'?'));
  // short on screen, complete in the tooltip: the same rule as the header line
  const where=PV.seq===null?'ทุกบรรทัด':('ช่วงที่ '+Math.floor(S/CHUNK)+'/'+(ticks-1));
  const info=$('#pv-ribbon-info');
  info.textContent=where+' · '+bars+' แท่ง';
  info.title=where+' · '+bars+' แท่ง'+(B>1?(' (แท่งละ '+B+' ช่วง)'):'')
    +' · สูงสุด '+maxCnt+' บรรทัด · ผู้เขียน '+seen.size;
}
function pvRibbonAt(ev){
  // returns a TICK, and lands on that chunk's last line: scrubbing to a tick
  // should show the plane as it was when that chunk was written
  if(!PV.rib||!PV.timeline.length)return null;
  const r=rb.getBoundingClientRect(),x=Math.max(0,Math.min(1,(ev.clientX-r.left)/r.width));
  const bar=Math.max(0,Math.min(PV.rib.bars-1,Math.floor(x*PV.rib.bars)));
  const tick=Math.min(PV.rib.ticks-1,bar*PV.rib.B);
  return Math.min(PV.timeline.length-1,tick*CHUNK+CHUNK-1);
}
/* ── the legend ────────────────────────────────────────────────────────
   A layer that does not say what it means is a stain. So the panel names the
   gradient, its ends, and — in writer mode — which hue belongs to whom. */
let _legendKey=null;   // redrawing a legend 16x/s during replay is pure waste
function pvLegend(){
  const box=$('#pv-legend');if(!box)return;const cells=PV.cells;
  const hubs=Object.keys(cells).map(Number).sort((a,b)=>cells[b]-cells[a]).slice(0,3);
  const key=[PV.colour,PV.metaOn,Object.keys(cells).length,hubs.join(','),PV.cellSeqMax,
    _idOf(PV.cellKind)].join('|');
  if(key===_legendKey)return;
  _legendKey=key;
  const end=(a,b)=>'<span>'+a+' → '+b+'</span>';
  let h='';
  const f=pvFilt();
  if(f)h+='<div style="color:var(--warn)">กรอง: '+esc(pvFiltLabel())
    +' — <b>สีและความสว่าง</b> นับเฉพาะ'+esc(pvFiltWord())+' · ช่องที่ไม่มี'+esc(pvFiltWord())
    +'จางเป็นสีเทา'
    +' · <b>ตัวเลขที่ผู้อ่านเห็น</b> (ช่อง / แว่น / ทิป) นับทั้งคลังเหมือนเดิม</div>';
  if(PV.colour==='writer'){
    const apps=pvTopApps();
    h='<div style="color:var(--fg)">สี = แอปที่เขียนล่าสุดในช่องนั้น</div>';
    h+=apps?Object.keys(apps).map(a=>'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:9px">'
      +'<span style="width:11px;height:11px;border-radius:3px;background:'+pvAppInk(a,1)+';display:inline-block"></span>'
      +esc(a)+'</span>').join('')
      :'<span style="color:var(--warn)">ยังโหลดข้อมูลผู้เขียนไม่เสร็จ — เลือกสีนี้ใหม่อีกครั้ง</span>';
  }else if(PV.colour==='kind'){
    // the count beside each swatch is how many CELLS carry that kind last, so
    // the legend says what the picture is showing rather than how many lines
    // exist — those two numbers are different and only one of them is drawn
    const ks=pvTopKinds();
    h='<div style="color:var(--fg)">สี = ชนิดของบรรทัดล่าสุดในช่องนั้น</div>';
    h+=ks?Object.keys(ks).sort((a,b)=>ks[b]-ks[a]).map(k=>
      '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:9px"'
      +' title="'+esc(k)+' · '+ks[k]+' ช่อง">'
      +'<span style="width:11px;height:11px;border-radius:3px;background:'+pvKindInk(k,1)+';display:inline-block"></span>'
      +esc(kindLabel(k))+' <span style="color:var(--dim)">'+ks[k]+'</span></span>').join('')
      :'<span style="color:var(--warn)">ยังโหลดข้อมูลชนิดไม่เสร็จ — เลือกสีนี้ใหม่อีกครั้ง</span>';
  }else if(PV.colour==='recent'){
    h='<div style="color:var(--fg)">สี = ช่องนี้เปลี่ยนล่าสุดเมื่อไร</div>'
      +'<span class="pvgrad" style="background:linear-gradient(90deg,'+pvRampCSS().join(',')+')"></span>'
      +end('เก่าสุด','ใหม่สุด');
  }else if(PV.colour==='heat'){
    h='<div style="color:var(--fg)">สี = ความร้อน: จำนวนบรรทัด (ย่อด้วย √ ให้ช่องที่แน่นมากไม่กลืนที่เหลือ)</div>'
      +'<span class="pvgrad" style="background:linear-gradient(90deg,'+pvRampCSS().join(',')+')"></span>'
      +end('เบา','แน่นสุด');
  }else{
    h='<div style="color:var(--fg)">สี = จำนวนบรรทัด (เข้ม = แน่นกว่า)</div>'
      +'<span class="pvgrad" style="background:linear-gradient(90deg,rgba(126,231,135,.22),rgba(126,231,135,1))"></span>'
      +end('เบา','แน่นสุด');
  }
  if(hubs.length)h+='<div style="margin-top:4px">จุดรวมเด่น: '
    +hubs.map(k=>'<span style="color:var(--warn)">#'+k+'</span> ('+PV.cells[k]+')').join(' · ')+'</div>';
  box.innerHTML=h;
}
function pvCellAt(ev){
  const r=pcv.getBoundingClientRect(),step=pstep();
  // Under the glass the pointer is over the MAGNIFIED cell, not the base one.
  // Answering with the base cell would make hovering and clicking the lens
  // disagree with what the lens is visibly showing.
  const on=pvLensHit(ev.clientX-r.left,ev.clientY-r.top);
  if(on){
    const cs=pvLensCS();
    const w=pvWrap(Math.floor(PV.lens.wedge+on[0]/cs));
    const q=pvWrap(Math.floor(PV.lens.ring+on[1]/cs));
    return[w,q,q*144+w];
  }
  let x=Math.floor((ev.clientX-r.left)/step),y=Math.floor((ev.clientY-r.top)/step);
  if(PV.mode==='plane'){
    /* torus: clicking outside 0..143 wraps around */
    x=((x%T144)+T144)%T144;y=((y%T144)+T144)%T144;
  }else{
    if(x<0||y<0||x>=PV.w||y>=PV.h)return null;
  }
  return[x,y,pvRkeyAt(x,y)];
}
// one way in, one way out: the top layer is the mechanism, so no call site can
// "hide" the popup in a way that leaves it open, or "show" it without showing.
function pvTopOpen(el){
  if(el&&!el.matches(':popover-open')){try{el.showPopover()}catch(e){}}
}
function pvTopHide(el){
  if(el&&el.matches(':popover-open')){try{el.hidePopover()}catch(e){}}
}
function pvPop(){
  const pop=$('#plane-pop'),d=PV.lines;
  // Only a deliberate READ may OPEN the popup: every refresh path (pan,
  // resize, refetch, refocus) calls this same function, and without the gate
  // any of them would pop the panel over a map the reader is still reading —
  // exactly the "it keeps covering what I look at" complaint. A re-place of
  // an already-open popup is fine; an open from closed is not, unless a read
  // just asked for it via PV.popWant.
  if(PV.sel==null){PV.popWant=false;pvTopHide(pop);return}
  if(!d){pvTopHide(pop);return}
  if(!PV.popWant&&!pop.matches(':popover-open'))return;
  const keep=pvKeep();
  const mine=pvGraphEdges().filter(b=>b.src_rkey===PV.sel||b.dst_rkey===PV.sel);
  let h='<div style="color:var(--warn)">ที่อยู่ '+d.point+' · '+d.count+' บรรทัด</div>';
  (d.lines||[]).slice(0,2).forEach(m=>{
    // the wire kind is OUR name for the record; the popup is a person's read,
    // so it says what the record IS, and a bond line shows the RELATION —
    // the hashes are what the plane is about, not what the line says
    const kind=m.kind||'chat';
    if(kind==='bond'){
      const p=String(m.text||'').match(/^([0-9a-f]{64}) (\S+) ([0-9a-f]{64})$/);
      h+='<div class="pp" style="color:var(--dim)">เส้นเชื่อม'+(p?' · '+esc(p[2]):'')+'</div>';
      return;
    }
    if(kind==='pointer'){
      h+='<div class="pp" style="color:var(--dim)">รอยอ่าน — มีคนเปิดที่อยู่นี้</div>';
      return;
    }
    h+='<div class="pp">'+esc(m.app||'?')+' '+esc((m.text||'').slice(0,90))+'</div>';
  });
  mine.slice(0,4).forEach(b=>{
    const out=b.src_rkey===PV.sel;
    h+='<div class="pp" style="color:'+pvRelInk(b)+'">'+esc(b.rel||'lifted')+' '
      +(out?'→ ':'← ')+(out?(b.dst8||('#row'+b.dst_rowid)):b.src8)+'</div>';
  });
  if(keep)h+='<div class="pp">รัศมี '+PV.focus+' ทอด → '+keep.size+' เซลล์</div>';
  h+='<div class="prow"><button class="pbtn" id="pop-focus">'+(PV.focus?'ปิดรัศมี':'โฟกัส 1 hop')+'</button>'
    +'<button class="pbtn" id="pop-zoom">ซูมบล็อก</button>'
    +'<button class="pbtn" id="pop-close">ปิด</button></div>';
  pop.innerHTML=h;pvTopOpen(pop);
  // Clamp against the VIEWPORT, never the canvas box — the box is a clipping
  // container, and clamping to it is precisely what guaranteed the cut-off. Its
  // rect only supplies where the selected cell is on screen. `max-height` plus
  // `overflow:auto` mean the clamp below is always satisfiable: the popup can
  // scroll, but it can no longer be chopped.
  const cr=pcv.getBoundingClientRect();
  const ctr=pvCenter(PV.sel)||[0,0];
  const vw=window.innerWidth||document.documentElement.clientWidth;
  const vh=window.innerHeight||document.documentElement.clientHeight;
  const pw=pop.offsetWidth,ph=pop.offsetHeight;
  const x=Math.max(10,Math.min(cr.left+ctr[0]+14,vw-pw-10));
  const y=Math.max(10,Math.min(cr.top+ctr[1]+14,vh-ph-10));
  pop.style.left=x+'px';pop.style.top=y+'px';
  $('#pop-focus').onclick=()=>setFocus(PV.focus?0:1);
  $('#pop-zoom').onclick=()=>pvZoomBlock(PV.sel);
  $('#pop-close').onclick=()=>{PV.popWant=false;pvTopHide(pop)};
}
function setFocus(h){
  PV.focus=h;pvFocusBtns();pvDraw();pvInfo();pvPop();
  if(h&&PV.sel==null)toast('เลือวจุดบนแผนที่ก่อน แล้วรัศมีจะบังส่วนที่ไม่เกี่ยวข้องให้');
}
function pvFocusBtns(){
  // A radius needs a CENTRE. With nothing selected these buttons only produced a
  // toast telling you to pick a point — a control that cannot do its job sitting
  // there looking available is how "what are hop 1-3?" happens. Disabled until
  // there is a point to be a radius of, and the tooltip says why.
  const noCentre=PV.sel==null;
  for(let i=0;i<4;i++){
    const b=$('#pv-f'+i);
    b.classList.toggle('on',PV.focus===i);
    b.disabled=i>0&&noCentre;
    b.title=i===0?'เห็นทั้งแผนที่ ไม่กรองอะไร'
      :('รัศมี '+i+' ทอด: เหลือเฉพาะช่องที่ต่อจากจุดที่เลือกภายใน '+i+' เส้น'
        +(noCentre?' — ต้องเลือกจุดบนแผนที่ก่อน':' · ตอนนี้เหลือ '+PV.focus+' จุด'));
  }
}
// A style that shows fewer edges must SAY so — a picture that quietly drops
// half the graph would be worse than a cluttered one.
function pvEdgeDisclosure(){
  const edges=pvGraphEdges();
  if(PV.style==='degree')
    return ' · วงตามจำนวนเส้น: ไม่วาดเส้น ('+edges.length+' เส้น) — เลือกจุดหรือกดรัศมี 1 ทอด เพื่อดูเส้นจริง';
  if(PV.style==='hub'||(PV.style==='auto'&&edges.length>PV.autoAt)){
    const deg=pvDegrees(edges),top=new Set(Object.keys(deg).map(Number)
      .sort((a,b)=>deg[b]-deg[a]).slice(0,PV.hubK));
    const shown=edges.filter(e=>top.has(e.src_rkey)||top.has(e.dst_rkey)).length;
    if(shown<edges.length)
      return ' · จุดรวม: แสดง '+shown+'/'+edges.length+' เส้น (ที่เหลือซ่อน — คลิกจุดหรือกดรัศมีเพื่อดูของมัน)';
  }
  return '';
}
// The legend has to be part of the picture, not decoration: it says how many
// edges each rel has AND how many of them the current style is not drawing.
function pvRelLegend(){
  const box=$('#rel-legend');if(!box)return;
  if(!PV.relColour){box.innerHTML='';return}
  // by content, not by object identity: every call rebuilds the edge objects,
  // so a Set of them would report that nothing was drawn while the plane
  // plainly draws it
  const key=e=>e.hash||(pvRelOf(e)+'|'+(e.src8||e.src_rkey)+'>'+(e.dst8||e.dst_rowid||e.dst_rkey));
  const all=pvGraphEdges(),drawn=new Set(pvDrawnEdges().map(key));
  const tot={},showed={};
  all.forEach(e=>{const r=pvRelOf(e);tot[r]=(tot[r]||0)+1;if(drawn.has(key(e)))showed[r]=(showed[r]||0)+1});
  const keys=Object.keys(tot).sort((a,b)=>tot[b]-tot[a]);
  let h=keys.map(r=>'<span style="display:inline-flex;align-items:center;gap:4px;margin:0 9px 3px 0">'
    +'<span style="width:12px;height:2px;background:'+relCSS(r,1)+';display:inline-block"></span>'
    +esc(r)+' <span style="color:var(--dim)">'+(showed[r]||0)+'/'+tot[r]+'</span></span>').join('');
  const hidden=all.length-drawn.size;
  if(hidden>0)h+='<div style="color:var(--warn)">จุดรวม: ไม่วาด '+hidden+' เส้นที่'
    +'ไม่แตะจุดรวม — ตัวเลข 0/12 ข้างบนคือความหมายที่ถูกตัดออกมากที่สุด</div>';
  // the measurement, said out loud: hue is free on coverage but past a couple of
  // hundred strokes the colours sit on top of each other and the panel is the
  // only place a rel is still certain
  if(drawn.size>200)h+='<div style="color:var(--dim)">สีเริ่มทับกันที่ขนาดนี้ —'
    +' คลิกจุดหรือกดรัศมีเพื่อดูความหมายของเส้นให้แน่</div>';
  box.innerHTML=h;
}
function pvInfo(){
  const src=(PV.seq===null||PV.seq>=PV.seqMax-1)?PV.plane:pvCellsUpTo(PV.seq);
  const tot=Object.values(src).reduce((a,b)=>a+b,0);
  const radius=pvKeep();
  const full='• ช่องที่มีบรรทัด '+Object.keys(src).length+'/'+20736+' · '+tot+' บรรทัด'
    +' · เพดานต่อช่อง '+PV.budget+(PV.over.length?' · แน่นเกินเพดาน '+PV.over.length+' ช่อง':'')
    // say what prune removed, and say it only about the LIVE plane: while
    // scrubbing back the numbers describe the present, not the frame drawn
    +(!PV.prune?' · 🧹 ตัดขยะ: ปิด (แผนที่ดิบ)'
      :(PV.seq!==null?' · 🧹 ตัดขยะ: เปิด'
        :(PV.pruned?' · 🧹 ตัด '+PV.pruned+' บรรทัด · ช่องที่หายไป '+PV.prunedCells:' · 🧹 ไม่มีขยะ')))
    +' · เส้นเชื่อม '+pvLiveBonds().length+' · รอยอ่าน '+PV.sights.length
    +(radius?(' · รัศมี '+PV.focus+' ทอด = '+radius.size+' ช่อง (บังที่เหลือ)'):'')
    +pvEdgeDisclosure()
    +(()=>{
      if(!PV.ctxOn)return '';
      const mem=Object.keys(PV.ctxCells).length;
      const shared=Object.keys(PV.ctxCells).filter(k=>PV.plane[k]).length;
      return ' · ความจำ '+mem+' ช่อง · ทับกับแผนที่ '+shared+' · เชื่อมข้ามระบบ '+PV.crossEdges.length+' เส้น';
    })();
  const who=(PV.colour==='writer'||PV.colour==='recent')
    ?(()=>{const owners=pvTopApps();return PV.metaOn
      ?(' · ข้อมูลผู้เขียน: '+(owners?Object.keys(owners).length:0)+' คน · ใหม่สุดที่บรรทัด '+Math.max(PV.cellSeqMax-1,0))
      :' · ข้อมูลผู้เขียน: ยังไม่โหลด'})()
    :(PV.colour==='kind'
    ?(()=>{const ks=pvTopKinds();return PV.metaOn
      ?(' · ข้อมูลชนิด: '+(ks?Object.keys(ks).length:0)+' ชนิด · ใหม่สุดที่บรรทัด '+Math.max(PV.cellSeqMax-1,0))
      :' · ข้อมูลชนิด: ยังไม่โหลด'})()
    :'');
  // One line on screen, everything else in the tooltip. This used to be a single
  // 166-CHARACTER line, which on a 709px screen took a row of its own: the
  // header measured 108px tall — three rows — for a caption. The numbers are not
  // less true for being short; they were just the loudest thing in a modal whose
  // job is to show a map.
  // A filter that is ON but matches nothing is a state the reader must be told
  // about, never one the map quietly answers by drawing the unfiltered plane.
  const f=pvFilt();
  // Counted over `src` — the cells actually on screen — not over the whole-log
  // map. Quoting PV.kinds here was right on the live plane and wrong the moment
  // time was scrubbed back: the picture fell to 1,421 bond lines while this
  // line still said 13,572. A number describing a different frame than the one
  // drawn is the exact failure this section exists to prevent, and the prune
  // figures a few lines above had already been fixed for the same reason ("say
  // it only about the LIVE plane") — the filter had not learned it.
  // The figures must describe THIS frame, and `src` means two different things:
  // on the LIVE plane it is the unfiltered map, so the filter is an intersection
  // with f. While scrubbing, pvCellsUpTo has ALREADY applied the filter, so src
  // is the answer — and summing the whole-log f[k] over it added bonds that had
  // not happened yet (the line said 7,938 while the picture held 1,421). Two
  // branches, because one rule cannot honestly describe both frames.
  const scrubbed=(PV.seq!==null&&PV.seq<PV.seqMax-1);
  const filt=PV.filt
    ?(!f?(' · กรอง: '+pvFiltLabel()+' — ยังไม่มีข้อมูล'+pvFiltWord()+'ในคลัง')
        :(()=>{const lit=scrubbed?Object.keys(src):Object.keys(src).filter(k=>f[k]);
          const n=scrubbed?Object.values(src).reduce((a,b)=>a+b,0)
                          :lit.reduce((a,k)=>a+f[k],0);
          return lit.length
            ?(' · กรอง: '+pvFiltLabel()+' · สว่าง '+lit.length.toLocaleString()
               +' ช่อง · '+n.toLocaleString()+' บรรทัดของ'+pvFiltWord())
            :(' · กรอง: '+pvFiltLabel()+' — ยังไม่มีข้อมูล'+pvFiltWord()+'ในมุมมองนี้');})())
    :'';
  const short='• '+Object.keys(src).length.toLocaleString()+'/20,736 ช่อง · '+tot.toLocaleString()+' บรรทัด'
    +filt
    +(!PV.prune?' · 🧹 ตัดขยะ: ปิด':(PV.pruned&&PV.seq===null?' · 🧹 ตัด '+PV.pruned.toLocaleString():''))
    +(radius?(' · รัศมี '+PV.focus+' ทอด = '+radius.size.toLocaleString()+' ช่อง'):'');
  const info=$('#plane-info');
  info.textContent=short;
  info.title=full+who;
  pvRelLegend();
  pvLegend();
}
function pvMode(){
  $('#pv-plane').classList.toggle('on',PV.mode==='plane');
  $('#pv-blocks').classList.toggle('on',PV.mode==='blocks');
  $('#pv-block').classList.toggle('on',PV.mode==='window');
  pvWinInfo();
  pvLensInfo();
  pvFocusBtns();
  // the frame-size picker must show the frame that is actually drawn: the pinch
  // and the glass both set PV.win, and a picker that disagrees is a control lying
  if(PV_WINS.indexOf(PV.win)>=0)$('#pv-win').value=String(PV.win);
  _legendKey=null;                         // a new view is a new legend
  pvDerive();pvDraw();pvInfo();pvPop();pvRibbon();   // popup+strip ride along
  pvFilterOptions();pvComposition();pvLensInfo();
}
// One place that moves time, so the slider, the arrow keys, the strip and the
// replay button can never disagree about where "now" is.
function pvSetSeq(S){
  clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ เล่นย้อน';
  const max=Math.max(PV.seqMax-1,0);
  PV.seq=Math.max(0,Math.min(max,S));
  if(PV.seq>=max)PV.seq=null;              // pinned at the end = the live plane
  $('#pv-seq').value=PV.seq===null?max:PV.seq;
  pvDerive();pvDraw();pvInfo();pvWhen();pvRibbon();
}
// the who/when layer is fetched the first time a colour mode needs it, not with
// the counts: a reader that only wants the map should not pay for the layer
async function loadMeta(){
  if(PV.metaOn)return;
  try{
    // the who/when layer must describe the SAME plane the counts do: with
    // prune off we ask for the unpruned layer, or a colour mode would
    // attribute a cell to a writer whose only line in it was noise
    const m=await fetch('/plane/meta'+(PV.prune?'':'?raw=1'),{headers:H}).then(r=>r.json());
    PV.cellApp=m.last_app||{};PV.cellSeq=m.last_seq||{};PV.cellKind=m.last_kind||{};
    PV.kinds=m.by_kind||{};PV.apps=m.by_app||{};
    PV.cellSeqMax=Math.max(m.seq_max||0,1);PV.metaOn=true;_legendKey=null;
  }catch(e){toast('โหลดข้อมูลผู้เขียน/ชนิด/เวลาไม่สำเร็จ — เลือกสีนั้นอีกครั้ง')}
}
function pvPNG(){
  const tick=PV.seq===null?Math.max(PV.seqMax-1,0):PV.seq;
  const a=document.createElement('a');
  a.download='rdh-plane-'+PV.mode+'-tick'+tick+'.png';
  a.href=pcv.toDataURL('image/png');a.click();
  toast('บันทึกภาพแล้ว: '+a.download);
}
async function loadPlane(){
  try{
    // /patterns rides along with the plane: 6.6 KB, and it is the read that
    // fills the composition strip, which used to need a button press before it
    // would admit what the corpus is made of
    const [d,s,b,t,c]=await Promise.all([
      fetch('/neighbors?rkey=0&level=2'+(PV.prune?'':'&raw=1'),{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()}),
      fetch('/pointers',{headers:H}).then(r=>r.json()),
      fetch('/bonds?fields=plane',{headers:H}).then(r=>r.json()),
      fetch('/plane/timeline',{headers:H}).then(r=>r.json()),
      fetch('/patterns',{headers:H}).then(r=>r.json()).catch(()=>null)
    ]);
    if(c)PV.census=c;
    PV.plane=d.fine_cells||{};PV.budget=d.budget||64;
    PV.pruned=d.pruned||0;PV.prunedCells=d.pruned_cells||0;
    PV.sights=s.pointers||[];
    PV.timeline=t.lines||[];PV.seqMax=t.count||0;
    // the who/when layer changes with the log, so re-read it only when a mode
    // is actually drawing from it (never for the plain count map)
    if(PV.colour==='writer'||PV.colour==='recent'||PV.colour==='kind'){PV.metaOn=false;await loadMeta()}
    // place each edge on the plane and stamp it with the tick of its own line,
    // so scrubbing back in time hides edges that had not been written yet
    const seqOf={};PV.timeline.forEach(r=>{seqOf[r[4]]=r[0]});
    PV.bonds=(b.edges||[]).map(e=>({...e,seq:seqOf[e.hash.slice(0,8)]}));
    const sl=$('#pv-seq');sl.max=Math.max(PV.seqMax-1,0);
    if(PV.seq===null)sl.value=sl.max;
    if(PV.ctxOn)await loadCtx();
    pvMode();pvWhen();
  }catch(e){$('#plane-info').textContent='โหลดระนาบไม่ได้ ('+e+')'}
}
async function loadCtx(){
  if(!PV.ctxOn){PV.ctxCells={};PV.crossEdges=[];return}
  try{
    const [m,l]=await Promise.all([
      fetch('/ctx/at?rkey=0&level=2',{headers:H}).then(r=>r.json()),
      fetch('/ctx/links',{headers:H}).then(r=>r.json())
    ]);
    PV.ctxCells=m.fine_cells||{};
    const seqOf={};PV.timeline.forEach(r=>{seqOf[r[4]]=r[0]});
    // a lift happens at flush time, but the edge exists as soon as its line does
    PV.crossEdges=(l.links||[]).map(e=>({...e,seq:seqOf[e.src8]}));
  }catch(e){toast('โหลดความจำกลางไม่สำเร็จ — แสดงเฉพาะแผนที่');PV.ctxCells={};PV.crossEdges=[]}
}
function pvWhen(){
  const S=PV.seq===null?PV.seqMax-1:PV.seq;
  const row=PV.timeline[S];
  $('#pv-when').textContent=(PV.seq===null?'ทุกบรรทัด':('บรรทัดที่ '+S+'/'+Math.max(PV.seqMax-1,0)))
    +(row?(' · '+fmt(row[1])+' · '+row[3]):'');
}
async function pvZoomBlock(rk,size){
  const d=await fetch('/neighbors?rkey='+rk+'&level=1',{headers:H}).then(r=>r.json());
  PV.origin=d.origin;PV.mode='window';PV.win=size||PV.win||12;pvMode();
  const keys=Object.keys(d.fine_cells).map(Number);
  const busiest=keys.length?keys.reduce((a,b)=>d.fine_cells[a]>=d.fine_cells[b]?a:b):d.base;
  openPoint(busiest);
}
async function openPoint(rk,path){
  PV.sel=rk;PV.path=path||[];
  pvFocusBtns();          // now there IS a centre, so the radius buttons wake up
  // Clicking a cell is a deliberate READ, so it declares itself: that is the
  // only honest source of a sighting (the system must be able to say "someone
  // looked here", and a person clicking is the someone). The server makes it
  // idempotent, so re-opening the same cell writes nothing new.
  const as=identity==='admin'?'':'&as='+encodeURIComponent(identity);
  const q='/neighbors?rkey='+rk+'&level=0&record=1'+as+(PV.path.length?'&path='+PV.path.join(','):'');
  // The cell read and its walk are two questions about ONE click, so they go
  // together: opening a point should not cost two round trips before the panel
  // can say what shape the address has. A walk that fails must not lose the
  // read, hence the catch — the cell is the answer, the shape is a bonus.
  const [d,w]=await Promise.all([
    fetch(q,{headers:H}).then(r=>r.json()),
    fetch('/explore?rkey='+rk+'&hops=2',{headers:H}).then(r=>r.json()).catch(()=>null)
  ]);
  PV.lines=d;PV.stacked=d.stacked||null;PV.walk=w;
  PV.walkSel=null;PV.trail=[];PV.hops=[];PV.showCensus=false;
  if(d.sighting_written){
    const s=await fetch('/pointers',{headers:H}).then(r=>r.json());
    PV.sights=s.pointers||[];                    // the circle appears now, not next poll
  }
  // a successful read is the ONE thing allowed to want the popup: every other
  // pvPop caller only re-places an already-open one (see the gate in pvPop)
  PV.popWant=true;
  pvDetail();pvDraw();pvPop();
}
// The shape of a node from its two degrees. Kept here as well as on the server
// because a HOP carries its degrees already — asking the server to re-derive a
// shape it just told us would be a round trip for arithmetic.
function pvShape(o,i){return (!o&&!i)?'○':!o?'◀':!i?'▶':'⇄'}
function pvShapeName(s){return s==='sink'?'ปลายน้ำ':s==='source'?'ต้นน้ำ':s==='chain'?'กลางสาย':s==='edge'?'เส้นเชื่อม':'เกาะ'}
function pvCrumbs(){
  if(!PV.trail.length&&PV.walkSel==null)return '';
  let c='<div class="crumbs">';
  if(PV.lines)c+='<span class="crumb'+(PV.walkSel==null?' here':' x')+'" data-crumb="-1">▣ ช่อง '
    +PV.lines.point+'</span>';
  PV.trail.forEach((t,i)=>{
    const last=i===PV.trail.length-1&&PV.walkSel===t.hash;
    c+='<span class="crumb'+(last?' here':'')+'" data-crumb="'+i+'" title="'+esc(t.text||t.hash8)+'">'
      +(i?'→ ':'')+pvShape(t.out||0,t.in||0)+' '+esc(t.hash8)+'</span>';
  });
  c+='</div>';
  return c;
}
// One hop node row. A node is ONE line with TWO reasons to be here (a rel on
// each edge), so the rel shown is the first and the rest are counted — a node
// listed twice would read as two neighbours.
function pvHopRows(level){
  return level.nodes.map(n=>{
    const i=PV.hops.push(n)-1;
    return '<div class="hop" data-hop="'+i+'" title="'+esc(n.text||'')+'">'
      +'<span class="shape" style="padding:1px 6px">'+pvShape(n.out,n.in)+'</span>'
      +'<span style="flex:1;min-width:0"><span class="badge">'+esc(n.app||'?')+'</span> '
      +'<span class="relchip">'+esc(n.rel)+'</span>'
      +'<div class="tx">'+esc((n.text||'').slice(0,180))+'</div></span>'
      +'<span class="deg" title="มี '+n.in+' เส้นชี้เข้ามา · นำไป '+n.out+' เส้น">'+n.in+'↓ '+n.out+'↑</span></div>';
  }).join('');
}
function pvHopSection(title,hint,levels,tone){
  const tot=levels.reduce((a,l)=>a+l.total,0);
  if(!tot)return '';
  let s='<div class="hopsec"><div class="hophead" style="color:'+tone+'">'+title
    +' <span class="n">'+tot+'</span></div><div class="wyhy">'+hint+'</div>';
  levels.forEach(l=>{
    if(!l.total)return;
    s+='<div class="hoplevel">ทอดที่ '+l.depth+' · '+l.total+' บรรทัด'
      +(l.nodes.length<l.total?' (แสดง '+l.nodes.length+')':'')+'</div>'+pvHopRows(l);
  });
  return s+'</div>';
}
// The census: proportions have to be SEEN. "3,001 islands" beside "13,572
// edges" reads as two comparable numbers, and they are not.
/* ── the composition strip ────────────────────────────────────────────
   "What is this corpus MADE OF", asked without opening anything.

   It used to be answerable only from inside the census button, behind a
   fetch, in a panel that REPLACED the cell detail — so the one question a
   reader opens the plane with ("out of 32,705 lines, what is this?") was
   the hardest one to ask. /patterns is 6.6 KB with the kind axis added,
   which is cheap enough to fetch WITH the plane, so this is simply always
   there.

   Four axes, one row shape, so they can be compared by eye. Two of them —
   kind and writer — are the two the plane already carries per line in the
   log's own order, so clicking a row filters. The other two are not, and
   the row says so rather than pretending: a filter the scrub cannot honour
   is a frame that lies about itself, which is what §20.2 is about. */
const PV_CMP=[
  {dim:'kind', label:'ชนิด', get:c=>c.kinds, lab:kindLabel},
  {dim:'app', label:'ผู้เขียน', get:c=>(c.by_app&&Object.fromEntries(Object.entries(c.by_app).map(([k,v])=>[k,v.lines]))), lab:appLabel},
  {dim:null, label:'ความสัมพันธ์', get:c=>c.rels, lab:r=>r},
  {dim:null, label:'รูปของกราฟ', get:c=>c.shapes, lab:(s,c)=>((c&&c.shape_meta&&c.shape_meta[s])?c.shape_meta[s].label:s)},
];
function pvComposition(){
  const box=_pvCompBox;if(!box)return;
  const c=PV.census;
  if(!c){box.innerHTML='<div style="color:var(--dim);font:10px var(--mono)">องค์ประกอบ: กำลังโหลด…</div>';return}
  const a=PV_CMP[PV.cmp||0],rows=a.get(c)||{};
  const entries=Object.keys(rows).sort((x,y)=>rows[y]-rows[x]||String(x).localeCompare(String(y)));
  const tot=entries.reduce((s,k)=>s+rows[k],0)||1;
  let h='<div style="display:flex;align-items:center;gap:4px;margin-bottom:5px">'
    +'<span style="color:var(--fg);font-size:11px">องค์ประกอบ</span>'
    +'<span style="color:var(--dim);font:9px var(--mono)">รวม '+tot.toLocaleString()+'</span></div>'
    +'<div style="display:flex;gap:3px;margin-bottom:5px">';
  PV_CMP.forEach((ax,i)=>{
    h+='<button class="pbtn" data-cmp="'+i+'" style="font:9px var(--mono);padding:2px 5px;'
      +(i===(PV.cmp||0)?'box-shadow:inset 0 0 0 1px var(--acc2);':'')+'">'+esc(ax.label)+'</button>';
  });
  h+='</div>';
  entries.forEach(k=>{
    const pct=100*rows[k]/tot,on=PV.filt&&PV.filt.dim===a.dim&&PV.filt.key===k;
    const clickable=!!a.dim;
    h+='<div class="cenrow" data-cmp-dim="'+(a.dim||'')+'" data-cmp-key="'+esc(k)+'"'
      +' style="grid-template-columns:84px 1fr 58px;'+(clickable?'cursor:pointer;':'')+(on?'background:var(--panel2);border-radius:6px;':'')+'"'
      +(clickable?'':' title="แกนนี้ไม่ใช่คุณสมบัติของแต่ละบรรทัดตามลำดับเวลา จึงกรองเฟรมที่ลากย้อนไม่ได้ — ดูอย่างเดียว"')+'>'
      +'<span style="font-size:10px;overflow:hidden;text-overflow:ellipsis'+(on?';color:var(--acc2);font-weight:600':'')+'">'+esc(a.lab(k,c))+'</span>'
      +'<span class="cenbar"><i style="width:'+Math.max(1.5,pct).toFixed(1)+'%"></i></span>'
      +'<span class="cennum">'+rows[k].toLocaleString()+' · '+(pct>=1?pct.toFixed(0):'<1')+'%</span></div>';
  });
  h+='<div style="color:var(--dim);font:9px var(--mono);margin-top:4px">'
    +(PV_CMP.some(x=>x.dim)?'คลิกแถวของชนิดหรือผู้เขียนเพื่อกรองแผนที่':'อ่านอย่างเดียว')
    +'</div>';
  const same=box._cmpHtml===h;
  if(!same){box.innerHTML=h;box._cmpHtml=h;}
  return same;
}

function pvCensusHTML(){
  const c=PV.census;
  if(!c)return '<div>…</div>';
  const tot=c.lines||1,meta=c.shape_meta||{};
  let h='<div style="color:var(--fg);font-size:12px">📊 แพทเทินของแผนที่ทั้งใบ</div>'
    +'<div class="wyhy">'+c.lines+' บรรทัด · '+c.edges+' เส้น · ข้าม platform '+c.cross+'</div><div class="cen">';
  Object.keys(c.shapes).forEach(s=>{
    const v=c.shapes[s],m=meta[s]||{},pct=(100*v/tot);
    h+='<div class="cenrow" title="'+esc(m.why||'')+'"><span style="font-size:12px">'
      +(m.icon||'?')+'</span><span>'+esc(m.label||s)+'</span>'
      +'<span class="cenbar"><i style="width:'+Math.max(1,pct).toFixed(1)+'%"></i></span>'
      +'<span class="cennum">'+(pct>=1?pct.toFixed(0):'<1')+'%</span></div>';
  });
  h+='</div><div class="hopsec"><div class="hophead">แยกตามคนเขียน</div>'
    +'<div class="wyhy">เส้น = จำนวนเส้นเชื่อมที่แอปนั้นเขียน · เกาะ = ไม่มีเส้นเลย</div>';
  Object.keys(c.by_app).forEach(a=>{
    const v=c.by_app[a];
    h+='<div style="display:flex;gap:6px;margin:3px 0"><span class="badge">'+esc(a)+'</span>'
      +'<span class="cennum" style="flex:1;text-align:left">'+v.lines+' บรรทัด · '+v.edges
      +' เส้น · '+v.isolated+' เกาะ ('+(v.lines?Math.round(100*v.isolated/v.lines):0)+'%)</span></div>';
  });
  h+='</div><div class="hopsec"><div class="hophead">ชุมทาง — บรรทัดที่มีคนอ้างถึงมากสุด</div>'
    +'<div class="wyhy">in ตั้งแต่ '+c.hub_at+' ขึ้นไป · ทั้งระนาบมีไม่กี่บรรทัดที่คนอื่นเกาะอยู่</div>';
  c.hubs.forEach(x=>{
    const i=PV.hops.push({hash:x.hash,hash8:x.hash8,rkey:x.rkey,app:x.app,text:x.text,out:x.out,in:x.in})-1;
    h+='<div class="cenhub" data-hop="'+i+'" title="'+esc(x.text||'')+'">'
      +'<span class="deg" style="color:var(--warn)">'+x.in+'↓</span>'
      +'<span style="flex:1;min-width:0"><span class="badge">'+esc(x.app||'?')+'</span> '
      +'<div class="tx">'+esc((x.text||'').slice(0,140))+'</div></span></div>';
  });
  h+='</div><div class="hopsec"><div class="hophead">คำกริยาที่ข้อมูลเขียนไว้</div>';
  Object.keys(c.rels).forEach(r=>{h+='<div style="margin:2px 0"><span class="relchip">'+esc(r)
    +'</span> <span class="cennum" style="text-align:left">'+c.rels[r]+'</span></div>'});
  return h+'</div>';
}
function pvDetail(){
  const d=PV.lines,el=$('#plane-detail');
  if(!d){el.textContent='…';pvAdoptLegends(el);return}
  PV.hops=[];
  if(PV.showCensus){el.innerHTML=pvCensusHTML();pvAdoptLegends(el);pvWireDetail();return}
  let h=pvCrumbs();
  if(PV.walkSel!=null&&PV.walk&&!PV.walk.gone){
    const f=PV.walk.shape||{};
    h+='<div><span class="shape"><b style="color:var(--warn)">'+(f.icon||'?')+'</b> '
      +esc(f.label||'?')+'</span> <span class="badge">'+esc(f.kind||'')+'</span> '
      +'<span class="badge">'+esc(f.app||'?')+'</span></div>'
      +'<div class="swhy">'+esc(f.why||'')+'</div>'
      +'<div style="margin:5px 0">← มี '+f.in+' เส้นชี้เข้ามา · นำไป '+f.out+' เส้น'
      +(f.cross?' · ข้าม platform '+f.cross:'')
      +(f.hub?' · <b style="color:var(--warn)">ชุมทาง</b>':'')+'</div>'
      +'<div class="pline" style="cursor:default"><div style="white-space:pre-wrap">'
      +esc((f.text||'').slice(0,400))+'</div></div>'
      +'<button class="pbtn" id="pv-clrc">✕ ออกจากเส้น กลับไปที่เซลล์</button>';
    if(PV.walk.out&&PV.walk.out.length)
      h+=pvHopSection('→ ต่อไปจากบรรทัดนี้','สิ่งที่มีคนเขียนไว้ว่าต่อจากมัน',PV.walk.out,'var(--acc2)');
    if(PV.walk.in&&PV.walk.in.length)
      h+=pvHopSection('← ย้อนกลับมาที่บรรทัดนี้','มีอะไรอ้างถึงมัน และอ้างด้วยคำกริยาอะไร',PV.walk.in,'var(--warn)');
  }else{
    const w=PV.walk||{};
    h+='<div style="color:var(--fg);font-size:12px">point <b style="color:var(--warn)">'+d.point
      +'</b> · '+d.count+' line(s)</div>'
      +'<div style="margin:4px 0">(x '+(d.point%144)+', y '+Math.floor(d.point/144)+') บนระนาบ'
      +(PV.path.length?' · path '+PV.path.map((v,i)=>'ชั้น '+(i+1)+'='+v).join(' → '):'')+'</div>';
    if(w.count){h+='<div><span class="shape"><b style="color:var(--warn)">'+w.icon+'</b> '
      +esc(w.label)+'</span></div><div class="swhy">'+esc(w.why)+'</div>'}
    if(PV.path.length)h+='<button class="pbtn" id="pv-up">↑ ขึ้นชั้น</button>';
    if(w.out&&w.out.length)
      h+=pvHopSection('→ ต่อไปจากที่อยู่นี้','ทุกบรรทัดในเซลล์นี้รวมกัน — ออกไปที่ไหน',w.out,'var(--acc2)');
    if(w.in&&w.in.length)
      h+=pvHopSection('← ย้อนกลับมาที่ที่อยู่นี้','อะไรอ้างถึงบรรทัดที่อยู่ในเซลล์นี้',w.in,'var(--warn)');
  }
  if(PV.stacked){
    const cells=PV.stacked.sub_cells,keys=Object.keys(cells).map(Number);
    const max=keys.length?Math.max(...keys.map(k=>cells[k])):1;
    h+='<div style="color:var(--warn);margin-top:8px">แน่นเกิน budget '+PV.stacked.budget
      +' — แตกเป็น sub-map ชั้น '+PV.stacked.depth+' (address ล้วน)'
      +(PV.stacked.still_over?' · ยังมี '+PV.stacked.still_over+' ช่องที่ต้องลงต่อ':'')+'</div>'
      +'<div id="subgrid">';
    for(let i=0;i<144;i++){
      const v=cells[i]||0,bg=v?'rgba(126,231,135,'+(0.25+0.75*(v/max)).toFixed(2)+')':'var(--panel2)';
      h+='<div data-sub="'+i+'" title="ช่องย่อย '+i+' · '+v+' บรรทัด" style="background:'+bg+'"></div>';
    }
    h+='</div>';
  }
  const mine=pvGraphEdges().filter(b=>b.src_rkey===PV.sel||b.dst_rkey===PV.sel);
  if(mine.length){
    h+='<div style="color:var(--acc2);margin-top:8px">edges ที่แตะจุดนี้ ('+mine.length+')</div>'
      +mine.map(b=>{
      const out=b.src_rkey===PV.sel,other=out?(b.dst8||('#row'+b.dst_rowid)):b.src8;
      const ork=out?b.dst_rkey:b.src_rkey,lift=b.kind==='lifted';
      return '<div class="pline" style="border-color:'+(lift?'#bc8cff':'var(--acc2)')+'"><span class="badge">'+esc(b.app||'?')
        +'</span> '+(out?'→':'←')+' <b style="color:'+pvRelInk(b)+'">'+esc(b.rel||'lifted')+'</b> '
        +'<span class="ph" data-goto="'+(ork==null?'':ork)+'" title="เปิดเซลล์ปลายทาง">'
        +other+(ork==null?' (ไม่เห็นปลายทาง)'+(lift?' · row '+b.dst_rowid:''):'')+'</span></div>';
    }).join('');
  }
  const deg={};(PV.walk&&PV.walk.lines||[]).forEach(l=>{deg[l.hash]=l});
  const ls=d.lines||[];
  h+=ls.length?'<div class="hoplevel" style="margin-top:9px">บรรทัดในเซลล์นี้ — กดที่บรรทัดเพื่อเดินตามมัน</div>'
    +ls.slice(0,40).map(m=>{
      const g=deg[m.hash],o=g?g.out:0,i=g?g.in:0;
      const bs=(m.kind==='bond')?bondShort(m.text)
              :(m.kind==='pointer')?pointerShort(m.text):null;
      const body=bs?bs.html:esc((m.text||'').slice(0,240));
      const btitle=bs?(' title="'+esc(bs.title||'')+'"'):'';
      return '<div class="pline"><span class="badge">'+esc(m.app||'?')
      +'</span> <span class="badge kind">'+esc(m.kind||'')+'</span> '
      +'<span class="shape" style="padding:1px 6px" title="มี '+i+' เส้นชี้เข้ามา · นำไป '+o+' เส้น">'+pvShape(o,i)+'</span> '
      +'<span class="ph" data-copy="'+(m.hash||'')+'">'+hash8(m.hash)+'</span>'
      +'<button class="pbtn" style="padding:0 6px;font-size:10px" data-walk="'+esc(m.hash||'')+'" title="🧭 เดินตามบรรทัดนี้ — ดูว่ามันไปเกี่ยวกับอะไร และอะไรอ้างถึงมัน">🧭</button>'
      +'<div style="white-space:pre-wrap;margin-top:3px"'+btitle+'>'+body+'</div></div>'
    }).join('')
    :'<div style="margin-top:6px">เซลล์นี้ว่าง — ไม่มี line อยู่ตรงนี้</div>';
  el.innerHTML=h;
  pvAdoptLegends(el);
  pvWireDetail();
}
// Wires what the render emitted. Handlers are attached here rather than inlined
// so a line's TEXT never has to be escaped into an onclick attribute — the
// text is data, and data must not become code.
function pvWireDetail(){
  const el=$('#plane-detail');
  el.querySelectorAll('#subgrid div').forEach(node=>{
    node.onclick=()=>openPoint(PV.sel,PV.path.concat([+node.dataset.sub]));
  });
  el.querySelectorAll('[data-goto]').forEach(n=>{n.onclick=()=>{if(n.dataset.goto)openPoint(+n.dataset.goto)}});
  el.querySelectorAll('[data-copy]').forEach(n=>{
    n.onclick=()=>{navigator.clipboard.writeText(n.dataset.copy);toast('คัดลอกแล้ว · '+hash8(n.dataset.copy))}});
  el.querySelectorAll('[data-walk]').forEach(n=>{n.onclick=()=>pvWalkLine(n.dataset.walk)});
  el.querySelectorAll('[data-hop]').forEach(n=>{n.onclick=()=>pvWalkHop(+n.dataset.hop)});
  el.querySelectorAll('[data-crumb]').forEach(n=>{n.onclick=()=>pvCrumb(+n.dataset.crumb)});
  const up=$('#pv-up');if(up)up.onclick=()=>openPoint(PV.sel,PV.path.slice(0,-1));
  const cl=$('#pv-clrc');if(cl)cl.onclick=()=>{PV.walkSel=null;PV.walk=null;PV.trail=[];openPoint(PV.sel,PV.path)};
}
function pvWalkHop(i){
  const n=(PV.hops||[])[i];
  if(!n)return;
  // walking to a node also MOVES the plane there: the shape badge is a claim
  // about an address, and leaving the map behind would let panel and plane
  // disagree about where the reader is
  if(n.rkey!=null){PV.sel=n.rkey;PV.path=[]}
  pvWalkLine(n.hash);
}
function pvCrumb(i){
  if(i<0){PV.walkSel=null;PV.walk=null;PV.trail=[];openPoint(PV.sel,PV.path);return}
  PV.trail=PV.trail.slice(0,i+1);
  PV.walkSel=PV.trail[PV.trail.length-1].hash;
  pvWalkLine(PV.walkSel,true);
}
async function pvWalkLine(h,sameTrail){
  if(!h)return;
  try{
    const w=await fetch('/explore?hash='+h+'&hops=2',{headers:H}).then(r=>r.json());
    if(w.gone){toast('บรรทัดนี้ถูกตัดขยะไปแล้ว — ไม่มีที่อยู่ให้เดินต่อ');return}
    PV.walk=w;PV.walkSel=h;
    if(!sameTrail){
      const s=w.shape||{};
      PV.trail.push({hash:h,hash8:w.hash8,text:s.text,app:s.app,rkey:s.rkey,
                     out:s.out,in:s.in});
      if(PV.trail.length>12)PV.trail.shift();      // a trail is a path, not a log
    }
    pvDetail();pvDraw();
  }catch(e){toast('เปิดเส้นทางของบรรทัดนี้ไม่สำเร็จ — ลองอีกครั้ง')}
}
async function pvCensus(){
  try{
    // already in hand from the plane load — refetching 6.6 KB because someone
    // pressed a button is the kind of thing that makes an opener slow
    if(!PV.census)PV.census=await fetch('/patterns',{headers:H}).then(r=>r.json());
    PV.showCensus=true;PV.walkSel=null;PV.trail=[];pvDetail();
  }catch(e){toast('โหลดแพทเทินไม่สำเร็จ — ลองอีกครั้ง')}
}
pcv.onmousemove=ev=>{
  // a drag owns the pointer: the tooltip mid-pan is noise over a moving map,
  // and its per-frame layout reads (offsetWidth) fight the redraw
  if(PV.panDrag||PV.lens.drag)return;
  const tip=$('#plane-tip'),hit=pvCellAt(ev);
  if(!hit){pvTopHide(tip);return}
  const [x,y,rk]=hit,n=pvCountAt(x,y,rk);
  // once the map can pan, the screen square is not the address: name the BLOCK
  const bx=PV.mode==='blocks'?pvB(PV.bOrig.x+x):x;
  const by=PV.mode==='blocks'?pvB(PV.bOrig.y+y):y;
  const who=PV.sights.filter(s=>s.point===rk).map(s=>s.app).join(',');
  // the TOTAL and the FILTERED count, both, labelled. Printing only the
  // filtered one would make a cell holding 34 lines read 0 with nothing on
  // screen to contradict it; printing only the total would make the filter
  // look like it did nothing.
  const kn=pvFiltN(rk),f=pvFilt();
  tip.textContent=(PV.mode==='blocks'?'ช่องย่อ ':'ที่อยู่ ')+rk+' (คอลัมน์ '+bx+', แถว '+by+') · '+n+' บรรทัด'
    +(f?(' · '+pvFiltLabel()+' '+kn+' ('+(kn?'เหลือ '+(n-kn)+' อีก':(PV.filt.dim==='app'?'ไม่มีผู้เขียนคนนี้ในช่องนี้':'ไม่มีชนิดนี้ในช่องนี้'))+')'):'')
    +(who?' · 👁 '+who:'');
  pvTopOpen(tip);
  const vw=window.innerWidth||document.documentElement.clientWidth;
  const vh=window.innerHeight||document.documentElement.clientHeight;
  const tw=tip.offsetWidth,th=tip.offsetHeight;
  tip.style.left=Math.max(6,Math.min(ev.clientX+12,vw-tw-6))+'px';
  tip.style.top=Math.max(6,Math.min(ev.clientY+12,vh-th-6))+'px';
};
pcv.onmouseleave=()=>pvTopHide($('#plane-tip'));
pcv.onclick=ev=>{
  if(PV.panSwallow&&Date.now()-PV.panSwallow<250)return;   // the pan, not a read
  const hit=pvCellAt(ev);
  if(!hit){PV.popWant=false;pvTopHide($('#plane-pop'));return}
  const [x,y,rk]=hit;
  openPoint(rk);
};
pcv.ondblclick=ev=>{
  const hit=pvCellAt(ev);if(!hit)return;
  const [x,y,rk]=hit;
  if(PV.mode==='blocks'){pvZoomBlock(rk)}
  else if(PV.mode==='plane')pvZoomBlock(rk);
};
$('#open-plane').onclick=()=>{
  $('#plane-modal').classList.add('open');
  // on a phone a 144x144 plane is 3px per cell — open on the 12x12 block map
  // instead, where a cell is big enough to hit, and let the caller zoom in
  if(window.innerWidth<900&&PV.mode==='plane')PV.mode='blocks';
  loadPlane();
  PV.timer=setInterval(loadPlane,8000);           // watch it breathe
};
function pvClose(){
  $('#plane-modal').classList.remove('open');
  PV.popWant=false;pvTopHide($('#plane-pop'));
  pvTopHide($('#plane-tip'));
  if(PV.timer){clearInterval(PV.timer);PV.timer=null}
}
// The popup is anchored to a POINT ON THE CANVAS, not to the box, so a scroll or
// a resize moves what it points at. Re-place it rather than letting it drift —
// the old code placed it once and never again, so a resize left it behind.
$('#plane-canvaswrap').addEventListener('scroll',()=>{
  if($('#plane-pop').matches(':popover-open'))pvPop();
  pvTopHide($('#plane-tip'));
},{passive:true});
$('#plane-close').onclick=pvClose;
$('#plane-modal').onclick=e=>{if(e.target.id==='plane-modal')pvClose()};
// Escape peels the overlays topmost-first. The plane modal is z-50 and the
// drawer is z-40, so the order is not a preference — it is the stacking. The
// drawer had no keyboard dismissal at all: Escape closed the modal and then
// did nothing, leaving a scrim that could only be got rid of by tapping it.
document.addEventListener('mousedown',e=>{
  const pop=$('#plane-pop');
  if(!pop||!pop.matches(':popover-open'))return;
  if(e.target===pop||pop.contains(e.target))return;
  if(e.target===pcv)return;
  PV.popWant=false;pvTopHide(pop);
});
document.addEventListener('keydown',e=>{
  if(e.key!=='Escape')return;
  if($('#plane-modal').classList.contains('open')){pvClose();return}
  if($('#side').classList.contains('open'))sideOpen(false);
});
$('#pv-plane').onclick=()=>{PV.mode='plane';pvMode()};
$('#pv-blocks').onclick=()=>{PV.mode='blocks';pvMode()};
$('#pv-block').onclick=()=>{
  // entering the window needs an origin, and the honest one is the point the
  // reader is already at — a window has to be opened SOMEWHERE
  if(PV.mode==='window'&&PV.origin){pvMode();return}
  const here=(PV.sel!=null)?PV.sel:busiestCell();
  if(here==null){toast('ยังไม่มีข้อมูลบนแผนที่');return}
  PV.win=PV.win||12;
  pvZoomBlock(here,PV.win);
};
$('#pv-win').value=String(PV.win);
$('#pv-win').onchange=e=>{
  PV.win=+e.target.value;
  if(PV.mode==='window')pvMode();
  else $('#pv-block').click();       // choosing a size IS asking for the window
};
function busiestCell(){
  const ks=Object.keys(PV.plane);
  if(!ks.length)return null;
  return +ks.reduce((a,b)=>PV.plane[a]>=PV.plane[b]?a:b);
}
// drag to pan. The pointer only moves the FRAME; every address it shows stays
// exactly where it was, which is the point of a viewport over immutable data.
pcv.style.touchAction='none';
function pvLensRead(){
  // the crosshair is the address the glass is ON, so that is the read
  const w=pvWrap(Math.floor(PV.lens.wedge)),r=pvWrap(Math.floor(PV.lens.ring));
  PV.lens.wedge=w;PV.lens.ring=r;
  openPoint(r*144+w);
}
/* ── ✋ pinch ────────────────────────────────────────────────────────────
   A phone has no wheel, and what a map gesture means is "how much of it am I
   looking at" — so a pinch walks the EXTENT ladder: the whole plane (either
   rendering of it), then a frame of 72, 48, 36, 24 or 12 cells. Those are the
   same five sizes the frame picker offers and nothing in between, because an
   extent with no name is an extent you cannot come back to. It stops where the
   frame stops: outward at the whole plane, since a picture smaller than the
   screen would be all frame and no plane; inward at 12x12, where the glass
   takes over — a frame can go no finer than one cell per square. */
function pvZoomStep(dir,ev){
  if(PV.mode==='plane'){
    if(dir<0){toast('ทั้งระนาบอยู่ในจอแล้ว — ย่อออกกว่านี้ไม่มีอะไรอยู่ข้างนอก');return}
    const hit=ev?pvCellAt(ev):null;
    const here=hit?hit[2]:(PV.sel!=null?PV.sel:busiestCell());
    if(here==null){toast('ยังไม่มีข้อมูลบนแผนที่');return}
    if(PV_WINS.indexOf(PV.win)<0)PV.win=48;
    pvZoomBlock(here,PV.win);          // the ▢ button's own move, at the fingers
    toast('▢ กรอบ '+PV.win+'×'+PV.win+' · ลากเพื่อเลื่อน');
    return;
  }
  if(PV.mode==='blocks'){
    if(dir<0){PV.mode='plane';pvMode();toast('▦ เห็นทั้งระนาบแล้ว — ทุกบล็อกอยู่ในจอ');return}
    const hit=ev?pvCellAt(ev):null;
    if(!hit){toast('บีบนิ้วบนบล็อกที่อยากดูใกล้ ๆ');return}
    pvZoomBlock(hit[2],12);            // one block IS 12x12 cells
    return;
  }
  const i=PV_WINS.indexOf(PV.win);
  if(dir>0){
    if(i<=0){toast('กรอบ 12×12 เล็กสุดของกรอบแล้ว — ละเอียดกว่านี้ใช้ 🔍 แว่นขยาย');return}
    PV.win=PV_WINS[i-1];
  }else{
    if(i<0||i>=PV_WINS.length-1){      // past the widest frame: back to the whole plane
      PV.mode='blocks';pvMode();
      toast('▩ ย่อออกสุดกรอบแล้ว — กลับไปผังบล็อกทั้งระนาบ');return;
    }
    PV.win=PV_WINS[i+1];
  }
  pvMode();toast('กรอบ '+PV.win+'×'+PV.win+' · ลากเพื่อเลื่อน');
}
/* The two-finger bookkeeping. A gesture is a POINTER GROUP, so the pointers in
   it have to be counted — and the first finger is already down when the second
   lands, which is why the drag it started has to be dropped explicitly: one
   pointer that is both panning and pinching moves the frame twice per frame. */
const pvPts=new Map();let pvPinch=null;
function pvPinchDist(){const a=[...pvPts.values()];
  return a.length<2?0:Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y)}
pcv.onpointerdown=ev=>{
  pvPts.set(ev.pointerId,{x:ev.clientX,y:ev.clientY});
  // a new drag starts its ride from zero: a stale _dw from the previous drag
  // would let the first move take the ride-only path and skip its origin step
  pvPanRideClear();
  if(pvPts.size>=2){
    PV.panDrag=null;PV.lens.drag=null;
    PV.lens.moved=true;                       // a pinch is not a tap on the glass
    pvPinch={anchor:pvPinchDist()};           // ...nor on the cell under it
    PV.panSwallow=Date.now();
    PV.popWant=false;pvTopHide($('#plane-pop'));
    return;
  }
  const rect=pcv.getBoundingClientRect();
  // Grabbing the GLASS moves the glass; grabbing the canvas outside it moves
  // the FRAME under it. Two different things to hold, told apart by which one
  // the pointer is on — and the second is what makes every address reachable
  // once the glass has a fixed radius it cannot get closer than.
  if(pvLensHit(ev.clientX-rect.left,ev.clientY-rect.top)){
    PV.lens.drag={x:ev.clientX,y:ev.clientY,w:PV.lens.wedge,r:PV.lens.ring};
    PV.lens.moved=false;
    pcv.setPointerCapture(ev.pointerId);
    return;
  }
  // the block map pans too — the same drag, the same swallow, its own pair of
  // coordinates, because its frame is measured in blocks rather than in cells
  if(PV.mode==='blocks'){
    PV.panDrag={x:ev.clientX,y:ev.clientY,bx:PV.bOrig.x,by:PV.bOrig.y,blocks:true};
    pvPopRideGrab(PV.panDrag);
    pcv.setPointerCapture(ev.pointerId);
    return;
  }
  if(PV.mode!=='window')return;
  PV.panDrag={x:ev.clientX,y:ev.clientY,ox:PV.origin.wedge,oy:PV.origin.ring};
  pvPopRideGrab(PV.panDrag);
  pcv.setPointerCapture(ev.pointerId);
};
pcv.onpointermove=ev=>{
  // A popup is anchored to the CELL the reader picked; the frame moves UNDER
  // that cell when the map pans, so wherever the cell goes, the popup goes.
  // Hiding it instead would read as "the thing I opened vanished" — the anchor
  // following the drag is the honest answer (this is the same reason pvPop
  // re-places itself on scroll/resize).
  const anchored=PV.sel!=null&&PV.panDrag&&$('#plane-pop').matches(':popover-open');
  if(pvPts.has(ev.pointerId))pvPts.set(ev.pointerId,{x:ev.clientX,y:ev.clientY});
  if(pvPts.size>=2){
    const d=pvPinchDist();
    if(!pvPinch)pvPinch={anchor:d};
    // the anchor moves WITH each step, so a long pinch walks several rungs of
    // the ladder instead of zooming once and then doing nothing at all
    if(pvPinch.anchor>4){
      if(d/pvPinch.anchor>=PV_PINCH_OUT){pvPinch.anchor=d;pvZoomStep(1,ev)}
      else if(d/pvPinch.anchor<=1/PV_PINCH_OUT){pvPinch.anchor=d;pvZoomStep(-1,ev)}
    }
    PV.panSwallow=Date.now();
    return;
  }
  if(PV.lens.on&&PV.lens.drag){
    const d=PV.lens.drag,step=pvLensPxPerCell();
    // the glass follows the POINTER at the base view's scale, not at the
    // magnified scale: you slide the glass across the page, the page does not
    // scroll under it — and this is what keeps high zoom from being twitchy
    if(Math.abs(ev.clientX-d.x)>2||Math.abs(ev.clientY-d.y)>2)PV.lens.moved=true;
    // the hand is holding the glass, so ONLY the glass moves: the frame must
    // not shift mid-drag, or the picture fights the hand (the measured flicker).
    // And NO rim clamp either: a clamp was measured SNAPPING the glass 51px
    // against the hand the moment it crossed the rim zone. The plane is a
    // torus — pvWrap re-enters the glass from the far rim, the same wrap every
    // cell obeys — so 1:1 hand tracking holds everywhere by construction.
    PV.lens.wedge=pvWrap(d.w+(ev.clientX-d.x)/step);
    PV.lens.ring=pvWrap(d.r+(ev.clientY-d.y)/step);
    pvLensPaint();
    return;
  }
  if(!PV.panDrag)return;
  const step=pstep(),d=PV.panDrag;
  // dragging right shows what is to the LEFT, so the frame moves the other way.
  // dw is measured from the ORIGINAL pointerdown coords every time — no per-step
  // rewriting of the anchor, so a slow wiggle can never accumulate error. The
  // blocks branch used to break exactly that promise: it wrote the JUST-MOVED
  // origin back into bx/by while still measuring dw from the original pointer,
  // so every step added the absolute delta onto an already-shifted base — a
  // hand that had moved +2 blocks slid the map +3, then +6, then +10… which
  // is the bounce a blocks drag kept making (ลากแล้วเด้ง). The anchor stays
  // the pointerdown's own bx/by; only bOrig moves.
  const dw=Math.round((d.x-ev.clientX)/step),dr=Math.round((d.y-ev.clientY)/step);
  if(d.blocks){
    if(dw===PV._dw&&dr===PV._dr){pvPanRide(d,ev,dw,dr,step);if(anchored)pvPopRide(d,ev);return}
    PV._dw=dw;PV._dr=dr;
    PV.bOrig={x:pvB(d.bx+dw),y:pvB(d.by+dr)};
    PV.panDrag={...d};                      // spread keeps the ORIGINAL anchor
  }else{
    if(dw===PV._dw&&dr===PV._dr){pvPanRide(d,ev,dw,dr,step);if(anchored)pvPopRide(d,ev);return}
    PV._dw=dw;PV._dr=dr;
    PV.origin={wedge:pvWrap(d.ox+dw),ring:pvWrap(d.oy+dr)};
    PV.panDrag={...d};
  }
  /* A pan changes WHERE you look, not WHAT exists: no fetch ran, PV.cells did
     not move, time did not move. pvMode() would still re-derive the cells,
     redraw the base, recount all 25k timeline rows into the ribbon (measured
     11.5ms per step — a whole frame's budget — which is the twitch of a map
     dragged across a full plane). The light path below draws only what the
     pan actually changed: origin is in pvBaseKey, so pvDraw re-paints from the
     cached base where it differs; pvInfo/pvPop re-speak the new position. */
  // a pan never OPENS the popup: it shares pvPop with every refresh path, so
  // the unconditional call re-opened the panel mid-drag whenever a selection
  // lingered from an earlier click. An already-open popup still rides its
  // anchor below; opening one is a read's job, not a pan's.
  pvDraw();pvInfo();pvLensInfo();
  pvPanRide(d,ev,dw,dr,step);
  // the popup rides its anchor through the pan — the same raw hand delta the
  // canvas ride uses, on EVERY move, so it tracks 1:1 instead of jumping one
  // whole cell per step and freezing between; the release snaps it exactly
  if(anchored)pvPopRide(d,ev);
};
/* The map moves in WHOLE CELLS (an address is a cell), but the hand moves in
   pixels. Without the fractional ride below, a hand hovering near a cell
   boundary flips dw between n and n+1 with every 1px wiggle — the map jumps a
   whole cell back and forth repeatedly, which is exactly "ลากแล้วเด้งไปมา".
   The ride paints the BETWEEN-steps remainder as a CSS translate: the picture
   tracks the hand 1:1 continuously, and when the remainder crosses a full cell
   the origin steps and the translate resets to zero — no visible jump, because
   the picture was already drawn there. Cleared on release. */
function pvPanRide(d,ev,dw,dr,step){
  const fx=(ev.clientX-d.x)+dw*step, fy=(ev.clientY-d.y)+dr*step;
  pcv.style.transform=(Math.abs(fx)>.5||Math.abs(fy)>.5)
    ?'translate('+fx.toFixed(1)+'px,'+fy.toFixed(1)+'px)':'';
}
function pvPanRideClear(){
  PV._dw=null;PV._dr=null;
  if(pcv.style.transform)pcv.style.transform='';
}
/* Where the open popup sits when a drag starts — the ride below moves it by
   the raw hand delta from here, so it stays glued to its anchor cell without
   rebuilding anything mid-drag. A rect, not style.left: always available. */
function pvPopRideGrab(d){
  const pop=$('#plane-pop');
  if(!pop.matches(':popover-open'))return;
  const r=pop.getBoundingClientRect();
  d.ppx=r.left;d.ppy=r.top;
}
/* The popup's half of the fractional ride: the painted anchor tracks the hand
   1:1 (that is the whole point of pvPanRide), so the open popup must move by
   the SAME raw delta — not by whole-cell steps, or it lags half a cell behind
   and jumps. Pure position: content cannot change mid-pan (no fetch ran), so
   no rebuild, no rewire, no scroll loss; only the viewport edges still clamp.
   The release snaps exactly (see onpointerup). */
function pvPopRide(d,ev){
  const pop=$('#plane-pop');
  if(d.ppx==null||!pop.matches(':popover-open'))return;
  const vw=window.innerWidth||document.documentElement.clientWidth;
  const vh=window.innerHeight||document.documentElement.clientHeight;
  const pw=pop.offsetWidth,ph=pop.offsetHeight;
  pop.style.left=Math.max(10,Math.min(d.ppx+(ev.clientX-d.x),vw-pw-10))+'px';
  pop.style.top=Math.max(10,Math.min(d.ppy+(ev.clientY-d.y),vh-ph-10))+'px';
}
pcv.onpointerup=ev=>{
  pvPts.delete(ev.pointerId);
  if(pvPts.size<2)pvPinch=null;
  pvPanRideClear();
  if(PV.lens.on&&PV.lens.drag){
    const moved=PV.lens.moved;PV.lens.drag=null;
    try{pcv.releasePointerCapture(ev.pointerId)}catch(e){}
    // moving the glass is not a read, for the same reason a pan is not — and
    // no frame catch-up here either: whatever the hand left in view stays
    if(moved){PV.panSwallow=Date.now();pvLensInfo();return}
    pvLensRead();
    return;
  }
  if(!PV.panDrag)return;
  const moved=Math.abs(ev.clientX-PV.panDrag.x)>2||Math.abs(ev.clientY-PV.panDrag.y)>2;
  PV.panDrag=null;PV.popRide=false;
  // the popup rode the hand by raw delta; snap it to the stepped anchor once —
  // one exact re-place per drag (re-clamped), not one rebuild per move
  if($('#plane-pop').matches(':popover-open'))pvPop();
  try{pcv.releasePointerCapture(ev.pointerId)}catch(e){}
  // a pan is not a read: without this, sliding the frame would open whatever
  // cell the pointer happened to stop on
  if(moved)PV.panSwallow=Date.now();
};
// a cancelled pointer (a system gesture, the tab losing focus) must not leave
// the frame held: a pan with no finger on it drags on the very next move
pcv.onpointercancel=ev=>{
  pvPts.delete(ev.pointerId);
  if(pvPts.size<2)pvPinch=null;
  if(!pvPts.size){PV.panDrag=null;PV.popRide=false}
  pvPanRideClear();
  // a cancelled drag still stepped the origin: the ride offset is stale, snap
  if($('#plane-pop').matches(':popover-open'))pvPop();
};
// the drag can also END off-canvas — the pointer keeps moving after capture
// only if it was captured, and capture is per-pointer: a release outside the
// canvas still fires onpointerup, but a lost one (alt-tab, context menu) does
// not. Guard the popup against a stuck PV.panDrag from any of those.
window.addEventListener('blur',()=>{PV.panDrag=null;PV.popRide=false;pvPanRideClear()});
// ░ the zoom wheel. It snaps between REAL resolutions rather than scaling
// continuously: between two stops the glass would be enlarging pixels, which
// is the opposite of what a viewport is for.
pcv.addEventListener('wheel',ev=>{
  if(!PV.lens.on)return;
  ev.preventDefault();
  pvLensZoom(ev.deltaY<0?1:-1);
},{passive:false});
// prune is a REFETCH, not a repaint: the server decides which cells exist, so
// flipping it re-reads the plane (and the who/when layer with it)
$('#pv-prune').onchange=e=>{
  PV.prune=e.target.checked;PV.metaOn=false;loadPlane();
  toast(PV.prune?'🧹 เปิดตัดขยะ — ข้อความไร้สาระไม่กินที่อยู่บนแผนที่'
               :'🧹 ปิดตัดขยะ — เห็นแผนที่ดิบ รวมข้อความทดสอบ (ในไฟล์ log ยังมีครบ)');
};
$('#pv-bonds').onchange=e=>{PV.showBonds=e.target.checked;pvDraw()};
$('#pv-ctx').onchange=async e=>{
  PV.ctxOn=e.target.checked;await loadCtx();pvMode();
  if(PV.ctxOn)toast('ความจำกลางที่แชร์ไว้: '+Object.keys(PV.ctxCells).length+' ช่อง · เชื่อมข้ามระบบ '+PV.crossEdges.length+' เส้น');
};
$('#pv-style').onchange=e=>{PV.style=e.target.value;pvMode()};
// how many cells count as a hub decides how much of the graph is drawn at all.
// 12 was tuned when a plane held a handful of edges; a real corpus puts
// thousands here, and the ceiling — not the style — was what hid them.
$('#pv-hubk').value=String(PV.hubK);
$('#pv-hubk').onchange=e=>{PV.hubK=+e.target.value;pvMode()};
$('#pv-rel').onchange=e=>{PV.relColour=e.target.checked;pvDraw();pvInfo()};
$('#pv-lens').onclick=()=>{
  const L=PV.lens;
  L.on=!L.on;
  $('#pv-lens').classList.toggle('on',L.on);
  if(L.on){
    // the glass has to be held over SOMETHING. The honest first position is
    // where the reader already is; failing that, the busiest cell, which is
    // where anyone would look first anyway.
    const here=PV.sel!=null?PV.sel:busiestCell();
    if(here!=null){
      const w=here%144,r=Math.floor(here/144);
      L.wedge=w;L.ring=r;
      // The glass has a RADIUS, so it cannot be centred closer than r to the
      // edge of the view — and on the full 144x144 plane that would put ~25
      // rings permanently out of reach. So the base becomes a WINDOW and the
      // glass gets a band to move in; the window still pans, so nothing is
      // unreachable, only out of frame until you slide.
      if(PV.mode!=='window'||PV.win<36||PV.win>96){PV.win=48}
      PV.mode='window';
      PV.origin={wedge:pvWrap(w-Math.floor(PV.win/2)),ring:pvWrap(r-Math.floor(PV.win/2))};
    }
    toast('🔍 แว่นขยาย — ลาก = เลื่อน · ล้อ/± = ซูม · คลิก/Enter = อ่านช่องใต้แว่น · หยุดที่ความละเอียดจริงเท่านั้น');
  }
  pvMode();
};
function pvLensZoom(dir){
  const s=pvLensStops(),z=pvLensZ();
  let i=s.findIndex(v=>v>=z);if(i<0)i=s.length-1;
  i=dir>0?Math.min(s.length-1,i+1):Math.max(0,i-1);
  PV.lens.z=s[i];
  pvDraw();pvLensInfo();
  const lv=pvLensLevel(pvLensCS());
  toast('🔍 ×'+s[i]+' · '+lv[1]+(s[i]>=pvLensZMax()
    ?' · ⛔ ละเอียดสุดแล้ว — ไม่มีอะไรให้ขยายมากกว่านี้ เนื้อหาข้างในอยู่ในแผงขวา':''));
}
$('#pv-lens-zin').onclick=()=>pvLensZoom(1);
$('#pv-lens-zout').onclick=()=>pvLensZoom(-1);
// ⟲ = "never mind, show me the whole thing again". It undoes BOTH ways of being
// zoomed in at once — the magnifier's ×level AND the view being narrowed to a
// block or a window — because the difference between "what magnification" and
// "which patch am I looking at" is OUR distinction, not the reader's. Two
// separate resets would just make them guess which one they wanted. It also
// re-centres the glass, so it doubles as "I dragged it somewhere and lost it".
function pvZoomReset(){
  const wasNarrow=PV.mode!=='plane';
  PV.lens.z=1;
  // the seam is part of the view: leaving the block map rotated to a place the
  // reader only got to by dragging would make "show me the whole thing again"
  // restore everything except the thing they actually moved
  PV.bOrig={x:0,y:0};
  const here=PV.sel!=null?PV.sel:busiestCell();
  if(here!=null){PV.lens.wedge=here%144;PV.lens.ring=Math.floor(here/144)}
  if(wasNarrow){PV.mode='plane';pvMode()}
  else{pvLensFollow();pvDraw();pvLensInfo()}
  toast(wasNarrow?'⟲ กลับไปทั้งแผนที่ · ความละเอียด ×1'
    :'⟲ ความละเอียด ×1 · แว่นกลับมาที่'+(PV.sel!=null?'จุดที่เลือกไว้':'ช่องที่แน่นที่สุด'));
}
$('#pv-zreset').onclick=pvZoomReset;
// The options bar is one button away, and the button says what is behind it.
// Kept in a variable so opening ⚙ stays open across a mode change — a panel
// that closes itself every time you touch something is worse than no panel.
let pvOptOn=false;
function pvOptApply(){
  $('#plane-bar').classList.toggle('off',!pvOptOn);
  $('#pv-opt').classList.toggle('on',pvOptOn);
}
$('#pv-opt').onclick=()=>{pvOptOn=!pvOptOn;pvOptApply();toast(pvOptOn?'⚙ ตัวเลือกการวาด':'⚙ ซ่อนตัวเลือก — เหลือแค่สิ่งที่ต้องใช้บ่อย')};
pvOptApply();
$('#pv-census').onclick=()=>{
  // toggle: the census REPLACES the detail panel rather than sitting beside it,
  // because both answer "what is this" and two answers on a 360px column is
  // how a panel becomes unreadable
  if(PV.showCensus){PV.showCensus=false;pvDetail();return}
  pvCensus();
};
// one delegation point for the whole strip, because the rows are rebuilt on
// every repaint and a handler bound to each row would die with the row
if(_pvCompBox)_pvCompBox.onclick=ev=>{
  const btn=ev.target.closest('[data-cmp]');
  if(btn){PV.cmp=+btn.dataset.cmp;pvComposition();return}
  const row=ev.target.closest('[data-cmp-dim]');
  if(!row)return;
  const dim=row.dataset.cmpDim,key=row.dataset.cmpKey;
  if(!dim)return;                       // an axis the scrub cannot honour
  const on=PV.filt&&PV.filt.dim===dim&&PV.filt.key===key;
  pvPickFilt(dim,on?'':key);
};
$('#pv-f0').onclick=()=>setFocus(0);
$('#pv-f1').onclick=()=>setFocus(1);
$('#pv-f2').onclick=()=>setFocus(2);
$('#pv-f3').onclick=()=>setFocus(3);
$('#pv-seq').oninput=e=>pvSetSeq(+e.target.value);
$('#pv-last').onclick=()=>pvSetSeq(PV.seqMax);          // "latest" = live plane
// Both controls are one function's two arguments, not two states: choosing an
// axis REPLACES whatever the other one was filtering (pvPickFilt explains why
// on screen), and pvFilterOptions repaints both from PV.filt.
$('#pv-kind').onchange=async e=>{await pvPickFilt('kind',e.target.value)};
$('#pv-app').onchange=async e=>{await pvPickFilt('app',e.target.value)};
$('#pv-colour').onchange=async e=>{
  PV.colour=e.target.value;
  if(PV.colour==='writer'||PV.colour==='recent'||PV.colour==='kind')await loadMeta();
  // the layer just arrived and the filter reads it too, so the list has to be
  // rebuilt here or it keeps offering "โหลดรายชื่อชนิด" to someone who already
  // paid for the kinds
  pvFilterOptions();
  _legendKey=null;pvDraw();pvInfo();
};
$('#pv-png2').onclick=pvPNG;
// the strip is a scrubber: press and drag to move through the log's order
const ribSeek=ev=>{const S=pvRibbonAt(ev);if(S!=null)pvSetSeq(S)};
let ribDrag=false;
rb.onmousedown=ev=>{ribDrag=true;ribSeek(ev)};
rb.onmousemove=ev=>{
  const S=pvRibbonAt(ev);
  if(!S&&S!==0)return;
  const row=PV.timeline[Math.min(PV.timeline.length-1,S)];
  rb.title=row?('tick '+S+' · '+fmt(row[1])+' · '+(row[5]||'?')+' ('+row[3]+') · กดเพื่อเลื่อนไปช่วงนี้'):'';
  if(ribDrag)ribSeek(ev);
};
window.addEventListener('mouseup',()=>{ribDrag=false});
rb.onmouseleave=()=>{ribDrag=false};
// arrow keys step time, space plays, Esc closes — the plane is something you
// look at for a while, so it should not need the mouse for the boring moves
document.addEventListener('keydown',e=>{
  if(!$('#plane-modal').classList.contains('open'))return;
  if(e.target&&/INPUT|TEXTAREA|SELECT/.test(e.target.tagName))return;
  // With the glass in hand the arrows move the GLASS, because that is what you
  // are holding — and one press is one CELL, which is the fine control a drag
  // at x50 cannot give you. Shift+arrows still move time, so the old keys stay
  // reachable instead of being taken away.
  if(PV.lens.on){
    const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];
    if(d&&!e.shiftKey){e.preventDefault();pvLensNudge(d[0],d[1]);return}
    if(e.key==='Enter'){e.preventDefault();pvLensRead();return}
    if(e.key==='+'||e.key==='='){e.preventDefault();$('#pv-lens-zin')&&$('#pv-lens-zin').click();return}
    if(e.key==='-'){e.preventDefault();$('#pv-lens-zout')&&$('#pv-lens-zout').click();return}
  }
  // arrows move TIME; shift+arrows move the FRAME. Two different kinds of
  // "move" on the same keys would be a trap, so they are kept apart.
  if(e.shiftKey&&PV.mode==='window'){
    const d={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,-1],ArrowDown:[0,1]}[e.key];
    if(d){e.preventDefault();pvPan(d[0],d[1])}
    return;
  }
  if(e.key==='ArrowLeft'){e.preventDefault();pvSetSeq((PV.seq===null?PV.seqMax-1:PV.seq)-1)}
  else if(e.key==='ArrowRight'){e.preventDefault();pvSetSeq((PV.seq===null?PV.seqMax-1:PV.seq)+1)}
  else if(e.key===' '){e.preventDefault();$('#pv-play').click()}
});
window.addEventListener('resize',()=>{
  if(!$('#plane-modal').classList.contains('open'))return;
  pvDraw();pvRibbon();          // both canvases are sized from the space they have
  pvPop();                      // and the popup is anchored to a point in them
  pvTopHide($('#plane-tip'));
});
/* ▶ replay. Two things a reader asked for, both measured:
   - SPEED: the old timer scaled the INTERVAL to fit ~2.4s total, but the 60ms
     floor turned a 25k-line log into 1 line per 60ms — a 25-MINUTE crawl. The
     step SIZE scales instead (60ms is already a frame; redraw cost per tick
     does not depend on how many lines it advances), so a full replay stays
     ~12s on any log: step = ceil(seqMax / (TARGET/60)).
   - ⏸/▶ is a PAUSE, not a restart: resuming continues from the line on
     screen. Only a replay that reached the live edge — or a view parked there
     — starts again from 0. Scrubbing (pvSetSeq) still pauses, and the
     scrubber shows the true position every tick. */
const PV_PLAY_MS=60,PV_PLAY_TARGET=12000;let pvPlayStep=1;
function pvPlayTick(){
  if(PV.seq===null||PV.seq>=PV.seqMax-1){
    clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ เล่นย้อน';
    PV.seq=null;$('#pv-seq').value=$('#pv-seq').max;pvDerive();pvDraw();pvInfo();pvWhen();pvRibbon();
    return;
  }
  PV.seq=Math.min(PV.seqMax-1,PV.seq+pvPlayStep);$('#pv-seq').value=PV.seq;
  pvDerive();pvDraw();pvInfo();pvWhen();
}
$('#pv-play').onclick=()=>{
  if(PV.playing){clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ เล่นย้อน';return}
  $('#pv-play').textContent='⏸ หยุด';
  const at=PV.seq===null?0:PV.seq;                 // live edge → start over
  PV.seq=(at>=PV.seqMax-1)?0:at;                   // otherwise continue from here
  $('#pv-seq').value=PV.seq;
  pvPlayStep=Math.max(1,Math.round(PV.seqMax/(PV_PLAY_TARGET/PV_PLAY_MS)));
  pvDerive();pvDraw();pvInfo();pvWhen();
  PV.playing=setInterval(pvPlayTick,PV_PLAY_MS);
};

// boot: initial pull for chain/registry/inbox — SSE only carries new lines,
// without this a LAN phone shows the shell with a stuck "chain…" forever
refresh().catch(()=>{});
loadInbox().catch(()=>{});

// ── ⏱ session-block timeline ──────────────────────────────────────────────
let tlOn=false, tlData=null;
async function loadTimeline(){
  try{ tlData=await fetch('/blocks/timeline',{headers:H}).then(r=>r.json()); }
  catch(e){ tlData={nodes:[],links:[]}; }
  paintTimeline();
}
function paintTimeline(){
  const d=tlData||{nodes:[],links:[]};
  const n=d.nodes||[], lk=d.links||[];
  if(!n.length){
    $('#tl-body').innerHTML='<div class="tl-empty">ยังไม่มีความจำที่ปิดงานแล้ว — ปิดงานด้วย <code>POST /session-end</code> หรือ <code>pool.py session-end &lt;app&gt;</code></div>';
    $('#tl-count').textContent='0 ชิ้น';return;
  }
  // build similarity lookup: slug → [{to, sim, via}]
  const simMap={};
  lk.forEach(l=>{
    if(!simMap[l.from])simMap[l.from]=[];
    if(!simMap[l.to])simMap[l.to]=[];
    simMap[l.from].push({slug:l.to,sim:l.sim,via:l.via});
    simMap[l.to].push({slug:l.from,sim:l.sim,via:l.via});
  });
  const hiSlugs=new Set(lk.map(l=>l.from).concat(lk.map(l=>l.to)));
  // render as vertical timeline (oldest first)
  const rows=n.slice().reverse().map((b,i)=>{
    const isLast=i===n.length-1;
    const isHi=hiSlugs.has(b.slug);
    const links=(simMap[b.slug]||[]).map(l=>
      '<span class="tl-link" data-goto="'+esc(l.slug)+'" title="คล้ายกัน '+l.sim+' ('+l.via+')">↗ '+esc(l.slug)+' <span class="sim">('+Math.round(l.sim*100)+'%)</span></span>'
    ).join(' ');
    return '<div class="tl-row"><div class="tl-rail">'
      +'<div class="tl-dot'+(isHi?' dup':'')+'"></div>'
      +(isLast?'':'<div class="tl-line"></div>')
      +'</div><div class="tl-card'+(isHi?' hi':'')+'">'
      +'<div class="tl-h"><span class="app">'+esc(b.app)+'</span>'
      +'<span class="ts">'+esc(b.ts)+'</span>'
      +'<span class="rev">rev '+b.rev+'</span>'
      +(b.status!=='closed'?'<span class="st">'+esc(b.status)+'</span>':'')
      +'</div>'
      +'<div class="tl-title">'+esc(b.title||'(no title)')+'</div>'
      +(b.evidence?'<div class="tl-ev">'+esc(b.evidence)+'</div>':'')
      +(links?'<div>'+links+'</div>':'')
      +'</div></div>';
  }).join('');
  $('#tl-body').innerHTML=rows;
  $('#tl-count').textContent=n.length+' blocks · '+lk.length+' dedup links';
}
$('#tl-body').onclick=ev=>{
  const lnk=ev.target.closest('[data-goto]');
  if(lnk){
    const slug=lnk.dataset.goto;
    blocksOn=true;$('#blocks').hidden=false;blkOpen.add(slug);
    loadBlocks().then(()=>$('#blk-list').querySelector('.blk[data-slug="'+slug+'"]')?.scrollIntoView({block:'nearest'}));
  }
};
$('#btn-tl').onclick=()=>{tlOn=!tlOn;$('#tl-panel').hidden=!tlOn;if(tlOn)loadTimeline()};
$('#tl-close').onclick=()=>{tlOn=false;$('#tl-panel').hidden=true};
setInterval(()=>{if(tlOn&&!document.hidden)loadTimeline()},30000);

</script>
</body>
</html>"""


class PoolServer(ThreadingHTTPServer):
    """Threaded server whose error path understands that a closed client is
    normal traffic.

    SSE keeps a socket open per dashboard tab: shutting the tab, reloading,
    or the preview webview detaching aborts the socket mid-write and Windows
    reports it as ConnectionAbortedError (WinError 10053) on the *server*
    side. That is not a crash — the reply is already going nowhere — but
    socketserver's default handle_error() prints a full traceback per
    occurrence, which buried the terminal under phantom failures. Count them
    and print one line; anything that is not a socket error keeps the
    traceback (that one is a real bug).
    """

    daemon_threads = True
    dropped = 0

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, OSError):
            PoolServer.dropped += 1
            print(f"  · client {client_address[0]} went away ({exc.__class__.__name__}"
                  f", {PoolServer.dropped} total) — reply dropped")
            return
        super().handle_error(request, client_address)


class PoolHandler(BaseHTTPRequestHandler):
    def _authorized(self) -> bool:
        """Loopback = trusted (dashboard on this PC). Non-loopback
        (phone/LAN) must present the bearer token from .pool_token
        (header, or ?token= for EventSource which cannot set headers)."""
        client = self.client_address[0]
        if client in ("127.0.0.1", "::1"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth == f"Bearer {_get_token()}":
            return True
        parsed = urlparse(self.path)
        return parse_qs(parsed.query).get("token", [""])[0] == _get_token()

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        """Request body -> dict, or None when it is absent, malformed, or not
        an object. Every POST branch reads its body through this.

        Written because `json.loads(self.rfile.read(length))` was unguarded in
        all four write endpoints, so a bad or empty body raised straight out of
        do_POST and the connection closed with NO response at all (curl reports
        http=000; fetch rejects). The caller could not tell a refused write from
        a dead server, and on the input path that is the difference between
        "retry" and "give up". A write path may refuse loudly; it must not fail
        silently."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return None
        if length <= 0:
            return None
        try:
            obj = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return None
        return obj if isinstance(obj, dict) else None

    def _int_param(self, qs, name, default, cap=None, error=None):
        """An integer query parameter, or a 400 answer. `_REFUSED` when it
        refused — the answer is already written, the caller only has to stop.

        Written because `int(qs.get("n")[0])` sat unguarded in /tail, /view,
        /stream and /curator (twice more for the curator thresholds), so a typo
        in ?n=abc raised straight out of do_GET: the client got NO response at
        all (curl reports http=000, fetch rejects) and stderr got a traceback.
        Measured: 6 of 39 malformed requests answered nothing — the same
        failure §12 fixed for POST bodies, still standing on every GET that
        reads a number. A request that fails silently and a server that is gone
        look identical from the outside, which is the one thing an error path
        must never be."""
        raw = qs.get(name, [""])[0]
        if raw == "":
            return default
        try:
            value = int(raw)
        except (TypeError, ValueError):
            self._json(400, {"error": error or ("?%s=<int> required" % name)})
            return _REFUSED
        return min(value, cap) if cap is not None else value

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        # the HTML shell carries no secrets — serve it to everyone so the
        # client JS can read #token= and authenticate its API calls;
        # only data endpoints below are token-gated
        public = parsed.path in ("/", "/index.html", "/qr")
        if not public and not self._authorized():
            self._json(401, {"error": "token required — open /qr from the server screen",
                             "howto": {"header": "Authorization: Bearer <token>"}})
            return

        if parsed.path == "/" or parsed.path == "/index.html":
            body = DASHBOARD.encode("utf-8")
            # LAN visitors: JS hides the QR panel once auth succeeds; it
            # stays visible when the token is missing/rejected.
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                body = body.replace(
                    b"</body>",
                    ("<div id='qrpanel' style='display:none;margin:12px 18px;padding:12px;border:1px solid var(--warn);border-radius:10px'>"
                     + "<div style='font:12px var(--mono);color:var(--warn);margin-bottom:6px'>⚠ ยังไม่ได้เข้าสู่ระบบ — สแกน QR นี้ด้วยมือถือ (รหัสเข้าใช้อยู่ในตัว QR)</div>"
                     + _qr_panel() + "</div></body>").encode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/qr":
            # always-available QR page (no auth needed to see the QR panel;
            # the token is inside the QR itself — LAN-only threat model)
            html = ("<html><head><meta charset='utf-8'><title>pool QR</title></head>"
                    "<body style='background:#0d1117;color:#e6edf3;font-family:sans-serif;"
                    "display:flex;flex-direction:column;align-items:center;gap:12px;padding:30px'>"
                    "<h2 style='margin:0'>chat-pool</h2>" + _qr_panel() +
                    "</body></html>").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        elif parsed.path == "/health":
            lines = 0
            if os.path.exists(pool.LOG):
                with open(pool.LOG, "rb") as f:
                    lines = f.read().count(b"\n")
            self._json(200, {"ok": True, "lines": lines, "log": pool.LOG})

        elif parsed.path == "/tail":
            n = self._int_param(qs, "n", 10, cap=500)
            if n is _REFUSED:
                return
            as_app = qs.get("as", [None])[0]
            clean = qs.get("clean", ["0"])[0] in ("1", "true")
            with _lock:
                # each line rides out with its tick (`seq`): the feed groups
                # lines into fixed-width chunks by seq // CHUNK, so a chunk keeps
                # its number as the log grows. Decoration on the wire only —
                # nothing new is stored and the hash never covered this field.
                msgs = pool.stamp_cur(pool.stamp_seq(pool.tail(n, as_app=as_app)))
            if clean:
                # opt-in, and opt-in only: a reader that asked for a window and
                # got fewer lines without asking would be the same silent
                # truncation tail(None) exists to prevent
                msgs = [m for m in msgs if not m["cur"]["h"]]
            self._json(200, msgs)

        elif parsed.path == "/curator":
            # what the feed would HIDE, and what the plane PRUNES, and why —
            # the report a human runs before trusting either. Same rules, two
            # consequences: a hidden line still happened, a pruned one stopped
            # occupying an address. ?prune=1 adds the plane's view (it reads the
            # whole log, so it is opt-in), ?at=<n> moves either threshold.
            n = self._int_param(qs, "n", 200, cap=5000)
            hide = self._int_param(qs, "hide", None, error=_THRESHOLD_ERR)
            at = self._int_param(qs, "at", None, error=_THRESHOLD_ERR)
            if n is _REFUSED or hide is _REFUSED or at is _REFUSED:
                return
            want_prune = qs.get("prune", [""])[0] not in ("", "0", "false")
            with _lock:
                rep = pool.curator_report(n=n, hide_at=hide)
                # the plane's numbers ride along for free: they are read from
                # the derived() cache the map is already drawn from
                rep["plane"] = pool.plane_prune_stats()
                if want_prune:
                    # this one walks the WHOLE log, so it is opt-in
                    rep["prune"] = pool.curator_prune_report(prune_at=at)
                self._json(200, rep)

        elif parsed.path == "/apps":
            reg = pool.list_apps()
            apps = sorted(
                ({"app": name, **info} for name, info in reg.items()),
                key=lambda a: -a.get("last_seen", 0))
            self._json(200, apps)

        elif parsed.path == "/blocks":
            # the story/method layer. One canonical view for every platform —
            # this is the endpoint that makes "ใช้ร่วมกัน + เข้าใจตรงกัน"
            # checkable instead of aspirational. ?slug= also returns the chain
            # check (verify) and, with &revs=1, the whole revision history.
            slug = qs.get("slug", [None])[0]
            with _lock:
                if slug:
                    row = pool.block_get(slug)
                    if row is None:
                        self._json(404, {"error": f"no such block: {slug}"})
                        return
                    row["verify"] = pool.verify_block(row)
                    if qs.get("revs", ["0"])[0] in ("1", "true"):
                        row["history"] = pool.block_revs(slug)
                    self._json(200, row)
                else:
                    self._json(200, pool.blocks(
                        status=qs.get("status", [None])[0],
                        query=qs.get("q", [None])[0]))

        elif parsed.path == "/blocks/timeline":
            # session-block timeline: which platform closed when, with
            # similarity links between deduped blocks. Read-only, derived.
            with _lock:
                self._json(200, pool.block_timeline())

        elif parsed.path == "/tasks":
            # the task layer: status/owner/parent per task, derived from
            # task-* blocks. ?status=&owner=&parent=&q= are AND filters.
            status = qs.get("status", [None])[0]
            owner = qs.get("owner", [None])[0]
            parent = qs.get("parent", [None])[0]
            query = qs.get("q", [None])[0]
            with _lock:
                tasks = taskboard.task_list(status=status, owner=owner,
                                            parent=parent, query=query)
            self._json(200, {"tasks": tasks, "summary": taskboard.task_summary()
                             if not (status or owner or parent or query) else None})

        elif parsed.path == "/tasks/summary":
            with _lock:
                self._json(200, taskboard.task_summary())

        elif parsed.path == "/tasks/ui":
            # read-only board page over /tasks — same data every platform reads
            body = TASKS_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif parsed.path == "/inbox":
            app = qs.get("app", [""])[0]
            mark = qs.get("mark", ["1"])[0] != "0"
            if not app:
                self._json(400, {"error": "?app= required"})
                return
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first"})
                return
            pool.touch_app(app)
            with _lock:
                msgs = pool.inbox(app, mark_read=mark)
            self._json(200, msgs)

        elif parsed.path == "/neighbors":
            # multi-pointer vision read at the read point — no in-between
            try:
                rk = int(qs.get("rkey", [""])[0])
            except ValueError:
                self._json(400, {"error": "?rkey=<int> required"})
                return
            lv = qs.get("level", ["0"])[0]
            as_app = qs.get("as", [None])[0]
            if lv not in ("0", "1", "2"):
                self._json(400, {"error": "?level=0 (cell) | 1 (block map) | 2 (plane map)"})
                return
            raw = qs.get("path", [""])[0].strip()
            try:
                path = tuple(int(p) for p in raw.split(",") if p != "") if raw else ()
            except ValueError:
                self._json(400, {"error": "?path=<int>[,<int>...] — the sub-cell chosen at each depth"})
                return
            # &raw=1 shows the map BEFORE the curator pruned it: the honest
            # escape hatch for judging the threshold instead of trusting it.
            # L0 content is never pruned — an explicit read is already the
            # reader asking for those exact lines.
            rawp = qs.get("raw", [""])[0] not in ("", "0", "false")
            # &record=1 declares the read as data (a sighting). A read that a
            # person deliberately made IS the thing a sighting describes, and
            # nothing else in the system can produce one — which is why the
            # plane had zero until this existed. sighting_once() makes it
            # idempotent, so a view that re-reads on render does not write a
            # line per render.
            rec = qs.get("record", [""])[0] not in ("", "0", "false")
            with _lock:
                # one pointer, one level, one read: L0 content · L1 12x12
                # block map · L2 whole-plane map. Over-budget cells come
                # back already split (stacked), never as a wall of lines,
                # and &path=1,42 walks one sub-cell down at a time.
                vis = pool.open_pointer(rk, level=int(lv), path=path,
                                        as_app=as_app, raw=rawp)
                if rec:
                    wrote = pool.sighting_once(rk, level=int(lv),
                                               app=as_app or "dashboard")
                    vis["sighting_written"] = wrote is not None
            self._json(200, vis)

        elif parsed.path == "/pointers":
            # shared awareness WITHOUT a central pointer: sightings are
            # ordinary lines, so this is derived, never stored server state
            try:
                rk = qs.get("rkey", [""])[0]
                lv = qs.get("level", [""])[0]
                rk = int(rk) if rk else None
                lv = int(lv) if lv else None
            except ValueError:
                self._json(400, {"error": "?rkey=<int>&level=<int> are optional"})
                return
            # &raw=1 keeps sightings whose address was emptied by prune,
            # each marked `dangling`, instead of dropping them
            rawp = qs.get("raw", [""])[0] not in ("", "0", "false")
            with _lock:
                seen = pool.pointers_at(rk, lv, raw=rawp)
            self._json(200, {"point": rk, "level": lv, "pointers": seen})

        elif parsed.path == "/bonds":
            # blast-radius read at one point: edges touching a line hash.
            # No ?hash= = every live edge, each end already placed in its cell
            # (that is what lets the plane draw the whole graph in one read).
            as_app = qs.get("as", [None])[0]
            h = qs.get("hash", [""])[0].strip().lower()
            # &raw=1 also returns edges whose far end was pruned (each marked
            # `dangling`) — the same escape hatch the map has
            rawp = qs.get("raw", [""])[0] not in ("", "0", "false")
            # &fields=plane drops the two 64-hex hashes. The plane addresses
            # edges by the CELLS their ends sit in (src_rkey/dst_rkey) and
            # carries src8/dst8 for the panel, so it never reads src/dst — and
            # at 10k edges those two fields are 38% of a 4 MB payload that a
            # phone re-fetches every time the plane opens.
            lean = qs.get("fields", [""])[0] == "plane"
            with _lock:
                if h:
                    edges = pool.bonds_for(h, as_app=as_app, raw=rawp)
                    self._json(200, {"hash": h, "edges": edges})
                    return
                edges = pool.all_bonds(as_app=as_app, raw=rawp)
            if lean:
                edges = [{k: v for k, v in e.items() if k not in ("src", "dst")}
                         for e in edges]
            self._json(200, {"hash": None, "count": len(edges), "edges": edges})

        elif parsed.path == "/explore":
            # The plane answers "what is where". This answers "what IS this in
            # relation to the rest" — what came out of it and what points back.
            # ?hash= walks one LINE (a panel click); ?rkey= walks one ADDRESS
            # (a plane click), because those are two different questions and
            # pooling them would make a cell read as if its lines were one line.
            h = qs.get("hash", [""])[0].strip().lower()
            try:
                rk = int(qs.get("rkey", [""])[0])
            except ValueError:
                rk = None
            try:
                hops = max(1, min(4, int(qs.get("hops", ["2"])[0] or 2)))
                cap = max(1, min(60, int(qs.get("cap", [""])[0] or pool.REACH_CAP)))
            except ValueError:
                self._json(400, {"error": "?hops=<1-4>&cap=<1-60>"})
                return
            if not h and rk is None:
                self._json(400, {"error": "?hash=<64hex> or ?rkey=<int>"})
                return
            with _lock:
                out = (pool.reach(h, hops=hops, cap=cap) if h
                       else pool.explore_cell(rk, hops=hops, cap=cap))
            self._json(200, out)

        elif parsed.path == "/patterns":
            # the whole plane's link shape: islands vs springs vs wells, and
            # which few lines everything hangs off. One read, because none of
            # it is visible from any single cell.
            with _lock:
                self._json(200, pool.pattern_census())

        elif parsed.path == "/plane/meta":
            # the plane's second map (who wrote each cell last, at which tick).
            # Its own endpoint because it is a layer: a reader that only wants
            # addresses and counts should not pay for a picture it will not draw
            # &raw=1 follows the same escape hatch as /neighbors: the who/when
            # layer of the UNPRUNED plane.
            as_app = qs.get("as", [None])[0]
            rawp = qs.get("raw", [""])[0] not in ("", "0", "false")
            with _lock:
                self._json(200, pool.plane_meta(raw=rawp))

        elif parsed.path == "/plane/timeline":
            # the plane's own tick: append-order ordinals, not wall clock,
            # so scrubbing to a tick replays the same plane every time
            as_app = qs.get("as", [None])[0]
            with _lock:
                self._json(200, pool.timeline(as_app=as_app))

        elif parsed.path == "/ctx" or parsed.path.startswith("/ctx/"):
            # BREATHING_SPEC §4 ทาง A — the same pointer read, pointed at
            # Magic Context's db instead of the log. Read-only by construction.
            try:
                import ctx_plane
            except Exception as e:
                self._json(500, {"error": "ctx_plane unavailable: %s" % e})
                return
            try:
                if parsed.path == "/ctx/stats":
                    self._json(200, ctx_plane.stats())
                    return
                if parsed.path == "/ctx/links":
                    # cross-system edges, derived from what the bridge already
                    # wrote — both ends sit in the same 144x144 plane.
                    # &raw=1 also returns edges whose LOG end was pruned.
                    rawp = qs.get("raw", [""])[0] not in ("", "0", "false")
                    with _lock:
                        # read once with the mark, then choose: the count of
                        # what was held back is part of the answer, not
                        # something the client has to infer from two requests
                        every = ctx_plane.links(raw=True)
                        live = [l for l in every if not l["dangling"]]
                        self._json(200, {"links": every if rawp else live,
                                         "total": len(every),
                                         "dangling": len(every) - len(live),
                                         "cross": ctx_plane.cross_stats()})
                    return
                rk = int(qs.get("rkey", [""])[0])
                lv = int(qs.get("level", ["0"])[0])
                if lv not in (0, 1, 2):
                    self._json(400, {"error": "?level=0 (rows in the cell) | 1 (block map) | 2 (plane map)"})
                    return
                raw = qs.get("path", [""])[0].strip()
                path = tuple(int(p) for p in raw.split(",") if p != "") if raw else ()
                with _lock:
                    self._json(200, ctx_plane.read_at(
                        rk, level=lv, path=path,
                        full=qs.get("full", ["0"])[0] == "1"))
            except FileNotFoundError:
                self._json(503, {"error": "no ctx index yet — run: python ctx_plane.py scan"})
            except (ValueError, TypeError):
                self._json(400, {"error": "?rkey=<int> and optional ?level=&path="})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif parsed.path == "/view":
            n = self._int_param(qs, "n", 20, cap=1000)
            if n is _REFUSED:
                return
            cloud = qs.get("cloud", ["0"])[0] == "1"
            as_app = qs.get("as", [None])[0]
            if as_app == "admin":
                as_app = None
            try:
                lines = pool.view(n, cloud=cloud, as_app=as_app,
                                  clean=qs.get("clean", ["0"])[0] in ("1", "true"))
                # chain audit for the dashboard header
                ok, hashes = _chain_audit()
                self._json(200, {"lines": lines, "chain_ok": ok, "hashes": hashes})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif parsed.path == "/stream":
            n = self._int_param(qs, "n", 5, cap=500)
            if n is _REFUSED:
                return
            as_app = qs.get("as", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            # No backfill: the client just loaded this window from /tail, and
            # re-sending 200 lines on every (re)connect is how one session's
            # read turned into the whole chat arriving at every card again.
            # The cursor is therefore the HASH of the newest line at connect
            # time, not a count: the window is capped at n, so past n lines the
            # length stops changing and a count-based cursor goes permanently
            # silent. A gap left by a reconnect is closed by the client's own
            # refresh on open, which is exact instead of a guess at a window.
            def _cursor(rows):
                return rows[-1].get("hash") if rows else None

            with _lock:
                last = _cursor(pool.tail(n, as_app=as_app))
            try:
                while True:
                    with _lock:
                        # same verdict the feed uses, so a line that arrives live
                        # is judged the moment it lands (no flash of noise that
                        # disappears one refresh later)
                        msgs = pool.stamp_cur(pool.stamp_seq(pool.tail(n, as_app=as_app)))
                    idx = next((i for i, m in enumerate(msgs)
                                if m.get("hash") == last), None)
                    # cursor not in the window (it fell off the end, or there
                    # was no line at all at connect time): send what we see and
                    # let the client de-dupe by hash
                    fresh = msgs[idx + 1:] if idx is not None else msgs
                    for m in fresh:
                        self.wfile.write(f"data: {json.dumps(m, ensure_ascii=False)}\n\n".encode())
                    if fresh:
                        self.wfile.flush()
                    last = _cursor(msgs) or last
                    time.sleep(1)
            except OSError:
                # OSError is the umbrella for every "peer left" shape:
                # BrokenPipeError / ConnectionResetError /
                # ConnectionAbortedError (WinError 10053 on Windows). The
                # dashboard reconnects on its own, so just drop this stream
                # and let the thread end.
                pass

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json(401, {"error": "token required — see .pool_token on the server"})
            return
        if parsed.path == "/register":
            body = self._read_json()
            if body is None:
                # say what is actually wrong: the body, not the registry.
                # Falling through to the app checks turned a malformed body
                # into "? not registered", which sends the caller to fix the
                # wrong thing — and now that the client SHOWS this error, a
                # label that lies costs a real debugging cycle.
                self._json(400, {"error": "body must be a JSON object",
                                 "howto": {"header": "Content-Type: application/json"}})
                return
            name = body.get("app", "")
            try:
                ok, created = pool.register_app(name)
                self._json(200, {"ok": ok, "created": created,
                                 "apps": sorted(pool.list_apps())})
            except ValueError as e:
                self._json(400, {"error": str(e)})
        elif parsed.path == "/append":
            body = self._read_json()
            if body is None:
                # say what is actually wrong: the body, not the registry.
                # Falling through to the app checks turned a malformed body
                # into "? not registered", which sends the caller to fix the
                # wrong thing — and now that the client SHOWS this error, a
                # label that lies costs a real debugging cycle.
                self._json(400, {"error": "body must be a JSON object",
                                 "howto": {"header": "Content-Type: application/json"}})
                return
            app = body.get("app", "?")
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first",
                                 "howto": {"method": "POST", "url": "/register", "body": {"app": app}}})
                return
            kind = body.get("kind", "chat")
            text = body.get("text", "")
            to = body.get("to", "all")
            if not text:
                self._json(400, {"error": "text required"})
                return
            with _lock:
                result = pool.append(app, kind, text, to=to)
            try:
                # bridge A: @<ws-id-prefix> in a LIVE line -> workspace inbox.
                # After the append, never before it: cloud failure must not
                # cost the caller their line.
                ws_bridge.forward(app, kind, text, to=to)
            except Exception as e:
                print(f"ws-bridge: forward FAIL {e}")
            self._json(200, {"ok": True, "hash": result["hash"],
                             "tags": result.get("tags", [])})
        elif parsed.path == "/block":
            # a block is a write like any other: same chain, same registry gate, and
            # a revision is a NEW line (nothing is edited, so the hash keeps meaning
            # what it means everywhere else in this log).
            body = self._read_json()
            if body is None:
                # say what is actually wrong: the body, not the registry.
                # Falling through to the app checks turned a malformed body
                # into "? not registered", which sends the caller to fix the
                # wrong thing — and now that the client SHOWS this error, a
                # label that lies costs a real debugging cycle.
                self._json(400, {"error": "body must be a JSON object",
                                 "howto": {"header": "Content-Type: application/json"}})
                return
            app = body.get("app", "?")
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first",
                                 "howto": {"method": "POST", "url": "/register", "body": {"app": app}}})
                return
            try:
                with _lock:
                    row = pool.block(app, body.get("slug", ""), body.get("title", ""),
                                     story=body.get("story", ""),
                                     method=body.get("method", ""),
                                     evidence=body.get("evidence", ""),
                                     refs=body.get("refs", ""),
                                     status=body.get("status", "open"))
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "slug": row["slug"], "rev": row["rev"],
                             "hash": row["hash"], "t": row["t"],
                             "howto": {"read": f"GET /blocks?slug={row['slug']}&revs=1"}})
        elif parsed.path == "/task":
            # create a task — a task-* block on the shared chain, so every
            # platform that can append can also post work here
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "body must be a JSON object",
                                 "howto": {"header": "Content-Type: application/json"}})
                return
            app = body.get("app", "?")
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first",
                                 "howto": {"method": "POST", "url": "/register", "body": {"app": app}}})
                return
            if not (body.get("title") or "").strip():
                self._json(400, {"error": "title required"})
                return
            try:
                with _lock:
                    t = taskboard.task_create(
                        app, body["title"], desc=body.get("desc", ""),
                        parent=body.get("parent", ""), owner=body.get("owner", ""),
                        due=body.get("due", ""), tags=body.get("tags", ""),
                        status=body.get("status", "pending"),
                        refs=body.get("refs", ""))
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "id": t["id"], "status": t["status"],
                             "hash": t["hash"],
                             "howto": {"claim": "POST /task/update {\"app\":\"…\",\"id\":\"%s\",\"action\":\"claim\"}" % t["id"]}})
        elif parsed.path == "/task/update":
            # advance a task: status/owner/note — a NEW rev, nothing edited
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "body must be a JSON object"})
                return
            app = body.get("app", "?")
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first"})
                return
            tid = body.get("id", "")
            if not tid:
                self._json(400, {"error": "id required"})
                return
            try:
                with _lock:
                    if body.get("action") == "claim":
                        t = taskboard.task_claim(app, tid)
                    else:
                        t = taskboard.task_update(
                            app, tid, status=body.get("status", ""),
                            owner=body.get("owner", ""),
                            note=body.get("note", ""))
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, {"ok": True, "id": t["id"], "status": t["status"],
                             "owner": t["owner"], "rev": t["rev"], "hash": t["hash"]})
        elif parsed.path == "/tasks/sync":
            # bridge: pull the inbox board into the pool as task blocks.
            # POST {} → dry-run (what WOULD be imported, nothing written);
            # POST {"apply": true} → writes; the inbox board stays the
            # source of record for those cards, the pool mirrors status.
            body = self._read_json() or {}
            apply = body.get("apply", False)
            with _lock:
                result = _inbox_sync_tasks(apply=apply)
            self._json(200, result)
        elif parsed.path == "/session-end":
            body = self._read_json()
            if body is None:
                # say what is actually wrong: the body, not the registry.
                # Falling through to the app checks turned a malformed body
                # into "? not registered", which sends the caller to fix the
                # wrong thing — and now that the client SHOWS this error, a
                # label that lies costs a real debugging cycle.
                self._json(400, {"error": "body must be a JSON object",
                                 "howto": {"header": "Content-Type: application/json"}})
                return
            app = body.get("app", "")
            if not app:
                self._json(400, {"error": "app required"})
                return
            if not pool.is_registered(app):
                self._json(403, {"error": f"{app} not registered — POST /register first",
                                 "howto": {"method": "POST", "url": "/register", "body": {"app": app}}})
                return
            force = body.get("force", False)
            try:
                with _lock:
                    b = pool.session_end(app, force=force)
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            if b is None:
                self._json(200, {"ok": False, "reason": "session too short or no substantive work"})
            else:
                self._json(200, {"ok": True, "slug": b["slug"], "rev": b["rev"],
                                 "hash": b["hash"], "title": b["title"],
                                 "t": b["t"],
                                 "howto": {"read": f"GET /blocks?slug={b['slug']}&revs=1"}})
        elif parsed.path == "/flush":
            path = pool.flush_emit()
            pushed, failed = pool.push_outbox()
            self._json(200, {"emitted": path, "pushed": pushed, "failed": failed})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # silent


def _chain_audit():
    """Walk the whole log: True when every line links to the previous hash."""
    if not os.path.exists(pool.LOG):
        return True, []
    hashes = []
    prevs = []
    with open(pool.LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = json.loads(line)
            hashes.append(m.get("hash"))
            prevs.append(m.get("prev"))
    ok = True
    for i in range(1, len(hashes)):
        if prevs[i] != hashes[i - 1]:
            ok = False
            break
    return ok, hashes


def main():
    # Threaded: SSE streams hold a connection each; the dashboard also
    # fires /tail+/view+/stream in parallel on load — a single-threaded
    # server deadlocks on the second request.
    #
    # Guard: the server itself is a writer (flush emit, curator, etc.),
    # so register + guard it — when the server stops, session_end fires.
    try:
        pool.register_app("pool-server")
        pool.pool_session_guard("pool-server")
    except Exception:
        pass
    server = PoolServer(("0.0.0.0", PORT), PoolHandler)
    threading.Thread(target=ws_bridge.loop, daemon=True,
                     name="ws-bridge").start()
    print("ws-bridge: chat-pool <-> workspace inbox bridge ON (poll 120s)")
    tok = _get_token()
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = "<your-lan-ip>"
    print(f"pool-server listening on http://0.0.0.0:{PORT}")
    print(f"  this PC : http://localhost:{PORT}")
    print(f"  LAN     : http://{lan_ip}:{PORT}  (Authorization: Bearer {tok[:4]}…{tok[-4:]})")
    print(f"  token   : {TOKEN_FILE}")
    print(f"  log: {pool.LOG}")
    print(f"  UI : http://localhost:{PORT}/   (dashboard)")
    print(f"  POST /register  {{app}}            (first-time identity)")
    print(f"  POST /append    {{app, kind, text, to?, }}  @name in text = tag")
    print(f"  GET  /blocks?q=&status=   (ก้อนเรื่องราว/วิธีการ — shared memory, every platform)")
    print(f"  GET  /blocks?slug=X&revs=1 (one block + ref check + history)")
    print(f"  GET  /blocks/timeline      (session-block timeline + dedup links)")
    print(f"  POST /block     {{app, slug, title, story?, method?, evidence?, refs?, status?}}")
    print(f"  GET  /inbox?app=X        (unread mentions+DMs, marks read; mark=0 peek)")
    print(f"  GET  /neighbors?rkey=0&level=2&raw=1  (plane map; raw=1 = before prune)")
    print(f"  GET  /neighbors?rkey=X&level=0&record=1 (read + declare it as a sighting)")
    print(f"  GET  /bonds?fields=plane  (every edge, without the two 64-hex ends)")
    print(f"  GET  /explore?hash=X&hops=2 (walk one line: backlinks + forward + its shape)")
    print(f"  GET  /explore?rkey=N&hops=2 (walk one address: shapes inside + edges leaving)")
    print(f"  GET  /patterns           (whole plane's link shape: islands/springs/wells + hubs)")
    print(f"  GET  /curator?prune=1    (what the feed hides AND what the plane prunes)")
    print(f"  GET  /tail?n=10   /view?n=20&cloud=0|1   /stream?n=5 (SSE)   /health")
    print(f"  GET  /tasks?q=&status=&owner= (task board: pending/in_progress/done/blocked/cancelled + owner)")
    print(f"  GET  /tasks/ui      (read-only board page)     /tasks/summary")
    print(f"  POST /task {{app, title, desc?, parent?, owner?, due?, tags?, refs?}}")
    print(f"  POST /task/update {{app, id, status?|owner?|note?|action:'claim'}}")
    print(f"  POST /tasks/sync {{apply?}}        (bridge: inbox board → pool tasks; dry-run by default)")
    print(f"  POST /session-end  {{app, force?}}   (auto-write a block on session close)")
    print(f"  POST /flush   (emit + push now)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.server_close()


if __name__ == "__main__":
    main()
