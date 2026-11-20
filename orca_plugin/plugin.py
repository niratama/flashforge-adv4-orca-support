# /// script
# [tool.orcaslicer.plugin]
# name = "Flashforge Adventurer 4 Bridge"
# description = "Local OctoPrint-compatible bridge and Device tab Web UI for Flashforge Adventurer 4."
# author = "local"
# version = "0.1.0"
# ///

"""OrcaSlicer 2.5.0+ Python plugin entrypoint (Pattern A: auto-start local bridge)."""

from __future__ import annotations

import atexit
import copy
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Any

PLUGIN_ID = "flashforge-adv4-bridge"
PLUGIN_DIR = Path(__file__).resolve().parent
LIB_DIR = PLUGIN_DIR / "lib"


def resolve_plugin_data_dir(plugin_dir: Path = PLUGIN_DIR) -> Path:
    """Resolve the persistent plugin data directory per OrcaSlicer's plugin_data convention."""
    parent = plugin_dir.parent
    if parent.name == "orca_plugins":
        return parent / "plugin_data" / PLUGIN_ID
    if (
        parent.parent.name == "_subscribed"
        and parent.parent.parent.name == "orca_plugins"
    ):
        return parent.parent.parent / "plugin_data" / PLUGIN_ID
    return plugin_dir
if LIB_DIR.is_dir():
    if str(LIB_DIR) not in sys.path:
        sys.path.insert(0, str(LIB_DIR))
else:
    for candidate in (PLUGIN_DIR, PLUGIN_DIR.parent):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))

from bridge_controller import BridgeController  # noqa: E402
from printer_agent import PrinterAgentMixin  # noqa: E402
from server import (  # noqa: E402
    PLUGIN_ROOT,
    BridgeServer,
    CameraRelayServer,
    _resolve_asset,
    load_config,
    render_dashboard_html,
)

try:
    import orca
except ImportError:  # pragma: no cover - allows importing/testing outside OrcaSlicer
    orca = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_bridge_lock = threading.Lock()
_controller_instance: BridgeController | None = None
_bridge_instance: BridgeServer | None = None
_camera_server_instance: CameraRelayServer | None = None
_atexit_registered = False
_cached_orca_config: dict[str, Any] = {}


def deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override values into a copy of base."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge_dicts(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def parse_orca_config_json(raw: Any) -> dict[str, Any]:
    """Normalize OrcaSlicer get_config() return value into a dict."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse OrcaSlicer plugin config JSON: %s", exc)
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def resolve_effective_config(
    orca_config: dict[str, Any] | None = None,
    base_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge OrcaSlicer UI config on top of local config.json / config-example.json."""
    base = copy.deepcopy(base_config) if base_config is not None else load_config()
    if orca_config:
        return deep_merge_dicts(base, orca_config)
    return base


def _extract_orca_config(owner: Any) -> dict[str, Any]:
    if owner is None:
        return {}
    getter = getattr(owner, "get_config", None)
    if not callable(getter):
        return {}
    try:
        return parse_orca_config_json(getter())
    except Exception as exc:
        logger.debug("get_config() failed on %s: %s", type(owner).__name__, exc)
        return {}


