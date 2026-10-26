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
import functools
import platform
import re
import shutil
import urllib.request
import zipfile
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:  # mcp v1
    from mcp.server.fastmcp import FastMCP as _Server
except ModuleNotFoundError:  # mcp v2 (FastMCP -> MCPServer)
    from mcp.server.mcpserver import MCPServer as _Server
from mcp.types import (
    ImageContent,
    TextContent,
    ToolAnnotations as _ToolAnnotations,
)

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
# ---------------------------------------------------------------------------
# Privilege levels + read-only mode (Phase 1)
# Levels: read < verify < modify < privileged. When DEFOLD_MCP_READONLY=1,
# modify/privileged tools are gated and return a structured error JSON.
# ---------------------------------------------------------------------------
READONLY = os.environ.get("DEFOLD_MCP_READONLY", "").strip().lower() in (
    "1", "true", "yes"
)

_TOOL_ANNOTATIONS: dict[str, "_ToolAnnotations"] = {
    "read": _ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
    "verify": _ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True
    ),
    "modify": _ToolAnnotations(
        read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
    ),
    "privileged": _ToolAnnotations(
        read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
    ),
}

_TOOL_LEVELS: dict[str, str] = {}  # tool name -> level (populated by @tool)


def tool(level):
    """Register a tool with privilege-level annotations and an optional read-only gate."""
    assert level in _TOOL_ANNOTATIONS, f"unknown tool level {level!r}"

    def decorator(fn):
        if level in ("modify", "privileged"):

            @functools.wraps(fn)
            def gated(*args, **kwargs):
                if READONLY:
                    return json.dumps(
                        {"success": False, "error": "blocked: read-only"}, indent=2
                    )
                return fn(*args, **kwargs)

            target = gated
        else:
            target = fn
        _TOOL_LEVELS[target.__name__] = level
        mcp.tool(annotations=_TOOL_ANNOTATIONS[level])(target)
        return target

    return decorator

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


def _result(status: str, issues=None, artifacts=None) -> str:
    """Uniform structured result for mutative ops: ``{status, issues[], artifacts{}}``.

    Artifacts (screenshots, videos, diffs, logs) are written under
    ``ai_work/artifacts/<run-id>/`` relative to this repository.
    """
    return json.dumps(
        {"status": status, "issues": list(issues or []), "artifacts": dict(artifacts or {})},
        indent=2,
        default=str,
    )


def _new_run_id(prefix: str = "run") -> str:
    """Run id for the ``ai_work/artifacts/<run-id>/`` convention."""
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


@tool("read")
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
    info["mcp_mode"] = "read-only" if READONLY else "standard"
    info["tool_levels"] = dict(_TOOL_LEVELS)
    return json.dumps(info, indent=2, default=str)


@tool("modify")
def defold_open_project(launch: bool = True, port: Optional[int] = None) -> str:
    """Connect to the Defold editor for this project (launching it if needed).

    Returns structured JSON ``{status, issues[], artifacts{}}``.

    Deterministic verification: after connecting/launching, re-probes the
    editor with a ``/openapi.json`` request; status is "ok" only when the
    editor answers (same probe as ``defold_status``).
    """
    client = _client()
    try:
        if not client.is_running():
            if not launch:
                return _result("error", ["Editor not running and launch=False"])
            client.launch(port=port)
        client.connect()
        # Deterministic verification: editor must answer on its URL.
        client.openapi()
    except DefoldError as e:
        return _result("error", [str(e)])
    return _result("ok")


@tool("verify")
def defold_compile() -> str:
    """Compile the project without running it.

    Returns ``success`` and any ``issues`` (errors/warnings) with file and line
    information.
    """
    client = _client()
    result = _safe(client.compile)
    return json.dumps(result, indent=2, default=str)


@tool("verify")
def defold_run(engine_timeout: int = 30) -> str:
    """Compile and run the project in the editor (debug build).

    Returns structured JSON ``{status, issues[], artifacts{}}``.

    Deterministic verification: after a successful ``run``, polls the engine
    service (port auto-discovered from the console) until ``/ping`` answers,
    then re-reads the last console lines; status is "ok" only when the engine
    service is reachable within ``engine_timeout`` seconds and no error line
    appears in the recent console output. Artifacts produced by later tools
    (screenshots, diffs, logs) go under ``ai_work/artifacts/<run-id>/``.
    """
    client = _client()
    result = _safe(client.run)
    if not isinstance(result, dict):
        return _result("error", [str(result)])
    issues: list[str] = list(result.get("issues") or [])
    if result.get("success") is False:
        issues.append(f"run failed: {result.get('error', 'editor run command rejected')}")
        return _result("error", issues)
    deadline = time.monotonic() + engine_timeout
    while time.monotonic() < deadline:
        try:
            client.engine_ping()
            break
        except DefoldError:
            time.sleep(1.0)
    else:
        issues.append(f"Engine service not reachable within {engine_timeout}s after run")
        return _result("error", issues)
    # Deterministic verification (2/2): re-read recent console lines for errors.
    try:
        console = client.console()
        lines = [str(x) for x in (console.get("lines", []) if isinstance(console, dict) else [])]
        issues.extend(ln for ln in lines[-5:] if "Error" in ln or "Traceback" in ln)
    except DefoldError:
        pass
    return _result("ok" if not issues else "error", issues)


@tool("read")
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


@tool("read")
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


# ------------------------------------------------------------------ #
# Headless documentation (official llms.txt indexes)
# ------------------------------------------------------------------ #
_DOCS_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "defold-docs"
_DOCS_INDEXES = {
    "manuals": "https://www.defold.com/llms/manuals.md",
    "apis": "https://www.defold.com/llms/apis.md",
    "examples": "https://www.defold.com/llms/examples.md",
}
_DOCS_TTL = 24 * 3600


