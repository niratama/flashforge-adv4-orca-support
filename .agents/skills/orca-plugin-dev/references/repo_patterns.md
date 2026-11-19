# Repository Implementation Patterns (`flashforge-adv4-orca-support`)

This repository supports both **Pattern A (local OctoPrint-compatible HTTP bridge)** and **Pattern B (native `Pages` tab + `PrinterAgent` integration)** inside OrcaSlicer 2.5.0+'s embedded Python environment. `server.enabled` (default `true`) toggles the local HTTP server without affecting `BridgeController`, `Pages`, or `PrinterAgent`.

## 1. Directory & Bundle Layout

Source files:
- `orca_plugin/orca-plugin.json`: Manifest (`manifestVersion: 1`, `entry: "plugin.py"`, `engines.orca: ">=2.5.0"`).
- `orca_plugin/plugin.py`: OrcaSlicer entrypoint with PEP 723 metadata, Config UI (`get_config_ui()`), `PagesBridgeMixin` / `FlashforgePagesCapability`, `FlashforgePrinterAgent`, and `FlashforgeBridgeCapability`.
- `orca_plugin/server.py`: Zero-dependency `_DaemonThreadingHTTPServer` (`daemon_threads = True`, `block_on_close = False`) + stdlib `email.parser.BytesParser` multipart handler.
- `orca_plugin/printer_agent.py`: Pure-Python `PrinterAgentMixin`, `.3mf` plate G-code extraction (`zipfile`), `push_status` JSON builder, and `start_local_print` / `start_send_gcode_to_sdcard`.
- `bridge_controller.py`, `bridge_services.py`, `flashforge_client.py`: Shared, HTTP-framework-agnostic core logic reused by `app.py` (Flask), `orca_plugin/server.py` (stdlib), `Pages`, and `PrinterAgent`.
- `scripts/build_orca_plugin.py`: Assembles `dist/flashforge_adv4_bridge/`.

Built bundle (`dist/flashforge_adv4_bridge/`):
```text
dist/flashforge_adv4_bridge/
├── plugin.py               # ONLY .py file at top level (verified by unit tests)
├── orca-plugin.json
├── config-example.json
├── config.json             # Copied if present in repo root
├── templates/
│   └── index.html
└── lib/
    ├── server.py
    ├── printer_agent.py
    ├── bridge_controller.py
    ├── bridge_services.py
    └── flashforge_client.py
```

## 2. `sys.path` Resolution in `plugin.py`

`plugin.py` supports running both from the built bundle (`lib/` present) and directly from the repository (`lib/` absent):

```python
PLUGIN_DIR = Path(__file__).resolve().parent
LIB_DIR = PLUGIN_DIR / "lib"
if LIB_DIR.is_dir():
    if str(LIB_DIR) not in sys.path:
        sys.path.insert(0, str(LIB_DIR))
else:
    for candidate in (PLUGIN_DIR, PLUGIN_DIR.parent):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
```

Similarly, `orca_plugin/server.py` resolves `PLUGIN_ROOT = PLUGIN_DIR.parent if PLUGIN_DIR.name == "lib" else PLUGIN_DIR` and searches `(PLUGIN_ROOT, REPO_ROOT)` for `config.json` and `templates/index.html`.

## 3. Shared Runtime Lifecycle & Shutdown

- `_bridge_lock` (`threading.Lock`) guards both `_controller_instance` (`BridgeController`) and `_bridge_instance` (`BridgeServer`).
- `_ensure_controller()` starts/reuses `BridgeController` independently of `BridgeServer`, so `Pages` and `PrinterAgent` continue working when `server.enabled` is `false`.
- `atexit.register(_stop_bridge)` and `on_unload()` stop `BridgeController` (ending `CameraRelay` / `BackgroundMonitor` and sending `~M602`) before shutting down `_DaemonThreadingHTTPServer`, preventing shutdown hangs when `/api/camera/stream` is open.
- `OSError` on port bind is caught and logged/returned as `orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, info)`.

## 4. Testability Outside OrcaSlicer

`orca_plugin/plugin.py` guards `import orca` and puts core logic into pure-Python mixins (`PagesBridgeMixin`, `PrinterAgentMixin`):
```python
try:
    import orca
except ImportError:
    orca = None
```
All server, pages, printer-agent, controller, protocol, and bundle-structure tests run in standard Python via `unittest`:
- `tests/test_plugin_server.py`: `BridgeServer`, multipart upload, Config UI, shutdown non-blocking with open MJPEG stream, and `build_plugin()` output (asserting `[p.name for p in out.glob("*.py")] == ["plugin.py"]`).
- `tests/test_plugin_pages.py`: `PagesBridgeMixin`, `server.enabled = false` mode, and mocked `orca.pages` capability registration.
- `tests/test_printer_agent.py`: `.3mf` G-code extraction, `push_status` JSON (`sdcard` + `home_flag`), commands/`gcode_line`, `start_local_print` progress/cancel, and mocked `orca.printer_agent` registration.

## 5. Verification Commands

```bash
# Build plugin bundle
.venv/bin/python scripts/build_orca_plugin.py

# Run plugin-specific tests
.venv/bin/python -m unittest tests/test_plugin_server.py tests/test_plugin_pages.py tests/test_printer_agent.py

# Run full test suite
.venv/bin/python -m unittest discover -s tests -v
```