def _safe_json_for_inline_script(value: Any) -> str:
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def build_config_ui_html(
    default_config: dict[str, Any],
    data_dir: Path | None = None,
) -> str:
    """Build a self-contained HTML/JS form for OrcaSlicer's plugin Config tab."""
    sanitized_default = copy.deepcopy(default_config)
    if isinstance(sanitized_default.get("server"), dict):
        sanitized_default["server"].pop("api_key", None)
    default_json = _safe_json_for_inline_script(sanitized_default)
    data_dir_json = _safe_json_for_inline_script(str(data_dir or resolve_plugin_data_dir(PLUGIN_DIR)))
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Flashforge Adventurer 4 Bridge 設定</title>
<style>
  :root {{
    color-scheme: light dark;
    --bg: var(--orca-bg, #1e1e1e);
    --fg: var(--orca-fg, #e6e6e6);
    --muted: var(--orca-muted, #9aa0a6);
    --accent: var(--orca-accent, #009688);
    --border: var(--orca-border, #3c3c3c);
    --font: var(--orca-font, system-ui, -apple-system, "Segoe UI", sans-serif);
    --input-bg: rgba(127, 127, 127, 0.12);
    --card-bg: rgba(127, 127, 127, 0.06);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 14px 16px 20px;
    background: var(--bg);
    color: var(--fg);
    font-family: var(--font);
    font-size: 13px;
    line-height: 1.45;
  }}
  .header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
  }}
  .header h2 {{
    margin: 0;
    font-size: 15px;
    font-weight: 600;
  }}
  .hint {{
    margin: 0 0 14px;
    color: var(--muted);
    font-size: 12px;
  }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 12px;
  }}
  fieldset {{
    margin: 0;
    padding: 10px 12px 12px;
    border: 1px solid var(--border);
    border-radius: 6px;
    background: var(--card-bg);
  }}
  legend {{
    padding: 0 6px;
    font-weight: 600;
    font-size: 12px;
    color: var(--fg);
  }}
  .field {{
    display: flex;
    flex-direction: column;
    gap: 4px;
    margin-top: 8px;
  }}
  .field:first-of-type {{
    margin-top: 4px;
  }}
  .field-row {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
  }}
  label {{
    font-size: 12px;
    color: var(--muted);
  }}
  input[type="text"],
  input[type="number"] {{
    width: 100%;
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--input-bg);
    color: var(--fg);
    font-size: 13px;
    font-family: inherit;
  }}
  input[type="text"]:focus,
  input[type="number"]:focus {{
    outline: 1px solid var(--accent);
    border-color: var(--accent);
  }}
  .checkbox-field {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 10px;
    cursor: pointer;
  }}
  .checkbox-field label {{
    color: var(--fg);
    cursor: pointer;
  }}
  .inline-action {{
    align-self: flex-start;
    margin-top: 2px;
    padding: 0;
    border: none;
    background: none;
    color: var(--accent);
    font-size: 11px;
    cursor: pointer;
    text-decoration: underline;
  }}
  .actions {{
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 10px;
    margin-top: 14px;
    padding-top: 12px;
    border-top: 1px solid var(--border);
  }}
  .status {{
    margin-right: auto;
    font-size: 12px;
    color: var(--muted);
  }}
  .status.ok {{ color: #4caf50; }}
  .status.err {{ color: #f44336; }}
  button.btn {{
    padding: 7px 14px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--input-bg);
    color: var(--fg);
    font-size: 13px;
    cursor: pointer;
  }}
  button.btn-primary {{
    background: var(--accent);
    border-color: var(--accent);
    color: #fff;
    font-weight: 600;
  }}
</style>
</head>
<body>
  <div class="header">
    <h2>Flashforge Adventurer 4 Bridge 設定</h2>
  </div>
  <p class="hint">
    「保存」を押すと OrcaSlicer のプラグイン設定に保存されます。変更を反映するには、保存後にプラグイン画面右上の「再読込」（または OrcaSlicer の再起動）を行ってください。
  </p>

  <form id="cfg-form" onsubmit="return false;">
    <div class="grid">
      <fieldset>
        <legend>プリンター接続 (printer)</legend>
        <div class="field-row">
          <div class="field">
            <label for="printer_ip">プリンター IP アドレス</label>
            <input id="printer_ip" type="text" placeholder="192.168.1.100" required />
          </div>
          <div class="field">
            <label for="printer_port">TCP ポート</label>
            <input id="printer_port" type="number" min="1" max="65535" step="1" required />
          </div>
        </div>
        <div class="field">
          <label for="printer_camera_url">カメラストリーム URL</label>
          <input id="printer_camera_url" type="text" placeholder="http://192.168.1.100:8080/?action=stream" />
          <button type="button" class="inline-action" id="btn-sync-camera">IP からカメラ URL を自動設定</button>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="printer_timeout">通信タイムアウト (秒)</label>
            <input id="printer_timeout" type="number" min="0.5" step="0.5" />
          </div>
          <div class="field">
            <label for="printer_transfer_timeout">転送タイムアウト (秒)</label>
            <input id="printer_transfer_timeout" type="number" min="1" step="1" />
          </div>
        </div>
      </fieldset>

      <fieldset>
        <legend>ローカルブリッジサーバー (server)</legend>
        <div class="checkbox-field">
          <input id="server_enabled" type="checkbox" />
          <label for="server_enabled">ローカル HTTP ブリッジを有効にする (enabled)</label>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="server_host">バインドホスト</label>
            <input id="server_host" type="text" placeholder="127.0.0.1" required />
          </div>
          <div class="field">
            <label for="server_port">待受ポート</label>
            <input id="server_port" type="number" min="1" max="65535" step="1" required />
          </div>
        </div>
        <div class="checkbox-field">
          <input id="server_debug" type="checkbox" />
          <label for="server_debug">デバッグログを有効にする (debug)</label>
        </div>
      </fieldset>

      <fieldset>
        <legend>カメラ・スナップショット (camera)</legend>
        <div class="checkbox-field">
          <input id="camera_snapshot_enabled" type="checkbox" />
          <label for="camera_snapshot_enabled">自動スナップショットを有効にする</label>
        </div>
        <div class="checkbox-field">
          <input id="camera_snapshot_only_while_printing" type="checkbox" />
          <label for="camera_snapshot_only_while_printing">印刷中のみ撮影する</label>
        </div>
        <div class="field-row">
          <div class="field">
            <label for="camera_snapshot_interval_seconds">撮影間隔 (秒)</label>
            <input id="camera_snapshot_interval_seconds" type="number" min="1" step="1" />
          </div>
          <div class="field">
            <label for="camera_snapshot_max_count">最大保存枚数</label>
            <input id="camera_snapshot_max_count" type="number" min="1" step="1" />
          </div>
        </div>
        <div class="field">
          <label for="camera_snapshot_dir">保存先ディレクトリ</label>
          <input id="camera_snapshot_dir" type="text" placeholder="snapshots" />
          <div id="camera_snapshot_dir_hint" class="hint" style="margin: 4px 0 0; word-break: break-all;"></div>
        </div>
      </fieldset>

      <fieldset>
        <legend>温度履歴 (history)</legend>
        <div class="field-row">
          <div class="field">
            <label for="history_temperature_hours">保持時間 (時間)</label>
            <input id="history_temperature_hours" type="number" min="0.1" step="0.5" />
          </div>
          <div class="field">
            <label for="history_sample_interval_seconds">取得間隔 (秒)</label>
            <input id="history_sample_interval_seconds" type="number" min="1" step="0.5" />
          </div>
        </div>
      </fieldset>
    </div>

    <div class="actions">
      <span id="status-msg" class="status"></span>
      <button type="button" class="btn" id="btn-reset">デフォルトに戻す</button>
      <button type="button" class="btn btn-primary" id="btn-save">保存</button>
    </div>
  </form>

<script>
(function() {{
  const DEFAULT_CONFIG = {default_json};
  const PLUGIN_DATA_DIR = {data_dir_json};

  function updateSnapshotDirHint() {{
    const input = document.getElementById("camera_snapshot_dir");
    const hint = document.getElementById("camera_snapshot_dir_hint");
    if (!input || !hint) return;
    const raw = (input.value || "").trim() || "snapshots";
    const isAbs = /^([a-zA-Z]:[\\\\/]|\\\\\\\\|\\/)/.test(raw);
    const sep = PLUGIN_DATA_DIR.includes("\\\\") ? "\\\\" : "/";
    const base = PLUGIN_DATA_DIR.replace(/[\\\\/]+$/, "");
    const resolved = isAbs ? raw : (base + sep + raw);
    hint.textContent = "実際の保存先: " + resolved + "（相対パス時は plugin_data 配下、絶対パス指定可）";
  }}

  function deepMerge(base, override) {{
    const result = Object.assign({{}}, base);
    if (!override || typeof override !== "object") return result;
    for (const key of Object.keys(override)) {{
      const val = override[key];
      if (val && typeof val === "object" && !Array.isArray(val) &&
          result[key] && typeof result[key] === "object" && !Array.isArray(result[key])) {{
        result[key] = deepMerge(result[key], val);
      }} else if (val !== undefined) {{
        result[key] = val;
      }}
    }}
    return result;
  }}

  function setStatus(text, kind) {{
    const el = document.getElementById("status-msg");
    el.textContent = text;
    el.className = "status" + (kind ? " " + kind : "");
  }}

  function applyToForm(cfg) {{
    const p = cfg.printer || {{}};
    const s = cfg.server || {{}};
    const c = cfg.camera || {{}};
    const h = cfg.history || {{}};

    document.getElementById("printer_ip").value = p.ip ?? "192.168.1.100";
    document.getElementById("printer_port").value = p.port ?? 8899;
    document.getElementById("printer_camera_url").value = p.camera_url ?? "";
    document.getElementById("printer_timeout").value = p.timeout ?? 5.0;
    document.getElementById("printer_transfer_timeout").value = p.transfer_timeout ?? 30.0;

    document.getElementById("server_enabled").checked = s.enabled !== false;
    document.getElementById("server_host").value = s.host ?? "127.0.0.1";
    document.getElementById("server_port").value = s.port ?? 5000;
    document.getElementById("server_debug").checked = Boolean(s.debug);

    document.getElementById("camera_snapshot_enabled").checked = c.snapshot_enabled !== false;
    document.getElementById("camera_snapshot_only_while_printing").checked = c.snapshot_only_while_printing !== false;
    document.getElementById("camera_snapshot_interval_seconds").value = c.snapshot_interval_seconds ?? 60;
    document.getElementById("camera_snapshot_max_count").value = c.snapshot_max_count ?? 200;
    document.getElementById("camera_snapshot_dir").value = c.snapshot_dir ?? "snapshots";
    updateSnapshotDirHint();

    document.getElementById("history_temperature_hours").value = h.temperature_hours ?? 4.0;
    document.getElementById("history_sample_interval_seconds").value = h.sample_interval_seconds ?? 5.0;
  }}

  function readFromForm() {{
    const printerIp = document.getElementById("printer_ip").value.trim();
    const printerPort = parseInt(document.getElementById("printer_port").value, 10);
    const serverHost = document.getElementById("server_host").value.trim();
    const serverPort = parseInt(document.getElementById("server_port").value, 10);

    if (!printerIp) {{
      throw new Error("プリンター IP アドレスを入力してください。");
    }}
    if (!Number.isInteger(printerPort) || printerPort < 1 || printerPort > 65535) {{
      throw new Error("プリンター TCP ポートは 1〜65535 の整数で指定してください。");
    }}
    if (!serverHost) {{
      throw new Error("バインドホストを入力してください。");
    }}
    if (!Number.isInteger(serverPort) || serverPort < 1 || serverPort > 65535) {{
      throw new Error("待受ポートは 1〜65535 の整数で指定してください。");
    }}

    const rawCameraUrl = document.getElementById("printer_camera_url").value.trim();
    const cameraUrl = rawCameraUrl || ("http://" + printerIp + ":8080/?action=stream");
    if (!/^https?:\\/\\/[^/\\s]+/i.test(cameraUrl)) {{
      throw new Error("カメラストリーム URL は http:// または https:// で指定してください。");
    }}

    return {{
      printer: {{
        ip: printerIp,
        port: printerPort,
        timeout: parseFloat(document.getElementById("printer_timeout").value) || 5.0,
        transfer_timeout: parseFloat(document.getElementById("printer_transfer_timeout").value) || 30.0,
        camera_url: cameraUrl
      }},
      server: {{
        enabled: document.getElementById("server_enabled").checked,
        host: serverHost,
        port: serverPort,
        debug: document.getElementById("server_debug").checked
      }},
      camera: {{
        snapshot_enabled: document.getElementById("camera_snapshot_enabled").checked,
        snapshot_only_while_printing: document.getElementById("camera_snapshot_only_while_printing").checked,
        snapshot_interval_seconds: parseInt(document.getElementById("camera_snapshot_interval_seconds").value, 10) || 60,
        snapshot_max_count: parseInt(document.getElementById("camera_snapshot_max_count").value, 10) || 200,
        snapshot_dir: document.getElementById("camera_snapshot_dir").value.trim() || "snapshots"
      }},
      history: {{
        temperature_hours: parseFloat(document.getElementById("history_temperature_hours").value) || 4.0,
        sample_interval_seconds: parseFloat(document.getElementById("history_sample_interval_seconds").value) || 5.0
      }}
    }};
  }}

  function loadInitialConfig() {{
    let saved = {{}};
    if (window.orca && typeof window.orca.getConfig === "function") {{
      try {{
        const raw = window.orca.getConfig();
        if (typeof raw === "string" && raw.trim()) {{
          saved = JSON.parse(raw);
        }} else if (raw && typeof raw === "object") {{
          saved = raw;
        }}
      }} catch (err) {{
        setStatus("設定の読み込みに失敗しました: " + err.message, "err");
      }}
    }}
    applyToForm(deepMerge(DEFAULT_CONFIG, saved));
  }}

  document.getElementById("btn-sync-camera").addEventListener("click", function() {{
    const ip = document.getElementById("printer_ip").value.trim() || "192.168.1.100";
    document.getElementById("printer_camera_url").value = "http://" + ip + ":8080/?action=stream";
  }});

  document.getElementById("btn-reset").addEventListener("click", function() {{
    applyToForm(DEFAULT_CONFIG);
    setStatus("デフォルト値をフォームに読み込みました（「保存」で確定します）。", "");
  }});

  document.getElementById("btn-save").addEventListener("click", function() {{
    try {{
      const cfg = readFromForm();
      if (window.orca && typeof window.orca.saveConfig === "function") {{
        window.orca.saveConfig(cfg);
        setStatus("保存しました。反映するには「再読込」を押してください。", "ok");
      }} else {{
        setStatus("OrcaSlicer 設定 API が利用できません。", "err");
      }}
    }} catch (err) {{
      setStatus(err.message, "err");
    }}
  }});

  document.getElementById("camera_snapshot_dir").addEventListener("input", updateSnapshotDirHint);

  loadInitialConfig();
}})();
</script>
</body>
</html>
"""


def _ensure_controller_locked(cfg: dict[str, Any]) -> BridgeController:
    """Create or reuse the shared BridgeController under _bridge_lock."""
    global _controller_instance, _atexit_registered
    if _controller_instance is not None and _controller_instance.config == cfg:
        return _controller_instance

    if _controller_instance is not None:
        try:
            _controller_instance.stop()
        except Exception as exc:
            logger.warning("BridgeController restart stop failed: %s", exc)
        _controller_instance = None

    controller = BridgeController(config=cfg, base_dir=resolve_plugin_data_dir(PLUGIN_DIR))
    controller.start()
    _controller_instance = controller
    if not _atexit_registered:
        atexit.register(_stop_bridge)
        _atexit_registered = True
    return controller


def _ensure_controller(orca_config: dict[str, Any] | None = None) -> BridgeController:
    """Return the active shared BridgeController, starting it if needed."""
    global _cached_orca_config
    with _bridge_lock:
        if orca_config:
            _cached_orca_config = deep_merge_dicts(_cached_orca_config, orca_config)
        cfg = resolve_effective_config(_cached_orca_config)
        return _ensure_controller_locked(cfg)


def _start_bridge(orca_config: dict[str, Any] | None = None) -> tuple[BridgeServer | None, str]:
    """Start the shared controller and (if server.enabled) the local HTTP bridge server."""
    global _bridge_instance, _camera_server_instance, _atexit_registered, _cached_orca_config
    with _bridge_lock:
        if orca_config:
            _cached_orca_config = deep_merge_dicts(_cached_orca_config, orca_config)

        cfg = resolve_effective_config(_cached_orca_config)
        server_enabled = bool(cfg.get("server", {}).get("enabled", True))
        controller = _ensure_controller_locked(cfg)

        if not server_enabled:
            if _bridge_instance is not None:
                try:
                    _bridge_instance.stop()
                except Exception as exc:
                    logger.warning("BridgeServer stop failed while disabling HTTP: %s", exc)
                _bridge_instance = None

            if (
                _camera_server_instance is not None
                and _camera_server_instance.is_running
                and _camera_server_instance.controller is controller
            ):
                cam_port = _camera_server_instance.port
                return None, f"HTTP bridge disabled (native mode, camera relay: http://127.0.0.1:{cam_port})"

            if _camera_server_instance is not None:
                try:
                    _camera_server_instance.stop()
                except Exception as exc:
                    logger.warning("CameraRelayServer restart stop failed: %s", exc)
                _camera_server_instance = None

            cam_server = CameraRelayServer(controller=controller, host="127.0.0.1", port=0)
            try:
                cam_port = cam_server.start()
                _camera_server_instance = cam_server
                return None, f"HTTP bridge disabled (native mode, camera relay: http://127.0.0.1:{cam_port})"
            except OSError as exc:
                logger.warning("CameraRelayServer failed to start: %s", exc)
                return None, "HTTP bridge disabled (native mode)"

        if _camera_server_instance is not None:
            try:
                _camera_server_instance.stop()
            except Exception as exc:
                logger.warning("CameraRelayServer stop failed: %s", exc)
            _camera_server_instance = None

        if _bridge_instance is not None and _bridge_instance.is_running:
            if _bridge_instance.config == cfg and _bridge_instance.controller is controller:
                return _bridge_instance, f"http://localhost:{_bridge_instance.port}"
            try:
                _bridge_instance.stop()
            except Exception as exc:
                logger.warning("BridgeServer restart stop failed: %s", exc)
            _bridge_instance = None

        bridge = BridgeServer(config=cfg, controller=controller)
        try:
            port = bridge.start()
        except OSError as exc:
            logger.warning("BridgeServer failed to bind port %s: %s", bridge.port, exc)
            return None, f"Port {bridge.port} unavailable ({exc})"

        _bridge_instance = bridge
        if not _atexit_registered:
            atexit.register(_stop_bridge)
            _atexit_registered = True
        return bridge, f"http://localhost:{port}"


def _stop_bridge() -> None:
    global _bridge_instance, _camera_server_instance, _controller_instance
    with _bridge_lock:
        if _controller_instance is not None:
            try:
                _controller_instance.stop()
            except Exception as exc:
                logger.warning("BridgeController stop failed: %s", exc)
            finally:
                _controller_instance = None
        if _camera_server_instance is not None:
            try:
                _camera_server_instance.stop()
            except Exception as exc:
                logger.warning("CameraRelayServer stop failed: %s", exc)
            finally:
                _camera_server_instance = None
        if _bridge_instance is not None:
            try:
                _bridge_instance.stop()
            except Exception as exc:
                logger.warning("BridgeServer stop failed: %s", exc)
            finally:
                _bridge_instance = None


class PagesBridgeMixin:
    """Shared implementation for OrcaSlicer PagesPluginCapabilityBase tab UI."""

    def get_name(self) -> str:
        return "Adventurer 4"

    def _get_bridge(self) -> BridgeServer | None:
        if _bridge_instance is not None and _bridge_instance.is_running:
            return _bridge_instance
        bridge, _ = _start_bridge(_extract_orca_config(self) or None)
        return bridge

    def _get_controller(self) -> BridgeController | None:
        bridge = self._get_bridge()
        if bridge is not None:
            return bridge.controller
        if _controller_instance is not None:
            return _controller_instance
        return None

    def get_ui(self) -> str:
        bridge = self._get_bridge()
        controller = bridge.controller if bridge is not None else _controller_instance
        if controller is not None:
            camera_url = controller.camera_url
            printer_ip = controller.printer_ip
            if bridge is not None:
                http_base_url = f"http://127.0.0.1:{bridge.port}"
            elif _camera_server_instance is not None and _camera_server_instance.is_running:
                http_base_url = f"http://127.0.0.1:{_camera_server_instance.port}"
            else:
                http_base_url = ""
        else:
            cfg = resolve_effective_config(_extract_orca_config(self) or None)
            printer_cfg = cfg.get("printer", {})
            printer_ip = str(printer_cfg.get("ip", "192.168.1.100"))
            camera_url = str(printer_cfg.get("camera_url", f"http://{printer_ip}:8080/?action=stream"))
            http_base_url = ""

        template_path = _resolve_asset("templates/index.html")
        template_text = template_path.read_text(encoding="utf-8")
        return render_dashboard_html(
            template_text,
            camera_url=camera_url,
            printer_ip=printer_ip,
            http_base_url=http_base_url,
        )

    def on_message(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        controller = self._get_controller()
        if controller is None:
            self.post_message(  # type: ignore[attr-defined]
                {
                    "id": data.get("id"),
                    "action": str(data.get("action") or data.get("type") or ""),
                    "ok": False,
                    "status": 503,
                    "data": {"ok": False, "error": "Bridge controller is not running"},
                }
            )
            return
        response = controller.handle_message(data)
        self.post_message(response)  # type: ignore[attr-defined]


def _resolve_camera_url_for_agent() -> str:
    if _bridge_instance is not None and _bridge_instance.is_running:
        return f"http://127.0.0.1:{_bridge_instance.port}/api/camera/stream"
    if _camera_server_instance is not None and _camera_server_instance.is_running:
        return f"http://127.0.0.1:{_camera_server_instance.port}/api/camera/stream"
    if _controller_instance is not None:
        return _controller_instance.camera_url
    return ""


def _resolve_model_id_for_agent() -> str:
    if orca is not None and hasattr(orca, "host") and hasattr(orca.host, "preset_bundle"):
        try:
            bundle = orca.host.preset_bundle()
            if bundle is not None:
                preset = bundle.current_printer_preset()
                if preset is not None:
                    val = preset.config_value("printer_model")
                    if val:
                        return str(val).strip()
        except Exception as exc:
            logger.debug("Failed to read current printer_model from orca.host: %s", exc)
    return ""


if orca is not None:  # pragma: no cover - exercised inside OrcaSlicer 2.5.0+

    _HAS_ORCA_PAGES = hasattr(orca, "pages") and hasattr(
        orca.pages, "PagesPluginCapabilityBase"
    )
    _HAS_ORCA_PRINTER_AGENT = hasattr(orca, "printer_agent") and hasattr(
        orca.printer_agent, "PrinterAgentBase"
    )

    if _HAS_ORCA_PAGES:

        class FlashforgePagesCapability(
            PagesBridgeMixin, orca.pages.PagesPluginCapabilityBase
        ):
            """Native OrcaSlicer tab exposing the Adventurer 4 dashboard via window.orca."""

    if _HAS_ORCA_PRINTER_AGENT:

        class FlashforgePrinterAgent(
            PrinterAgentMixin, orca.printer_agent.PrinterAgentBase
        ):
            """Native OrcaSlicer PrinterAgentBase capability for Flashforge Adventurer 4."""

            def __init__(self) -> None:
                orca.printer_agent.PrinterAgentBase.__init__(self)
                PrinterAgentMixin.__init__(
                    self,
                    controller_provider=_ensure_controller,
                    camera_url_provider=_resolve_camera_url_for_agent,
                    model_id_provider=_resolve_model_id_for_agent,
                    orca_pa_module=orca.printer_agent,
                )

    class FlashforgeBridgeCapability(orca.script.ScriptPluginCapabilityBase):
        """Starts the Adventurer 4 local bridge when loaded by OrcaSlicer."""

        def get_name(self) -> str:
            return "Flashforge Adventurer 4 Bridge Status"

        def get_default_config(self) -> dict[str, Any]:
            return load_config()

        def has_config_ui(self) -> bool:
            return True

        def get_config_ui(self) -> str:
            return build_config_ui_html(self.get_default_config())

        def on_load(self) -> None:
            _, info = _start_bridge(_extract_orca_config(self))
            logger.info("Flashforge Adventurer 4 Bridge loaded: %s", info)

        def on_unload(self) -> None:
            _stop_bridge()

        def execute(self):
            bridge, info = _start_bridge(_extract_orca_config(self))
            if bridge is None:
                if _controller_instance is not None and "disabled" in info:
                    printer_ip = _controller_instance.printer_ip
                    return orca.ExecutionResult.success(
                        f"{info} (printer: {printer_ip}:8899)"
                    )
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, info)
            printer_ip = bridge.config.get("printer", {}).get("ip", "unknown")
            return orca.ExecutionResult.success(
                f"Bridge running at {info} (printer: {printer_ip}:8899)"
            )

    @orca.plugin
    class FlashforgeAdv4BridgePlugin(orca.base):
        """OrcaSlicer plugin package registering the Adventurer 4 bridge capability."""

        def get_default_config(self) -> dict[str, Any]:
            return load_config()

        def has_config_ui(self) -> bool:
            return True

        def get_config_ui(self) -> str:
            return build_config_ui_html(self.get_default_config())

        def register_capabilities(self) -> None:
            orca_cfg = _extract_orca_config(self)
            cfg = resolve_effective_config(orca_cfg)
            printer_cfg = cfg.get("printer", {})
            server_cfg = cfg.get("server", {})
            camera_cfg = cfg.get("camera", {})

            printer_ip = printer_cfg.get("ip", "192.168.1.100")
            printer_port = int(printer_cfg.get("port", 8899))
            camera_url = printer_cfg.get("camera_url", f"http://{printer_ip}:8080/?action=stream")
            server_host = server_cfg.get("host", "127.0.0.1")
            server_port = int(server_cfg.get("port", 5000))
            data_dir = resolve_plugin_data_dir(PLUGIN_DIR)
            snapshot_dir = Path(camera_cfg.get("snapshot_dir", "snapshots"))
            if not snapshot_dir.is_absolute():
                snapshot_dir = data_dir / snapshot_dir

            if hasattr(orca, "request_permissions"):
                perm_kwargs = dict(
                    fs_read=[str(PLUGIN_DIR)],
                    fs_readwrite=[str(snapshot_dir), f"{snapshot_dir}/*"],
                    network_http=[camera_url],
                    network_socket=[
                        f"('{printer_ip}', {printer_port})",
                        f"('{server_host}', {server_port})",
                        f"('127.0.0.1', {server_port})",
                        "('127.0.0.1', 0)",
                    ],
                    threading=True,
                )
                try:
                    orca.request_permissions(
                        **perm_kwargs,
                        fs_create=[str(data_dir), str(snapshot_dir)],
                        fs_delete=[str(snapshot_dir)],
                    )
                except TypeError:
                    try:
                        orca.request_permissions(**perm_kwargs)
                    except Exception as exc:
                        logger.debug("request_permissions skipped/failed: %s", exc)
                except Exception as exc:
                    logger.debug("request_permissions skipped/failed: %s", exc)
            orca.register_capability(FlashforgeBridgeCapability)
            if _HAS_ORCA_PAGES:
                orca.register_capability(FlashforgePagesCapability)
            if _HAS_ORCA_PRINTER_AGENT:
                orca.register_capability(FlashforgePrinterAgent)
            _, info = _start_bridge(orca_cfg)
            logger.info("Flashforge Adventurer 4 Bridge registered: %s", info)

        def on_load(self) -> None:
            _, info = _start_bridge(_extract_orca_config(self))
            logger.info("Flashforge Adventurer 4 Bridge plugin loaded: %s", info)

        def on_unload(self) -> None:
            _stop_bridge()