def _docs_index(name: str) -> str:
    """Fetch one llms index (TTL 24 h cache in ``.cache/defold-docs/``)."""
    _DOCS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = _DOCS_CACHE_DIR / ("llms-" + name + ".md")
    fresh = f.exists() and (time.time() - f.stat().st_mtime) < _DOCS_TTL
    if not fresh:
        req = urllib.request.Request(_DOCS_INDEXES[name], headers={"User-Agent": "defold-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            f.write_bytes(r.read())
    return f.read_text(encoding="utf-8")


def _docs_search_and_fetch(query: str, fetch: bool = True, max_chars: int = 20000, note: str = "") -> str:
    """Resolve ``query`` against the llms indexes; optionally fetch ONE page.

    Returns JSON with ``status``, ``matches[]`` (top 5), and — when fetching —
    ``page`` + ``content`` trimmed to ``max_chars``. Only index files and a
    single targeted .md are ever loaded (never the huge llms-*.txt blobs).
    """
    q = [t.lower() for t in query.split() if len(t) > 1]
    out: dict = {"status": "ok", "query": query, "matches": [], "artifacts": {}}
    if note:
        out["note"] = note
    if not q:
        out["status"] = "error"
        out["issues"] = ["Empty query"]
        return json.dumps(out, indent=2)
    cands: list = []
    for name in _DOCS_INDEXES:
        try:
            text = _docs_index(name)
        except Exception:
            continue  # one index failing must not kill the search
        for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+\.md)\)", text):
            title, url = m.group(1), m.group(2)
            hay = (title + " " + url.rsplit("/", 1)[-1].replace("-", " ")).lower()
            score = sum(t in hay for t in q)
            if score:
                cands.append((score, title, name, url))
    cands.sort(key=lambda c: (-c[0], list(_DOCS_INDEXES).index(c[2])))
    seen, uniq = set(), []
    for sc, title, name, url in cands:
        if url not in seen:
            seen.add(url)
            uniq.append((sc, title, name, url))
    out["matches"] = [{"title": t, "index": n, "url": u} for _, t, n, u in uniq[:5]]
    issues = []
    if not uniq:
        issues.append("No documentation page matched the query.")
    elif fetch:
        url = uniq[0][3]
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "defold-mcp/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                content = r.read().decode("utf-8", "replace")
        except Exception as e:
            issues.append(f"Failed to fetch {url}: {e}")
            content = None
        if content is not None:
            out["page"] = {"title": uniq[0][1], "url": url, "truncated": len(content) > max_chars}
            out["content"] = content[:max_chars]
    if issues:
        out["status"] = "error"
        out["issues"] = issues
    return json.dumps(out, indent=2)


@tool("read")
def defold_docs(query: str, fetch: bool = True, max_chars: int = 20000) -> str:
    """Search Defold documentation headlessly (no editor required).

    Resolves the query against the official llms.txt indexes (manuals, apis,
    examples), cached in ``.cache/defold-docs/`` for 24 h, then fetches ONE
    targeted .md page (the best match) trimmed to ``max_chars`` characters.
    With ``fetch=False`` only the candidate list is returned.

    Version note: llms docs track the latest Defold release; ``defold_ref``
    matches the exact build of the running editor.
    """
    return _docs_search_and_fetch(query, fetch=fetch, max_chars=max_chars)


# ------------------------------------------------------------------ #
# Headless Bob builds (no editor required)
# ------------------------------------------------------------------ #
_BOB_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "bob"
_JAVA_CACHE_DIR = Path(__file__).resolve().parent / ".cache" / "java"
_BOB_FALLBACK_URL = "https://github.com/defold/defold/releases/latest/download/bob.jar"
_BOB_MIN_JAVA = 25


def _artifact_dir(run_id: str) -> Path:
    """``ai_work/artifacts/<run-id>/`` (created)."""
    d = Path(__file__).resolve().parent / "ai_work" / "artifacts" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _java_major(java_exe: str) -> Optional[int]:
    try:
        p = subprocess.run([java_exe, "-version"], capture_output=True, text=True, timeout=30)
        out = ((p.stderr or "") + (p.stdout or ""))
        m = re.search(r'version "(\d+)(?:\.([0-9]+))?', out)
        if not m:
            return None
        major = int(m.group(1))
        if major == 1 and m.group(2):
            return int(m.group(2))
        return major
    except Exception:
        return None


def _resolve_java(min_major: int = _BOB_MIN_JAVA, download: bool = True) -> tuple:
    """Find a java executable with major >= ``min_major``.

    Order: ``DEFOLD_MCP_JAVA`` env var, PATH ``java``, cached JRE dirs in
    ``.cache/java/`` (auto-downloaded from Temurin when nothing works).
    Returns ``(path, major)``; raises DefoldError when resolution fails.
    """
    cands: list = []
    env = os.environ.get("DEFOLD_MCP_JAVA", "").strip()
    if env and Path(env).exists():
        cands.append(env)
    which_java = shutil.which("java")
    if which_java:
        cands.append(which_java)
    exe_name = "java.exe" if os.name == "nt" else "java"
    for d in sorted(_JAVA_CACHE_DIR.glob("jdk-*"), reverse=True):
        exe = d / "bin" / exe_name
        if exe.exists():
            cands.append(str(exe))
    for c in cands:
        mj = _java_major(c)
        if mj is not None and mj >= min_major:
            return c, mj

    if not download:
        raise DefoldError(f"no java >= {min_major} found locally (set DEFOLD_MCP_JAVA)")
    # Nothing suitable locally: download Temurin JRE into .cache/java/
    arch = "aarch64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    osname = {"nt": "windows", "darwin": "mac"}.get(os.name, "linux")
    api = "https://api.github.com/repos/adoptium/temurin25-binaries/releases/latest"
    req = urllib.request.Request(api, headers={"User-Agent": "defold-mcp/1.0"})
    rel = json.load(urllib.request.urlopen(req, timeout=60))
    pat = re.compile(r"^OpenJDK25U-jre_%s_%s_hotspot_.*\.zip$" % (arch, osname))
    asset = next((a for a in rel.get("assets", []) if pat.match(a["name"])), None)
    if asset is None:
        raise DefoldError(f"No Temurin 25 JRE {arch}/{osname} asset found")
    _JAVA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    z = _JAVA_CACHE_DIR / "jre25.zip"
    dl = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "defold-mcp/1.0"})
    with urllib.request.urlopen(dl, timeout=900) as r:
        with open(z, "wb") as f:
            shutil.copyfileobj(r, f)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(_JAVA_CACHE_DIR)
    z.unlink(missing_ok=True)
    for d in sorted(_JAVA_CACHE_DIR.glob("jdk-*"), reverse=True):
        exe = d / "bin" / exe_name
        if exe.exists():
            return str(exe), 25
    raise DefoldError("Downloaded JRE extracted but no java executable found")


