"""
Mock Defold editor + engine service for testing ``defold_client`` without a
real Defold installation.

Run standalone::

    python tools/defold-mcp/mock_editor.py

It starts two HTTP servers:
  * an "editor" server on a free port (openapi, /command/*, /console, /preview, /ref)
  * an "engine service" on another free port (/ping, /info, /state, automation-bridge)

The editor port is written to a temp ``.internal/editor.port`` file so the
client's discovery path is exercised end-to-end.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# A valid 1x1 transparent PNG.
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9aaD1kAAAsCwYAAAAABJRU5ErkJggg=="
)


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EditorHandler(BaseHTTPRequestHandler):
    engine_port: int = 0

    def log_message(self, *args):  # silence
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/openapi.json":
            self._json(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "Defold Editor", "version": "1.13.2-mock"},
                    "paths": {
                        "/command/compile": {},
                        "/command/run": {},
                        "/command/clean-build": {},
                        "/command/hot-reload": {},
                        "/console": {},
                        "/preview/{path}": {},
                        "/ref": {},
                    },
                }
            )
        elif p == "/console":
            self._json(
                {
                    "lines": [
                        "INFO:ENGINE: Engine service started on port %d" % self.engine_port,
                        "INFO:ENGINE: Engine started",
                        "DEBUG:GAME: hello from mock",
                    ],
                    "regions": [],
                }
            )
        elif p.startswith("/preview/"):
            self._send(200, _TINY_PNG, "image/png")
        elif p == "/ref":
            self._json({"results": [{"name": "go", "description": "mock result"}]})
        else:
            self._json({"error": "not found"}, 404)

    def _drain_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 0:
            self.rfile.read(n)

    def do_POST(self):
        self._drain_body()
        p = self.path.split("?")[0]
        if p == "/command/compile":
            self._json({"success": True, "issues": []})
        elif p == "/command/run":
            self._json({"success": True, "issues": []})
        elif p == "/command/clean-build":
            self._json({"success": True, "issues": []})
        elif p == "/command/hot-reload":
            self._json({"success": True, "issues": []})
        else:
            self._json({"error": "not found"}, 404)


class EngineHandler(BaseHTTPRequestHandler):
    engine_port: int = 0
    capture_dir: Path = Path(tempfile.gettempdir())
    captures: dict = {}
    _next_capture_id = 1
    _capture_lock = threading.Lock()

    def log_message(self, *args):
        pass

    def _send(self, status: int, body: bytes, ctype: str = "application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200):
        self._send(status, json.dumps(obj).encode("utf-8"))

    def _schedule_screenshot(self) -> tuple[int, Path]:
        """Write a capture file and register it under a fresh capture id."""
        with self._capture_lock:
            capture_id = self._next_capture_id
            self._next_capture_id += 1
            path = self.capture_dir / f"capture-{capture_id}.png"
            path.write_bytes(_TINY_PNG)
            self.captures[capture_id] = path
        return capture_id, path

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/ping":
            self._json({"status": "ok"})
        elif p == "/info":
            self._json({"version": "1.13.2-mock", "platform": "windows"})
        elif p == "/state":
            self._json({"state": "running"})
        elif p == "/automation-bridge/v1/screenshot":
            self._send(200, _TINY_PNG, "image/png")
        elif p == "/automation-bridge/v2/screenshot":
            capture_id, path = self._schedule_screenshot()
            self._json({"ok": True, "data": {"capture_id": capture_id, "path": str(path)}})
        elif p == "/automation-bridge/v2/screenshot/status":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                capture_id = int(qs.get("capture_id", ["0"])[0])
            except ValueError:
                capture_id = 0
            path = self.captures.get(capture_id)
            if path is None:
                self._json(
                    {
                        "ok": False,
                        "error": {
                            "status": 404,
                            "code": "not_found",
                            "message": f"unknown capture_id {capture_id}",
                        },
                    }
                )
            else:
                self._json({"ok": True, "data": {"state": "complete", "path": str(path)}})
        else:
            self._json({"error": "not found"}, 404)

    def _drain_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 0:
            self.rfile.read(n)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n > 0 else b""
        p = self.path.split("?")[0]
        if p == "/automation-bridge/v1/input/click":
            body = raw
            try:
                data = json.loads(body.decode("utf-8"))
            except Exception:
                data = {}
            self._json({"clicked": True, "x": data.get("x"), "y": data.get("y")})
        elif p == "/automation-bridge/v2/input/click":
            qs = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")

            def _num(key: str):
                raw = qs.get(key, [None])[0]
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    return None

            self._json(
                {
                    "ok": True,
                    "data": {
                        "clicked": True,
                        "x": _num("x"),
                        "y": _num("y"),
                        "button": qs.get("button", ["left"])[0],
                    },
                }
            )
        else:
            self._json({"error": "not found"}, 404)


class MockDefold:
    """Starts both mock servers and writes a temp editor.port file."""

    def __init__(self):
        self.editor_port = _free_port()
        self.engine_port = _free_port()
        EditorHandler.engine_port = self.engine_port

        self.editor_srv = ThreadingHTTPServer(("127.0.0.1", self.editor_port), EditorHandler)
        self.engine_srv = ThreadingHTTPServer(("127.0.0.1", self.engine_port), EngineHandler)

        self.tmpdir = tempfile.mkdtemp(prefix="defold-mock-")
        EngineHandler.capture_dir = Path(self.tmpdir) / "screenshots"
        EngineHandler.capture_dir.mkdir(parents=True)
        EngineHandler.captures = {}
        EngineHandler._next_capture_id = 1
        self.project_dir = Path(self.tmpdir) / "project"
        self.internal_dir = self.project_dir / ".internal"
        self.internal_dir.mkdir(parents=True)
        self.port_file = self.internal_dir / "editor.port"
        self.port_file.write_text(str(self.editor_port))
        (self.project_dir / "game.project").write_text("[project]\n")

        self._threads = [
            threading.Thread(target=self.editor_srv.serve_forever, daemon=True),
            threading.Thread(target=self.engine_srv.serve_forever, daemon=True),
        ]
        for t in self._threads:
            t.start()

    @property
    def project_path(self) -> Path:
        return self.project_dir

    def stop(self):
        self.editor_srv.shutdown()
        self.engine_srv.shutdown()
        self.editor_srv.server_close()
        self.engine_srv.server_close()


if __name__ == "__main__":
    mock = MockDefold()
    print(f"Mock editor on http://127.0.0.1:{mock.editor_port}")
    print(f"Mock engine  on http://127.0.0.1:{mock.engine_port}")
    print(f"Project dir : {mock.project_path}")
    print("Press Ctrl+C to stop.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        mock.stop()
