"""pool_server.py — stdlib HTTP wrapper for pool.py + dashboard UI.
POST /append   {app, kind, text}  → {ok, hash}
GET  /tail?n=10                    → [messages...]
GET  /view?n=20&cloud=0            → rendered chat log (pool.view)
GET  /stream?n=5  (SSE)            → real-time new messages
POST /flush                        → flush_emit + push_outbox now
GET  /health                       → {ok, lines}
GET  /                             → dashboard (single file, no CDN)
"""
import json
import os
import sys
import time
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool

PORT = int(os.environ.get("POOL_PORT", 8390))
_lock = threading.Lock()

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
            "สแกนแล้วเบราว์เซอร์จะเก็บ token ให้อัตโนมัติ</div>")
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
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,'Segoe UI',system-ui,sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;align-items:center;gap:14px;padding:10px 18px;border-bottom:1px solid var(--line);background:var(--panel);flex-wrap:wrap}
header h1{font-size:15px;font-weight:600;letter-spacing:.4px}
header h1 b{color:var(--acc)}
.dot{width:9px;height:9px;border-radius:50%;background:#f85149;transition:.3s}
.dot.on{background:var(--acc);box-shadow:0 0 8px var(--acc)}
#chain{font:11px var(--mono);color:var(--dim)}
#chain.ok{color:var(--acc)}
main{flex:1;display:flex;min-height:0}
#feed{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:8px;min-width:0}
@media (max-width:720px){
  main{flex-direction:column}
  aside{width:100%;border-left:none;border-top:1px solid var(--line);flex-direction:row;flex-wrap:wrap}
  aside section{flex:1 1 46%;border-bottom:none}
  #composer{width:100%;margin-top:0}
  #flushtip{width:100%}
}
#feed::-webkit-scrollbar{width:8px}#feed::-webkit-scrollbar-thumb{background:var(--line);border-radius:4px}
.msg{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:9px 13px;max-width:72ch;animation:in .25s ease}
@keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1}}
.msg .top{display:flex;gap:8px;align-items:baseline;margin-bottom:2px}
.badge{font:10px var(--mono);padding:1px 8px;border-radius:9px;background:var(--panel2);color:var(--acc2);border:1px solid var(--line)}
.badge.kind{color:var(--warn)}
.time{font:10px var(--mono);color:var(--dim)}
.hash{font:10px var(--mono);color:var(--dim);margin-left:auto;cursor:pointer}
.hash:hover{color:var(--acc2)}
.text{white-space:pre-wrap;word-break:break-word}
aside{width:300px;border-left:1px solid var(--line);background:var(--panel);display:flex;flex-direction:column}
aside section{padding:13px 15px;border-bottom:1px solid var(--line)}
aside h2{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--dim);margin-bottom:9px}
#q{width:100%;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:7px 11px;font-size:13px;outline:none}
#q:focus{border-color:var(--acc2)}
.apps{display:flex;flex-wrap:wrap;gap:6px}
.appbtn{font:11px var(--mono);padding:3px 10px;border-radius:9px;border:1px solid var(--line);background:var(--panel2);color:var(--dim);cursor:pointer}
.appbtn.on{color:var(--acc);border-color:var(--acc)}
.stat{font:11px var(--mono);color:var(--dim);display:flex;justify-content:space-between;padding:2px 0}
.stat b{color:var(--fg)}
#composer{display:flex;gap:8px;padding:12px 15px;border-top:1px solid var(--line);margin-top:auto}
#msg-in{flex:1;background:var(--bg);border:1px solid var(--line);color:var(--fg);border-radius:8px;padding:8px 11px;font:13px var(--mono);outline:none;resize:none;height:38px}
#msg-in:focus{border-color:var(--acc)}
button.send{background:var(--acc);color:#0d1117;border:none;border-radius:8px;padding:0 16px;font-weight:600;cursor:pointer}
button.send:hover{filter:brightness(1.1)}
#flushtip{font:10px var(--mono);color:var(--dim);text-align:center;padding:6px}
.toast{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--acc);color:var(--acc);font:12px var(--mono);padding:8px 18px;border-radius:9px;opacity:0;transition:.3s;pointer-events:none}
.toast.show{opacity:1}
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
#plane-head{display:flex;align-items:center;gap:10px;padding:11px 15px;border-bottom:1px solid var(--line);flex-wrap:wrap}
#plane-bar{display:flex;align-items:center;gap:12px;padding:8px 15px;border-bottom:1px solid var(--line);font:10px var(--mono);color:var(--dim);flex-wrap:wrap}
#pv-seq{flex:1;min-width:150px;accent-color:var(--acc)}
#pv-when{min-width:220px}
#plane-pop{position:absolute;display:none;z-index:6;background:var(--panel2);border:1px solid var(--acc);border-radius:10px;padding:9px 11px;font:11px var(--mono);color:var(--fg);max-width:330px;box-shadow:0 6px 24px rgba(0,0,0,.5)}
#plane-pop .pp{color:var(--dim);white-space:pre-wrap;margin-top:4px}
#plane-pop .prow{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
#plane-body{flex:1;display:flex;min-height:0}
#plane-canvaswrap{flex:1;display:flex;align-items:center;justify-content:center;padding:14px;position:relative;min-width:0;overflow:auto}
#plane-cv{background:var(--bg);border:1px solid var(--line);border-radius:8px;cursor:crosshair;image-rendering:pixelated}
#plane-side{width:360px;border-left:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
#plane-detail{flex:1;overflow-y:auto;padding:12px 15px;font:11px var(--mono);color:var(--dim);line-height:1.6}
#plane-tip{position:absolute;pointer-events:none;background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:3px 8px;font:10px var(--mono);color:var(--fg);display:none;white-space:nowrap}
#subgrid{display:grid;grid-template-columns:repeat(12,1fr);gap:2px;margin-top:7px}
#subgrid div{aspect-ratio:1;border-radius:2px;background:var(--panel2);cursor:pointer}
#subgrid div:hover{outline:1px solid var(--fg)}
.pline{background:var(--panel2);border:1px solid var(--line);border-radius:7px;padding:6px 8px;margin:6px 0;color:var(--fg)}
.pline .ph{color:var(--dim);cursor:pointer}
.pline .ph:hover{color:var(--acc2)}
@media (max-width:860px){
  #plane-body{flex-direction:column}
  #plane-side{width:100%;border-left:none;border-top:1px solid var(--line)}
}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>chat<b>-pool</b></h1>
  <span id="chain">chain…</span>
  <button class="pbtn" id="open-plane" title="มองระนาบ RDH 144×144 — เปิด pointer ที่จุดที่คลิก">▦ ระนาบ</button>
  <span style="margin-left:auto;font:10px var(--mono);color:var(--dim)" id="clock"></span>
