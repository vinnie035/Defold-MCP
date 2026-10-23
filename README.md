# Defold MCP Server

An **MCP** (Model Context Protocol) server for the **Defold** engine. It lets an
AI agent drive the Defold editor and the running game: compile, run, read logs,
capture screenshots, search the API docs, and control the game via the
**Automation Bridge v2** (scene inspection, input, video recording, events,
states, commands, markers).

> Defold is designed for HTTP automation: the editor exposes a local server
> (port stored in `.internal/editor.port`) and a debug build exposes an "engine
> service". This MCP is a thin HTTP client over those two surfaces.

## Generic, project-agnostic server

This server lives in a **single shared location** (e.g. `C:\GAMEDEV\DEFOLD\defold-mcp`),
**not** inside each project. It is configured **once** in your MCP client and
works for **any** Defold project. The active project is resolved
**automatically** at runtime — no per-project setup, no `.bat`, no hardcoded
path, no required env var.

## Project resolution

The project to control is resolved on **every tool call**, so switching projects
mid-session works **without restarting the server**. Priority:

1. **`DEFOLD_PROJECT`** env var — explicit override (optional).
2. **The MCP client's working directory** — if it (or an ancestor) contains a
   `game.project` file. *(Most clients, including Bionic, do not pass their
   working directory to the MCP process, so this usually resolves to nothing.)*
3. **The project open in the Defold editor** — auto-detected by scanning for a
   live editor server (`.internal/editor.port` present + `/openapi.json`
   reachable). If several editors are open, the most recently touched port
   file wins.
4. **`active_project.txt`** — a pointer file next to `server.py` (see below).
5. **The nearest ancestor** of `server.py` containing a `game.project` file.
6. **Fallback**: the parent of the server's directory.

In practice, **rule 3** (editor open) is the main automatic signal: whatever
project you have open in the Defold editor is the one the MCP controls. When no
editor is open, **rule 4** (`active_project.txt`) is the fallback.

### `active_project.txt`

A plain-text file (gitignored) containing the absolute path to a Defold
project, e.g.:

```
C:\GAMEDEV\DEFOLD\MyGameProject
```

It is used **only** when no editor is open. Update it when you switch projects
without opening the editor. It is **not committed** because it holds a
machine-specific absolute path.

## Files

| File | Role |
|---|---|
| `defold_client.py` | HTTP client (editor + engine service). No MCP dependency, testable standalone. |
| `server.py` | MCP server (FastMCP) exposing the tools. |
| `mock_editor.py` | Simulated editor + engine service for testing without Defold. |
| `test_client.py` | Client tests against the mock. |
| `requirements.txt` | Dependencies. |
| `active_project.txt` | *(gitignored)* Local pointer to the active project (fallback when no editor is open). |
| `mcp-config.example.json` | *(example)* Ready-to-use MCP client config template (120 s request timeout). |

## Exposed tools

> **⚠️** marks tools that require the game to be **running** and the
> `automation-bridge` debug extension **installed in the project**. All
> `defold_bridge_*` tools, plus `defold_screenshot` and `defold_inject_input`,
> fall in this category. Everything else works with just the editor.