def _resolve_bob(download: bool = True) -> Path:
    """Locate bob.jar.

    Order: ``DEFOLD_MCP_BOB`` env var, a Defold install
    (``<install>/bob/jar/bob.jar``), else ``.cache/bob/bob.jar`` auto-downloaded
    from the latest defold/defold release (~219 MiB, one-time).
    """
    env = os.environ.get("DEFOLD_MCP_BOB", "").strip()
    if env:
        p = Path(env)
        if not p.exists():
            raise DefoldError(f"DEFOLD_MCP_BOB does not exist: {env}")
        return p
    for base in (Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Defold", Path("C:/Defold")):
        p = base / "bob" / "jar" / "bob.jar"
        if p.exists():
            return p
    cached = _BOB_CACHE_DIR / "bob.jar"
    if not cached.exists() and not download:
        raise DefoldError("bob.jar not found locally (set DEFOLD_MCP_BOB)")
    if not cached.exists():
        _BOB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cached.with_suffix(".part")
        req = urllib.request.Request(_BOB_FALLBACK_URL, headers={"User-Agent": "defold-mcp/1.0"})
        with urllib.request.urlopen(req, timeout=1800) as r:
            with open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        tmp.replace(cached)
    return cached


def _run_bob(java_exe: str, bob_jar: Path, project: Path, options: list, commands: list) -> tuple:
    """Run ``java -jar bob.jar <options> <commands>``; returns (rc, stdout+stderr)."""
    cmd = [java_exe, "-jar", str(bob_jar), "--root", str(project)] + options + commands
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1800, cwd=str(project))
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _parse_build_report(path: Path) -> Optional[dict]:
    """Parse the official bob --build-report-json file; None when absent/invalid."""
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _editor_command_names(client) -> list:
    """Command names discovered from the editor's /openapi.json spec."""
    try:
        spec = client.openapi() or {}
    except DefoldError:
        return []
    paths = spec.get("paths") or {}
    return sorted(p[len("/command/"):] for p in paths if isinstance(p, str) and p.startswith("/command/"))


@tool("modify")
def defold_editor_command(op: str, params: dict | None = None) -> str:
    """Generic editor command dispatcher for any ``/command/*`` endpoint.

    ``op`` is a command name (e.g. ``"compile"``, ``"run"``); the full path
    ``"/command/run"`` also works. Available names are discovered at runtime
    from the editor's OpenAPI spec (GET /openapi.json), so new editor commands
    become usable without updating this server. ``params`` is sent as the JSON
    request body and validated by the editor itself; omit it for no-arg commands.

    Deterministic verification: status "ok" requires HTTP 200 from
    POST /command/<op>; any other response puts the editor's payload in
    ``issues[]`` with status "error". On success the editor's JSON body is
    returned under ``response``. No artifacts are produced.
    """
    try:
        client = _client()
    except DefoldError as e:
        return _result("error", [str(e)])
    name = (op or "").strip()
    if name.startswith("/command/"):
        name = name[len("/command/"):]
    name = name.strip("/")
    names = _editor_command_names(client)
    if not name:
        avail = f" Available: {', '.join(names)}" if names else ""
        return _result("error", ["Empty command name." + avail])
    data = params if isinstance(params, dict) else {}
    unknown_hint = ""
    if names and name not in names:
        unknown_hint = f" Unknown command '{name}'. Available: {', '.join(names)}"
    result = _safe(client._post_command, name, data)
    if isinstance(result, dict) and result.get("success") is False:
        return _result("error", [str(result.get("error")) + unknown_hint])
    status_code = (result or {}).get("_http_status") if isinstance(result, dict) else None
    if status_code != 200:
        msg = json.dumps(result, default=str)[:1500]
        extra = []
        if name not in names and names:
            extra.append(f"Unknown command '{name}'. Available: {', '.join(names)}")
        return _result("error", extra + [msg])
    payload = {k: v for k, v in (result or {}).items() if k != "_http_status"}
    return json.dumps({"status": "ok", "issues": [], "artifacts": {}, "response": payload}, indent=2, default=str)