</header>
<main>
  <div id="feed"></div>
  <aside>
    <section>
      <div style="display:flex;gap:8px">
        <input id="q" placeholder="กรองข้อความ…" style="flex:1">
        <select id="apps-sel" title="กรองตามแอป" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);padding:0 6px;max-width:110px">
          <option value="">ทุกแอป</option>
        </select>
      </div>
    </section>
    <section>
      <div style="display:flex;gap:8px;align-items:center">
        <span style="font:10px var(--mono);color:var(--dim)">มุมมอง</span>
        <select id="id-sel" title="View as" style="flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);padding:4px 6px">
          <option value="admin">admin (เห็นหมด)</option>
        </select>
      </div>
      <div id="regbox" style="margin-top:9px;display:none">
        <div style="font:10px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:1px">registry</div>
        <div id="reg-list" style="font:11px var(--mono);color:var(--dim);max-height:130px;overflow-y:auto;white-space:pre-wrap;padding-top:4px"></div>
      </div>
      <div id="inboxbox" style="margin-top:9px;display:none">
        <div style="font:10px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:1px">inbox <b id="ib-count" style="color:var(--acc)">0</b></div>
        <div id="ib-list" style="font:11px var(--mono);color:var(--dim);max-height:150px;overflow-y:auto;white-space:pre-wrap;padding-top:4px"></div>
      </div>
    </section>
    <section>
      <div class="stat"><span>lines</span><b id="st-lines">–</b></div>
      <div class="stat"><span>last append</span><b id="st-last">–</b></div>
      <div class="stat">
        <span>cloud (pool.db)</span>
        <label class="clabel"><input type="checkbox" id="cloudck" style="accent-color:var(--acc)"> show</label>
      </div>
      <div class="cloudwrap" id="cloudwrap"><div id="cloudout" style="font:11px var(--mono);color:var(--dim);white-space:pre-wrap;padding-top:6px">…</div></div>
    </section>
    <div id="composer">
      <textarea id="msg-in" placeholder="พิมพ์ข้อความ… @ชื่อ=tag (Enter ส่ง)"></textarea>
      <button class="send" id="send">ส่ง</button>
      <select id="to-sel" title="ผู้รับ" style="background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;font:12px var(--mono);padding:0 6px">
        <option value="all">→ all</option>
      </select>
    </div>
    <div id="flushtip">flush ทุกชม.อัตโนมัติ · <a href="#" id="flushnow" style="color:var(--acc2)">flush เดี๋ยวนี้</a> = สำเนาขึ้น pool.db ให้ค้นได้</div>
  </aside>
</main>
<div class="toast" id="toast"></div>
<div id="plane-modal">
  <div id="plane-box">
    <div id="plane-head">
      <b style="font-size:13px;color:var(--acc)">ระนาบ RDH 144×144</b>
      <span id="plane-info" style="font:10px var(--mono);color:var(--dim)"></span>
      <span style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">
        <button class="pbtn on" id="pv-plane" title="L2 — ทั้งระนาบเป็นแผนที่ (address+count เท่านั้น)">L2 ระนาบ</button>
        <button class="pbtn" id="pv-blocks" title="L1 — ย่อเป็นบล็อก 12×12">L1 บล็อก</button>
        <button class="pbtn" id="pv-block" title="ซูมเข้าบล็อก 12×12 ที่จุดที่เลือก">ซูมบล็อก</button>
        <button class="pbtn" id="plane-close">ปิด ✕</button>
      </span>
    </div>
    <div id="plane-bar">
      <label class="clabel"><input type="checkbox" id="pv-bonds" checked style="accent-color:var(--acc2)"> bonds</label>
      <span id="pv-when">ทุกบรรทัด</span>
      <input type="range" id="pv-seq" min="0" max="0" value="0">
      <button class="pbtn" id="pv-play">▶ replay</button>
      <button class="pbtn" id="pv-last">ล่าสุด</button>
      <span style="display:flex;gap:4px;align-items:center;margin-left:auto">
        <span style="color:var(--dim)">รัศมี</span>
        <button class="pbtn on" id="pv-f0" title="เห็นทั้งระนาบ">ปิด</button>
        <button class="pbtn" id="pv-f1" title="บังทุกอย่างที่ไม่เกี่ยวข้อง — เหลือเฉพาะที่ต่อจากจุดที่เลือก 1 ช่วง">1 hop</button>
        <button class="pbtn" id="pv-f2">2 hop</button>
        <button class="pbtn" id="pv-f3">3 hop</button>
      </span>
    </div>
    <div id="plane-body">
      <div id="plane-canvaswrap">
        <canvas id="plane-cv"></canvas>
        <div id="plane-tip"></div>
        <div id="plane-pop"></div>
      </div>
      <div id="plane-side"><div id="plane-detail">คลิกช่องบนระนาบเพื่อเปิด pointer ที่จุดนั้น<br><br><span style="color:var(--dim)">◌ เขียว = เซลล์ที่มีข้อมูล (เข้ม = แน่นกว่า)<br>◌ วงฟ้า = จุดที่มีคนประกาศว่าอ่าน (sighting)<br>◌ เส้นฟ้า = bond ระหว่างบรรทัด · เส้นส้ม = bond ที่แตะจุดที่เลือก (blast radius)<br>◌ ลากแถบ tick เพื่อย้อนดูว่าระนาบค่อย ๆ ติดทีละบรรทัด</span></div></div>
    </div>
  </div>
