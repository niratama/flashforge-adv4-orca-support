"""HTTP-independent runtime and controller for Flashforge Adventurer 4 bridge & OrcaSlicer plugins."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable

from bridge_services import (
    BackgroundMonitor,
    CameraRelay,
    SnapshotManager,
    TemperatureHistory,
    build_services,
)
from flashforge_client import FlashforgeClient, FlashforgeError

logger = logging.getLogger(__name__)


def load_config(
    path: Path | None = None,
    search_dirs: tuple[Path, ...] | None = None,
) -> dict[str, Any]:
    """Load configuration from config.json (falling back to config-example.json) with env overrides."""
    if path is not None:
        cfg_path = Path(path)
    else:
        roots = search_dirs or (Path(__file__).resolve().parent,)
        cfg_path = _find_config_path(roots)

    with cfg_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    printer_cfg = config.setdefault("printer", {})
    server_cfg = config.setdefault("server", {})

    if os.getenv("PRINTER_IP"):
        printer_cfg["ip"] = os.environ["PRINTER_IP"]
    if os.getenv("PRINTER_PORT"):
        printer_cfg["port"] = int(os.environ["PRINTER_PORT"])
    if os.getenv("CAMERA_URL"):
        printer_cfg["camera_url"] = os.environ["CAMERA_URL"]
    if os.getenv("SERVER_PORT"):
        server_cfg["port"] = int(os.environ["SERVER_PORT"])

    return config


def _find_config_path(roots: tuple[Path, ...]) -> Path:
    for root in roots:
        candidate = root / "config.json"
        if candidate.exists():
            return candidate
    for root in roots:
        candidate = root / "config-example.json"
        if candidate.exists():
            return candidate
    return roots[0] / "config.json"


def parse_bool_flag(value: str | None) -> bool:
    """Parse common truthy query/form string flags."""
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_disconnected_status(
    error: str,
    camera_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a stable status payload when the printer is unreachable."""
    payload: dict[str, Any] = {
        "connected": False,
        "error": error,
        "machine_status": "DISCONNECTED",
        "move_mode": "UNKNOWN",
        "led": None,
        "current_file": None,
        "temperatures": {
            "nozzle": {"actual": None, "target": None},
            "bed": {"actual": None, "target": None},
        },
        "progress": {
            "is_printing": False,
            "bytes_printed": 0,
            "bytes_total": 0,
            "percent": 0.0,
        },
    }
    if camera_status is not None:
        payload["camera"] = camera_status
    return payload