@tool("modify")
def defold_bob_build(settings_name: str = "", settings_file: str = "", target_platform: str = "") -> str:
    """Headless bob ``resolve build`` without the Defold editor.

    Runs ``java -jar bob.jar --root <project> [--settings FILE]
    [--platform X] --build-report-json <artifact> resolve build`` and parses
    the official JSON build report (not just the exit status). Bob resolves
    from ``DEFOLD_MCP_BOB``, a Defold install, or ``.cache/bob/``; java >= 25
    from PATH or ``.cache/java/`` (auto-downloaded).

    Verification before reporting ok: process exit code 0 AND a parseable
    report with non-empty ``resources[]``. Artifacts under
    ``ai_work/artifacts/<run-id>/``: ``bob-build-report.json``, ``bob-stdout.log``.
    """
    run_id = _new_run_id("bob-build")
    outdir = _artifact_dir(run_id)
    report_path = outdir / "bob-build-report.json"
    issues: list = []
    artifacts = {"build_report": str(report_path)}
    try:
        java_exe, java_mj = _resolve_java()
        bob_jar = _resolve_bob()
        project = Path(_default_project())
        options = ["--build-report-json", str(report_path)]
        if settings_file:
            options += ["--settings", settings_file]
        elif settings_name:
            issues.append(
                "Provide settings_file: Bob takes a single `--settings <file>` path;"
                " settings_name alone cannot be resolved.")
            return _result("error", issues, artifacts)
        if target_platform:
            options += ["--platform", target_platform]
        rc, log = _run_bob(java_exe, bob_jar, project, options, ["resolve", "build"])
    except DefoldError as e:
        return _result("error", [str(e)], artifacts)
    (outdir / "bob-stdout.log").write_text(log, encoding="utf-8")
    artifacts["log"] = str(outdir / "bob-stdout.log")
    if rc != 0:
        issues.append(f"bob exited with code {rc}: " + log.strip().splitlines()[-1] if log.strip() else f"bob exited with code {rc}")
    report = _parse_build_report(report_path)
    if report is None:
        issues.append("Build report JSON missing or unparseable.")
    summary = {}
    if report:
        res = report.get("resources", [])
        summary = {
            "project_name": report.get("project_name"),
            "report_version": report.get("report_version"),
            "resources": len(res),
            "total_bytes": sum(r.get("size", 0) for r in res if isinstance(r, dict)),
        }
        if not res:
            issues.append("Build report contains no resources.")
    status = "ok" if (rc == 0 and report is not None and summary.get("resources")) else "error"
    out = json.loads(_result(status, issues, artifacts))
    out["report"] = summary
    return json.dumps(out, indent=2)


@tool("modify")
def defold_bob_bundle(target_platform: str, settings_name: str = "", settings_file: str = "", variant: str = "headless", archive: bool = True) -> str:
    """Headless bob ``resolve build bundle`` for one platform (no editor).

    Reference shape from the Defold manual::

      java -jar bob.jar --root . --settings test/test.settings \
        --platform x86_64-linux --variant headless --archive \
        --bundle-output <dir> resolve build bundle

    ``settings_file`` is passed as Bob's single ``--settings <file>`` argument
    (Bob has no name/file pair); e.g. the CI-only ``test/test.settings``. Output goes to ``ai_work/artifacts/<run-id>/bundle``
    by default; the JSON build report is also captured and parsed.

    Verification before reporting ok: exit code 0, parseable report, and at
    least one file in the bundle output directory.
    """
    run_id = _new_run_id("bob-bundle")
    outdir = _artifact_dir(run_id)
    bundle_out = outdir / "bundle"
    bundle_out.mkdir(parents=True, exist_ok=True)
    report_path = outdir / "bob-build-report.json"
    issues: list = []
    artifacts = {"build_report": str(report_path), "bundle_output": str(bundle_out)}
    try:
        java_exe, _ = _resolve_java()
        bob_jar = _resolve_bob()
        project = Path(_default_project())
        options = ["--platform", target_platform, "--variant", variant,
                   "--build-report-json", str(report_path),
                   "--bundle-output", str(bundle_out)]
        if archive:
            options.append("--archive")
        if settings_file:
            options += ["--settings", settings_file]
        elif settings_name:
            issues.append(
                "Provide settings_file: Bob takes a single `--settings <file>` path;"
                " settings_name alone cannot be resolved.")
            return _result("error", issues, artifacts)
        rc, log = _run_bob(java_exe, bob_jar, project, options, ["resolve", "build", "bundle"])
    except DefoldError as e:
        return _result("error", [str(e)], artifacts)
    (outdir / "bob-stdout.log").write_text(log, encoding="utf-8")
    artifacts["log"] = str(outdir / "bob-stdout.log")
    if rc != 0:
        tail = log.strip().splitlines()[-1] if log.strip() else ""
        issues.append(f"bob exited with code {rc}: {tail}")
    report = _parse_build_report(report_path)
    if report is None:
        issues.append("Build report JSON missing or unparseable.")
    bundled = [str(p.relative_to(outdir)) for p in bundle_out.rglob("*") if p.is_file()]
    status = "ok" if (rc == 0 and report is not None and bundled) else "error"
    out = json.loads(_result(status, issues, artifacts))
    out["bundle_files"] = bundled[:50]
    return json.dumps(out, indent=2)


@tool("read")
def defold_bob_doctor() -> str:
    """Check headless build prerequisites: java version, bob.jar location, extender env.

    Read-only; downloads nothing. Reports what ``defold_bob_build`` would use.
    """
    info: dict = {"ok": True, "issues": []}
    try:
        java_exe, mj = _resolve_java(download=False)
        info["java"] = {"path": java_exe, "major": mj}
    except Exception as e:
        info["java"] = None
        info["issues"].append(f"java >= {_BOB_MIN_JAVA} not resolvable locally: {e}")
        info["ok"] = False
    try:
        bob_jar = _resolve_bob(download=False)
        info["bob_jar"] = {"path": str(bob_jar), "bytes": bob_jar.stat().st_size}
    except Exception as e:
        info["bob_jar"] = None
        info["issues"].append(f"bob.jar not resolvable: {e}")
        info["ok"] = False
    ext = os.environ.get("DEFOLD_EXTENDER", "").strip()
    info["extender_env"] = ext or None
    return json.dumps(info, indent=2)


@tool("read")
def defold_ref(query: str, environment: str = "runtime", language: str = "Lua") -> str:
    """Search the Defold API documentation bundled with the running editor.

    ``query`` uses whitespace as AND and ``|`` as OR. ``environment``:
    editor|runtime (comma-separated). ``language``: Lua|C|C++ (comma-separated).

    Fallback: if the editor is unreachable, transparently falls back to the
    headless ``defold_docs`` search (latest release docs, not this build).
    """
    try:
        client = _client()
        result = _safe(client.ref, query, environment, language)
    except DefoldError:
        return _docs_search_and_fetch(
            query, note="Editor unreachable; fell back to headless llms docs (latest release)."
        )
    if isinstance(result, dict) and result.get("success") is False:
        return _docs_search_and_fetch(
            query, note="Editor unreachable; fell back to headless llms docs (latest release)."
        )
    return json.dumps(result, indent=2, default=str)