</div>
<script>
const $=s=>document.querySelector(s);
let msgs=[], filterQ='', filterApps=new Set(), identity='admin', myInbox=[];
// LAN access: token prompt (localhost skips auth server-side, this is harmless there)
const TOKEN=(()=>{const m=location.hash.match(/token=([0-9a-f]+)/);const q=new URLSearchParams(location.search).get('token');const t=m?m[1]:q;if(t){localStorage.setItem('pool_token',t);history.replaceState(null,'',location.pathname)}return t||localStorage.getItem('pool_token')||''})();
const H=TOKEN?{'Authorization':'Bearer '+TOKEN}:{'x-noauth':'1'};
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function fmt(t){const d=new Date(t*1000);return d.toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'})+(d.getDate()!=new Date().getDate()?' '+d.toLocaleDateString('th-TH',{day:'2-digit',month:'short'}):'')}
function hash8(h){return h?h.slice(0,8):''}

function render(){
  const feed=$('#feed');
  const list=msgs.filter(m=>
    (!filterQ||(m.text||'').toLowerCase().includes(filterQ))&&
    (filterApps.size===0||filterApps.has(m.app))
  );
  feed.innerHTML=list.map(m=>{
    const kind=m.kind||'chat';
    const kbadge=kind==='chat'?'':`<span class="badge kind">${esc(kind)}</span>`;
    const arrow=m.to&&m.to!=='all'?`<span class="badge kind">→ ${esc(m.to)}</span>`:'';
    return `<div class="msg"><div class="top"><span class="badge">${esc(m.app||'?')}</span>${kbadge}${arrow}<span class="time">${fmt(m.t)}</span><span class="hash" title="คลิกเพื่อ copy" onclick="navigator.clipboard.writeText('${m.hash||''}');toast('copied '+ '${hash8(m.hash)}')">${hash8(m.hash)}</span></div><div class="text">${esc(m.text||'')}</div></div>`;
  }).join('')||'<div style="color:var(--dim);text-align:center;padding:40px">ไม่มีข้อความ</div>';
  // populate selects (apps filter, view-as, to)
  const apps={};msgs.forEach(m=>apps[m.app||'?']=(apps[m.app||'?']||0)+1);
  const known=[...new Set([...Object.keys(apps),'hermes','codex','human'])].sort();
  const asel=$('#apps-sel');const acur=asel.value;
  asel.innerHTML='<option value="">ทุกแอป</option>'+Object.entries(apps).sort((a,b)=>b[1]-a[1]).map(([a,n])=>`<option value="${esc(a)}">${esc(a)} (${n})</option>`).join('');
  if([...asel.options].some(o=>o.value===acur))asel.value=acur;
  const isel=$('#id-sel');const icur=isel.value;
  isel.innerHTML='<option value="admin">admin (เห็นหมด)</option>'+known.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');
  if([...isel.options].some(o=>o.value===icur))isel.value=icur;
  const sel=$('#to-sel');const cur=sel.value;
  sel.innerHTML='<option value="all">→ all</option>'+known.filter(a=>a!=='human').map(a=>`<option value="${esc(a)}">→ ${esc(a)}</option>`).join('');
  if([...sel.options].some(o=>o.value===cur))sel.value=cur;
  $('#st-lines').textContent=msgs.length;
  $('#st-last').textContent=msgs.length?fmt(msgs[msgs.length-1].t):'–';
}
window.setIdentity=async a=>{identity=a;refresh().catch(()=>{});await registerIdentity();loadInbox().catch(()=>{})};

async function loadInbox(mark){
  const regbox=$('#regbox'), inboxbox=$('#inboxbox');
  try{
    const reg=await fetch('/apps',{headers:H}).then(r=>r.json());
    regbox.style.display='block';
    $('#reg-list').innerHTML=reg.map(a=>{
      const seen=new Date(a.last_seen*1000);
      const live=(Date.now()-seen.getTime())<120000;
      return `${live?'🟢':'⚪'} ${esc(a.app)} · ${seen.toLocaleTimeString('th-TH',{hour:'2-digit',minute:'2-digit'})}`;
    }).join('\n')||'(ยังไม่มีใคร register)';
  }catch{regbox.style.display='none'}
  if(identity==='admin'){inboxbox.style.display='none';return}
  inboxbox.style.display='block';
  const r=await fetch('/inbox?app='+identity+(mark===false?'&mark=0':''),{headers:H}).then(r=>r.json());
  if(r.error){$('#ib-list').textContent='⚠ '+r.error;$('#ib-count').textContent='?';return}
  myInbox=r;
  $('#ib-count').textContent=r.length;
  $('#ib-list').innerHTML=r.length?r.map(m=>`[${fmt(m.t)}] ${esc(m.app)}: ${esc(m.text).slice(0,90)}`).join('\n'):'(ว่าง — ไม่มี mention/DM ใหม่)';
}
window.toast=t=>{const el=$('#toast');el.textContent=t;el.classList.add('show');setTimeout(()=>el.classList.remove('show'),1600)};

$('#q').oninput=e=>{filterQ=e.target.value.toLowerCase();render()};
$('#apps-sel').onchange=e=>{filterApps=new Set(e.target.value?[e.target.value]:[]);render()};
$('#id-sel').onchange=e=>setIdentity(e.target.value);
$('#cloudck').onchange=async e=>{
  $('#cloudwrap').classList.toggle('open',e.target.checked);
  if(e.checked){const r=await fetch('/view?n=6&cloud=1',{headers:H}).then(r=>r.json());
    $('#cloudout').textContent=r.lines?.join('\n')||'(empty)';}
};
async function send(){
  const el=$('#msg-in');const text=el.value.trim();if(!text)return;
  el.value='';el.style.height='38px';
  const r=await fetch('/append',{method:'POST',headers:{'Content-Type':'application/json',...H},body:JSON.stringify({app:'human',kind:'chat',text,to:$('#to-sel').value})}).then(r=>r.json());
  if(r.ok)toast('sent '+r.hash.slice(0,8));else toast('error: '+(r.error||''));
}

async function registerIdentity(){
  if(identity==='admin')return;
  const r=await fetch('/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({app:identity})}).then(r=>r.json());
  if(r.error)toast('register fail: '+r.error);else toast(r.created?('registered '+identity):identity+' ok');
}
$('#send').onclick=send;
$('#msg-in').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send()}};
$('#msg-in').oninput=e=>{e.target.style.height='38px';e.target.style.height=Math.min(e.target.scrollHeight,140)+'px'};
$('#flushnow').onclick=async e=>{e.preventDefault();const r=await fetch('/flush',{method:'POST',headers:H}).then(r=>r.json());toast(r.pushed?`flush ok: ${r.pushed.length} pushed`:(r.emitted?'flush ok':'nothing to flush'));refresh()};

