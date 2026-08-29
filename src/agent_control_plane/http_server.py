"""Small localhost JSON-RPC transport for installations that must outlive a terminal."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .mcp_server import dispatch
from .store import Store

def serve_http(state: str | Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    store = Store(state)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health": self._send(200, {"ok": True, "transport": "http"})
            else: self._send(404, {"error": "not found"})
        def do_POST(self):
            try:
                request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                result = dispatch(store, request.get("method", ""), request.get("params", {}))
                if "id" not in request: self.send_response(204); self.end_headers(); return
                self._send(200, {"jsonrpc":"2.0", "id":request.get("id"), "result":result})
            except Exception as error: self._send(400, {"jsonrpc":"2.0", "id":None, "error":{"code":-32000,"message":str(error)}})
        def _send(self, status, payload):
            body=json.dumps(payload, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
        def log_message(self, *_): pass
    try: ThreadingHTTPServer((host, port), Handler).serve_forever()
    finally: store.close()