@tool("read")
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


@tool("read")
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


@tool("read")
def defold_test_run(run_id: str = "", timeout: float = 90.0) -> str:
    """Poll structured TEST events until a final result (DefTest protocol).

    Protocol: DefTest emits one JSON line per console event, prefixed with ``TEST``::

        TEST {"run": "<run-id>", "type": "start"}
        TEST {"run": "<run-id>", "type": "case", "name": "...", "result": "pass" | "failure"}
        TEST {"run": "<run-id>", "type": "finish", "status": "pass" | "failure" | "crash"}

    Only events whose ``run`` field matches the active run id are considered; all
    other console output (engine noise) is ignored. The editor is not launched by
    this tool - start the game first (e.g. via `defold_run` with a test build).

    Polls the editor console every second until: a final event arrives, the editor
    process exits (status "crash"), ``timeout`` expires (status "timeout"), or the
    2000-line log cap is reached.

    Deterministic verification: status is one of pass|failure|crash|timeout;
    passed/failed are counted from case events; every TEST line seen is appended
    to the log file referenced by artifacts.log.
    """
    rid = (run_id or "").strip() or _new_run_id("test")
    outdir = _artifact_dir(rid)
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / "test.log"

    client = _client()
    deadline = time.monotonic() + min(max(timeout, 1.0), 300.0)
    cases: list[dict] = []
    passed = failed = 0
    final_status: Optional[str] = None
    log_lines = 0
    test_re = re.compile(r"^\s*TEST\s+(\{.*\})\s*$")

    while True:
        if not client.is_running():
            final_status = "crash"
            break
        console = _safe(client.console)
        lines = console.get("lines", []) if isinstance(console, dict) else []
        for line in lines:
            m = test_re.match(str(line))
            if not m:
                continue  # engine noise
            log_lines += 1
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(str(line).rstrip() + "\n")
            try:
                ev = json.loads(m.group(1))
            except (ValueError, TypeError):
                continue
            if not isinstance(ev, dict) or ev.get("run") != rid:
                continue
            etype = str(ev.get("type", ""))
            if etype == "case":
                cases.append({"name": ev.get("name"), "result": ev.get("result")})
                res = str(ev.get("result", "")).lower()
                if res == "pass":
                    passed += 1
                elif res in ("failure", "fail"):
                    failed += 1
            elif etype in ("finish", "done", "end", "crash", "failure"):
                final_status = str(ev.get("status") or etype).lower()
                if final_status == "fail":
                    final_status = "failure"
                break
        if final_status is not None:
            break
        if log_lines >= 2000:
            break
        if time.monotonic() >= deadline:
            final_status = "timeout"
            break
        time.sleep(1.0)

    issues: list[str] = []
    if final_status == "crash" and not cases:
        issues.append("Editor process exited before any TEST event was seen.")
    if final_status == "timeout":
        issues.append(f"No final TEST event within {min(max(timeout, 1.0), 300.0):.0f}s.")
    out = json.loads(_result("ok" if final_status in ("pass", "failure") else "error", issues, {"log": str(log_path)}))
    out["test"] = {
        "run": rid,
        "status": final_status,
        "passed": passed,
        "failed": failed,
        "cases": cases[:200],
        "case_count": len(cases),
    }
    return json.dumps(out, indent=2, default=str)


@tool("read")
def defold_screenshot_diff(scene: str, baseline_path: str = "", save_baseline: bool = False, tolerance: int = 0) -> str:
    """Visually compare a runtime screenshot against a stored baseline (Pillow).

    Captures the running game via Automation Bridge v2 and compares it pixel by
    pixel with ``baseline_path`` (default: <project>/.baselines/<scene>.png).

    - No baseline + ``save_baseline=True`` -> stores the capture as the new baseline.
    - No baseline otherwise                -> status "error" with an actionable issue.
    - Size mismatch                        -> current capture resized to baseline size (issue).

    Deterministic verification: a pixel is changed when any RGB channel differs by
    more than ``tolerance``. Reports changed-pixel count, percentage and bounding
    boxes of connected change regions (top 10 by area) under "comparison".

    Artifacts under ai_work/artifacts/<run-id>/: shot.png (current capture),
    diff.png (channel difference + red outlines around reported regions).
    """
    from PIL import Image, ImageChops, ImageDraw

    client = _client()
    png = _safe(client.screenshot)
    if not isinstance(png, (bytes, bytearray)) or not png:
        return _result("error", [f"Screenshot failed: {png!r}"])
    run_id = _new_run_id("shot-diff")
    outdir = _artifact_dir(run_id)
    outdir.mkdir(parents=True, exist_ok=True)
    shot_path = outdir / "shot.png"
    shot_path.write_bytes(bytes(png))

    if baseline_path:
        base = Path(baseline_path).expanduser()
    else:
        try:
            base = Path(_default_project()) / ".baselines" / f"{scene}.png"
        except DefoldError as e:
            return _result("error", [f"No project resolved for default baseline: {e}"])
    issues: list[str] = []

    if not base.exists():
        if save_baseline:
            base.parent.mkdir(parents=True, exist_ok=True)
            base.write_bytes(bytes(png))
            return _result("ok", [f"No baseline; created {base}"], {"shot": str(shot_path), "baseline": str(base)})
        return _result("error", [f"No baseline at {base}. Re-run with save_baseline=True to create it."], {"shot": str(shot_path)})

    cur = Image.open(str(shot_path)).convert("RGB")
    ref = Image.open(str(base)).convert("RGB")
    if cur.size != ref.size:
        issues.append(f"Size mismatch {cur.size} vs baseline {ref.size}; resized current.")
        cur = cur.resize(ref.size)

    diff = ImageChops.difference(cur, ref)
    masks = [ch.point(lambda v: 255 if v > tolerance else 0) for ch in diff.split()]
    mask = ImageChops.lighter(ImageChops.lighter(masks[0], masks[1]), masks[2])

    width, height = cur.size
    data = bytearray(mask.tobytes())
    total = width * height
    changed = sum(1 for v in data if v)

    visited = bytearray(total)
    regions: list[tuple[int, int, int, int, int]] = []
    for start in range(total):
        if not data[start] or visited[start]:
            continue
        sx, sy = start % width, start // width
        x0 = x1 = sx
        y0 = y1 = sy
        count = 0
        stack = [start]
        visited[start] = 1
        while stack:
            i = stack.pop()
            x, y = i % width, i // width
            count += 1
            if x < x0: x0 = x
            if x > x1: x1 = x
            if y < y0: y0 = y
            if y > y1: y1 = y
            for j in (i - 1 if x > 0 else -1, i + 1 if x < width - 1 else -1,
                      i - width if y > 0 else -1, i + width if y < height - 1 else -1):
                if j >= 0 and data[j] and not visited[j]:
                    visited[j] = 1
                    stack.append(j)
        regions.append((x0, y0, x1, y1, count))

    diff_path = outdir / "diff.png"
    top = sorted(regions, key=lambda r: r[4], reverse=True)[:10]
    draw = ImageDraw.Draw(diff)
    for (rx0, ry0, rx1, ry1, _n) in top:
        draw.rectangle([rx0, ry0, rx1, ry1], outline=(255, 0, 0), width=2)
    diff.save(str(diff_path))

    bboxes = [{"x": r[0], "y": r[1], "w": r[2] - r[0] + 1, "h": r[3] - r[1] + 1} for r in top]
    comparison = {
        "scene": scene,
        "size": [width, height],
        "total_pixels": total,
        "changed_pixels": changed,
        "percent_changed": round(changed / total * 100, 4) if total else 0.0,
        "tolerance": tolerance,
        "regions": bboxes,
        "region_count": len(regions),
    }
    out = json.loads(_result("ok", issues, {"shot": str(shot_path), "diff": str(diff_path), "baseline": str(base)}))
    out["comparison"] = comparison
    return json.dumps(out, indent=2, default=str)


