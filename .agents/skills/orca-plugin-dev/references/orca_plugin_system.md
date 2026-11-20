# OrcaSlicer Plugin System & API Reference

Reference summary based on the OrcaSlicer Plugin Development documentation
(`OrcaSlicer/OrcaSlicer/wiki/plugin_development`, `orcaslicer.com` developer
reference) and direct verification against OrcaSlicer `main` C++ source.

## 1. Architecture Overview

- **Embedded CPython**: OrcaSlicer 2.5.0+ embeds a CPython interpreter and exposes a narrow `orca` module.
- **Capabilities-Based Design**: A plugin package does not act as a single monolithic patch. Instead, it defines a `@orca.plugin` class subclassing `orca.base` and registers one or more capabilities via `orca.register_capability(...)` inside `register_capabilities(self)`.
- **Distribution Formats**:
  - Single `.py` script with PEP 723-style inline metadata (`# /// script` / `# [tool.orcaslicer.plugin]`)
  - Plugin folder with `orca-plugin.json` + entry `.py`
  - `.whl` Python wheel package
- **Local Plugin & Persistent Data Directories**:
  - Manually installed plugins: `<data_dir>/orca_plugins/<folder>/` (e.g., `%APPDATA%\OrcaSlicer\orca_plugins\` on Windows).
  - Cloud-subscribed plugins: `<data_dir>/orca_plugins/_subscribed/<folder>/`.
  - Persistent plugin data: `<data_dir>/orca_plugins/plugin_data/<plugin_id>/` (survives plugin upgrades/reinstalls).

## 2. Entry Class, Metadata & Config UI

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

### Plugin Package Class & Config UI

```python
import orca

@orca.plugin
class MyPlugin(orca.base):
    def get_default_config(self) -> dict:
        return {"printer": {"ip": "192.168.1.100"}}

    def has_config_ui(self) -> bool:
        return True

    def get_config_ui(self) -> str:
        return "<!DOCTYPE html>..."

    def register_capabilities(self) -> None:
        orca.request_permissions(
            network_http=["https://api.example.com"],
            threading=True,
        )
        orca.register_capability(MyCapability)
```

- `self.get_config()` reads user-saved settings from `<data_dir>/orca_plugins/config.json` (may return a JSON string or a `dict`; normalize defensively).
- OrcaSlicer injects CSS theme custom properties (`--orca-bg`, `--orca-fg`, `--orca-muted`, `--orca-accent`, `--orca-border`, `--orca-font`) into Config UI webviews.

## 3. Core Capability Types

| Capability | Module / Base Class | Purpose |
| :--- | :--- | :--- |
| **Script** | `orca.script.ScriptPluginCapabilityBase` | Manual action run from *File > Plugins > Run*; supports `get_name()`, `execute()`, `on_load()`, `on_unload()`. Runs on UI thread. |
| **Pages** | `orca.pages.PagesPluginCapabilityBase` | Adds a custom tab to OrcaSlicer's main window (next to Prepare/Preview/Device) while active. |
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
- `fs_readwrite`: List of writable paths (include `f"{dir}/*"` for files inside a writable directory; intercepts write/append/create/truncate `open()`, `os.open()`, etc.)
- `fs_create` / `fs_delete`: Supported in newer OrcaSlicer builds for directory/file creation and deletion (wrap with `try ... except TypeError` fallback to older kwargs if needed)
- `network_http`: List of HTTP/HTTPS URLs
- `network_socket`: List of socket target strings (e.g., `"('192.168.1.100', 8899)"`, `"('127.0.0.1', 5000)"`, `"('127.0.0.1', 0)"` for ephemeral loopback binds)
- `threading`: `bool` (allows spawning background threads)
- Process spawning (`subprocess`, etc.) is also gated by the audit hook.

Calling `orca.request_permissions(...)` inside `register_capabilities()` presents a combined upfront dialog on load; if the user declines, loading is aborted cleanly.

## 6. Host Access (`orca.host`, `orca.host.ui`) & Lifecycle

- **Reading active presets**: `orca.host.preset_bundle().current_printer_preset().config_value("printer_model")` returns the currently selected printer model string (useful for matching `PrinterAgent` `model_id` / `dev_type`).
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

## 7. Pages Capability (`orca.pages.PagesPluginCapabilityBase`)

- Subclass `orca.pages.PagesPluginCapabilityBase` and implement `get_name() -> str`, `get_ui() -> str`, and `on_message(self, data)`.
- Frontend communicates via injected `window.orca.postMessage(obj)` and `window.orca.onMessage(cb)`; Python pushes responses via `self.post_message(data)`.
- `get_ui()` is lazily invoked on first tab open and re-initialized on reload; keep authoritative state in Python.
- **Theme override gotcha**: OrcaSlicer injects host theme CSS into `Pages` webviews that can override bare `body`, `button`, `select`, and `input` rules. Scope custom styles under a body class with `!important` (e.g., `body.ff-bridge-body button { ... !important }`) and `color-scheme: dark` when maintaining a custom theme.

## 8. Printer Agent Capability (`orca.printer_agent.PrinterAgentBase`)

Verified against OrcaSlicer `main` (`PrinterAgentPluginCapability.cpp`, `IPrinterAgent.hpp`, `bambu_networking.hpp`, `MoonrakerPrinterAgent.cpp`, and Device tab UI):

- **Activation**: Requires `Preferences -> Developer -> Experimental Features -> Use printer agents instead of print hosts` (`use_printer_agents`, default OFF) and selecting the agent under `Printer settings -> Basic information -> Advanced -> Printer Agent`.
- **Core types**: `AgentInfo(id, name, version, description)`, `PrintParams`, `PrinterConnectionParams`, `DetectResult`, `CameraStreamMode` (`None_`, `HTTP`, `HTTPS`, `HTTP_SNAPSHOT`, `RTSP`), `FilamentSyncMode` (`None_`, `Subscription`, `Pull`).
- **Return codes & printing stages (`bambu_networking.hpp`)**:
  - Codes: `0` (`BAMBU_NETWORK_SUCCESS`), `-1` (`INVALID_HANDLE`), `-14` (`FILE_NOT_EXIST`), `-18` (`CANCELED`), `-19` (`INVALID_RESULT`), `-4020` (`PRINT_LP_UPLOAD_FTP_FAILED`), `-5010` (`PRINT_SG_UPLOAD_FTP_FAILED`), `-6010` (`CONNECTION_TO_PRINTER_FAILED`), `-7020` (`ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE`).
  - Progress stages (`update_fn(stage, percent, message)`): `0` (`CREATE`), `1` (`UPLOAD`), `3` (`SENDING`), `6` (`FINISHED`).
- **Threading & main-thread marshaling**:
  - `start_discovery()`, `connect_printer()`, `disconnect_printer()` (3 s deadline during app shutdown) run on the UI thread.
  - `start_local_print()` / `start_send_gcode_to_sdcard()` run on background job threads.
  - Store `set_queue_on_main_fn(fn)` and marshal `on_local_connect_fn`, `on_printer_connected_fn`, and `on_local_message_fn` through it from background polling threads.
- **Discovery / model matching**:
  - Set `dev_type` in SSDP payload (`start_discovery`) and `detect.model_id` in `bind_detect` to match the active preset's `printer_model` (via `orca.host.preset_bundle()`).
- **`push_status` JSON contract (`on_local_message_fn(dev_id, json_str)`)**:
  - **Storage flag**: Must set **both** `print.sdcard = true` and `print.home_flag` bits 8-9 (`1 << 8`, `DevStorage::HAS_SDCARD_NORMAL`), or OrcaSlicer's send dialog blocks thinking no storage is present.
  - **Homed flag**: Set `print.home_flag` bits 0-2 (`0x07`, X/Y/Z homed), or Device tab axis controls stay disabled as unhomed.
  - **Device tab controls**:
    - Lamp state: `lights_report: [{"node": "chamber_light", "mode": "on"|"off"}]`
    - Speed meter: `spd_lvl` (`1`=50% Silent, `2`=100% Standard, `3`=124% Sport, `4`=166% Ludicrous) and `spd_mag` (`int` %)
    - Fan indicator: `cooling_fan_speed` (`"0"`..`"15"`) and `fan_gear` (`0`..`255`)
    - State & metadata: `gcode_state` (`IDLE` / `RUNNING` / `PAUSE` / `FINISH`), `mc_print_stage` (`0..3`), `mc_percent`, `subtask_name`, `gcode_file`, `nozzle_temp_range`, `bed_temp_range`, `support_send_to_sd`, `support_bed_leveling`.
- **Incoming Device tab commands**:
  - Direct methods: `command_set_nozzle`, `command_set_bed`, `command_go_home`, `command_axis_control` (move delta = `input_val * unit`, guard against printing state and return `-7020` when unavailable).
  - JSON via `send_message_to_printer(dev_id, json_str, qos, flag)`:
    - `info.command == "get_version"` → reply with `info.module` + `result: "success"`.
    - `system.command == "ledctrl"` (`led_mode: "on"|"off"`, `led_node`) → toggle lamp & push `lights_report`.
    - `print.command`: `pause`, `resume`, `stop`, `set_bed_temp` (`temp`), `set_nozzle_temp` (`target_temp`), `print_speed` (`param`: `"1".."4"`), `home` / `back_to_center`, `gcode_line` (`param`: `M104`, `M140`, `M663`, `M106`/`M107`, `G28` — reply with `gcode_line` result + `sequence_id`).
- **3MF bundles**:
  - `params.dst_file` / `params.filename` may point to a `.3mf` ZIP archive containing `Metadata/plate_<plate_index>.gcode`.
  - Stream-read entries (`zf.open(info)` in chunks) with an explicit byte limit instead of trusting ZIP headers or calling unbounded `zf.read()`.
