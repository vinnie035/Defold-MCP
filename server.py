"""
Defold MCP server.

Exposes the Defold editor HTTP API and the running engine service as MCP
tools so an AI agent can compile, run, read logs, capture screenshots, and
drive the running game through the Automation Bridge v2 API (scene
inspection, input, video recording, events, states, commands, markers).

Run (stdio transport)::

    python tools/defold-mcp/server.py

The project to control is resolved automatically (see ``_default_project``):
the ``DEFOLD_PROJECT`` env var, then the project currently open in the Defold
editor (auto-detected by scanning for a live editor server), then the
``active_project.txt`` file next to this file, then the nearest ancestor
containing a ``game.project`` file.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any, Optional

try:  # mcp v1
    from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:  # mcp v2 (FastMCP -> MCPServer)
    from mcp.server.mcpserver import MCPServer as _Server
from mcp.types import ImageContent, TextContent

from defold_client import DefoldClient, DefoldError

def _candidate_roots() -> list[Path]:
    """Directories to scan for Defold projects (``game.project`` files).

    Defaults to the parent of this file's directory (sibling projects).
    Override with the ``DEFOLD_SCAN_ROOTS`` env var (path-separator list).
    """
    roots: list[Path] = []
    env_roots = os.environ.get("DEFOLD_SCAN_ROOTS")
    if env_roots:
        roots += [Path(p.strip()) for p in env_roots.split(os.pathsep) if p.strip()]
    default_root = Path(__file__).resolve().parent.parent
    if default_root not in roots:
        roots.append(default_root)
    return roots


_SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".internal"}


def _find_projects(root: Path, max_depth: int = 3) -> list[Path]:
    """Bounded search for project roots (folders containing ``game.project``)."""
    found: list[Path] = []

    def walk(d: Path, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            entries = list(d.iterdir())
        except (PermissionError, OSError):
            return
        for e in entries:
            if not e.is_dir() or e.name in _SKIP_DIRS:
                continue
            if (e / "game.project").is_file():
                found.append(e)
            else:
                walk(e, depth + 1)

    walk(root, 1)
    return found


def _detect_open_project() -> Optional[str]:
    """Return the Defold project currently open in the editor, if any.

    Scans the candidate roots for projects, then returns the one whose editor
    server is live (``.internal/editor.port`` present and ``/openapi.json``
    reachable). If several editors are open, the most recently touched port
    file wins. Returns ``None`` when no project has a live editor.
    """
    seen: set[Path] = set()
    candidates: list[Path] = []
    for root in _candidate_roots():
        if not root.is_dir():
            continue
        for proj in _find_projects(root):
            if proj not in seen:
                seen.add(proj)
                candidates.append(proj)
    live: list[Path] = []
    for proj in candidates:
        try:
            if DefoldClient(proj).is_running():
                live.append(proj)
        except Exception:
            continue
    if not live:
        return None
    if len(live) == 1:
        return str(live[0])

    def _mtime(p: Path) -> float:
        pf = p / ".internal" / "editor.port"
        try:
            return pf.stat().st_mtime
        except OSError:
            return 0.0

    return str(max(live, key=_mtime))


def _bionic_working_dir() -> Optional[str]:
    """Try to determine Bionic's working directory.

    Checks a set of likely env var names, then the process cwd (and its
    ancestors) for a ``game.project`` file. Returns ``None`` when nothing
    matches.
    """
    candidates: list[Path] = []
    for var in (
        "BIONIC_WORKING_DIR",
        "BIONIC_CWD",
        "WORKSPACE",
        "WORKSPACE_DIR",
        "PWD",
        "LMSTUDIO_WORKING_DIR",
        "AGENT_WORKING_DIR",
    ):
        val = os.environ.get(var)
        if val:
            val = val.strip()
            if val:
                candidates.append(Path(val))
    candidates.append(Path(os.getcwd()))
    for base in candidates:
        if not base.is_dir():
            continue
        if (base / "game.project").is_file():
            return str(base)
        for parent in base.parents:
            if (parent / "game.project").is_file():
                return str(parent)
    return None


def _default_project() -> str:
    """Resolve the Defold project root.

    Priority:
      1. ``DEFOLD_PROJECT`` env var (explicit override)
      2. Bionic's working directory (the folder open in Bionic)
      3. the project currently open in the Defold editor (auto-detected)
      4. ``active_project.txt`` next to this file (manual pin / fallback)
      5. the nearest ancestor of this file containing a ``game.project``
      6. fallback: the parent of this file's directory
    """
    env = os.environ.get("DEFOLD_PROJECT")
    if env:
        return env
    wd = _bionic_working_dir()
    if wd:
        return wd
    detected = _detect_open_project()
    if detected:
        return detected
    pointer = Path(__file__).resolve().parent / "active_project.txt"
    if pointer.is_file():
        pinned = pointer.read_text(encoding="utf-8").strip()
        if pinned:
            return pinned
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "game.project").is_file():
            return str(parent)
    return str(here.parents[1])


mcp = _Server("defold")

# A single client instance so that a process launched by defold_open_project
# stays tracked across tool calls within this server's lifetime.
_client_instance: Optional[DefoldClient] = None


def _client() -> DefoldClient:
    global _client_instance
    # Reuse the current client while its project's editor is still live;
    # otherwise re-resolve the active project (handles switching projects).
    if _client_instance is not None:
        try:
            if _client_instance.is_running():
                return _client_instance
        except Exception:
            pass
    _client_instance = DefoldClient(_default_project())
    return _client_instance


def _text(payload) -> list:
    if isinstance(payload, (dict, list)):
        s = json.dumps(payload, indent=2, default=str)
    else:
        s = str(payload)
    return [TextContent(type="text", text=s)]


def _image(png: bytes, caption: str = "") -> list:
    blocks = []
    if caption:
        blocks.append(TextContent(type="text", text=caption))
    blocks.append(
        ImageContent(
            type="image",
            data=base64.b64encode(png).decode("ascii"),
            mimeType="image/png",
        )
    )
    return blocks


def _safe(fn, *args, **kwargs):
    """Run a client call, returning a JSON-serialisable error dict on failure."""
    try:
        return fn(*args, **kwargs)
    except DefoldError as e:
        return {"success": False, "error": str(e)}


@mcp.tool()
def defold_status() -> str:
    """Check whether the Defold editor is installed and whether a project is open/running.

    Returns the editor path, the editor URL (if reachable), the editor version,
    and the list of available /command/* operations.
    """
    client = _client()
    editor = client.find_editor()
    running = client.is_running()
    info = {
        "project": str(client.project_path),
        "editor_installed": bool(editor),
        "editor_path": editor,
        "editor_running": running,
        "editor_url": client.base_url(),
    }
    if running:
        try:
            oa = client.openapi()
            info["editor_version"] = oa.get("info", {}).get("version")
            info["commands"] = sorted(
                p for p in oa.get("paths", {}) if p.startswith("/command/")
            )
        except DefoldError:
            pass
    return json.dumps(info, indent=2, default=str)


@mcp.tool()
def defold_open_project(launch: bool = True, port: Optional[int] = None) -> str:
    """Connect to the Defold editor for this project.

    If the editor is already running, connect to it. If ``launch`` is True and
    the editor is not running, start it and wait until it is reachable.
    """
    client = _client()
    try:
        if client.is_running():
            base = client.connect()
            return json.dumps({"connected": True, "editor_url": base, "launched": False})
        if launch:
            base = client.launch(port=port)
            return json.dumps({"connected": True, "editor_url": base, "launched": True})
        return json.dumps({"connected": False, "error": "Editor not running and launch=False"})
    except DefoldError as e:
        return json.dumps({"connected": False, "error": str(e)})


@mcp.tool()
def defold_compile() -> str:
    """Compile the project without running it.

    Returns ``success`` and any ``issues`` (errors/warnings) with file and line
    information.
    """
    client = _client()
    result = _safe(client.compile)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_run() -> str:
    """Compile and run the project in the editor (debug build).

    The running game exposes an engine service that can be queried for state
    and screenshots.
    """
    client = _client()
    result = _safe(client.run)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_console(tail: int = 200) -> str:
    """Read the editor console (game + build output).

    Returns the last ``tail`` lines plus any semantic regions (errors, etc.).
    """
    client = _client()
    result = _safe(client.console)
    if isinstance(result, dict) and "lines" in result:
        lines = result.get("lines", [])
        return json.dumps(
            {"lines": lines[-tail:], "regions": result.get("regions", [])},
            indent=2,
            default=str,
        )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_preview(path: str, width: int = 1280, height: int = 720) -> list:
    """Render a scene resource to a PNG using the editor's scene view.

    ``path`` is the project path without a leading slash, e.g.
    ``main/main.collection``. Returns the image. Note: editor previews are not
    runtime screenshots (no dynamic objects / post-processing).
    """
    client = _client()
    try:
        png = client.preview(path, width, height)
        return _image(png, f"Editor preview of {path}")
    except DefoldError as e:
        return _text({"error": str(e)})


@mcp.tool()
def defold_ref(query: str, environment: str = "runtime", language: str = "Lua") -> str:
    """Search the Defold API documentation bundled with the running editor.

    ``query`` uses whitespace as AND and ``|`` as OR. ``environment``:
    editor|runtime (comma-separated). ``language``: Lua|C|C++ (comma-separated).
    """
    client = _client()
    result = _safe(client.ref, query, environment, language)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_engine_info() -> str:
    """Query the running game's engine service: /ping, /info, /state.

    Returns engine version, platform, and connection state. Requires the game
    to be running (defold_run).
    """
    client = _client()
    try:
        url = client.engine_service_url()
        if not url:
            return json.dumps(
                {"error": "No running engine service. Run the game first (defold_run)."}
            )
        out = {"engine_url": url}
        for ep in ("/ping", "/info", "/state"):
            status, body = client._request("GET", ep, base=url)
            out[ep.strip("/")] = client._json(body)
        return json.dumps(out, indent=2, default=str)
    except DefoldError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def defold_screenshot(after_frames: int = 0) -> list:
    """Capture a runtime screenshot of the running game.

    Uses the Automation Bridge v2 extension (GET /automation-bridge/v2/screenshot,
    then polls /screenshot/status). Requires the game to be running and the
    official ``automation-bridge`` debug extension to be installed in the
    project. ``after_frames`` delays the capture by that many frames.
    Returns the image.
    """
    client = _client()
    try:
        png = client.screenshot(after_frames=after_frames)
        return _image(png, "Runtime screenshot")
    except DefoldError as e:
        return _text({"error": str(e)})


@mcp.tool()
def defold_inject_input(x: int, y: int, button: str = "left") -> str:
    """Inject a mouse click into the running game.

    Uses the Automation Bridge v2 extension (POST /automation-bridge/v2/input/click).
    Requires the game to be running and the ``automation-bridge`` extension.
    """
    client = _client()
    result = _safe(client.input_click, x, y, button)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_stop() -> str:
    """Best-effort stop: terminates the editor process that this MCP server
    launched (and thus the running game). Does nothing if the editor was not
    launched by this server session.
    """
    client = _client()
    result = _safe(client.stop)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Editor commands (extra)
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_clean_build() -> str:
    """Clean build: delete build artifacts and recompile the project.

    Returns ``success`` and any ``issues`` (errors/warnings).
    """
    client = _client()
    result = _safe(client.clean_build)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_hot_reload() -> str:
    """Hot-reload all modified files into the running project.

    Pushes changed scripts/resources into the game that is currently running
    (started with ``defold_run``) without a full recompile/relaunch. Returns
    ``success`` and any ``issues``. Requires the game to be running.
    """
    client = _client()
    result = _safe(client.hot_reload)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — health, screen, frame
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_health() -> str:
    """Query the Automation Bridge v2 health endpoint.

    Returns the bridge version, capabilities, screen size, and input
    devices. Requires the game to be running and the official
    ``automation-bridge`` debug extension.
    """
    client = _client()
    result = _safe(client.bridge_health)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_lifecycle() -> str:
    """Get the ordered runtime lifecycle stages of the running game."""
    client = _client()
    result = _safe(client.bridge_lifecycle)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_screen() -> str:
    """Get the game's screen layout: named rectangles, display scale, and the
    coordinate convention used by the bridge.
    """
    client = _client()
    result = _safe(client.bridge_screen)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_resize_screen(width: int, height: int) -> str:
    """Resize the game's native window to ``width`` x ``height``."""
    client = _client()
    result = _safe(client.bridge_resize_screen, width, height)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_convert_coordinates(x: float, y: float, from_space: str, to_space: str) -> str:
    """Convert a point between coordinate spaces.

    See ``defold_bridge_screen`` for the available spaces and the
    coordinate convention.
    """
    client = _client()
    result = _safe(client.bridge_convert_coordinates, x, y, from_space, to_space)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_frame() -> str:
    """Get the current engine frame number and scene sequence."""
    client = _client()
    result = _safe(client.bridge_frame)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — scene inspection
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_scene(visible: Optional[bool] = None, include: Optional[str] = None) -> str:
    """Inspect the full runtime scene tree of the running game.

    ``visible`` filters to visible elements only. ``include`` is a
    comma-separated list of extra fields (e.g. ``bounds,properties``).
    """
    client = _client()
    result = _safe(client.bridge_scene, visible=visible, include=include)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_elements(
    type: Optional[str] = None,
    name: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    id: Optional[str] = None,
    path: Optional[str] = None,
    kind: Optional[str] = None,
    visible: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    cursor: Optional[str] = None,
    include: Optional[str] = None,
) -> str:
    """Search runtime elements with filters and pagination.

    Any combination of filters: ``type``, ``name``, ``text``, ``url``, ``id``,
    ``path``, ``kind``, ``visible``; plus ``limit``/``offset``/``cursor``
    pagination and ``include`` for extra fields (e.g. ``bounds,properties``).
    """
    client = _client()
    result = _safe(
        client.bridge_elements,
        type=type,
        name=name,
        text=text,
        url=url,
        id=id,
        path=path,
        kind=kind,
        visible=visible,
        limit=limit,
        offset=offset,
        cursor=cursor,
        include=include,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_element(element_id: str, include: Optional[str] = None) -> str:
    """Get one runtime element in detail (bounds + properties by default)."""
    client = _client()
    result = _safe(client.bridge_element, element_id, include=include)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — input
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_click(
    x: Optional[float] = None,
    y: Optional[float] = None,
    element_id: Optional[str] = None,
    button: str = "left",
    modifiers: Optional[str] = None,
    visualize: Optional[bool] = None,
) -> str:
    """Click in the running game, by window coordinates (``x``/``y``) or by
    ``element_id``.

    ``button``: left|right|middle. ``modifiers``: comma-separated
    (ctrl, shift, alt). ``visualize`` highlights the click in the game.
    """
    client = _client()
    result = _safe(
        client.bridge_input_click,
        x=x,
        y=y,
        element_id=element_id,
        button=button,
        modifiers=modifiers,
        visualize=visualize,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_drag(
    x1: Optional[float] = None,
    y1: Optional[float] = None,
    x2: Optional[float] = None,
    y2: Optional[float] = None,
    from_id: Optional[str] = None,
    to_id: Optional[str] = None,
    duration: Optional[float] = None,
    easing: Optional[str] = None,
    button: str = "left",
    visualize: Optional[bool] = None,
) -> str:
    """Drag between two points (``x1``,``y1`` -> ``x2``,``y2``) or between two
    element ids (``from_id`` -> ``to_id``).

    ``duration`` is seconds; ``easing`` e.g. linear|ease-in|ease-out.
    """
    client = _client()
    result = _safe(
        client.bridge_input_drag,
        x1=x1, y1=y1, x2=x2, y2=y2,
        from_id=from_id, to_id=to_id,
        duration=duration, easing=easing,
        button=button, visualize=visualize,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_drag_path(
    points: str,
    durations: Optional[str] = None,
    easing: Optional[str] = None,
    button: str = "left",
    visualize: Optional[bool] = None,
) -> str:
    """Drag along a multi-point path.

    ``points`` is ``x,y`` pairs separated by semicolons, e.g.
    ``"100,100;180,60;260,140"``. ``durations`` is per-segment seconds,
    semicolon-separated.
    """
    client = _client()
    result = _safe(
        client.bridge_input_drag_path,
        points=points,
        durations=durations,
        easing=easing,
        button=button,
        visualize=visualize,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_key(
    text: Optional[str] = None,
    keys: Optional[str] = None,
    hold: Optional[float] = None,
    modifiers: Optional[str] = None,
) -> str:
    """Type literal text and/or press special keys in the running game.

    ``keys`` is one or more brace-wrapped special keys, e.g. ``"{KEY_ENTER}"``
    (comma-separated for several). ``hold`` is seconds to hold the keys down.
    """
    client = _client()
    result = _safe(
        client.bridge_input_key,
        text=text, keys=keys, hold=hold, modifiers=modifiers,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_wheel(
    steps: int,
    x: Optional[float] = None,
    y: Optional[float] = None,
    element_id: Optional[str] = None,
) -> str:
    """Turn the mouse wheel by ``steps`` detents (-64..64) at a window point
    (``x``/``y``) or an ``element_id``.
    """
    client = _client()
    result = _safe(
        client.bridge_input_wheel,
        steps=steps, x=x, y=y, element_id=element_id,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_input_status(input_id: int) -> str:
    """Get the current (or bounded-history) status of one injected input."""
    client = _client()
    result = _safe(client.bridge_input_status, input_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_input_pending() -> str:
    """List the FIFO queue of pending injected inputs."""
    client = _client()
    result = _safe(client.bridge_input_pending)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_input_cancel(input_id: int, release: Optional[bool] = None) -> str:
    """Cancel one queued/active injected input by id."""
    client = _client()
    result = _safe(client.bridge_input_cancel, input_id, release=release)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_input_flush(release: Optional[bool] = None) -> str:
    """Cancel the session's active and later queued inputs."""
    client = _client()
    result = _safe(client.bridge_input_flush, release=release)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_input_configure(
    client_id: str,
    session_id: str,
    lease: float = 5.0,
    device: str = "auto",
    visualize: Optional[bool] = None,
) -> str:
    """Acquire or renew the input controller lease for a client/session.

    ``device``: auto|mouse|touch. ``lease`` is the lease duration in seconds.
    """
    client = _client()
    result = _safe(
        client.bridge_input_configure,
        client_id, session_id, lease=lease, device=device, visualize=visualize,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_pointer_open(
    x: float,
    y: float,
    pointer_lease: float = 2.0,
    button: str = "left",
    modifiers: Optional[str] = None,
) -> str:
    """Open a leased pointer session at window ``x``/``y``. Returns an input id
    to use with the other pointer tools.
    """
    client = _client()
    result = _safe(
        client.bridge_pointer_open,
        x, y, pointer_lease=pointer_lease, button=button, modifiers=modifiers,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_pointer_move(
    input_id: int,
    x: float,
    y: float,
    duration: float,
    easing: str = "linear",
    pointer_lease: float = 2.0,
) -> str:
    """Move an open pointer to ``x``/``y`` over ``duration`` seconds."""
    client = _client()
    result = _safe(
        client.bridge_pointer_move,
        input_id, x, y, duration, easing=easing, pointer_lease=pointer_lease,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_pointer_hold(input_id: int, duration: float, pointer_lease: float = 2.0) -> str:
    """Hold an open pointer for ``duration`` seconds."""
    client = _client()
    result = _safe(client.bridge_pointer_hold, input_id, duration, pointer_lease=pointer_lease)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_pointer_up(input_id: int, pointer_lease: float = 2.0) -> str:
    """Release an open pointer."""
    client = _client()
    result = _safe(client.bridge_pointer_up, input_id, pointer_lease=pointer_lease)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — native video recording
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_recording_capabilities() -> str:
    """Check native video recording availability on this platform."""
    client = _client()
    result = _safe(client.bridge_recording_capabilities)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_recording_status() -> str:
    """Get the active or most recently finalized recording."""
    client = _client()
    result = _safe(client.bridge_recording_status)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_recording_start(
    path: str,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fps: int = 30,
    audio: Optional[bool] = None,
) -> str:
    """Start a native video recording of the running game to ``path`` (MP4).

    ``width``/``height`` override the capture resolution; ``audio`` captures
    audio when supported.
    """
    client = _client()
    result = _safe(
        client.bridge_recording_start,
        path, width=width, height=height, fps=fps, audio=audio,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_recording_stop() -> str:
    """Stop the active recording and finalize the MP4 file."""
    client = _client()
    result = _safe(client.bridge_recording_stop)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — events, states, commands, markers
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_events_cursor() -> str:
    """Get the next and oldest retained event cursors."""
    client = _client()
    result = _safe(client.bridge_events_cursor)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_events(
    cursor: Optional[int] = None,
    timeout_ms: Optional[int] = None,
    limit: Optional[int] = None,
) -> str:
    """Long-poll for game events at or after ``cursor``.

    ``timeout_ms`` bounds the long poll; ``limit`` caps the number of events.
    """
    client = _client()
    result = _safe(client.bridge_events, cursor=cursor, timeout_ms=timeout_ms, limit=limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_state(name: Optional[str] = None) -> str:
    """Get all published game states, or one by exact ``name``."""
    client = _client()
    result = _safe(client.bridge_state, name=name)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_state_wait(after_revision: int, timeout_ms: int = 10000) -> str:
    """Long-poll until a newer state revision appears (or the timeout elapses)."""
    client = _client()
    result = _safe(client.bridge_state_wait, after_revision, timeout_ms=timeout_ms)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_application_catalog(
    kind: Optional[str] = None,
    name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
    cursor: Optional[str] = None,
) -> str:
    """List the game's registered commands, states, and events.

    ``kind``: command|state|event. Paginate with ``limit``/``offset``/``cursor``.
    """
    client = _client()
    result = _safe(
        client.bridge_application_catalog,
        kind=kind, name=name, limit=limit, offset=offset, cursor=cursor,
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_command(name: str, data: Optional[Any] = None, timeout_ms: Optional[int] = None) -> str:
    """Submit a named command to the running game.

    ``data`` is the command payload (a dict, which is JSON-encoded, or a JSON
    string). Returns a command id to poll with ``defold_bridge_command_status``.
    """
    client = _client()
    result = _safe(client.bridge_command, name, data=data, timeout_ms=timeout_ms)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_command_status(command_id: int) -> str:
    """Poll a submitted command by id."""
    client = _client()
    result = _safe(client.bridge_command_status, command_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_command_cancel(command_id: int) -> str:
    """Cancel a pending command by id."""
    client = _client()
    result = _safe(client.bridge_command_cancel, command_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_marker(
    name: str,
    data: Optional[Any] = None,
    recording_timestamp_us: Optional[int] = None,
) -> str:
    """Insert a timeline marker event (useful while recording video).

    ``data`` is the marker payload (dict or JSON string).
    """
    client = _client()
    result = _safe(
        client.bridge_marker,
        name, data=data, recording_timestamp_us=recording_timestamp_us,
    )
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — Metal GPU trace (macOS only)
# ------------------------------------------------------------------ #


@mcp.tool()
def defold_bridge_metal(path: str, frames: Optional[int] = None) -> str:
    """Schedule a Metal GPU trace capture to ``path`` (macOS only).

    ``frames`` caps the number of captured frames.
    """
    client = _client()
    result = _safe(client.bridge_metal, path, frames=frames)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_metal_status() -> str:
    """Poll a Metal GPU trace capture's status (macOS only)."""
    client = _client()
    result = _safe(client.bridge_metal_status)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def defold_bridge_metal_stop() -> str:
    """Request an early stop of a Metal GPU trace capture (macOS only)."""
    client = _client()
    result = _safe(client.bridge_metal_stop)
    return json.dumps(result, indent=2, default=str)


if __name__ == "__main__":
    mcp.run()
