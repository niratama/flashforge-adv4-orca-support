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
import logging
import sys
import threading
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
LIB_DIR = PLUGIN_DIR / "lib"
if LIB_DIR.is_dir():
    if str(LIB_DIR) not in sys.path:
        sys.path.insert(0, str(LIB_DIR))
else:
    for candidate in (PLUGIN_DIR, PLUGIN_DIR.parent):
        if str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))

from server import BridgeServer, load_config  # noqa: E402

try:
    import orca
except ImportError:  # pragma: no cover - allows importing/testing outside OrcaSlicer
    orca = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_bridge_lock = threading.Lock()
_bridge_instance: BridgeServer | None = None
_atexit_registered = False


def _start_bridge() -> tuple[BridgeServer | None, str]:
    """Start the local bridge server once and register shutdown cleanup."""
    global _bridge_instance, _atexit_registered
    with _bridge_lock:
        if _bridge_instance is not None and _bridge_instance.is_running:
            return _bridge_instance, f"http://localhost:{_bridge_instance.port}"

        cfg = load_config()
        bridge = BridgeServer(config=cfg)
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
    global _bridge_instance
    with _bridge_lock:
        if _bridge_instance is not None:
            try:
                _bridge_instance.stop()
            except Exception as exc:
                logger.warning("BridgeServer stop failed: %s", exc)
            finally:
                _bridge_instance = None


if orca is not None:  # pragma: no cover - exercised inside OrcaSlicer 2.5.0+

    class FlashforgeBridgeCapability(orca.script.ScriptPluginCapabilityBase):
        """Starts the Adventurer 4 local bridge when loaded by OrcaSlicer."""

        def get_name(self) -> str:
            return "Flashforge Adventurer 4 Bridge Status"

        def on_load(self) -> None:
            _, info = _start_bridge()
            logger.info("Flashforge Adventurer 4 Bridge loaded: %s", info)

        def on_unload(self) -> None:
            _stop_bridge()

        def execute(self):
            bridge, info = _start_bridge()
            if bridge is None:
                return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, info)
            printer_ip = bridge.config.get("printer", {}).get("ip", "unknown")
            return orca.ExecutionResult.success(
                f"Bridge running at {info} (printer: {printer_ip}:8899)"
            )

    @orca.plugin
    class FlashforgeAdv4BridgePlugin(orca.base):
        """OrcaSlicer plugin package registering the Adventurer 4 bridge capability."""

        def register_capabilities(self) -> None:
            cfg = load_config()
            printer_cfg = cfg.get("printer", {})
            server_cfg = cfg.get("server", {})
            camera_cfg = cfg.get("camera", {})

            printer_ip = printer_cfg.get("ip", "192.168.1.100")
            printer_port = int(printer_cfg.get("port", 8899))
            camera_url = printer_cfg.get("camera_url", f"http://{printer_ip}:8080/?action=stream")
            server_host = server_cfg.get("host", "127.0.0.1")
            server_port = int(server_cfg.get("port", 5000))
            snapshot_dir = Path(camera_cfg.get("snapshot_dir", "snapshots"))
            if not snapshot_dir.is_absolute():
                snapshot_dir = PLUGIN_DIR / snapshot_dir

            if hasattr(orca, "request_permissions"):
                try:
                    orca.request_permissions(
                        fs_read=[str(PLUGIN_DIR)],
                        fs_readwrite=[str(snapshot_dir)],
                        network_http=[camera_url],
                        network_socket=[
                            f"('{printer_ip}', {printer_port})",
                            f"('{server_host}', {server_port})",
                            f"('127.0.0.1', {server_port})",
                        ],
                        threading=True,
                    )
                except Exception as exc:
                    logger.debug("request_permissions skipped/failed: %s", exc)
            orca.register_capability(FlashforgeBridgeCapability)
            _, info = _start_bridge()
            logger.info("Flashforge Adventurer 4 Bridge registered: %s", info)

        def on_load(self) -> None:
            _, info = _start_bridge()
            logger.info("Flashforge Adventurer 4 Bridge plugin loaded: %s", info)

        def on_unload(self) -> None:
            _stop_bridge()