@tool("modify")
def defold_inject_input(x: int, y: int, button: str = "left") -> str:
    """Inject a mouse click into the running game.

    Uses the Automation Bridge v2 extension (POST /automation-bridge/v2/input/click).
    Requires the game to be running and the ``automation-bridge`` extension.
    """
    client = _client()
    result = _safe(client.input_click, x, y, button)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_stop() -> str:
    """Best-effort stop of the editor/game process launched by this MCP session.

    Returns structured JSON ``{status, issues[], artifacts{}}``. Does nothing if
    the editor was not launched by this server session (reported as an issue).

    Deterministic verification: after killing the process tree, re-checks that
    the editor is no longer reachable; status is "ok" only when it has gone down.
    """
    client = _client()
    result = _safe(client.stop)
    if not isinstance(result, dict):
        return _result("error", [str(result)])
    issues: list[str] = []
    stopped = bool(result.get("stopped"))
    if not stopped:
        issues.append(str(result.get("detail") or "Editor was not launched by this MCP session; stop is a no-op."))
    else:
        try:
            if client.is_running():
                issues.append("Editor still reports as reachable after stop (port may take a moment to release).")
        except DefoldError:
            pass  # unreachable => stopped, verification passed
    return _result("ok" if stopped and not issues else "error", issues)


# ------------------------------------------------------------------ #
# Editor commands (extra)
# ------------------------------------------------------------------ #


@tool("verify")
def defold_clean_build() -> str:
    """Clean build: delete build artifacts and recompile the project.

    Returns structured JSON ``{status, issues[], artifacts{}}`` where issues
    carry the editor's compile errors/warnings (file/line detail) verbatim.

    Deterministic verification: status is "ok" only when the editor answers
    HTTP 200 with ``success: true`` and no issues; a non-empty issue list
    downgrades the status to "error".
    """
    client = _client()
    result = _safe(client.clean_build)
    if not isinstance(result, dict):
        return _result("error", [str(result)])
    issues: list[str] = list(result.get("issues") or [])
    ok = bool(result.get("success")) and result.get("_http_status") == 200
    if not ok:
        issues.append(f"clean build reported success={result.get('success')} http={result.get('_http_status')}")
    return _result("ok" if ok and not issues else "error", issues)


@tool("modify")
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


@tool("read")
def defold_bridge_health() -> str:
    """Query the Automation Bridge v2 health endpoint.

    Returns the bridge version, capabilities, screen size, and input
    devices. Requires the game to be running and the official
    ``automation-bridge`` debug extension.
    """
    client = _client()
    result = _safe(client.bridge_health)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_lifecycle() -> str:
    """Get the ordered runtime lifecycle stages of the running game."""
    client = _client()
    result = _safe(client.bridge_lifecycle)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_screen() -> str:
    """Get the game's screen layout: named rectangles, display scale, and the
    coordinate convention used by the bridge.
    """
    client = _client()
    result = _safe(client.bridge_screen)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_bridge_resize_screen(width: int, height: int) -> str:
    """Resize the game's native window to ``width`` x ``height``."""
    client = _client()
    result = _safe(client.bridge_resize_screen, width, height)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_convert_coordinates(x: float, y: float, from_space: str, to_space: str) -> str:
    """Convert a point between coordinate spaces.

    See ``defold_bridge_screen`` for the available spaces and the
    coordinate convention.
    """
    client = _client()
    result = _safe(client.bridge_convert_coordinates, x, y, from_space, to_space)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_frame() -> str:
    """Get the current engine frame number and scene sequence."""
    client = _client()
    result = _safe(client.bridge_frame)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — scene inspection
# ------------------------------------------------------------------ #


