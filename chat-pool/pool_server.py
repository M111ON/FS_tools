"""pool_server.py — stdlib HTTP wrapper for pool.py.
POST /append   {app, kind, text}  → {ok, hash}
GET  /tail?n=10                    → [{t, app, ...}, ...]
GET  /stream?n=5  (SSE)           → real-time new messages
GET  /health                       → {ok, lines}
No dependencies beyond stdlib.
"""
import json
import os
import sys
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pool

PORT = int(os.environ.get("POOL_PORT", 8390))
_lock = threading.Lock()


class PoolHandler(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        if parsed.path == "/health":
            lines = 0
            if os.path.exists(pool.LOG):
                with open(pool.LOG, "rb") as f:
                    lines = f.read().count(b"\n")
            self._json(200, {"ok": True, "lines": lines, "log": pool.LOG})

        elif parsed.path == "/tail":
            n = int(qs.get("n", ["10"])[0])
            n = min(n, 500)
            with _lock:
                msgs = pool.tail(n)
            self._json(200, msgs)

        elif parsed.path == "/stream":
            n = int(qs.get("n", ["5"])[0])
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            seen = 0
            try:
                while True:
                    with _lock:
                        msgs = pool.tail(n)
                    if len(msgs) > seen:
                        for m in msgs[max(seen - len(msgs) + len(msgs), 0):]:
                            data = json.dumps(m, ensure_ascii=False)
                            self.wfile.write(f"data: {data}\n\n".encode())
                            self.wfile.flush()
                        seen = len(msgs)
                    time.sleep(1)
            except (BrokenPipeError, ConnectionResetError):
                pass

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/append":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        app = body.get("app", "?")
        kind = body.get("kind", "chat")
        text = body.get("text", "")
        if not text:
            self._json(400, {"error": "text required"})
            return
        with _lock:
            result = pool.append(app, kind, text)
        self._json(200, {"ok": True, "hash": result["hash"]})

    def log_message(self, fmt, *args):
        pass  # silent


def main():
    server = HTTPServer(("0.0.0.0", PORT), PoolHandler)
    print(f"pool-server listening on http://0.0.0.0:{PORT}")
    print(f"  log: {pool.LOG}")
    print(f"  POST /append   {{app, kind, text}}")
    print(f"  GET  /tail?n=10")
    print(f"  GET  /stream?n=5  (SSE)")
    print(f"  GET  /health")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        server.server_close()


if __name__ == "__main__":
    main()
