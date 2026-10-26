"""
DefoldClient -- a thin, dependency-light HTTP client for the Defold editor
server and the running engine service.

Background
----------
While a project is open, the Defold editor runs a local HTTP server whose port
is written to ``<project>/.internal/editor.port``. That server exposes an
OpenAPI document (``/openapi.json``) and operations such as::

    POST /command/compile      -> {"success": true, "issues": []}
    POST /command/run          -> compile + run (debug build)
    GET  /console              -> {"lines": [...], "regions": [...]}
    GET  /preview/{path}       -> PNG of a scene resource
    GET  /ref?q=...            -> search the bundled API docs

A *running* debug build exposes a separate "engine service" on its own port
(reported in the console as ``Engine service started on port <port>``). It
serves ``/ping``, ``/info``, ``/state`` and, when the official
``automation-bridge`` debug extension is installed, the full
``/automation-bridge/v2/*`` API (scene inspection, input, screenshots,
native video recording, events/state/commands, timeline markers, and
Metal GPU trace capture).

This module is transport-only: it has no dependency on the MCP SDK so it can
be exercised standalone (see ``mock_editor.py`` / ``test_client.py``).
"""

from __future__ import annotations

import http.client
import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


class DefoldError(RuntimeError):
    """Raised when a Defold operation fails."""