@tool("read")
def defold_bridge_scene(visible: Optional[bool] = None, include: Optional[str] = None) -> str:
    """Inspect the full runtime scene tree of the running game.

    ``visible`` filters to visible elements only. ``include`` is a
    comma-separated list of extra fields (e.g. ``bounds,properties``).
    """
    client = _client()
    result = _safe(client.bridge_scene, visible=visible, include=include)
    return json.dumps(result, indent=2, default=str)


@tool("read")
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


@tool("read")
def defold_bridge_element(element_id: str, include: Optional[str] = None) -> str:
    """Get one runtime element in detail (bounds + properties by default)."""
    client = _client()
    result = _safe(client.bridge_element, element_id, include=include)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — input
# ------------------------------------------------------------------ #


@tool("modify")
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


@tool("modify")
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


@tool("modify")
def defold_bridge_drag_path(
    points: str,
    durations: Optional[str] = None,
    easing: Optional[str] = None,
    button: str = "left",
    visualize: Optional[bool] = None,
) -> str:
    """Drag along a multi-point path.

    ``points`` is ``x,y`` pairs separated by semicolons, e.g.
    ``"100,100;180,60;260,140"``. ``durations`` (per-segment seconds) and
    ``easing`` are comma-separated: N-1 values for N points.
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


@tool("modify")
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


@tool("modify")
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


@tool("read")
def defold_bridge_input_status(input_id: int) -> str:
    """Get the current (or bounded-history) status of one injected input."""
    client = _client()
    result = _safe(client.bridge_input_status, input_id)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_input_pending() -> str:
    """List the FIFO queue of pending injected inputs."""
    client = _client()
    result = _safe(client.bridge_input_pending)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_bridge_input_cancel(input_id: int, release: Optional[bool] = None) -> str:
    """Cancel one queued/active injected input by id."""
    client = _client()
    result = _safe(client.bridge_input_cancel, input_id, release=release)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_bridge_input_flush(release: Optional[bool] = None) -> str:
    """Cancel the session's active and later queued inputs."""
    client = _client()
    result = _safe(client.bridge_input_flush, release=release)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
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


@tool("modify")
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


@tool("modify")
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


@tool("modify")
def defold_bridge_pointer_hold(input_id: int, duration: float, pointer_lease: float = 2.0) -> str:
    """Hold an open pointer for ``duration`` seconds."""
    client = _client()
    result = _safe(client.bridge_pointer_hold, input_id, duration, pointer_lease=pointer_lease)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_bridge_pointer_up(input_id: int, pointer_lease: float = 2.0) -> str:
    """Release an open pointer."""
    client = _client()
    result = _safe(client.bridge_pointer_up, input_id, pointer_lease=pointer_lease)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — native video recording
# ------------------------------------------------------------------ #


@tool("read")
def defold_bridge_recording_capabilities() -> str:
    """Check native video recording availability on this platform."""
    client = _client()
    result = _safe(client.bridge_recording_capabilities)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_recording_status() -> str:
    """Get the active or most recently finalized recording."""
    client = _client()
    result = _safe(client.bridge_recording_status)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
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


@tool("modify")
def defold_bridge_recording_stop() -> str:
    """Stop the active recording and finalize the MP4 file."""
    client = _client()
    result = _safe(client.bridge_recording_stop)
    return json.dumps(result, indent=2, default=str)


# ------------------------------------------------------------------ #
# Automation Bridge v2 — events, states, commands, markers
# ------------------------------------------------------------------ #


@tool("read")
def defold_bridge_events_cursor() -> str:
    """Get the next and oldest retained event cursors."""
    client = _client()
    result = _safe(client.bridge_events_cursor)
    return json.dumps(result, indent=2, default=str)


@tool("read")
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


@tool("read")
def defold_bridge_state(name: Optional[str] = None) -> str:
    """Get all published game states, or one by exact ``name``."""
    client = _client()
    result = _safe(client.bridge_state, name=name)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_state_wait(after_revision: int, timeout_ms: int = 10000) -> str:
    """Long-poll until a newer state revision appears (or the timeout elapses)."""
    client = _client()
    result = _safe(client.bridge_state_wait, after_revision, timeout_ms=timeout_ms)
    return json.dumps(result, indent=2, default=str)


@tool("read")
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


@tool("privileged")
def defold_bridge_command(name: str, data: Optional[Any] = None, timeout_ms: Optional[int] = None) -> str:
    """Submit a named command to the running game.

    ``data`` is the command payload (a dict, which is JSON-encoded, or a JSON
    string). Returns a command id to poll with ``defold_bridge_command_status``.
    """
    client = _client()
    result = _safe(client.bridge_command, name, data=data, timeout_ms=timeout_ms)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_command_status(command_id: int) -> str:
    """Poll a submitted command by id."""
    client = _client()
    result = _safe(client.bridge_command_status, command_id)
    return json.dumps(result, indent=2, default=str)


@tool("privileged")
def defold_bridge_command_cancel(command_id: int) -> str:
    """Cancel a pending command by id."""
    client = _client()
    result = _safe(client.bridge_command_cancel, command_id)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
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


@tool("modify")
def defold_bridge_metal(path: str, frames: Optional[int] = None) -> str:
    """Schedule a Metal GPU trace capture to ``path`` (macOS only).

    ``frames`` caps the number of captured frames.
    """
    client = _client()
    result = _safe(client.bridge_metal, path, frames=frames)
    return json.dumps(result, indent=2, default=str)


@tool("read")
def defold_bridge_metal_status() -> str:
    """Poll a Metal GPU trace capture's status (macOS only)."""
    client = _client()
    result = _safe(client.bridge_metal_status)
    return json.dumps(result, indent=2, default=str)


@tool("modify")
def defold_bridge_metal_stop() -> str:
    """Request an early stop of a Metal GPU trace capture (macOS only)."""
    client = _client()
    result = _safe(client.bridge_metal_stop)
    return json.dumps(result, indent=2, default=str)


