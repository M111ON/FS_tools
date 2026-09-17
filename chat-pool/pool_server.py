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
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>chat<b>-pool</b></h1>
  <span id="chain">chain…</span>
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
  const links=Math.max(h.hashes.length-1,0);
  const el=$('#chain');
  el.textContent=h.chain_ok?`chain ✓ ${links} links`:'chain ✗ BROKEN';
  el.className=h.chain_ok?'ok':'';$('#dot').classList.toggle('on',h.chain_ok);
}
function listen(){
  const as=identity==='admin'?'':'&as='+identity;
  const es=new EventSource('/stream?n=200'+as+(TOKEN?'&token='+TOKEN:''));
  es.onopen=()=>$('#dot').classList.add('on');
  es.onerror=()=>{$('#dot').classList.remove('on');es.close();setTimeout(listen,3000)};
  es.onmessage=ev=>{const m=JSON.parse(ev.data);
    if(!msgs.find(x=>x.hash===m.hash)){msgs.push(m);render()}};
}
listen();
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
            with _lock:
                # one pointer, one level, one read: L0 content · L1 12x12
                # block map · L2 whole-plane map. Over-budget cells come
                # back already split (stacked), never as a wall of lines.
                vis = pool.open_pointer(rk, level=int(lv), as_app=as_app)
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
            # blast-radius read at one point: edges touching a line hash
            h = qs.get("hash", [""])[0].strip().lower()
            if not h:
                self._json(400, {"error": "?hash=<line hash (8+ hex)> required"})
                return
            as_app = qs.get("as", [None])[0]
            with _lock:
                edges = pool.bonds_for(h, as_app=as_app)
            self._json(200, {"hash": h, "edges": edges})

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
