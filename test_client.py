"""
Tests for ``defold_client`` against the mock editor/engine service.

Run::

    python tools/defold-mcp/test_client.py

No external dependencies beyond the standard library.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from defold_client import DefoldClient, DefoldError  # noqa: E402
from mock_editor import MockDefold  # noqa: E402


def _check(label: str, cond: bool):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    return cond


def main() -> int:
    mock = MockDefold()
    failures = 0
    try:
        client = DefoldClient(mock.project_path)

        # Discovery
        port = client.read_port()
        failures += 0 if _check("read_port matches mock", port == mock.editor_port) else 1
        failures += 0 if _check("base_url", client.base_url() == f"http://127.0.0.1:{mock.editor_port}") else 1
        failures += 0 if _check("is_running", client.is_running()) else 1

        # openapi
        oa = client.openapi()
        failures += 0 if _check("openapi version", oa.get("info", {}).get("version") == "1.13.2-mock") else 1
        failures += 0 if _check("openapi has /command/compile", "/command/compile" in oa.get("paths", {})) else 1

        # commands
        compile_res = client.compile()
        failures += 0 if _check("compile success", compile_res.get("success") is True) else 1
        run_res = client.run()
        failures += 0 if _check("run success", run_res.get("success") is True) else 1
        hot_reload_res = client.hot_reload()
        failures += 0 if _check("hot-reload success", hot_reload_res.get("success") is True) else 1

        # console + engine discovery
        console = client.console()
        failures += 0 if _check("console has lines", len(console.get("lines", [])) > 0) else 1
        engine_url = client.engine_service_url()
        failures += 0 if _check("engine_service_url discovered", engine_url == f"http://127.0.0.1:{mock.engine_port}") else 1

        # engine endpoints
        ping = client.engine_ping()
        failures += 0 if _check("engine ping", ping.get("status") == "ok") else 1
        info = client.engine_info()
        failures += 0 if _check("engine info version", info.get("version") == "1.13.2-mock") else 1
        state = client.engine_state()
        failures += 0 if _check("engine state", state.get("state") == "running") else 1

        # preview (PNG)
        png = client.preview("main/main.collection", 64, 64)
        failures += 0 if _check("preview returns PNG bytes", png[:8] == b"\x89PNG\r\n\x1a\n") else 1

        # ref
        ref = client.ref("go")
        failures += 0 if _check("ref returns results", isinstance(ref.get("results"), list)) else 1

        # automation bridge
        shot = client.screenshot()
        failures += 0 if _check("screenshot returns PNG", shot[:8] == b"\x89PNG\r\n\x1a\n") else 1
        click = client.input_click(10, 20, "left")
        failures += 0 if _check("input click", click.get("clicked") is True and click.get("x") == 10) else 1

        # stop (no process launched -> graceful)
        stop = client.stop()
        failures += 0 if _check("stop graceful (no proc)", stop.get("stopped") is False) else 1

        # error path: no engine service when console has no port line
        client2 = DefoldClient(mock.project_path, port=mock.editor_port)
        # point at a dead port to force a connection error
        bad = DefoldClient(mock.project_path, port=1)
        try:
            bad.connect()
            _check("connect to dead port raises", False)
            failures += 1
        except DefoldError:
            _check("connect to dead port raises", True)

    finally:
        mock.stop()

    # --- Pure logic checks (no editor required) ---
    import server as _srv
    lock_en = "copy failed: dmengine.exe is being used by another process."
    lock_fr = ("java.io.FileNotFoundException: dmengine.exe "
               "(Le processus ne peut pas acc\u00e9der au fichier car il est utilis\u00e9 par un autre processus).")
    no_ctx = "some.dll is being used by another process."
    unrelated = "Bob failed with exit code 1"
    failures += 0 if _check("lock detect: English + dmengine ctx", _srv._is_engine_copy_lock(lock_en)) else 1
    failures += 0 if _check("lock detect: French + dmengine ctx", _srv._is_engine_copy_lock(lock_fr)) else 1
    failures += 0 if _check("lock detect: no dmengine context -> False", not _srv._is_engine_copy_lock(no_ctx)) else 1
    failures += 0 if _check("lock detect: unrelated error -> False", not _srv._is_engine_copy_lock(unrelated)) else 1

    print()
    if failures:
        print(f"{failures} check(s) FAILED")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