class DefoldClient:
    """Client for one Defold project's editor server + engine service."""

    def __init__(
        self,
        project_path: "str | Path",
        editor_path: "str | Path | None" = None,
        port: Optional[int] = None,
        timeout: float = 30.0,
        command_timeout: float = 180.0,
        launch_timeout: float = 120.0,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        self.game_project = self.project_path / "game.project"
        self.internal_dir = self.project_path / ".internal"
        self.port_file = self.internal_dir / "editor.port"
        self.token_file = self.internal_dir / "editor.token"
        self._editor_path = Path(editor_path) if editor_path else None
        self._port_override = port
        self.timeout = timeout
        self.command_timeout = command_timeout
        self.launch_timeout = launch_timeout
        self._base_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None

    # ------------------------------------------------------------------ #
    # Editor discovery
    # ------------------------------------------------------------------ #
    @staticmethod
    def _installations_json_paths() -> list[Path]:
        home = Path.home()
        system = platform.system()
        if system == "Windows":
            local = os.environ.get("LOCALAPPDATA")
            if local:
                return [Path(local) / "Defold" / "installations.json"]
            return []
        if system == "Darwin":
            return [home / "Library" / "Application Support" / "Defold" / "installations.json"]
        xdg = os.environ.get("XDG_STATE_HOME", str(home / ".local" / "state"))
        return [Path(xdg) / "Defold" / "installations.json"]

    @classmethod
    def find_editor(cls) -> Optional[str]:
        """Locate the Defold editor executable, if one is installed."""
        env = os.environ.get("DEFOLD_EDITOR")
        if env and Path(env).exists():
            return env

        for p in cls._installations_json_paths():
            if not p.exists():
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            entries = [
                e
                for e in data
                if isinstance(e, dict)
                and e.get("launcherPath")
                and Path(e["launcherPath"]).exists()
            ]
            if entries:
                entries.sort(key=lambda e: e.get("lastLaunchedAt") or "", reverse=True)
                return entries[0]["launcherPath"]

        home = Path.home()
        system = platform.system()
        candidates: list[Path] = []
        if system == "Windows":
            candidates += [home / "Defold" / "Defold.exe", Path("C:/Program Files/Defold/Defold.exe")]
        elif system == "Darwin":
            candidates += [Path("/Applications/Defold.app/Contents/MacOS/Defold")]
        else:
            candidates += [home / "defold" / "Defold"]
        for c in candidates:
            if c.exists():
                return str(c)

        return shutil.which("Defold") or shutil.which("defold")

    # ------------------------------------------------------------------ #
    # HTTP helpers
    # ------------------------------------------------------------------ #
    def _request(
        self,
        method: str,
        path: str,
        *,
        data: Optional[bytes] = None,
        headers: Optional[dict] = None,
        base: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> tuple[int, bytes]:
        base = base or self._base_url
        if not base:
            raise DefoldError("Editor is not connected (no base URL).")
        url = f"{base}{path}"
        req = urllib.request.Request(url, data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                try:
                    body = resp.read()
                except http.client.IncompleteRead as e:
                    # Some endpoints (e.g. the Automation Bridge click) reply
                    # 200 with an empty body and close the connection, which
                    # makes resp.read() raise IncompleteRead. Treat the partial
                    # bytes as the body so the call still reports success.
                    body = e.partial
                return resp.status, body
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            except Exception:
                # The peer may close the connection while sending the error body.
                return e.code, b
        except urllib.error.URLError as e:
            raise DefoldError(f"Cannot reach {url}: {e.reason}") from e
        except http.client.IncompleteRead as e:
            # Status was received but the body was cut short.
            return 200, e.partial
        except ConnectionResetError:
            # The Defold engine service / Automation Bridge can close an
            # established connection abruptly after receiving the request
            # (e.g. a 200 with an empty body). The request was delivered, so
            # report a best-effort success with an empty body. A refused
            # connection (server not running) is a ConnectionRefusedError and
            # is handled by the URLError branch above.
            return 200, b""

    @staticmethod
    def _json(body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

    # ------------------------------------------------------------------ #
    # Connection / lifecycle
    # ------------------------------------------------------------------ #
    def read_port(self) -> Optional[int]:
        if self._port_override is not None:
            return self._port_override
        if self.port_file.exists():
            try:
                return int(self.port_file.read_text(encoding="utf-8").strip())
            except Exception:
                return None
        return None

    def base_url(self) -> Optional[str]:
        port = self.read_port()
        return f"http://127.0.0.1:{port}" if port else None

    def is_running(self) -> bool:
        base = self.base_url()
        if not base:
            return False
        try:
            status, _ = self._request("GET", "/openapi.json", base=base, timeout=3.0)
            return status == 200
        except DefoldError:
            return False

    def connect(self) -> str:
        """Ensure a reachable editor base URL; raise ``DefoldError`` otherwise."""
        base = self.base_url()
        if not base:
            raise DefoldError(
                "No editor port found. Is the Defold editor open on this project? "
                f"(looked for {self.port_file})"
            )
        status, _ = self._request("GET", "/openapi.json", base=base)
        if status != 200:
            raise DefoldError(f"Editor responded with HTTP {status} on {base}")
        self._base_url = base
        return base

    def launch(self, port: Optional[int] = None) -> str:
        """Start the editor for this project and wait until it is reachable."""
        if self.is_running():
            return self.connect()

        editor = self._editor_path or self.find_editor()
        if not editor:
            raise DefoldError(
                "Defold editor not found. Install Defold, set the DEFOLD_EDITOR "
                "environment variable to the editor executable, or open the "
                "project in the editor manually."
            )
        if not self.game_project.exists():
            raise DefoldError(f"No game.project found at {self.game_project}")

        cmd = [editor]
        if port:
            cmd += ["--port", str(port)]
        cmd += [str(self.game_project)]

        creationflags = 0
        if platform.system() == "Windows":
            # Do not pop a console window for the GUI editor.
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._proc = subprocess.Popen(cmd, creationflags=creationflags)

        deadline = time.time() + self.launch_timeout
        while time.time() < deadline:
            if self.is_running():
                return self.connect()
            time.sleep(1.0)
        raise DefoldError(f"Editor did not become reachable within {self.launch_timeout}s")

    def stop(self) -> dict:
        """Best-effort stop of the editor process *this client* launched.

        On Windows, ``Defold.exe`` is a launcher that spawns the real editor
        (and the game) as child processes, so a plain ``terminate()`` only
        kills the launcher. We therefore kill the whole process tree with
        ``taskkill /F /T /PID``. On other platforms a normal terminate/kill
        is sufficient.
        """
        if self._proc is None or self._proc.poll() is not None:
            return {
                "stopped": False,
                "detail": "No editor process was launched by this client; close the game in the editor to stop it.",
            }
        pid = self._proc.pid
        if platform.system() == "Windows":
            # Kill the entire process tree (launcher + editor + game).
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        else:
            self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        return {
            "stopped": True,
            "detail": f"Terminated the editor process tree (PID {pid}) launched by this client.",
        }

    # ------------------------------------------------------------------ #
    # Editor HTTP API
    # ------------------------------------------------------------------ #
    def openapi(self) -> dict:
        self.connect()
        status, body = self._request("GET", "/openapi.json")
        if status != 200:
            raise DefoldError(f"openapi.json: HTTP {status}")
        return self._json(body)

    def _post_command(self, name: str, data: dict | None = None) -> dict:
        self.connect()
        headers = {"Content-Type": "application/json"} if data is not None else None
        payload = json.dumps(data).encode("utf-8") if data is not None else None
        status, body = self._request(
            "POST", f"/command/{name}", data=payload, headers=headers, timeout=self.command_timeout
        )
        data = self._json(body)
        if isinstance(data, dict):
            data["_http_status"] = status
        return data

    def compile(self) -> dict:
        return self._post_command("compile")

    def run(self) -> dict:
        return self._post_command("run")

    def clean_build(self) -> dict:
        return self._post_command("clean-build")

    def hot_reload(self) -> dict:
        """Hot-reload all modified files into the running project.

        Requires the game to be running (``run``). Returns the editor's
        response (``success`` and any ``issues``).
        """
        return self._post_command("hot-reload")

    def console(self) -> dict:
        self.connect()
        status, body = self._request("GET", "/console")
        if status != 200:
            raise DefoldError(f"console: HTTP {status}")
        return self._json(body)

    def preview(self, path: str, width: int = 1280, height: int = 720) -> bytes:
        self.connect()
        clean = path.lstrip("/")
        q = urllib.parse.urlencode({"width": width, "height": height})
        status, body = self._request("GET", f"/preview/{clean}?{q}", timeout=self.command_timeout)
        if status != 200:
            raise DefoldError(f"preview {path}: HTTP {status}")
        return body

    def ref(self, query: str, environment: str = "runtime", language: str = "Lua") -> dict:
        self.connect()
        params = urllib.parse.urlencode(
            {"q": query, "environment": environment, "language": language}
        )
        status, body = self._request("GET", f"/ref?{params}")
        if status != 200:
            raise DefoldError(f"ref: HTTP {status}")
        return self._json(body)

    # ------------------------------------------------------------------ #
    # Engine service (running game)
    # ------------------------------------------------------------------ #
    def engine_service_url(self) -> Optional[str]:
        """Discover the running engine service port from the console."""
        try:
            console = self.console()
        except DefoldError:
            return None
        lines = console.get("lines", []) if isinstance(console, dict) else []
        text = "\n".join(str(x) for x in lines)
        m = re.search(r"Engine service started on port (\d+)", text)
        if m:
            return f"http://127.0.0.1:{m.group(1)}"
        return None

    def _engine_get(self, path: str) -> tuple[int, bytes]:
        url = self.engine_service_url()
        if not url:
            raise DefoldError("No running engine service found. Run the game first (defold_run).")
        return self._request("GET", path, base=url)

    def engine_ping(self) -> Any:
        status, body = self._engine_get("/ping")
        if status != 200:
            raise DefoldError(f"engine /ping: HTTP {status}")
        return self._json(body)

    def engine_info(self) -> Any:
        status, body = self._engine_get("/info")
        if status != 200:
            raise DefoldError(f"engine /info: HTTP {status}")
        return self._json(body)

    def engine_state(self) -> Any:
        status, body = self._engine_get("/state")
        if status != 200:
            raise DefoldError(f"engine /state: HTTP {status}")
        return self._json(body)

    # ------------------------------------------------------------------ #
    # Automation Bridge v2 (running game)
    # ------------------------------------------------------------------ #
    def automation_bridge_url(self) -> Optional[str]:
        """Base URL of the Automation Bridge v2 API on the engine service."""
        url = self.engine_service_url()
        if not url:
            return None
        return f"{url}/automation-bridge/v2"

    @staticmethod
    def _encode_params(params: Optional[dict]) -> dict:
        """Convert Python values to query-safe strings, dropping ``None``.

        Booleans become ``1``/``0`` (the bridge treats any other present
        value as false), lists are comma-joined (e.g. ``include``).
        """
        out: dict = {}
        for key, value in (params or {}).items():
            if value is None:
                continue
            if isinstance(value, bool):
                out[key] = "1" if value else "0"
            elif isinstance(value, (list, tuple)):
                out[key] = ",".join(str(v) for v in value)
            else:
                out[key] = value
        return out

    def _bridge_request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Call an Automation Bridge v2 endpoint and unwrap the envelope.

        Success responses are ``{"ok": true, "data": {...}}``; errors are
        ``{"ok": false, "error": {"status", "code", "message"}}``. The logical
        status lives in ``error.status`` (Defold's DLIB server transports most
        non-2xx logical statuses as HTTP 500), so the envelope — not the HTTP
        code — is the source of truth.
        """
        base = self.automation_bridge_url()
        if not base:
            raise DefoldError(
                "No running engine service found. Run the game first (defold_run) "
                "and install the official 'automation-bridge' debug extension."
            )
        query = urllib.parse.urlencode(self._encode_params(params), doseq=True)
        full_path = f"{path}?{query}" if query else path
        data = None
        headers: dict = {}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, body = self._request(
            method, full_path, data=data, headers=headers, base=base, timeout=timeout
        )
        payload = self._json(body)
        if isinstance(payload, dict) and payload.get("ok") is False:
            err = payload.get("error") or {}
            raise DefoldError(
                f"Automation Bridge {method} {path} failed "
                f"(status={err.get('status')}, code={err.get('code')}): {err.get('message')}"
            )
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- Health / lifecycle / screen / coordinates / frame -------------- #
    def bridge_health(self) -> dict:
        """GET /health — version, capabilities, screen, input devices."""
        return self._bridge_request("GET", "/health")

    def bridge_lifecycle(self) -> dict:
        """GET /lifecycle — ordered runtime stages."""
        return self._bridge_request("GET", "/lifecycle")

    def bridge_screen(self) -> dict:
        """GET /screen — named rectangles, display scale, coordinate convention."""
        return self._bridge_request("GET", "/screen")

    def bridge_resize_screen(self, width: int, height: int) -> dict:
        """PUT /screen — set the native window size."""
        return self._bridge_request("PUT", "/screen", params={"width": width, "height": height})

    def bridge_convert_coordinates(
        self, x: float, y: float, from_space: str, to_space: str
    ) -> dict:
        """POST /coordinates/convert — convert a point between coordinate spaces."""
        return self._bridge_request(
            "POST",
            "/coordinates/convert",
            json_body={"point": {"x": x, "y": y}, "from_space": from_space, "to_space": to_space},
        )

    def bridge_frame(self) -> dict:
        """GET /frame — current engine_frame and scene_sequence."""
        return self._bridge_request("GET", "/frame")

    # -- Scene inspection ------------------------------------------------ #
    def bridge_scene(
        self, visible: Optional[bool] = None, include: Optional[str] = None
    ) -> dict:
        """GET /scene — the full runtime scene tree."""
        return self._bridge_request("GET", "/scene", params={"visible": visible, "include": include})

    def bridge_elements(self, **filters: Any) -> dict:
        """GET /elements — search elements with filters and pagination.

        Pass any documented filter as a keyword argument (``type``, ``name``,
        ``text``, ``url``, ``id``, ``path``, ``kind``, ``visible``, ``limit``,
        ``offset``, ``cursor``, ``include``, ...).
        """
        return self._bridge_request("GET", "/elements", params=filters)

    def bridge_element(self, element_id: str, include: Optional[str] = None) -> dict:
        """GET /element — one element in detail (bounds+properties by default)."""
        return self._bridge_request("GET", "/element", params={"id": element_id, "include": include})

    # -- Input ------------------------------------------------------------ #
    def bridge_input_click(
        self,
        x: Optional[float] = None,
        y: Optional[float] = None,
        element_id: Optional[str] = None,
        button: str = "left",
        modifiers: Optional[str] = None,
        device: Optional[str] = None,
        pointer_id: Optional[int] = None,
        visualize: Optional[bool] = None,
        expected_scene_sequence: Optional[int] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/click — click by element id or window x/y."""
        return self._bridge_request(
            "POST",
            "/input/click",
            params={
                "x": x, "y": y, "id": element_id, "button": button,
                "modifiers": modifiers, "device": device, "pointer_id": pointer_id,
                "visualize": visualize, "expected_scene_sequence": expected_scene_sequence,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_input_drag(
        self,
        x1: Optional[float] = None,
        y1: Optional[float] = None,
        x2: Optional[float] = None,
        y2: Optional[float] = None,
        from_id: Optional[str] = None,
        to_id: Optional[str] = None,
        duration: Optional[float] = None,
        hold_before: Optional[float] = None,
        hold_after: Optional[float] = None,
        easing: Optional[str] = None,
        button: str = "left",
        modifiers: Optional[str] = None,
        device: Optional[str] = None,
        pointer_id: Optional[int] = None,
        visualize: Optional[bool] = None,
        expected_scene_sequence: Optional[int] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/drag — drag between two points or element ids."""
        return self._bridge_request(
            "POST",
            "/input/drag",
            params={
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "from_id": from_id, "to_id": to_id,
                "duration": duration, "hold_before": hold_before, "hold_after": hold_after,
                "easing": easing, "button": button, "modifiers": modifiers,
                "device": device, "pointer_id": pointer_id, "visualize": visualize,
                "expected_scene_sequence": expected_scene_sequence,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_input_drag_path(
        self,
        points: str,
        durations: Optional[str] = None,
        easing: Optional[str] = None,
        path: Optional[str] = None,
        hold_before: Optional[float] = None,
        hold_after: Optional[float] = None,
        button: str = "left",
        modifiers: Optional[str] = None,
        device: Optional[str] = None,
        pointer_id: Optional[int] = None,
        visualize: Optional[bool] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/drag_path — multi-point drag path.

        ``points`` is ``x,y`` pairs separated by semicolons, e.g.
        ``"100,100;180,60;260,140"``.
        """
        return self._bridge_request(
            "POST",
            "/input/drag_path",
            json_body={
                "points": points, "durations": durations, "easing": easing,
                "path": path, "hold_before": hold_before, "hold_after": hold_after,
                "button": button, "modifiers": modifiers, "device": device,
                "pointer_id": pointer_id, "visualize": visualize,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_input_key(
        self,
        text: Optional[str] = None,
        keys: Optional[str] = None,
        hold: Optional[float] = None,
        modifiers: Optional[str] = None,
        device: Optional[str] = None,
        visualize: Optional[bool] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/key — type literal text or press special keys.

        ``keys`` is one or more brace-wrapped special keys, e.g.
        ``"{KEY_ENTER}"`` (URL-encode the braces if using a query string).
        """
        return self._bridge_request(
            "POST",
            "/input/key",
            params={
                "text": text, "keys": keys, "hold": hold, "modifiers": modifiers,
                "device": device, "visualize": visualize,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_input_wheel(
        self,
        steps: int,
        x: Optional[float] = None,
        y: Optional[float] = None,
        element_id: Optional[str] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/wheel — turn the mouse wheel by ``steps`` detents (-64..64)."""
        return self._bridge_request(
            "POST",
            "/input/wheel",
            params={
                "steps": steps, "x": x, "y": y, "id": element_id,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_input_status(self, input_id: int) -> dict:
        """GET /input/status — current/bounded-history status for one input."""
        return self._bridge_request("GET", "/input/status", params={"input_id": input_id})

    def bridge_input_pending(self) -> dict:
        """GET /input/pending — the FIFO queue of pending inputs."""
        return self._bridge_request("GET", "/input/pending")

    def bridge_input_cancel(
        self,
        input_id: int,
        release: Optional[bool] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """POST /input/cancel — cancel one queued/active input."""
        return self._bridge_request(
            "POST",
            "/input/cancel",
            params={"input_id": input_id, "release": release, "client_id": client_id, "session_id": session_id},
        )

    def bridge_input_flush(
        self,
        release: Optional[bool] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """POST /input/flush — cancel the owning session's active and later inputs."""
        return self._bridge_request(
            "POST",
            "/input/flush",
            params={"release": release, "client_id": client_id, "session_id": session_id},
        )

    def bridge_input_configure(
        self,
        client_id: str,
        session_id: str,
        lease: float = 5.0,
        device: str = "auto",
        visualize: Optional[bool] = None,
    ) -> dict:
        """PUT /input/configure — acquire or renew the controller lease."""
        return self._bridge_request(
            "PUT",
            "/input/configure",
            params={
                "client_id": client_id, "session_id": session_id,
                "lease": lease, "device": device, "visualize": visualize,
            },
        )

    def bridge_pointer_open(
        self,
        x: float,
        y: float,
        pointer_lease: float = 2.0,
        button: str = "left",
        modifiers: Optional[str] = None,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/pointer/open — open a leased pointer session at x/y."""
        return self._bridge_request(
            "POST",
            "/input/pointer/open",
            params={
                "x": x, "y": y, "pointer_lease": pointer_lease,
                "button": button, "modifiers": modifiers,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_pointer_move(
        self,
        input_id: int,
        x: float,
        y: float,
        duration: float,
        easing: str = "linear",
        pointer_lease: float = 2.0,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/pointer/move — move an open pointer to x/y."""
        return self._bridge_request(
            "POST",
            "/input/pointer/move",
            params={
                "input_id": input_id, "x": x, "y": y, "duration": duration,
                "easing": easing, "pointer_lease": pointer_lease,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_pointer_hold(
        self,
        input_id: int,
        duration: float,
        pointer_lease: float = 2.0,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/pointer/hold — hold an open pointer for ``duration`` seconds."""
        return self._bridge_request(
            "POST",
            "/input/pointer/hold",
            params={
                "input_id": input_id, "duration": duration, "pointer_lease": pointer_lease,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    def bridge_pointer_up(
        self,
        input_id: int,
        pointer_lease: float = 2.0,
        client_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> dict:
        """POST /input/pointer/up — release an open pointer."""
        return self._bridge_request(
            "POST",
            "/input/pointer/up",
            params={
                "input_id": input_id, "pointer_lease": pointer_lease,
                "client_id": client_id, "session_id": session_id, "request_id": request_id,
            },
        )

    # -- Screenshots ------------------------------------------------------ #
    def bridge_screenshot(self, after_frames: int = 0) -> dict:
        """GET /screenshot — schedule a capture; returns a receipt with capture_id."""
        return self._bridge_request("GET", "/screenshot", params={"after_frames": after_frames})

    def bridge_screenshot_status(self, capture_id: int) -> dict:
        """GET /screenshot/status — poll a capture by id."""
        return self._bridge_request("GET", "/screenshot/status", params={"capture_id": capture_id})

    def screenshot(self, after_frames: int = 0, timeout: float = 15.0) -> bytes:
        """Runtime screenshot via the Automation Bridge v2 extension.

        Schedules a capture, polls until it completes, then reads the PNG from
        the local path (the game runs on this machine).
        """
        receipt = self.bridge_screenshot(after_frames=after_frames)
        capture_id = receipt.get("capture_id")
        path = receipt.get("path")
        deadline = time.time() + timeout
        while True:
            status = self.bridge_screenshot_status(capture_id)
            state = status.get("state")
            if state == "complete":
                png_path = status.get("path") or path
                if not png_path:
                    raise DefoldError("Screenshot completed but no file path was returned.")
                try:
                    with open(png_path, "rb") as f:
                        return f.read()
                except OSError as e:
                    raise DefoldError(
                        f"Screenshot completed but the file could not be read: {png_path} ({e})"
                    )
            if state == "failed":
                raise DefoldError(
                    f"Screenshot failed: {status.get('failure_reason') or 'unknown reason'}"
                )
            if time.time() > deadline:
                raise DefoldError(f"Screenshot did not complete within {timeout}s (state={state}).")
            time.sleep(0.1)

    # -- Native video recording ------------------------------------------- #
    def bridge_recording_capabilities(self) -> dict:
        """GET /recording/capabilities — video recording availability."""
        return self._bridge_request("GET", "/recording/capabilities")

    def bridge_recording_status(self) -> dict:
        """GET /recording/status — active or most recently finalized capture."""
        return self._bridge_request("GET", "/recording/status")

    def bridge_recording_start(
        self,
        path: str,
        width: Optional[int] = None,
        height: Optional[int] = None,
        fps: int = 30,
        audio: Optional[bool] = None,
    ) -> dict:
        """POST /recording/start — start a native video recording to ``path``."""
        body: dict = {"path": path, "fps": fps}
        if width is not None:
            body["width"] = width
        if height is not None:
            body["height"] = height
        if audio is not None:
            body["audio"] = audio
        return self._bridge_request("POST", "/recording/start", json_body=body)

    def bridge_recording_stop(self) -> dict:
        """POST /recording/stop — stop capture and finalize the MP4."""
        return self._bridge_request("POST", "/recording/stop")

    # -- Events / state / commands / markers (application API) ------------ #
    def bridge_events_cursor(self) -> dict:
        """GET /events/cursor — next and oldest retained event cursors."""
        return self._bridge_request("GET", "/events/cursor")

    def bridge_events(
        self,
        cursor: Optional[int] = None,
        timeout_ms: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """GET /events — long-poll for events at or after ``cursor``."""
        return self._bridge_request(
            "GET", "/events", params={"cursor": cursor, "timeout_ms": timeout_ms, "limit": limit}
        )

    def bridge_state(self, name: Optional[str] = None) -> dict:
        """GET /state — all published states (or one by exact ``name``)."""
        return self._bridge_request("GET", "/state", params={"name": name})

    def bridge_state_wait(self, after_revision: int, timeout_ms: int = 10000) -> dict:
        """GET /state/wait — long-poll until a newer state revision."""
        return self._bridge_request(
            "GET", "/state/wait", params={"after_revision": after_revision, "timeout_ms": timeout_ms}
        )

    def bridge_application_catalog(
        self,
        kind: Optional[str] = None,
        name: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> dict:
        """GET /application/catalog — registered commands, states, and events."""
        return self._bridge_request(
            "GET",
            "/application/catalog",
            params={"kind": kind, "name": name, "limit": limit, "offset": offset, "cursor": cursor},
        )

    def bridge_command(self, name: str, data: Any = None, timeout_ms: Optional[int] = None) -> dict:
        """POST /commands — submit a named command.

        ``data`` is the command payload; if it is not already a string it is
        JSON-encoded (the bridge expects ``data`` to be a string containing
        strict JSON).
        """
        body: dict = {"name": name}
        if data is not None and not isinstance(data, str):
            data = json.dumps(data)
        if data is not None:
            body["data"] = data
        if timeout_ms is not None:
            body["timeout_ms"] = timeout_ms
        return self._bridge_request("POST", "/commands", json_body=body)

    def bridge_command_status(self, command_id: int) -> dict:
        """GET /commands — poll a command by id."""
        return self._bridge_request("GET", "/commands", params={"id": command_id})

    def bridge_command_cancel(self, command_id: int) -> dict:
        """DELETE /commands — cancel a pending command by id."""
        return self._bridge_request("DELETE", "/commands", params={"id": command_id})

    def bridge_marker(
        self,
        name: str,
        data: Any = None,
        recording_timestamp_us: Optional[int] = None,
    ) -> dict:
        """POST /markers — insert a timeline marker event."""
        body: dict = {"name": name}
        if data is not None and not isinstance(data, str):
            data = json.dumps(data)
        if data is not None:
            body["data"] = data
        if recording_timestamp_us is not None:
            body["recording_timestamp_us"] = recording_timestamp_us
        return self._bridge_request("POST", "/markers", json_body=body)

    # -- Metal GPU trace (macOS only) ------------------------------------- #
    def bridge_metal(self, path: str, frames: Optional[int] = None) -> dict:
        """POST /metal — schedule a Metal GPU trace capture (macOS only)."""
        body: dict = {"path": path}
        if frames is not None:
            body["frames"] = frames
        return self._bridge_request("POST", "/metal", json_body=body)

    def bridge_metal_status(self) -> dict:
        """GET /metal — poll a Metal capture's status."""
        return self._bridge_request("GET", "/metal")

    def bridge_metal_stop(self) -> dict:
        """DELETE /metal — request an early stop of a Metal capture."""
        return self._bridge_request("DELETE", "/metal")

    # -- Backwards-compatible helpers -------------------------------------- #
    def input_click(self, x: int, y: int, button: str = "left") -> dict:
        """Inject a mouse click via the Automation Bridge v2 extension."""
        return self.bridge_input_click(x=x, y=y, button=button)
