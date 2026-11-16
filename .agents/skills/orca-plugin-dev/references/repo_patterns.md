# Repository Implementation Patterns (`flashforge-adv4-orca-support`)

This repository implements **Pattern A (auto-start local bridge)**: an OctoPrint-compatible local HTTP bridge + Web UI started inside OrcaSlicer's embedded Python environment.

## 1. Directory & Bundle Layout

Source files:
- `orca_plugin/orca-plugin.json`: Manifest (`manifestVersion: 1`, `entry: "plugin.py"`, `engines.orca: ">=2.5.0"`).
- `orca_plugin/plugin.py`: OrcaSlicer entrypoint with PEP 723 metadata + `orca` integration.
- `orca_plugin/server.py`: Zero-dependency `ThreadingHTTPServer` + stdlib `email.parser.BytesParser` multipart handler.
- `bridge_controller.py`, `bridge_services.py`, `flashforge_client.py`: Shared, HTTP-framework-agnostic core logic reused by both `app.py` (Flask) and `orca_plugin/server.py` (stdlib).
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

## 3. Singleton Lifecycle & Shutdown

- `_bridge_lock` (`threading.Lock`) guards `_bridge_instance` so `register_capabilities()`, `on_load()`, and `execute()` are idempotent.
- `atexit.register(_stop_bridge)` ensures clean socket shutdown even if `on_unload()` is not invoked on exit.
- `OSError` on port bind is caught and logged/returned as `orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, info)`.

## 4. Testability Outside OrcaSlicer

`orca_plugin/plugin.py` guards `import orca`:
```python
try:
    import orca
except ImportError:
    orca = None
```
All server, controller, protocol, and bundle-structure tests run in standard Python via `unittest`:
- `tests/test_plugin_server.py` tests `BridgeServer`, multipart upload, JSON endpoints, and `build_plugin()` output (including asserting `[p.name for p in out.glob("*.py")] == ["plugin.py"]`).

## 5. Verification Commands

```bash
# Build plugin bundle
uv run python scripts/build_orca_plugin.py

# Run plugin-specific tests
uv run python -m unittest tests/test_plugin_server.py

# Run full test suite
uv run python -m unittest discover -s tests
```