class BridgeController:
    """Transport-agnostic controller coordinating FlashforgeClient and background services."""

    def __init__(
        self,
        config: dict[str, Any],
        base_dir: Path,
        client: FlashforgeClient | None = None,
        start_background: bool | None = None,
    ) -> None:
        self.config = config
        self.base_dir = Path(base_dir)
        self.printer_cfg: dict[str, Any] = config["printer"]
        self.server_cfg: dict[str, Any] = config.get("server", {})

        self.owns_client = client is None
        self.client = client or FlashforgeClient(
            host=self.printer_cfg["ip"],
            port=int(self.printer_cfg.get("port", 8899)),
            timeout=float(self.printer_cfg.get("timeout", 5.0)),
            transfer_timeout=float(self.printer_cfg.get("transfer_timeout", 30.0)),
        )
        self.should_start_background = (
            (client is None) if start_background is None else bool(start_background)
        )

        (
            self.temp_history,
            self.camera_relay,
            self.snapshot_manager,
            self.monitor,
        ) = build_services(
            cfg=self.config,
            base_dir=self.base_dir,
            ff_client=self.client,
        )

    @property
    def camera_url(self) -> str:
        return self.printer_cfg.get(
            "camera_url", f"http://{self.printer_cfg['ip']}:8080/?action=stream"
        )

    @property
    def printer_ip(self) -> str:
        return str(self.printer_cfg["ip"])

    def start(self) -> None:
        """Start background monitoring if enabled for this controller."""
        if self.should_start_background:
            self.monitor.start()

    def stop(self) -> None:
        """Stop background monitoring and close the printer session if owned."""
        self.monitor.stop()
        if self.owns_client:
            self.client.close()

    # ------------------------------------------------------------------
    # Printer Status & Controls
    # ------------------------------------------------------------------

    def get_printer_status(self) -> dict[str, Any]:
        """Return full printer + camera status (always safe for polling)."""
        cam_status = self.camera_relay.get_status()
        try:
            status = self.client.get_full_status()
            self.monitor.observe_status(status)
            status["camera"] = cam_status
            return status
        except FlashforgeError as exc:
            return build_disconnected_status(str(exc), camera_status=cam_status)

    def get_printer_info(self) -> tuple[int, dict[str, Any]]:
        try:
            return 200, {"connected": True, "info": self.client.get_info()}
        except FlashforgeError as exc:
            return 502, {"connected": False, "error": str(exc)}

    def _run_control(self, action: str, fn: Callable[[], Any]) -> tuple[int, dict[str, Any]]:
        try:
            fn()
            return 200, {"ok": True, "action": action}
        except FlashforgeError as exc:
            return 502, {"ok": False, "error": str(exc)}

    def pause_print(self) -> tuple[int, dict[str, Any]]:
        return self._run_control("pause", self.client.pause_print)

    def resume_print(self) -> tuple[int, dict[str, Any]]:
        return self._run_control("resume", self.client.resume_print)

    def cancel_print(self) -> tuple[int, dict[str, Any]]:
        return self._run_control("cancel", self.client.cancel_print)

    def set_led(self, on: bool) -> tuple[int, dict[str, Any]]:
        flag = bool(on)
        try:
            self.client.set_led(flag)
            return 200, {"ok": True, "led": flag}
        except FlashforgeError as exc:
            return 502, {"ok": False, "error": str(exc)}

    def set_nozzle_temp(self, target: Any) -> tuple[int, dict[str, Any]]:
        return self._run_int_param(target, "target", self.client.set_nozzle_temp)

    def set_bed_temp(self, target: Any) -> tuple[int, dict[str, Any]]:
        return self._run_int_param(target, "target", self.client.set_bed_temp)

    def set_print_speed(self, percent: Any) -> tuple[int, dict[str, Any]]:
        return self._run_int_param(percent, "percent", self.client.set_print_speed)

    def _run_int_param(
        self,
        raw_value: Any,
        field_name: str,
        fn: Callable[[int], Any],
    ) -> tuple[int, dict[str, Any]]:
        if raw_value is None or isinstance(raw_value, bool):
            return 400, {"ok": False, "error": f"Invalid {field_name}: {raw_value!r}"}
        try:
            value = int(raw_value)
            fn(value)
            return 200, {"ok": True, field_name: value}
        except (TypeError, ValueError) as exc:
            return 400, {"ok": False, "error": str(exc)}
        except FlashforgeError as exc:
            return 502, {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Camera, Snapshots & History
    # ------------------------------------------------------------------

    def reconnect_camera(self) -> dict[str, Any]:
        self.camera_relay.request_reconnect()
        return {"ok": True}

    def list_snapshots(self) -> dict[str, Any]:
        return self.snapshot_manager.to_payload()

    def capture_snapshot(self, wait_timeout: float = 5.0) -> tuple[int, dict[str, Any]]:
        frame = self.camera_relay.get_latest_frame(wait_timeout=wait_timeout)
        if not frame:
            return 502, {"ok": False, "error": "カメラフレームを取得できませんでした"}
        saved = self.snapshot_manager.save_frame(frame)
        return 201, {"ok": True, "snapshot": saved}

    def get_snapshot_path(self, filename: str) -> Path | None:
        return self.snapshot_manager.get_snapshot_path(filename)

    def get_temperature_history(self) -> dict[str, Any]:
        return self.temp_history.to_payload()

    # ------------------------------------------------------------------
    # OctoPrint Compatibility & File Transfer
    # ------------------------------------------------------------------

    @staticmethod
    def get_octoprint_version() -> dict[str, str]:
        return {
            "api": "0.1",
            "server": "1.9.0",
            "text": "OctoPrint 1.9.0",
        }

    def get_octoprint_settings(self) -> dict[str, Any]:
        return {
            "api": {"allowCrossOrigin": True},
            "feature": {"sdSupport": True},
            "webcam": {"streamUrl": self.camera_url},
        }

    def upload_local_file(
        self,
        filename: str,
        file_bytes: bytes,
        should_print: bool = False,
    ) -> tuple[int, dict[str, Any]]:
        if not filename:
            return 400, {"error": "No file provided in 'file' field."}

        logger.info(
            "Received file upload: filename=%s size=%d bytes print=%s",
            filename,
            len(file_bytes),
            should_print,
        )
        try:
            saved_name = self.client.send_file(
                filename=filename,
                data=file_bytes,
                start_print=should_print,
            )
        except FlashforgeError as exc:
            logger.error("Upload/print failed for %s: %s", filename, exc)
            return 502, {"error": str(exc)}

        return 201, {
            "files": {
                "local": {
                    "name": saved_name,
                    "origin": "local",
                    "path": saved_name,
                    "size": len(file_bytes),
                }
            },
            "done": True,
        }

    # ------------------------------------------------------------------
    # OrcaSlicer Pattern B (Pages window.orca) Message Dispatch
    # ------------------------------------------------------------------

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Dispatch a JSON RPC-style message from OrcaSlicer Pages UI."""
        msg_id = message.get("id")
        action = str(message.get("action") or message.get("type") or "").strip()
        payload = message.get("payload")
        if not isinstance(payload, dict):
            payload = {}

        status, data = self._dispatch_action(action, payload)
        return {
            "id": msg_id,
            "action": action,
            "ok": 200 <= status < 300 and data.get("ok", True),
            "status": status,
            "data": data,
        }

    def _dispatch_action(self, action: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if action in {"ready", "printer.status"}:
            return 200, self.get_printer_status()
        if action == "printer.info":
            return self.get_printer_info()
        if action == "printer.pause":
            return self.pause_print()
        if action == "printer.resume":
            return self.resume_print()
        if action == "printer.cancel":
            return self.cancel_print()
        if action == "printer.led":
            return self.set_led(bool(payload.get("on", True)))
        if action == "printer.temperature.nozzle":
            return self.set_nozzle_temp(payload.get("target"))
        if action == "printer.temperature.bed":
            return self.set_bed_temp(payload.get("target"))
        if action == "printer.speed":
            return self.set_print_speed(payload.get("percent"))
        if action == "camera.reconnect":
            return 200, self.reconnect_camera()
        if action == "camera.snapshots.list":
            return 200, self.list_snapshots()
        if action == "camera.snapshots.capture":
            return self.capture_snapshot()
        if action == "history.temperature":
            return 200, self.get_temperature_history()
        return 404, {"ok": False, "error": f"Unknown action: {action}"}
