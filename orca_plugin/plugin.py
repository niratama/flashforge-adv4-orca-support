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
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from server import BridgeServer, load_config  # noqa: E402

try:
    import orca
except ImportError:  # pragma: no cover - allows importing/testing outside OrcaSlicer
    orca = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
_bridge_instance: BridgeServer | None = None


def _start_bridge() -> tuple[BridgeServer | None, str]:
    """Start the local bridge server once and register shutdown cleanup."""
    global _bridge_instance
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
    atexit.register(_stop_bridge)
    return bridge, f"http://localhost:{port}"


def _stop_bridge() -> None:
    global _bridge_instance
    if _bridge_instance is not None:
        _bridge_instance.stop()
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
                return orca.ExecutionResult.failure(info)
            printer_ip = bridge.config.get("printer", {}).get("ip", "unknown")
            return orca.ExecutionResult.success(
                f"Bridge running at {info} (printer: {printer_ip}:8899)"
            )

    @orca.plugin
    class FlashforgeAdv4BridgePlugin(orca.base):
        """OrcaSlicer plugin package registering the Adventurer 4 bridge capability."""

        def register_capabilities(self) -> None:
            cfg = load_config()
            printer_ip = cfg.get("printer", {}).get("ip", "192.168.1.100")
            server_port = int(cfg.get("server", {}).get("port", 5000))
            if hasattr(orca, "request_permissions"):
                try:
                    orca.request_permissions(
                        network=[
                            f"{printer_ip}:8899",
                            f"{printer_ip}:8080",
                            f"127.0.0.1:{server_port}",
                            f"0.0.0.0:{server_port}",
                        ],
                        fs_read=[str(PLUGIN_DIR)],
                    )
                except Exception as exc:
                    logger.debug("request_permissions skipped/failed: %s", exc)
            orca.register_capability(FlashforgeBridgeCapability)