async function refresh(){
  const as=identity==='admin'?'':'&as='+identity;
  const [tail,h]=await Promise.all([
    fetch('/tail?n=200'+as,{headers:H}).then(r=>r.json()),
    fetch('/view?n=500'+as,{headers:H}).then(r=>r.json())
  ]);
  msgs=tail;
  render();
  const links=Math.max(h.hashes.length-1,0);
  const el=$('#chain');
  el.textContent=h.chain_ok?`chain ✓ ${links} links`:'chain ✗ BROKEN';
  el.className=h.chain_ok?'ok':'';$('#dot').classList.toggle('on',h.chain_ok);
}
setInterval(()=>$('#clock').textContent=new Date().toLocaleTimeString('th-TH'),1000);
async function refresh(){
  const as=identity==='admin'?'':'&as='+identity;
  const [tail,h]=await Promise.all([
    fetch('/tail?n=200'+as,{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()}),
    fetch('/view?n=500'+as,{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()})
  ]).catch(()=>{document.getElementById('qrpanel')?.style.setProperty('display','block');throw 'auth'});
  document.getElementById('qrpanel')?.style.setProperty('display','none');
  msgs=tail;
  render();
  paintChain(h);
}
function listen(){
  const as=identity==='admin'?'':'&as='+identity;
  const es=new EventSource('/stream?n=200'+as+(TOKEN?'&token='+TOKEN:''));
  es.onopen=()=>$('#dot').classList.add('on');
  es.onerror=()=>{$('#dot').classList.remove('on');es.close();setTimeout(listen,3000)};
  es.onmessage=ev=>{const m=JSON.parse(ev.data);
    if(!msgs.find(x=>x.hash===m.hash)){msgs.push(m);render();syncChain()}};
}
function paintChain(h){
  const links=Math.max(h.hashes.length-1,0);
  const el=$('#chain');
  el.textContent=h.chain_ok?`chain ✓ ${links} links`:'chain ✗ BROKEN';
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
      paintChain(await fetch('/view?n=500'+as,{headers:H}).then(r=>r.json()));
    }catch{}
  },1200);
}
listen();

/* ── RDH plane 144×144 — vision you can see ───────────────────────────
   The plane is a MAP, so this canvas can only ever draw addresses and
   counts: no content reaches it without a pointer. Every click is one
   explicit read (cell → sub-cell → sub-sub-cell), and L1/L2 are just
   later reads at a coarser point — nothing here is cached state. */
const PV={mode:'plane',n:144,sel:null,path:[],plane:{},cells:{},origin:null,
          budget:64,over:[],sights:[],lines:null,stacked:null,timer:null,
          bonds:[],timeline:[],seqMax:0,seq:null,showBonds:true,playing:null,focus:0};
const pcv=$('#plane-cv');
const pstep=()=>PV.n===144?3:36;

