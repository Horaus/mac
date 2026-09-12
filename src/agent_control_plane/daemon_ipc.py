"""Small durable Unix-socket transport owned by the organization daemon."""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import argparse
import signal
import time
from pathlib import Path

from .mcp_server import dispatch
from .organization_runtime import OrganizationDaemon
from .store import Store


class _IPCHandler(socketserver.StreamRequestHandler):
    def handle(self):
        owner = self.server.owner
        for raw in self.rfile:
            try:
                request = json.loads(raw.decode("utf-8"))
                if request.get("method") == "cli":
                    import io
                    import contextlib
                    from .cli import main as cli_main
                    buffer = io.StringIO()
                    with contextlib.redirect_stdout(buffer):
                        exit_code = cli_main(request.get("params", {}).get("argv", []), _daemon_owner=True)
                    result = {"exit_code": exit_code, "stdout": buffer.getvalue()}
                elif request.get("method") == "daemon":
                    action = request.get("action", "health")
                    result = owner.start() if action == "start" else owner.tick() if action == "tick" else owner.stop() if action == "stop" else owner.health()
                else:
                    result = dispatch(owner.store, request.get("method", ""), request.get("params", {}), _daemon_owner=True)
                payload = {"ok": True, "result": result}
            except Exception as error:
                payload = {"ok": False, "error": str(error)}
            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")); self.wfile.flush()


class DaemonIPCServer:
    """Long-lived daemon owner; MCP clients never own its Store/scheduler."""
    def __init__(self, state: str | Path, socket_path: str | Path):
        self.store = Store(state); self.daemon = OrganizationDaemon(self.store, mode="ipc")
        self.socket_path = Path(socket_path); self.httpd = None; self.thread = None

    def start(self):
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        try: self.socket_path.unlink()
        except FileNotFoundError: pass
        server = socketserver.ThreadingUnixStreamServer(str(self.socket_path), _IPCHandler)
        server.owner = self.daemon; self.httpd = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True); self.thread.start()
        os.chmod(self.socket_path, 0o600)
        self.daemon.start()
        (self.socket_path.parent / "daemon.ipc.json").write_text(json.dumps({"socket": str(self.socket_path)}) + "\n")
        return {"socket": str(self.socket_path), "owner": "daemon", "status": "LISTENING"}

    def close(self):
        if self.httpd:
            self.httpd.shutdown(); self.httpd.server_close(); self.httpd = None
        try: self.socket_path.unlink()
        except FileNotFoundError: pass
        try: (self.socket_path.parent / "daemon.ipc.json").unlink()
        except FileNotFoundError: pass
        self.store.close()


class DaemonIPCClient:
    def __init__(self, socket_path: str | Path): self.socket_path = str(socket_path)

    def call(self, request: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(self.socket_path); client.sendall((json.dumps(request) + "\n").encode())
            stream = client.makefile("rb"); return json.loads(stream.readline().decode())


def main(argv=None):
    parser = argparse.ArgumentParser(description="long-lived MAC daemon IPC owner")
    parser.add_argument("--state", required=True)
    parser.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    server = DaemonIPCServer(args.state, args.socket)
    stopping = threading.Event()
    def stop(*_): stopping.set()
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    server.start()
    try:
        while not stopping.wait(0.25):
            pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