# Phase B: wrap tools/editor_gui.py (pyautogui/pywinauto/mss/Tesseract OCR) as MCP
# tools. The subprocess runs under a system Python that has the GUI packages;
# this venv does not need them. Override with DEFOLD_EDITOR_PYTHON if required.

_EDITOR_GUI_PYTHON: Optional[str] = None


def _editor_gui_python() -> str:
    """Find a Python interpreter with mss+pyautogui (cached)."""
    global _EDITOR_GUI_PYTHON
    if _EDITOR_GUI_PYTHON is not None:
        return _EDITOR_GUI_PYTHON
    env = os.environ.get("DEFOLD_EDITOR_PYTHON")
    candidates = [c for c in ([env, sys.executable, "python", "py"]) if c]
    seen = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        try:
            probe = subprocess.run(
                [cand, "-c", "import mss, pyautogui"],
                capture_output=True, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _EDITOR_GUI_PYTHON = cand
            return cand
    raise DefoldError(
        "No Python interpreter with mss+pyautogui found. Point DEFOLD_EDITOR_PYTHON "
        "at the system python that runs tools/editor_gui.py."
    )


def _run_editor_gui(args: list) -> dict:
    """Run a tools/editor_gui.py subcommand and return its JSON payload."""
    py = _editor_gui_python()
    script = Path(__file__).resolve().parents[1] / "editor_gui.py"
    if not script.exists():  # fallback: canonical Phase A helper
        fb = Path("C:/GAMEDEV/DEFOLD/TrickyChallenge/tools/editor_gui.py")
        if fb.exists():
            script = fb
    try:
        proc = subprocess.run(
            [py, str(script), *args], capture_output=True, text=True, timeout=180
        )
    except subprocess.TimeoutExpired as e:
        raise DefoldError(f"editor_gui {' '.join(args)} timed out") from e
    stdout = proc.stdout.strip()
    if not stdout:
        raise DefoldError(
            f"editor_gui {' '.join(args)} produced no output (rc={proc.returncode}); "
            f"stderr={proc.stderr[-500:]}"
        )
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        raise DefoldError(f"editor_gui {' '.join(args)} returned invalid JSON: {stdout[:300]}") from e


@tool("read")
def defold_editor_screenshot(save_path: str = "") -> list:
    """Capture a screenshot of the Defold editor window (OS level).

    Returns a PNG image plus metadata: window title, size in pixels, capture
    offset and foreground status. Coordinates reported by defold_editor_find are
    relative to this captured window; pass them straight to defold_editor_click.

    Optional ``save_path`` also writes the PNG to that file on disk.
    """
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix="defold_gui_")
    out_png = str(Path(tmp_dir) / "shot.png")
    result = _run_editor_gui(["screenshot", "--out", out_png])
    if not result.get("ok"):
        return [TextContent(type="text", text=json.dumps({"success": False, **result}, ensure_ascii=True))]
    with open(out_png, "rb") as f:
        png = f.read()
    meta = {
        "window_title": result.get("window_title"),
        "size": result.get("size"),
        "offset": result.get("offset"),
        "foreground": result.get("foreground"),
    }
    if save_path:
        try:
            target = Path(save_path).expanduser().resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(png)
            meta["saved_to"] = str(target)
        except OSError as e:
            meta["save_error"] = str(e)
    blocks = [TextContent(type="text", text=json.dumps(meta, indent=2, ensure_ascii=True))]
    blocks.append(
        ImageContent(
            type="image",
            data=base64.b64encode(png).decode("ascii"),
            mimeType="image/png",
        )
    )
    return blocks


@tool("read")
def defold_editor_find(grep: Optional[str] = None, max_words: int = 50) -> str:
    """OCR the Defold editor window and locate UI text (OS level).

    Two-pass Tesseract OCR (full page + dedicated low-contrast menu bar pass),
    so it also reads the File/Edit/View/Project/Debug/Help menu. ``grep`` filters
    words containing this substring (case-insensitive); omit it to list up to
    ``max_words`` words. Each word carries its bounding box and center in
    window-relative pixels - pass that center to defold_editor_click.

    Always re-run this after every action to validate the result.
    """
    args = ["ocr", "--max", str(max(1, int(max_words)))]
    if grep:
        args += ["--grep", grep]
    try:
        result = _run_editor_gui(args)
    except DefoldError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=True)
    return json.dumps(result, indent=2, default=str, ensure_ascii=True)


@tool("modify")
def defold_editor_click(x: int, y: int, button: str = "left", double: bool = False) -> str:
    """Click at a window-relative pixel coordinate in the Defold editor (OS level).

    ``x``/``y`` are pixels relative to the captured Defold window (the same space
    as defold_editor_find bounding boxes); the offset is applied internally. Do not move
    the mouse manually during this call; re-run defold_editor_find afterwards to validate.
    """
    args = ["click", str(int(x)), str(int(y)), "--button", button]
    if double:
        args.append("--double")
    try:
        result = _run_editor_gui(args)
    except DefoldError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=True)
    return json.dumps(result, indent=2, default=str, ensure_ascii=True)


@tool("modify")
def defold_editor_type(text: str) -> str:
    """Type text into the focused Defold editor control (OS level).

    Uses SendInput scan codes so typing is independent of the keyboard layout.
    Click the target field first with defold_editor_click; re-run defold_editor_find afterwards
    to validate what was typed.
    """
    try:
        result = _run_editor_gui(["type", text])
    except DefoldError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=True)
    return json.dumps(result, indent=2, default=str, ensure_ascii=True)


@tool("modify")
def defold_editor_key(combo: str) -> str:
    """Press a key or chord in the Defold editor (OS level).

    ``combo`` examples: "F5", "Escape", "ctrl+s", "shift+tab". Re-run
    defold_editor_find afterwards to validate the effect.
    """
    try:
        result = _run_editor_gui(["key", combo])
    except DefoldError as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=True)
    return json.dumps(result, indent=2, default=str, ensure_ascii=True)


if __name__ == "__main__":
    mcp.run()