// the plane at tick S: every line whose append ordinal is <= S, counted
// straight from the log's own order — no wall clock is consulted anywhere
function pvCellsUpTo(S){
  const c={};
  for(let i=0;i<PV.timeline.length&&i<=S;i++){
    const rk=PV.timeline[i][2];
    if(rk!=null)c[rk]=(c[rk]||0)+1;
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
function pvKeep(){
  if(!PV.focus||PV.sel==null)return null;
  const adj={};
  pvLiveBonds().forEach(b=>{
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
  return seen;
}
function pvDerive(){
  const full=PV.seq===null||PV.seq>=PV.seqMax-1;
  const src=full?PV.plane:pvCellsUpTo(PV.seq);
  PV.over=Object.keys(src).filter(k=>src[k]>PV.budget).map(Number);
  if(PV.mode==='plane'){PV.n=144;PV.cells=src;return}
  if(PV.mode==='blocks'){
    PV.n=12;const agg={};
    for(const k in src){
      const rk=+k,bx=Math.floor((rk%144)/12),by=Math.floor(rk/144/12),key=by*12+bx;
      agg[key]=(agg[key]||0)+src[k];
    }
    PV.cells=agg;return;
  }
  const bw=PV.origin.wedge,br=PV.origin.ring;PV.n=12;const c={};
  for(const k in src){
    const rk=+k,w=rk%144,r=Math.floor(rk/144);
    if(w>=bw&&w<bw+12&&r>=br&&r<br+12)c[rk]=src[k];
  }
  PV.cells=c;
}
function pvCenter(rk){
  const [x,y]=pvXY(rk);
  if(x<0||y<0||x>=PV.n||y>=PV.n)return null;
  const step=pstep();
  return[x*step+step/2,y*step+step/2];
}
function pvXY(rk){
  if(PV.mode==='plane')return[rk%144,Math.floor(rk/144)];
  if(PV.mode==='blocks')return[Math.floor((rk%144)/12),Math.floor(rk/144/12)];
  return[rk%144-PV.origin.wedge,Math.floor(rk/144)-PV.origin.ring];
}
function pvRkeyAt(x,y){
  if(PV.mode==='plane')return y*144+x;
  if(PV.mode==='blocks')return(y*12)*144+x*12;          // origin of that square
  return(PV.origin.ring+y)*144+PV.origin.wedge+x;
}
function pvCountAt(x,y,rk){return(PV.mode==='blocks'?PV.cells[y*12+x]:PV.cells[rk])||0}
function pvDraw(){
  const n=PV.n,step=pstep(),px=n*step,dpr=window.devicePixelRatio||1;
  pcv.width=px*dpr;pcv.height=px*dpr;pcv.style.width=px+'px';pcv.style.height=px+'px';
  const c=pcv.getContext('2d');c.setTransform(dpr,0,0,dpr,0,0);
  c.fillStyle='#0d1117';c.fillRect(0,0,px,px);
  const vals=Object.values(PV.cells),max=vals.length?Math.max(...vals):1;
  const keep=pvKeep();
  for(const k in PV.cells){
    const [x,y]=pvXY(+k);if(x<0||y<0||x>=n||y>=n)continue;
    const near=!keep||keep.has(+k);
    const a=(0.22+0.78*(PV.cells[k]/max))*(near?1:0.10);   // masked = barely there
    c.fillStyle='rgba(126,231,135,'+a.toFixed(2)+')';
    // at plane scale one cell is ~3px, so a single line is nearly invisible;
    // every occupied cell gets a floor of 2px so the map is actually readable
    const w=Math.max(step,2),o=(w-step)/2;
    c.fillRect(x*step-o,y*step-o,w,w);
    if(keep&&near&&+k!==PV.sel){                        // in radius: mark it
      c.strokeStyle='rgba(126,231,135,.75)';c.lineWidth=1;
      c.strokeRect(x*step-o-.5,y*step-o-.5,w+1,w+1);
    }
  }
  PV.sights.forEach(s=>{                    // declared reads (data, not state)
    const [x,y]=pvXY(s.point);if(x<0||y<0||x>=n||y>=n)return;
    c.strokeStyle='#79c0ff';c.lineWidth=1;c.strokeRect(x*step+.5,y*step+.5,step-1,step-1);
  });
  // edges are drawn from the addresses of their two ends — a bond line
  // carries the meaning, the plane only shows where both ends sit
  if(PV.showBonds){
    pvLiveBonds().forEach(b=>{
      const a=pvCenter(b.src_rkey),z=pvCenter(b.dst_rkey);
      if(!a||!z)return;                       // only edges fully inside this view
      const hot=PV.sel!=null&&(b.src_rkey===PV.sel||b.dst_rkey===PV.sel);
      // mask: an edge is dropped when neither end is in the radius; an edge
      // half-way out stays as a faint hint that the graph continues
      let alpha=0.5;
      if(keep){
        const s=keep.has(b.src_rkey),d=keep.has(b.dst_rkey);
        if(!s&&!d)return;
        if(!s||!d)alpha=0.16;
      }
      if(a[0]===z[0]&&a[1]===z[1]){           // self-loop: same cell, ring it
        c.strokeStyle=hot?'#ffa657':'rgba(121,192,255,'+alpha+')';c.lineWidth=hot?2:1;
        c.beginPath();c.arc(a[0],a[1],step*1.6+2,0,7);c.stroke();return;
      }
      c.strokeStyle=hot?'#ffa657':'rgba(121,192,255,'+alpha+')';
      c.lineWidth=hot?2:1;
      c.beginPath();c.moveTo(a[0],a[1]);c.lineTo(z[0],z[1]);c.stroke();
      if(step>6||hot)[a,z].forEach(p=>{c.fillStyle=hot?'#ffa657':'#79c0ff';c.beginPath();c.arc(p[0],p[1],hot?2.5:1.8,0,7);c.fill()});
    });
  }
  if(PV.sel!=null){
    const [x,y]=pvXY(PV.sel);
    if(x>=0&&y>=0&&x<n&&y<n){c.strokeStyle='#ffa657';c.lineWidth=2;c.strokeRect(x*step+1,y*step+1,step-2,step-2)}
  }
}
function pvCellAt(ev){
  const r=pcv.getBoundingClientRect(),step=pstep();
  const x=Math.floor((ev.clientX-r.left)/step),y=Math.floor((ev.clientY-r.top)/step);
  if(x<0||y<0||x>=PV.n||y>=PV.n)return null;
  return[x,y,pvRkeyAt(x,y)];
}
function pvPop(){
  const pop=$('#plane-pop'),d=PV.lines;
  if(PV.sel==null||!d){pop.style.display='none';return}
  const keep=pvKeep();
  const mine=PV.bonds.filter(b=>b.src_rkey===PV.sel||b.dst_rkey===PV.sel);
  let h='<div style="color:var(--warn)">point '+d.point+' · '+d.count+' line(s)</div>';
  (d.lines||[]).slice(0,2).forEach(m=>{
    h+='<div class="pp">'+esc(m.app||'?')+' '+(m.kind&&m.kind!=='chat'?esc(m.kind)+' ':'')+esc((m.text||'').slice(0,90))+'</div>';
  });
  mine.slice(0,4).forEach(b=>{
    const out=b.src_rkey===PV.sel;
    h+='<div class="pp" style="color:var(--acc2)">'+esc(b.rel)+' '+(out?'→ ':'← ')+(out?b.dst8:b.src8)+'</div>';
  });
  if(keep)h+='<div class="pp">รัศมี '+PV.focus+' hop → '+keep.size+' เซลล์</div>';
  h+='<div class="prow"><button class="pbtn" id="pop-focus">'+(PV.focus?'ปิดรัศมี':'โฟกัส 1 hop')+'</button>'
    +'<button class="pbtn" id="pop-zoom">ซูมบล็อก</button>'
    +'<button class="pbtn" id="pop-close">ปิด</button></div>';
  pop.innerHTML=h;pop.style.display='block';
  const cr=pcv.getBoundingClientRect(),wr=$('#plane-canvaswrap').getBoundingClientRect();
  const ctr=pvCenter(PV.sel)||[0,0];
  const x=Math.max(4,Math.min((cr.left-wr.left)+ctr[0]+14,wr.width-pop.offsetWidth-10));
  const y=Math.max(4,Math.min((cr.top-wr.top)+ctr[1]+14,wr.height-pop.offsetHeight-10));
  pop.style.left=x+'px';pop.style.top=y+'px';
  $('#pop-focus').onclick=()=>setFocus(PV.focus?0:1);
  $('#pop-zoom').onclick=()=>pvZoomBlock(PV.sel);
  $('#pop-close').onclick=()=>pop.style.display='none';
}
function setFocus(h){
  PV.focus=h;pvFocusBtns();pvDraw();pvInfo();pvPop();
  if(h&&PV.sel==null)toast('เลือวจุดบนระนาบก่อน แล้วรัศมีจะบังส่วนที่ไม่เกี่ยวข้องให้');
}
function pvFocusBtns(){
  for(let i=0;i<4;i++)$('#pv-f'+i).classList.toggle('on',PV.focus===i);
}
function pvInfo(){
  const src=(PV.seq===null||PV.seq>=PV.seqMax-1)?PV.plane:pvCellsUpTo(PV.seq);
  const tot=Object.values(src).reduce((a,b)=>a+b,0);
  $('#plane-info').textContent='• เซลล์ '+Object.keys(src).length+'/'+20736+' · '+tot+' lines'
    +' · budget '+PV.budget+(PV.over.length?' · ต้องแตกชั้น '+PV.over.length+' เซลล์':'')
    +' · edges '+pvLiveBonds().length+' · sightings '+PV.sights.length
    +(()=>{const k=pvKeep();return k?(' · รัศมี '+PV.focus+' hop = '+k.size+' เซลล์ (บังที่เหลือ)'):''})();
}
function pvMode(){
  $('#pv-plane').classList.toggle('on',PV.mode==='plane');
  $('#pv-blocks').classList.toggle('on',PV.mode==='blocks');
  $('#pv-block').classList.toggle('on',PV.mode==='block');
  pvDerive();pvDraw();pvInfo();pvPop();   // popup rides along as the view changes
}
async function loadPlane(){
  try{
    const [d,s,b,t]=await Promise.all([
      fetch('/neighbors?rkey=0&level=2',{headers:H}).then(r=>{if(r.status===401)throw 401;return r.json()}),
      fetch('/pointers',{headers:H}).then(r=>r.json()),
      fetch('/bonds',{headers:H}).then(r=>r.json()),
      fetch('/plane/timeline',{headers:H}).then(r=>r.json())
    ]);
    PV.plane=d.fine_cells||{};PV.budget=d.budget||64;
    PV.sights=s.pointers||[];
    PV.timeline=t.lines||[];PV.seqMax=t.count||0;
    // place each edge on the plane and stamp it with the tick of its own line,
    // so scrubbing back in time hides edges that had not been written yet
    const seqOf={};PV.timeline.forEach(r=>{seqOf[r[4]]=r[0]});
    PV.bonds=(b.edges||[]).map(e=>({...e,seq:seqOf[e.hash.slice(0,8)]}));
    const sl=$('#pv-seq');sl.max=Math.max(PV.seqMax-1,0);
    if(PV.seq===null)sl.value=sl.max;
    pvMode();pvWhen();
  }catch(e){$('#plane-info').textContent='โหลดระนาบไม่ได้ ('+e+')'}
}
function pvWhen(){
  const S=PV.seq===null?PV.seqMax-1:PV.seq;
  const row=PV.timeline[S];
  $('#pv-when').textContent=(PV.seq===null?'ทุกบรรทัด':('tick '+S+'/'+Math.max(PV.seqMax-1,0)))
    +(row?(' · '+fmt(row[1])+' · '+row[3]):'');
}
async function pvZoomBlock(rk){
  const d=await fetch('/neighbors?rkey='+rk+'&level=1',{headers:H}).then(r=>r.json());
  PV.origin=d.origin;PV.mode='block';pvMode();
  const keys=Object.keys(d.fine_cells).map(Number);
  const busiest=keys.length?keys.reduce((a,b)=>d.fine_cells[a]>=d.fine_cells[b]?a:b):d.base;
  openPoint(busiest);
}
async function openPoint(rk,path){
  PV.sel=rk;PV.path=path||[];
  const q='/neighbors?rkey='+rk+'&level=0'+(PV.path.length?'&path='+PV.path.join(','):'');
  const d=await fetch(q,{headers:H}).then(r=>r.json());
  PV.lines=d;PV.stacked=d.stacked||null;
  pvDetail();pvDraw();pvPop();
}
function pvDetail(){
  const d=PV.lines,el=$('#plane-detail');
  if(!d){el.textContent='…';return}
  let h='<div style="color:var(--fg);font-size:12px">point <b style="color:var(--warn)">'+d.point
    +'</b> · '+d.count+' line(s)</div>';
  h+='<div style="margin:4px 0">(x '+(d.point%144)+', y '+Math.floor(d.point/144)+') บนระนาบ'
    +(PV.path.length?' · path '+PV.path.map((v,i)=>'ชั้น '+(i+1)+'='+v).join(' → '):'')+'</div>';
  if(PV.path.length)h+='<button class="pbtn" id="pv-up">↑ ขึ้นชั้น</button>';
  if(PV.stacked){
    const cells=PV.stacked.sub_cells,keys=Object.keys(cells).map(Number);
    const max=keys.length?Math.max(...keys.map(k=>cells[k])):1;
    h+='<div style="color:var(--warn);margin-top:8px">แน่นเกิน budget '+PV.stacked.budget
      +' — แตกเป็น sub-map ชั้น '+PV.stacked.depth+' (address ล้วน)'
      +(PV.stacked.still_over?' · ยังมี '+PV.stacked.still_over+' ช่องที่ต้องลงต่อ':'')+'</div>';
    h+='<div id="subgrid">';
    for(let i=0;i<144;i++){
      const v=cells[i]||0,bg=v?'rgba(126,231,135,'+(0.25+0.75*(v/max)).toFixed(2)+')':'var(--panel2)';
      h+='<div data-sub="'+i+'" title="sub '+i+' · '+v+' line(s)" style="background:'+bg+'"></div>';
    }
    h+='</div>';
  }
  const mine=PV.bonds.filter(b=>b.src_rkey===PV.sel||b.dst_rkey===PV.sel);
  if(mine.length){
    h+='<div style="color:var(--acc2);margin-top:8px">bonds ที่แตะจุดนี้ ('+mine.length+') — คลิกปลายทางเพื่อเดินต่อ</div>';
    h+=mine.map(b=>{
      const out=b.src_rkey===PV.sel,other=out?b.dst8:b.src8,ork=out?b.dst_rkey:b.src_rkey;
      return '<div class="pline" style="border-color:var(--acc2)"><span class="badge">'+esc(b.app||'?')
        +'</span> '+(out?'→':'←')+' <b style="color:var(--acc2)">'+esc(b.rel)+'</b> '
        +'<span class="ph" title="walk ไปปลายอีกด้าน" onclick="openPoint('+(ork==null?'null':ork)+')">'
        +other+(ork==null?' (ไม่เห็นปลายทาง)':'')+'</span></div>';
    }).join('');
  }
  const ls=d.lines||[];
  h+=ls.length?ls.slice(0,40).map(m=>'<div class="pline"><span class="badge">'+esc(m.app||'?')
      +'</span> <span class="badge kind">'+esc(m.kind||'')+'</span> <span class="ph" onclick="navigator.clipboard.writeText(\''
      +(m.hash||'')+'\');toast(\'copied '+hash8(m.hash)+'\')">'+hash8(m.hash)+'</span>'
      +'<div style="white-space:pre-wrap;margin-top:3px">'+esc((m.text||'').slice(0,240))+'</div></div>').join('')
    :'<div style="margin-top:6px">เซลล์นี้ว่าง — ไม่มี line อยู่ตรงนี้</div>';
  el.innerHTML=h;
  el.querySelectorAll('#subgrid div').forEach(node=>{
    node.onclick=()=>openPoint(PV.sel,PV.path.concat([+node.dataset.sub]));
  });
  const up=$('#pv-up');
  if(up)up.onclick=()=>openPoint(PV.sel,PV.path.slice(0,-1));
}
pcv.onmousemove=ev=>{
  const tip=$('#plane-tip'),hit=pvCellAt(ev);
  if(!hit){tip.style.display='none';return}
  const [x,y,rk]=hit,n=pvCountAt(x,y,rk);
  const who=PV.sights.filter(s=>s.point===rk).map(s=>s.app).join(',');
  tip.textContent=(PV.mode==='blocks'?'บล็อก ':'rkey ')+rk+' ('+x+','+y+') · '+n+' line'+(n===1?'':'s')
    +(who?' · 👁 '+who:'');
  tip.style.display='block';
  const wrap=$('#plane-canvaswrap').getBoundingClientRect();
  tip.style.left=(ev.clientX-wrap.left+12)+'px';tip.style.top=(ev.clientY-wrap.top+12)+'px';
};
pcv.onmouseleave=()=>$('#plane-tip').style.display='none';
pcv.onclick=ev=>{
  const hit=pvCellAt(ev);if(!hit)return;
  const [x,y,rk]=hit;
  if(PV.mode==='blocks'){pvZoomBlock(rk);return}   // zoom = another read, finer
  openPoint(rk);
};
$('#open-plane').onclick=()=>{
  $('#plane-modal').classList.add('open');
  loadPlane();
  PV.timer=setInterval(loadPlane,8000);           // watch it breathe
};
function pvClose(){
  $('#plane-modal').classList.remove('open');
  $('#plane-pop').style.display='none';
  if(PV.timer){clearInterval(PV.timer);PV.timer=null}
}
$('#plane-close').onclick=pvClose;
$('#plane-modal').onclick=e=>{if(e.target.id==='plane-modal')pvClose()};
document.addEventListener('keydown',e=>{if(e.key==='Escape')pvClose()});
$('#pv-plane').onclick=()=>{PV.mode='plane';pvMode()};
$('#pv-blocks').onclick=()=>{PV.mode='blocks';pvMode()};
$('#pv-block').onclick=()=>{
  if(PV.sel==null||!PV.plane[PV.sel]){toast('เลือกจุดบนระนาบก่อน');return}
  pvZoomBlock(PV.sel);
};
$('#pv-bonds').onchange=e=>{PV.showBonds=e.target.checked;pvDraw()};
$('#pv-f0').onclick=()=>setFocus(0);
$('#pv-f1').onclick=()=>setFocus(1);
$('#pv-f2').onclick=()=>setFocus(2);
$('#pv-f3').onclick=()=>setFocus(3);
$('#pv-seq').oninput=e=>{
  clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ replay';
  PV.seq=+e.target.value;
  if(PV.seq>=PV.seqMax-1)PV.seq=null;      // pinned at the end = live plane
  pvDerive();pvDraw();pvInfo();pvWhen();
};
$('#pv-last').onclick=()=>{
  PV.seq=null;$('#pv-seq').value=$('#pv-seq').max;
  pvDerive();pvDraw();pvInfo();pvWhen();
};
$('#pv-play').onclick=()=>{
  if(PV.playing){clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ replay';return}
  $('#pv-play').textContent='⏸ หยุด';
  PV.seq=0;$('#pv-seq').value=0;
  PV.playing=setInterval(()=>{
    if(PV.seq===null||PV.seq>=PV.seqMax-1){
      clearInterval(PV.playing);PV.playing=null;$('#pv-play').textContent='▶ replay';
      PV.seq=null;$('#pv-seq').value=$('#pv-seq').max;pvDerive();pvDraw();pvInfo();pvWhen();
      return;
    }
    PV.seq++;$('#pv-seq').value=PV.seq;
    pvDerive();pvDraw();pvInfo();pvWhen();
  },Math.max(60,Math.round(2400/Math.max(PV.seqMax,1))));
};

// boot: initial pull for chain/registry/inbox — SSE only carries new lines,
// without this a LAN phone shows the shell with a stuck "chain…" forever
refresh().catch(()=>{});
loadInbox().catch(()=>{});
</script>
</body>
</html>"""


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
                     + "<div style='font:12px var(--mono);color:var(--warn);margin-bottom:6px'>⚠ token ไม่ถูกต้อง — สแกน QR นี้จากมือถือ (token ฝังใน QR)</div>"
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
            n = min(int(qs.get("n", ["10"])[0]), 500)
            as_app = qs.get("as", [None])[0]
            with _lock:
                msgs = pool.tail(n, as_app=as_app)
            self._json(200, msgs)

        elif parsed.path == "/apps":
            reg = pool.list_apps()
            apps = sorted(
                ({"app": name, **info} for name, info in reg.items()),
                key=lambda a: -a.get("last_seen", 0))
            self._json(200, apps)

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
            with _lock:
                # one pointer, one level, one read: L0 content · L1 12x12
                # block map · L2 whole-plane map. Over-budget cells come
                # back already split (stacked), never as a wall of lines,
                # and &path=1,42 walks one sub-cell down at a time.
                vis = pool.open_pointer(rk, level=int(lv), path=path, as_app=as_app)
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
            with _lock:
                seen = pool.pointers_at(rk, lv)
            self._json(200, {"point": rk, "level": lv, "pointers": seen})

        elif parsed.path == "/bonds":
            # blast-radius read at one point: edges touching a line hash.
            # No ?hash= = every live edge, each end already placed in its cell
            # (that is what lets the plane draw the whole graph in one read).
            as_app = qs.get("as", [None])[0]
            h = qs.get("hash", [""])[0].strip().lower()
            with _lock:
                if h:
                    self._json(200, {"hash": h, "edges": pool.bonds_for(h, as_app=as_app)})
                    return
                edges = pool.all_bonds(as_app=as_app)
            self._json(200, {"hash": None, "count": len(edges), "edges": edges})

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
            n = min(int(qs.get("n", ["20"])[0]), 1000)
            cloud = qs.get("cloud", ["0"])[0] == "1"
            as_app = qs.get("as", [None])[0]
            if as_app == "admin":
                as_app = None
            try:
                lines = pool.view(n, cloud=cloud, as_app=as_app)
                # chain audit for the dashboard header
                ok, hashes = _chain_audit()
                self._json(200, {"lines": lines, "chain_ok": ok, "hashes": hashes})
            except Exception as e:
                self._json(500, {"error": str(e)})

        elif parsed.path == "/stream":
            n = min(int(qs.get("n", ["5"])[0]), 500)
            as_app = qs.get("as", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            seen = 0
            try:
                while True:
                    with _lock:
                        msgs = pool.tail(n, as_app=as_app)
                    if len(msgs) > seen:
                        for m in msgs[seen:]:
                            self.wfile.write(f"data: {json.dumps(m, ensure_ascii=False)}\n\n".encode())
                        self.wfile.flush()
                        seen = len(msgs)
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not self._authorized():
            self._json(401, {"error": "token required — see .pool_token on the server"})
            return
        if parsed.path == "/register":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            name = body.get("app", "")
            try:
                ok, created = pool.register_app(name)
                self._json(200, {"ok": ok, "created": created,
                                 "apps": sorted(pool.list_apps())})
            except ValueError as e:
                self._json(400, {"error": str(e)})
        elif parsed.path == "/append":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
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
            self._json(200, {"ok": True, "hash": result["hash"],
                             "tags": result.get("tags", [])})
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
    server = ThreadingHTTPServer(("0.0.0.0", PORT), PoolHandler)
    server.daemon_threads = True
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
    print(f"  GET  /inbox?app=X        (unread mentions+DMs, marks read; mark=0 peek)")
    print(f"  GET  /tail?n=10   /view?n=20&cloud=0|1   /stream?n=5 (SSE)   /health")
    print(f"  POST /flush   (emit + push now)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.server_close()


if __name__ == "__main__":
    main()
