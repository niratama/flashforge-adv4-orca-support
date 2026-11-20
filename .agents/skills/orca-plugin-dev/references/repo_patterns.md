# Repository Implementation Patterns (`flashforge-adv4-orca-support`)

This repository supports both **Pattern A (local OctoPrint-compatible HTTP bridge)** and **Pattern B (native `Pages` tab + `PrinterAgent` integration)** inside OrcaSlicer 2.5.0+'s embedded Python environment. `server.enabled` (default `true`) toggles the local HTTP server without affecting `BridgeController`, `Pages`, or `PrinterAgent`.

## 1. Directory & Bundle Layout

Source files:
- `orca_plugin/orca-plugin.json`: Manifest (`manifestVersion: 1`, `entry: "plugin.py"`, `engines.orca: ">=2.5.0"`).
- `orca_plugin/plugin.py`: OrcaSlicer entrypoint with PEP 723 metadata, `resolve_plugin_data_dir()`, Config UI (`build_config_ui_html()`), `PagesBridgeMixin` / `FlashforgePagesCapability`, `FlashforgePrinterAgent`, and `FlashforgeBridgeCapability`.
- `orca_plugin/server.py`: Zero-dependency `_DaemonThreadingHTTPServer` (`daemon_threads = True`, `block_on_close = False`), `CameraRelayServer` (ephemeral `127.0.0.1:0` MJPEG relay when `server.enabled=false`), and stdlib `email.parser.BytesParser` multipart handler.
- `orca_plugin/printer_agent.py`: Pure-Python `PrinterAgentMixin`, stream-bounded `.3mf` plate G-code extraction (`extract_gcode_from_file`), `build_push_status_payload`, Device tab command handlers, and `start_local_print` / `start_send_gcode_to_sdcard`.
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

## 2. `sys.path` & `plugin_data` Resolution in `plugin.py`

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

- `orca_plugin/server.py` resolves `PLUGIN_ROOT = PLUGIN_DIR.parent if PLUGIN_DIR.name == "lib" else PLUGIN_DIR` and searches `(PLUGIN_ROOT, REPO_ROOT)` for `config.json` and `templates/index.html`.
- `resolve_plugin_data_dir()` maps installed plugin folders (`orca_plugins/<folder>` or `orca_plugins/_subscribed/<folder>`) to `orca_plugins/plugin_data/flashforge-adv4-bridge/` for relative `camera.snapshot_dir` paths (falling back to `PLUGIN_DIR` in dev/test). The Config UI displays the live resolved path hint.

## 3. Shared Runtime Lifecycle, Camera Relay & Shutdown

- `_bridge_lock` (`threading.Lock`) guards `_controller_instance` (`BridgeController`), `_bridge_instance` (`BridgeServer`), and `_camera_server_instance` (`CameraRelayServer`).
- `_ensure_controller()` starts/reuses `BridgeController` independently of `BridgeServer`.
- When `server.enabled` is `false`, `CameraRelayServer` binds an ephemeral loopback port (`127.0.0.1:0`) exposing only `/api/camera/stream`, so both `Pages` and `PrinterAgent.get_camera_url()` share the single MJPEG relay connection to the printer without running the full OctoPrint bridge.
- `_resolve_model_id_for_agent()` reads `printer_model` via `orca.host.preset_bundle()` and falls back to `DEFAULT_MODEL_ID = "Flashforge Adventurer 4 Series"`.
- `atexit.register(_stop_bridge)` and `on_unload()` stop `BridgeController` (ending `CameraRelay` / `BackgroundMonitor` and sending `~M602`) before shutting down `_DaemonThreadingHTTPServer` / `CameraRelayServer`, preventing shutdown hangs when `/api/camera/stream` is open.
- `OSError` on port bind is caught and logged/returned as `orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, info)`.

## 4. Diagnostics, Security & UI Conventions

- **PrinterAgent diagnostic trace**: `_trace()` writes to `snapshot_dir/printer_agent_debug.log` (5 MiB rotation, guarded by `_trace_lock`) **only** when `server.debug` is `true`.
- **Config UI sanitization**: `build_config_ui_html()` strips `server.api_key` from the embedded default config and escapes `<`, `>`, `&` via `_safe_json_for_inline_script()`.
- **Pages CSS scoping**: `templates/index.html` scopes dark-theme rules under `body.ff-bridge-body ... !important` with `color-scheme: dark` so OrcaSlicer's Pages webview theme does not override buttons/inputs.
- **Stream-bounded 3MF extraction**: `extract_gcode_from_file()` streams ZIP entries in 64 KiB chunks and enforces `max_bytes` (256 MiB default) on actual decompressed bytes.
- **Permissions fallback**: `register_capabilities()` requests `fs_create` / `fs_delete` alongside `fs_read`, `fs_readwrite` (`snapshot_dir` + `snapshot_dir/*`), `network_http`, `network_socket` (including `('127.0.0.1', 0)`), and `threading`, falling back on `TypeError` for older OrcaSlicer builds.

## 5. Testability Outside OrcaSlicer

`orca_plugin/plugin.py` guards `import orca` and puts core logic into pure-Python mixins (`PagesBridgeMixin`, `PrinterAgentMixin`):
```python
try:
    import orca
except ImportError:
    orca = None
```
All server, pages, printer-agent, controller, protocol, and bundle-structure tests run in standard Python via `unittest`:
- `tests/test_plugin_server.py`: `BridgeServer`, multipart upload, Config UI, shutdown non-blocking with open MJPEG stream, and `build_plugin()` output (asserting `[p.name for p in out.glob("*.py")] == ["plugin.py"]`).
- `tests/test_plugin_pages.py`: `PagesBridgeMixin`, `resolve_plugin_data_dir()`, `server.enabled = false` + `CameraRelayServer` mode, and mocked `orca.pages` capability registration.
- `tests/test_printer_agent.py`: stream-bounded `.3mf` G-code extraction, `push_status` JSON (`sdcard`, `home_flag` bits 0-2 & 8-9, `lights_report`, `spd_lvl`/`spd_mag`, fan), `system.ledctrl` / `print_speed` / `home` / `back_to_center` / `gcode_line`, `start_local_print` progress/cancel, and mocked `orca.printer_agent` registration.

## 6. Verification Commands

```bash
# Build plugin bundle
.venv/bin/python scripts/build_orca_plugin.py

# Run plugin-specific tests
.venv/bin/python -m unittest tests/test_plugin_server.py tests/test_plugin_pages.py tests/test_printer_agent.py

# Run full test suite
.venv/bin/python -m unittest discover -s tests -v
```