| Tool | Description |
|---|---|
| `defold_status` | Editor installed? project open? port? version? available commands? |
| `defold_open_project` | Connect to the editor (or launch it if `launch=true`). |
| `defold_compile` | Compile without running. Returns `success` + `issues`. |
| `defold_run` | Compile + run (debug build). |
| `defold_console` | Read the logs (latest lines + semantic regions). |
| `defold_preview` | Render a resource (collection, GUI, atlas…) as a **PNG**. |
| `defold_ref` | Search the API docs embedded in the editor. |
| `defold_engine_info` | `/ping`, `/info`, `/state` of the running game. |
| `defold_screenshot` ⚠️ | **Runtime** capture via the *Automation Bridge v2* extension (optional `after_frames`). |
| `defold_inject_input` ⚠️ | Inject a mouse click via the *Automation Bridge v2*. |
| `defold_stop` | Stop the editor launched by this server (best-effort). |
| `defold_clean_build` | Clean build: delete artifacts and recompile. |
| `defold_hot_reload` | Hot-reload: reload modified files into the running game. |
| `defold_bridge_health` | Bridge v2 health: version, capabilities, screen, devices. |
| `defold_bridge_lifecycle` | Runtime stages (execution order). |
| `defold_bridge_screen` | Named rectangles, display scale, coordinate convention. |
| `defold_bridge_resize_screen` | Resize the game's native window. |
| `defold_bridge_convert_coordinates` | Convert a point between coordinate spaces. |
| `defold_bridge_frame` | Current engine frame and scene_sequence. |
| `defold_bridge_scene` | Full runtime scene tree of the game. |
| `defold_bridge_elements` | Search runtime elements (filters + pagination). |
| `defold_bridge_element` | Element details (bounds + properties). |
| `defold_bridge_click` | Click by x/y or by element id (button, modifiers, visualization). |
| `defold_bridge_drag` | Drag-and-drop between two points or two elements. |
| `defold_bridge_drag_path` | Multi-point drag path. |
| `defold_bridge_key` | Text input and/or special keys (`{KEY_ENTER}`…). |
| `defold_bridge_wheel` | Mouse wheel (steps, position, element). |
| `defold_bridge_input_status` | Status of an injected input. |
| `defold_bridge_input_pending` | FIFO queue of pending inputs. |
| `defold_bridge_input_cancel` | Cancel a queued/active input. |
| `defold_bridge_input_flush` | Flush the session's input queue. |
| `defold_bridge_input_configure` | Acquire/renew the input controller lease. |
| `defold_bridge_pointer_open` | Open a pointer session under a lease (returns an id). |
| `defold_bridge_pointer_move` | Move an open pointer. |
| `defold_bridge_pointer_hold` | Hold an open pointer. |
| `defold_bridge_pointer_up` | Release an open pointer. |
| `defold_bridge_recording_capabilities` | Availability of native video recording. |
| `defold_bridge_recording_status` | Active recording or last finalized one. |
| `defold_bridge_recording_start` | Start video recording (MP4). |
| `defold_bridge_recording_stop` | Stop and finalize the recording. |
| `defold_bridge_events_cursor` | Event cursors (next/oldest). |
| `defold_bridge_events` | Long-poll game events. |
| `defold_bridge_state` | Published states (all or by exact name). |
| `defold_bridge_state_wait` | Wait for a new state revision. |
| `defold_bridge_application_catalog` | Commands/states/events registered by the game. |
| `defold_bridge_command` | Submit a named command (returns an id). |
| `defold_bridge_command_status` | Status of a submitted command. |
| `defold_bridge_command_cancel` | Cancel a pending command. |
| `defold_bridge_marker` | Insert a timeline marker. |
| `defold_bridge_metal` | Metal GPU trace (macOS only). |
| `defold_bridge_metal_status` | Metal trace status. |
| `defold_bridge_metal_stop` | Stop the Metal trace. |

## Installation

The server lives in a shared folder (a sibling of your projects), e.g.
`C:\GAMEDEV\DEFOLD\defold-mcp`:

```bash
cd C:\GAMEDEV\DEFOLD\defold-mcp
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

Optionally create `active_project.txt` with the path to your current project
(used as a fallback when no editor is open):

```
C:\GAMEDEV\DEFOLD\TrickyChallenge
```

## Configuration

A ready-to-use MCP client config template is provided in
[`mcp-config.example.json`](mcp-config.example.json). It points at the venv's
Python interpreter and `server.py`, needs no env vars, and sets a **120 s**
request timeout (`timeoutMs: 120000`). Import it into your MCP client and
adjust the paths to your install location.

## Typical usage (by an agent)

1. `defold_status` — check that the editor is installed/open.
2. `defold_open_project` — open/launch the project.
3. `defold_compile` — compile and fix the `issues`.
4. `defold_run` — run the game.
5. `defold_console` — read the logs.
6. `defold_screenshot` / `defold_preview` — see the visual state.
7. `defold_inject_input` — interact.
8. `defold_bridge_scene` / `defold_bridge_click` / `defold_bridge_key` — inspect and drive the game via the Automation Bridge v2.

## Important notes

- **Automatic project detection**: the active project follows the Defold editor
  that is open. Close one project and open another, and the next tool call
  targets the new project — no server restart, no config change.
- **Experimental API**: the editor's HTTP API may change between versions.
  `/openapi.json` is the source of truth; the client reads it to discover the
  available commands.
- **Preview ≠ runtime screenshot**: `/preview/` renders a static resource
  (no dynamic objects or post-processing). For a real screenshot of the game,
  you need the engine service + the **Automation Bridge** extension.
- **Automation Bridge v2**: all `defold_bridge_*` tools (as well as
  `defold_screenshot` and `defold_inject_input`) require the game to be
  running and the official debug extension `automation-bridge` to be installed
  in the project. Without it, these tools return a clear error. The
  `defold_bridge_metal*` tools only work on macOS.
- **Editor launch**: discovery goes through
  `%LOCALAPPDATA%\Defold\installations.json` (`launcherPath` field), then
  common locations, then the `PATH`. Launching waits for the creation of
  `.internal/editor.port` and then for the `/openapi.json` response.

## Tests (without Defold installed)

```bash
python test_client.py
```

This starts the mock (editor + engine service) and validates all the HTTP
logic: port discovery, openapi, compile/run, console, engine service
discovery, PNG preview, ref, screenshot, input, and the error paths.

## License

This project is licensed under the **MIT** License. See the [LICENSE](LICENSE)
file for details.
