#!/usr/bin/env python3
"""chat_export_ui.py — WebUI ไฟล์เดียวสำหรับ export + กรองแชท opencode.

ใช้:  python chat_export_ui.py [--port 8099]
เปิด:  http://127.0.0.1:8099
"""
import argparse
import html
import http.server
import json
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

OUT = Path("I:/.vault/exported_session")
OUT.mkdir(parents=True, exist_ok=True)

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>chat export</title><style>body{font-family:system-ui;max-width:720px;margin:32px auto;padding:0 16px}input{width:320px;padding:8px}button{padding:8px 16px}#r,#pv{white-space:pre-wrap;background:#f4f4f4;padding:12px}</style>
<h2>opencode chat export</h2>
<form id=f><input id=sid name=sid placeholder="ses_..." size=40 required>
<button>export + กรองแชท</button></form>
<p id=r>พร้อม</p>
<h3>แปลงไฟล์ JSON ที่มีอยู่แล้ว (ลากไฟล์มาใส่ ไม่ต้อง export ใหม่)</h3>
<input type=file id=file accept=".json,application/json">
<pre id=pv>เอาไฟล์ export (.json) มาวางตรงนี้ จะกรองเหลือแต่แชทให้เลย</pre>
<script>
document.getElementById('file').onchange=e=>{const f=e.target.files[0];if(!f)return;
const rd=new FileReader();rd.onload=()=>{try{let t=rd.result;t=t.slice(t.indexOf('{'));const d=JSON.parse(t);
const chat=[];for(const m of(d.messages||[])){const r=m.info&&m.info.role;if(r!=='user'&&r!=='assistant')continue;
const tx=(m.parts||[]).filter(p=>p.type==='text'&&(p.text||'').trim()).map(p=>p.text);if(tx.length)chat.push({role:r,text:tx.join('\n')})}
document.getElementById('pv').textContent=chat.map((m,i)=>`## ${i+1}. ${m.role}\n${m.text}`).join('\n\n---\n\n')||'(ไม่มี text parts)';
const blob=new Blob([chat.map((m,i)=>`## ${i+1}. ${m.role}\n\n${m.text}\n\n---`).join('\n\n')],{type:'text/markdown'});
const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=f.name.replace(/\\.json$/,'')+'.chat.md';a.textContent='โหลด .chat.md';document.getElementById('pv').after(a);
}catch(err){document.getElementById('pv').textContent='ERR: '+err}};rd.readAsText(f)};</script>
<h3>ไฟล์ chat ที่แปลงแล้ว</h3><ul id=l></ul>
<script>
const r=document.getElementById('r');
async function list(){const j=await(await fetch('/api/list')).json();
document.getElementById('l').innerHTML=j.files.map(f=>`<li><a href="/dl?f=${f}">${f}</a></li>`).join('')||'<li>(ยังไม่มี)</li>'}
list();
document.getElementById('f').onsubmit=async e=>{e.preventDefault();r.textContent='กำลัง export...';
const sid=document.getElementById('sid').value.trim();
const j=await(await fetch('/api/export',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:'sid='+encodeURIComponent(sid)})).json();
r.textContent=j.ok?`OK: ${j.chat_json} (${j.n_chat}/${j.n_total} msg)\\n${j.chat_md}`:'ERR: '+j.error;list()};
</script>"""


def _opencode_bin():
    for c in (r"C:\nvm4w\nodejs\node_modules\opencode-ai\bin\opencode.exe",
              shutil.which("opencode.exe"), shutil.which("opencode")):
        if c and os.path.exists(c):
            return c
    return "opencode"


def export_and_filter(sid):
    sid = sid.strip()
    if not sid.startswith("ses_"):
        raise ValueError("session id ต้องขึ้นต้นด้วย ses_")
    p = subprocess.run([_opencode_bin(), "export", sid], capture_output=True, text=True, timeout=300)
    raw = (p.stdout or "")
    i = raw.find("{")
    if i < 0:
        raise RuntimeError("opencode export ไม่คืน JSON: " + (p.stderr or "")[:300])
    d = json.loads(raw[i:])
    (OUT / f"{sid}.json").write_text(raw[i:], encoding="utf-8")
    chat = []
    for m in d.get("messages", []):
        info = m.get("info", {})
        if info.get("role") not in ("user", "assistant"):
            continue
        texts = [x.get("text", "") for x in m.get("parts", [])
                 if x.get("type") == "text" and x.get("text", "").strip()]
        if texts:
            chat.append({"role": info.get("role"), "text": "\n".join(texts)})
    (OUT / f"{sid}.chat.json").write_text(
        json.dumps({"info": d.get("info"), "messages": chat}, ensure_ascii=False), encoding="utf-8")
    md = [f"# {d.get('info', {}).get('title', sid)}", ""]
    for n, m in enumerate(chat, 1):
        md += [f"## {n}. {m['role']}", "", m["text"], "", "---", ""]
    (OUT / f"{sid}.chat.md").write_text("\n".join(md), encoding="utf-8")
    return len(d.get("messages", [])), len(chat)


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            return self._send(PAGE)
        if self.path == "/api/list":
            files = sorted(p.name for p in OUT.glob("*.chat.*"))
            return self._send(json.dumps({"files": files}), "application/json")
        if self.path.startswith("/dl?"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            f = (q.get("f") or [""])[0]
            p = OUT / f
            # กัน path traversal: ต้องอยู่ใน OUT เท่านั้น
            if not f or p.resolve().parent != OUT.resolve() or not p.exists():
                return self._send("not found", code=404)
            ctype = "application/json" if f.endswith(".json") else "text/markdown; charset=utf-8"
            return self._send(p.read_bytes(), ctype)
        return self._send("not found", code=404)

    def do_POST(self):
        if self.path != "/api/export":
            return self._send("not found", code=404)
        n = int(self.headers.get("content-length") or 0)
        q = urllib.parse.parse_qs(self.rfile.read(n).decode())
        sid = (q.get("sid") or [""])[0]
        try:
            total, kept = export_and_filter(sid)
            self._send(json.dumps({"ok": True, "n_total": total, "n_chat": kept,
                                   "chat_json": f"{sid}.chat.json", "chat_md": f"{sid}.chat.md"}),
                       "application/json")
        except Exception as e:
            self._send(json.dumps({"ok": False, "error": html.escape(str(e))[:500]}), "application/json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    a = ap.parse_args()
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
