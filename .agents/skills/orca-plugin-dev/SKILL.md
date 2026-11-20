---
name: orca-plugin-dev
description: >-
  Guide and workflow for developing, modifying, packaging, and testing
  OrcaSlicer 2.5.0+ Python plugins in this repository. Use when editing
  orca_plugin/, scripts/build_orca_plugin.py, orca-plugin.json, or when
  adding new OrcaSlicer plugin capabilities, permissions, or UI integrations.
---

# OrcaSlicer Plugin Development (Project Skill)

This skill covers OrcaSlicer 2.5.0+ Python plugin development using both the
official OrcaSlicer plugin architecture (including C++ core source findings)
and this repository's conventions.

## Core Rules for This Repository

1. **Zero external runtime dependencies in the plugin**:
   OrcaSlicer runs plugins inside an embedded CPython interpreter. Do not
   require `Flask` or third-party packages in `orca_plugin/`. Use Python
   standard library modules (e.g., `http.server.ThreadingHTTPServer`,
   `email.parser.BytesParser`, `urllib.request`, `threading`, `zipfile`).
2. **Keep `plugin.py` testable outside OrcaSlicer**:
   Guard `import orca` with `try ... except ImportError: orca = None`, put
   core logic in pure-Python mixins (`PagesBridgeMixin`, `PrinterAgentMixin`),
   and wrap `orca`-dependent classes in `if orca is not None:`.
3. **Respect the `lib/` bundle layout**:
   Only `plugin.py` (and `orca-plugin.json`) should sit at the top level of
   the built plugin folder (`dist/flashforge_adv4_bridge/`). Place helper
   modules (`server.py`, `printer_agent.py`, `bridge_controller.py`,
   `bridge_services.py`, `flashforge_client.py`) under `lib/` in the bundle
   and configure `sys.path` in `plugin.py` for both `lib/` (built) and repo
   root (dev/test).
4. **Never block the OrcaSlicer UI thread**:
   `ScriptPluginCapabilityBase.execute()`, `PrinterAgentBase.start_discovery()`,
   `connect_printer()`, and `disconnect_printer()` (3 s shutdown deadline) run
   on the main/UI thread. Offload blocking I/O or servers to `threading.Thread`
   (`daemon=True`, and `daemon_threads = True` / `block_on_close = False` on
   `ThreadingHTTPServer`), dispatch callbacks to the UI thread via
   `set_queue_on_main_fn`, and never import external GUI toolkits (PyQt,
   wxPython, Tkinter); use `orca.pages.PagesPluginCapabilityBase` or
   `orca.host.ui` for HTML UI.
5. **Declare permissions upfront & follow `plugin_data` conventions**:
   - Call `orca.request_permissions(...)` inside `register_capabilities()`
     (guarded with `hasattr(orca, "request_permissions")` and `TypeError`
     fallback for `fs_create`/`fs_delete`) for `fs_read`, `fs_readwrite`,
     `network_http`, `network_socket` (including `('127.0.0.1', 0)` for the
     ephemeral `CameraRelayServer`), and `threading`.
   - Store persistent data (snapshots, debug logs) under
     `orca_plugins/plugin_data/<plugin_id>/` via `resolve_plugin_data_dir()`.
6. **UI theming & security hygiene**:
   - Scope `Pages` dark-theme CSS (e.g., `body.ff-bridge-body ... !important`)
     so OrcaSlicer's injected webview styles do not override controls.
   - Strip secrets (`server.api_key`) and escape `<`, `>`, `&` before embedding
     config JSON into inline `<script>` in Config UI HTML.
   - Stream-bound `.3mf` ZIP extraction (`max_bytes`) and gate `PrinterAgent`
     diagnostic file tracing behind `server.debug`.

## Development Workflow

1. **Edit plugin / shared logic**:
   - Entrypoint & capability registration (`Script`, `Pages`, `PrinterAgent`, Config UI): [plugin.py](../../../../orca_plugin/plugin.py)
   - Manifest: [orca-plugin.json](../../../../orca_plugin/orca-plugin.json)
   - Stdlib HTTP server & ephemeral `CameraRelayServer` (`server.enabled=false`): [server.py](../../../../orca_plugin/server.py)
   - Native `PrinterAgentBase` implementation: [printer_agent.py](../../../../orca_plugin/printer_agent.py)
   - Shared controller/services: [bridge_controller.py](../../../../bridge_controller.py), [bridge_services.py](../../../../bridge_services.py)
2. **Update bundle script if files are added/renamed**:
   - Edit [build_orca_plugin.py](../../../../scripts/build_orca_plugin.py) so all new files/assets are copied into `dist/flashforge_adv4_bridge/`.
3. **Build the plugin bundle**:
   ```bash
   .venv/bin/python scripts/build_orca_plugin.py
   ```
4. **Run unit tests**:
   ```bash
   .venv/bin/python -m unittest tests/test_plugin_server.py tests/test_plugin_pages.py tests/test_printer_agent.py
   ```
   (Run the full test suite with `.venv/bin/python -m unittest discover -s tests` when shared modules change.)
5. **Deploy to OrcaSlicer (Windows)**:
   Copy `dist/flashforge_adv4_bridge/` to `%APPDATA%\OrcaSlicer\orca_plugins\flashforge_adv4_bridge\`.

## Reference Documentation

- [OrcaSlicer Plugin System & API Reference](./references/orca_plugin_system.md) — Architecture, `@orca.plugin`, capabilities (`Script`, `Pages`, `PrinterAgent`, `SlicingPipeline`, `ResourceTexture`), Audit Hook & `request_permissions`, `plugin_data`, `orca.host` / `orca.host.ui`, and `PrinterAgentBase` C++ contract details.
- [Repository Implementation Patterns](./references/repo_patterns.md) — Concrete patterns used in `orca_plugin/`, `build_orca_plugin.py`, and `tests/test_plugin_*.py` / `tests/test_printer_agent.py`.
