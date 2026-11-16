# OrcaSlicer Plugin System & API Reference

Reference summary based on the OrcaSlicer Plugin Development documentation
(`OrcaSlicer/OrcaSlicer/wiki/plugin_development` and `orcaslicer.com` developer reference).

## 1. Architecture Overview

- **Embedded CPython**: OrcaSlicer 2.5.0+ embeds a CPython interpreter and exposes a narrow `orca` module.
- **Capabilities-Based Design**: A plugin package does not act as a single monolithic patch. Instead, it defines a `@orca.plugin` class subclassing `orca.base` and registers one or more capabilities via `orca.register_capability(...)` inside `register_capabilities(self)`.
- **Distribution Formats**:
  - Single `.py` script with PEP 723-style inline metadata (`# /// script` / `# [tool.orcaslicer.plugin]`)
  - Plugin folder with `orca-plugin.json` + entry `.py`
  - `.whl` Python wheel package
- **Local Plugin Directory**: `<data_dir>/orca_plugins/` (e.g., `%APPDATA%\OrcaSlicer\orca_plugins\` on Windows).

## 2. Entry Class & Metadata

### Inline Metadata (PEP 723 style)

```python
# /// script
# [tool.orcaslicer.plugin]
# name = "My Plugin"
# description = "Short description."
# author = "Author"
# version = "0.1.0"
# ///
```

### Plugin Package Class

```python
import orca

@orca.plugin
class MyPlugin(orca.base):
    def register_capabilities(self) -> None:
        orca.request_permissions(
            network_http=["https://api.example.com"],
            threading=True,
        )
        orca.register_capability(MyCapability)

    def on_load(self) -> None:
        pass

    def on_unload(self) -> None:
        pass
```

## 3. Core Capability Types

| Capability | Module / Base Class | Purpose |
| :--- | :--- | :--- |
| **Script** | `orca.script.ScriptPluginCapabilityBase` | Manual action run from *File > Plugins > Run*; supports `get_name()`, `execute()`, `on_load()`, `on_unload()`. Runs on UI thread. |
| **Pages** | `Pages` | Adds a custom tab to OrcaSlicer's main window (next to Prepare/Preview/Device) while active. |
| **Printer Agent** | `orca.printer_agent.PrinterAgentBase` | Device abstraction for printer discovery, connection, job upload/start, commands, and status streaming. |
| **Slicing Pipeline** | `orca.slicing` (`SlicingPipelineCapabilityBase`) | Hooks into live slicing steps or G-code post-processing (`psGCodePostProcess`). |
| **Resource Texture** | `ResourceTexture` | Contributes resource packs such as bed textures / height-maps. |

## 4. Return Values & Error Handling

Use `orca.ExecutionResult` and `orca.PluginResult` (`Success`, `Skipped`, `RecoverableError`, `FatalError`) instead of raising raw exceptions for expected failures:

```python
return orca.ExecutionResult.success("Completed successfully")
return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, "Reason")
```

## 5. Security: Plugin Audit Hook & `orca.request_permissions`

OrcaSlicer uses a **CPython audit hook permission boundary** rather than an OS container sandbox. Sensitive operations trigger user approval prompts and are persisted per target:

- `fs_read`: List of readable paths
- `fs_readwrite`: List of writable paths (intercepts write/append/create/truncate `open()`, `os.open()`, etc.)
- `network_http`: List of HTTP/HTTPS URLs
- `network_socket`: List of socket target strings (e.g., `"('192.168.1.100', 8899)"`, `"('127.0.0.1', 5000)"`)
- `threading`: `bool` (allows spawning background threads)
- Process spawning (`subprocess`, etc.) is also gated by the audit hook.

Calling `orca.request_permissions(...)` inside `register_capabilities()` presents a combined upfront dialog on load; if the user declines, loading is aborted cleanly.

## 6. Host UI (`orca.host.ui`) & Lifecycle

- **Do NOT import PyQt, wxPython, or Tkinter**: Script plugins share OrcaSlicer's main wxWidgets thread (event loop clash), and background/printer-agent plugins run off-thread (crash risk).
- **Non-modal interactive HTML window**:
  ```python
  win = orca.host.ui.create_window(
      html=PAGE_HTML,
      title="Panel",
      on_message=self.on_message,
      on_close=self.on_close,
  )
  win.post({"type": "update", "data": ...})
  win.is_open()
  win.close()
  ```
- **Lifecycle**: Capabilities and plugins support `on_load()`, `on_unload()`, and `on_lifecycle_event(event, ctx)` for observing application events (project save/load, slicing).
