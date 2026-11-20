"""OrcaSlicer 2.5.0+ PrinterAgentBase implementation for Flashforge Adventurer 4 (Pattern B Phase 2)."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from bridge_controller import BridgeController
from flashforge_client import FlashforgeTransferCancelledError

logger = logging.getLogger(__name__)

# OrcaSlicer bambu_networking.hpp return codes & printing stages
BAMBU_NETWORK_SUCCESS = 0
BAMBU_NETWORK_ERR_INVALID_HANDLE = -1
BAMBU_NETWORK_ERR_FILE_NOT_EXIST = -14
BAMBU_NETWORK_ERR_CANCELED = -18
BAMBU_NETWORK_ERR_INVALID_RESULT = -19
BAMBU_NETWORK_ERR_PRINT_LP_UPLOAD_FTP_FAILED = -4020
BAMBU_NETWORK_ERR_PRINT_SG_UPLOAD_FTP_FAILED = -5010
BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED = -6010
ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE = -7020

PRINTING_STAGE_CREATE = 0
PRINTING_STAGE_UPLOAD = 1
PRINTING_STAGE_SENDING = 3
PRINTING_STAGE_FINISHED = 6

# DevStorage::HAS_SDCARD_NORMAL (1) shifted into home_flag bits 8-9, plus X/Y/Z homed bits (0..2)
_HOME_FLAG_HAS_SDCARD = 1 << 8
_HOME_FLAG_XYZ_HOMED = 0x07

_SPEED_LEVEL_TO_PERCENT: dict[int, int] = {
    1: 50,   # Silent
    2: 100,  # Standard
    3: 124,  # Sport
    4: 166,  # Ludicrous
}

_PLATE_GCODE_RE = re.compile(r"^Metadata/plate_\d+\.gcode$")
_GCODE_M104_RE = re.compile(r"\bM104\b[^;\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_GCODE_M140_RE = re.compile(r"\bM140\b[^;\n]*\bS(-?\d+(?:\.\d+)?)", re.IGNORECASE)
_GCODE_M663_RE = re.compile(r"\bM663\b[^;\n]*\bS(\d+)", re.IGNORECASE)
_GCODE_M106_RE = re.compile(r"\bM106\b([^;\n]*)", re.IGNORECASE)
_GCODE_M107_RE = re.compile(r"\bM107\b", re.IGNORECASE)
_GCODE_G28_RE = re.compile(r"\bG28\b([^;\n]*)", re.IGNORECASE)
_GCODE_S_PARAM_RE = re.compile(r"\bS(\d+(?:\.\d+)?)", re.IGNORECASE)


def percent_to_speed_level(percent: int) -> int:
    """Map a Flashforge print speed percentage to OrcaSlicer's 1..4 speed level."""
    if percent <= 75:
        return 1
    if percent <= 112:
        return 2
    if percent <= 145:
        return 3
    return 4

AGENT_ID = "flashforge_adv4"
AGENT_NAME = "Flashforge Adventurer 4"
AGENT_VERSION = "0.2.0"
AGENT_DESCRIPTION = "Native TCP 8899 printer agent for Flashforge Adventurer 4."
DEFAULT_MODEL_ID = "Flashforge Adventurer 4 Series"


def extract_gcode_from_file(
    source_path: Path,
    plate_index: int = 1,
    project_name: str = "",
    max_bytes: int = 256 * 1024 * 1024,
) -> tuple[str, bytes]:
    """Read raw G-code/GX bytes from a file path, extracting plate G-code if it is a .3mf archive."""
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(f"G-code file not found: {path}")

    if path.suffix.lower() == ".3mf":
        with zipfile.ZipFile(path, "r") as zf:
            target_name = f"Metadata/plate_{plate_index}.gcode"
            names = zf.namelist()
            if target_name not in names:
                matches = [n for n in names if _PLATE_GCODE_RE.match(n)]
                if len(matches) == 1:
                    target_name = matches[0]
                else:
                    raise FileNotFoundError(
                        f"No G-code entry for plate {plate_index} in 3MF archive: {path}"
                    )

            info = zf.getinfo(target_name)
            if info.file_size > max_bytes:
                raise ValueError(
                    f"Extracted G-code exceeds max size ({info.file_size} > {max_bytes})"
                )
            data = zf.read(info)

        base_name = project_name.strip() if project_name else path.stem
        if base_name.lower().endswith(".gcode"):
            base_name = base_name[:-6]
        upload_name = f"{base_name or 'print_job'}.gcode"
        return upload_name, data

    size = path.stat().st_size
    if size > max_bytes:
        raise ValueError(f"G-code file exceeds max size ({size} > {max_bytes})")

    data = path.read_bytes()
    upload_name = path.name
    if project_name and project_name.strip():
        stem = project_name.strip()
        if not stem.lower().endswith((".gcode", ".gx")):
            stem = f"{stem}.gcode"
        upload_name = stem
    return upload_name, data


def map_machine_status_to_orca(machine_status: str) -> tuple[str, int]:
    """Map Flashforge M119 MachineStatus to OrcaSlicer (gcode_state, mc_print_stage)."""
    code = (machine_status or "").strip().upper()
    if code == "BUILDING_FROM_SD":
        return "RUNNING", 1
    if code == "PAUSED":
        return "PAUSE", 2
    if code == "BUILDING_COMPLETED":
        return "FINISH", 3
    return "IDLE", 0


def build_push_status_payload(
    status: dict[str, Any],
    speed_percent: int = 100,
    fan_on: bool | None = None,
) -> dict[str, Any]:
    """Translate BridgeController.get_printer_status() into OrcaSlicer push_status JSON."""
    gcode_state, mc_print_stage = map_machine_status_to_orca(
        str(status.get("machine_status") or "")
    )
    temps = status.get("temperatures") or {}
    nozzle = temps.get("nozzle") or {}
    bed = temps.get("bed") or {}
    progress = status.get("progress") or {}

    print_obj: dict[str, Any] = {
        "command": "push_status",
        "msg": 0,
        "support_mqtt_alive": True,
        "gcode_state": gcode_state,
        "mc_print_stage": mc_print_stage,
        "mc_print_error_code": 0,
        "print_error": 0,
        "sdcard": True,
        "home_flag": _HOME_FLAG_HAS_SDCARD | _HOME_FLAG_XYZ_HOMED,
        "support_send_to_sd": True,
        "support_bed_leveling": 0,
        "nozzle_temp_range": [0, 265],
        "bed_temp_range": [0, 110],
        "spd_lvl": percent_to_speed_level(int(speed_percent)),
        "spd_mag": int(speed_percent),
    }

    if fan_on is not None:
        print_obj["cooling_fan_speed"] = "15" if fan_on else "0"
        print_obj["fan_gear"] = 255 if fan_on else 0

    if nozzle.get("actual") is not None:
        print_obj["nozzle_temper"] = float(nozzle["actual"])
    if nozzle.get("target") is not None:
        print_obj["nozzle_target_temper"] = float(nozzle["target"])
    if bed.get("actual") is not None:
        print_obj["bed_temper"] = float(bed["actual"])
    if bed.get("target") is not None:
        print_obj["bed_target_temper"] = float(bed["target"])

    pct = progress.get("percent")
    if pct is not None:
        print_obj["mc_percent"] = max(0, min(100, int(round(float(pct)))))

    current_file = status.get("current_file")
    if current_file:
        print_obj["subtask_name"] = str(current_file)
        print_obj["gcode_file"] = str(current_file)

    led_state = status.get("led")
    if led_state is not None:
        print_obj["lights_report"] = [
            {
                "node": "chamber_light",
                "mode": "on" if bool(led_state) else "off",
            }
        ]

    return {
        "print": print_obj,
        "t_utc": int(time.time() * 1000),
    }


class PrinterAgentMixin:
    """Transport-agnostic implementation of OrcaSlicer's PrinterAgentBase contract."""

    def __init__(
        self,
        controller_provider: Callable[[], BridgeController | None] | None = None,
        camera_url_provider: Callable[[], str] | None = None,
        model_id_provider: Callable[[], str] | None = None,
        orca_pa_module: Any | None = None,
        status_poll_interval: float = 2.5,
    ) -> None:
        self._controller_provider = controller_provider
        self._camera_url_provider = camera_url_provider
        self._model_id_provider = model_id_provider
        self._orca_pa = orca_pa_module
        self._status_poll_interval = max(0.2, float(status_poll_interval))

        self._state_lock = threading.RLock()
        self._callbacks: dict[str, Any] = {}
        self._selected_machine: str = ""
        self._connected_dev_id: str = ""
        self._stop_event = threading.Event()
        self._status_thread: threading.Thread | None = None

    def _get_controller(self) -> BridgeController | None:
        if self._controller_provider is None:
            return None
        return self._controller_provider()

    def _trace(self, line: str) -> None:
        """Append a diagnostic line to snapshot_dir/printer_agent_debug.log when server.debug is enabled."""
        ctrl = self._get_controller()
        if ctrl is None or not ctrl.server_cfg.get("debug", False):
            return
        try:
            snap_dir = ctrl.snapshot_manager.snapshot_dir
            snap_dir.mkdir(parents=True, exist_ok=True)
            log_path = snap_dir / "printer_agent_debug.log"
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"[{ts}] {line}\n")
        except Exception as exc:
            logger.debug("PrinterAgent trace write failed: %s", exc)

    def _default_dev_id(self) -> str:
        ctrl = self._get_controller()
        ip = ctrl.printer_ip if ctrl is not None else "192.168.1.100"
        return f"ff_adv4_{ip.replace('.', '_')}"

    def _resolve_model_id(self) -> str:
        if self._model_id_provider is not None:
            try:
                model = str(self._model_id_provider() or "").strip()
                if model:
                    return model
            except Exception as exc:
                logger.debug("model_id_provider failed: %s", exc)
        return DEFAULT_MODEL_ID

    # ------------------------------------------------------------------
    # Metadata & Callback Registration
    # ------------------------------------------------------------------

    def get_name(self) -> str:
        return AGENT_NAME

    def get_agent_info(self) -> Any:
        if self._orca_pa is not None and hasattr(self._orca_pa, "AgentInfo"):
            return self._orca_pa.AgentInfo(
                AGENT_ID, AGENT_NAME, AGENT_VERSION, AGENT_DESCRIPTION
            )
        return {
            "id": AGENT_ID,
            "name": AGENT_NAME,
            "version": AGENT_VERSION,
            "description": AGENT_DESCRIPTION,
        }

    def _store_callback(self, key: str, fn: Any) -> int:
        with self._state_lock:
            self._callbacks[key] = fn
        return BAMBU_NETWORK_SUCCESS

    def set_on_ssdp_msg_fn(self, fn: Any) -> int:
        return self._store_callback("ssdp", fn)

    def set_on_local_connect_fn(self, fn: Any) -> int:
        return self._store_callback("local_connect", fn)

    def set_on_local_message_fn(self, fn: Any) -> int:
        return self._store_callback("local_message", fn)

    def set_on_message_fn(self, fn: Any) -> int:
        return self._store_callback("message", fn)

    def set_on_user_message_fn(self, fn: Any) -> int:
        return self._store_callback("user_message", fn)

    def set_on_printer_connected_fn(self, fn: Any) -> int:
        return self._store_callback("printer_connected", fn)

    def set_on_subscribe_failure_fn(self, fn: Any) -> int:
        return self._store_callback("subscribe_failure", fn)

    def set_server_callback(self, fn: Any) -> int:
        return self._store_callback("server_error", fn)

    def set_queue_on_main_fn(self, fn: Any) -> int:
        return self._store_callback("queue_on_main", fn)

    def _dispatch_on_main(self, fn: Callable[[], None]) -> None:
        with self._state_lock:
            queue_fn = self._callbacks.get("queue_on_main")
        if callable(queue_fn):
            queue_fn(fn)
        else:
            fn()

    def _emit_local_message(self, dev_id: str, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        with self._state_lock:
            local_fn = self._callbacks.get("local_message")
            cloud_fn = self._callbacks.get("message")
        target_fn = local_fn if callable(local_fn) else cloud_fn
        if callable(target_fn):
            self._dispatch_on_main(lambda: target_fn(dev_id, raw))

    # ------------------------------------------------------------------
    # Discovery, Selection & Connection
    # ------------------------------------------------------------------

    def start_discovery(self, start: bool, sending: bool) -> bool:
        del sending
        if not start:
            return True

        with self._state_lock:
            ssdp_fn = self._callbacks.get("ssdp")
        if not callable(ssdp_fn):
            return True

        ctrl = self._get_controller()
        printer_ip = ctrl.printer_ip if ctrl is not None else "192.168.1.100"
        dev_id = self._default_dev_id()
        model_id = self._resolve_model_id()

        payload = {
            "dev_name": AGENT_NAME,
            "dev_id": dev_id,
            "dev_ip": printer_ip,
            "dev_type": model_id,
            "dev_signal": "0",
            "connect_type": "lan",
            "bind_state": "free",
            "sec_link": "secure",
            "ssdp_version": "v1",
        }
        ssdp_fn(json.dumps(payload, ensure_ascii=False))
        with self._state_lock:
            if not self._selected_machine:
                self._selected_machine = dev_id
        return True

    def bind_detect(self, dev_ip: str, sec_link: str, detect: Any) -> int:
        del sec_link
        dev_id = self._default_dev_id() if not dev_ip else f"ff_adv4_{dev_ip.replace('.', '_')}"
        detect.dev_id = dev_id
        detect.dev_name = AGENT_NAME
        detect.model_id = self._resolve_model_id()
        detect.version = AGENT_VERSION
        detect.connect_type = "lan"
        detect.bind_state = "free"
        return BAMBU_NETWORK_SUCCESS

    def get_user_selected_machine(self) -> str:
        with self._state_lock:
            return self._selected_machine or self._default_dev_id()

    def set_user_selected_machine(self, dev_id: str) -> int:
        with self._state_lock:
            self._selected_machine = str(dev_id or "")
        return BAMBU_NETWORK_SUCCESS

    def check_cert(self) -> int:
        return BAMBU_NETWORK_SUCCESS

    def ping_bind(self, ping_code: str) -> int:
        del ping_code
        return BAMBU_NETWORK_SUCCESS

    def bind(
        self,
        dev_ip: str,
        dev_id: str,
        dev_model: str,
        sec_link: str,
        timezone: str,
        improved: bool,
        update_fn: Any,
    ) -> int:
        del dev_ip, dev_id, dev_model, sec_link, timezone, improved, update_fn
        return BAMBU_NETWORK_SUCCESS

    def unbind(self, dev_id: str) -> int:
        del dev_id
        return BAMBU_NETWORK_SUCCESS

    def connect_printer(self, params: Any) -> int:
        dev_id = str(getattr(params, "dev_id", "") or self._default_dev_id())
        with self._state_lock:
            self._stop_event.set()
            self._connected_dev_id = dev_id
            self._selected_machine = dev_id
            self._stop_event = threading.Event()
            stop_event = self._stop_event
            self._status_thread = threading.Thread(
                target=self._status_loop,
                args=(dev_id, stop_event),
                name="ff-printer-agent-status",
                daemon=True,
            )
            self._status_thread.start()
        return BAMBU_NETWORK_SUCCESS

    def disconnect_printer(self) -> int:
        with self._state_lock:
            self._stop_event.set()
            self._connected_dev_id = ""
        return BAMBU_NETWORK_SUCCESS

    def _status_loop(self, dev_id: str, stop_event: threading.Event) -> None:
        with self._state_lock:
            local_connect_fn = self._callbacks.get("local_connect")
            connected_fn = self._callbacks.get("printer_connected")

        if callable(local_connect_fn):
            self._dispatch_on_main(lambda: local_connect_fn(BAMBU_NETWORK_SUCCESS, dev_id, ""))
        if callable(connected_fn):
            self._dispatch_on_main(lambda: connected_fn(dev_id))

        while not stop_event.is_set():
            ctrl = self._get_controller()
            if ctrl is not None:
                try:
                    status = ctrl.get_printer_status()
                    payload = build_push_status_payload(
                        status,
                        speed_percent=getattr(ctrl, "_last_print_speed", 100),
                        fan_on=getattr(ctrl, "_last_fan_on", None),
                    )
                    self._emit_local_message(dev_id, payload)
                except Exception as exc:
                    logger.debug("PrinterAgent status poll failed: %s", exc)
            stop_event.wait(self._status_poll_interval)

    # ------------------------------------------------------------------
    # Camera & Filament Capabilities
    # ------------------------------------------------------------------

    def get_filament_sync_mode(self) -> Any:
        if self._orca_pa is not None and hasattr(self._orca_pa, "FilamentSyncMode"):
            return self._orca_pa.FilamentSyncMode.None_
        return 0

    def get_camera_stream_mode(self) -> Any:
        if self._orca_pa is not None and hasattr(self._orca_pa, "CameraStreamMode"):
            return self._orca_pa.CameraStreamMode.HTTP
        return 1

    def get_camera_url(self) -> str:
        if self._camera_url_provider is not None:
            url = self._camera_url_provider()
            if url:
                return url
        ctrl = self._get_controller()
        return ctrl.camera_url if ctrl is not None else ""

    # ------------------------------------------------------------------
    # Direct Commands & JSON Message Handling
    # ------------------------------------------------------------------

    def command_set_nozzle(
        self, dev_id: str, temp: int, sequence_id: int, lan_mode: bool
    ) -> int:
        del dev_id, sequence_id, lan_mode
        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE
        code, _ = ctrl.set_nozzle_temp(int(temp))
        return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED

    def command_set_bed(
        self,
        dev_id: str,
        temp: int,
        supports_mqtt_bed_ctrl: bool,
        sequence_id: int,
        lan_mode: bool,
    ) -> int:
        del dev_id, supports_mqtt_bed_ctrl, sequence_id, lan_mode
        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE
        code, _ = ctrl.set_bed_temp(int(temp))
        return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED

    def command_go_home(
        self,
        dev_id: str,
        is_printing: bool,
        supports_mqtt_homing: bool,
        sequence_id: int,
        lan_mode: bool,
    ) -> int:
        self._trace(
            f"command_go_home dev_id={dev_id!r} is_printing={is_printing} "
            f"supports_mqtt_homing={supports_mqtt_homing} seq={sequence_id} lan={lan_mode}"
        )
        logger.info("PrinterAgent command_go_home called (is_printing=%s)", is_printing)
        if is_printing:
            return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE
        code, data = ctrl.home_axes()
        self._trace(f"command_go_home -> code={code} data={data!r}")
        if code == 200:
            return BAMBU_NETWORK_SUCCESS
        if code == 409:
            return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
        return BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED

    def command_axis_control(
        self,
        dev_id: str,
        axis: str,
        unit: float,
        input_val: float,
        speed: int,
        is_core_xy: bool,
        supports_mqtt_axis_control: bool,
        sequence_id: int,
        lan_mode: bool,
    ) -> int:
        del dev_id, is_core_xy, supports_mqtt_axis_control, sequence_id, lan_mode
        ax = str(axis or "").strip().upper()
        if ax not in {"X", "Y", "Z"}:
            return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE
        delta = float(input_val) * float(unit)
        feedrate = int(speed) if speed else 3000
        code, _ = ctrl.move_axis_relative(ax, delta, feedrate=feedrate)
        if code == 200:
            return BAMBU_NETWORK_SUCCESS
        if code == 409:
            return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
        return BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED

    def send_message(self, dev_id: str, json_str: str, qos: int, flag: int) -> int:
        return self.send_message_to_printer(dev_id, json_str, qos, flag)

    def send_message_to_printer(
        self, dev_id: str, json_str: str, qos: int, flag: int
    ) -> int:
        del qos, flag
        self._trace(f"send_message_to_printer dev_id={dev_id!r} json={json_str}")
        try:
            req = json.loads(json_str)
        except json.JSONDecodeError:
            return BAMBU_NETWORK_ERR_INVALID_RESULT
        if not isinstance(req, dict):
            return BAMBU_NETWORK_ERR_INVALID_RESULT

        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE

        info_obj = req.get("info")
        if isinstance(info_obj, dict) and info_obj.get("command") == "get_version":
            self._emit_local_message(
                dev_id,
                {
                    "info": {
                        "command": "get_version",
                        "module": [{"name": "ota", "sw_ver": AGENT_VERSION}],
                        "result": "success",
                    }
                },
            )
            return BAMBU_NETWORK_SUCCESS

        system_obj = req.get("system")
        if isinstance(system_obj, dict) and system_obj.get("command") == "ledctrl":
            mode = str(system_obj.get("led_mode") or "").strip().lower()
            if mode in {"on", "off"}:
                on = mode == "on"
                code, _ = ctrl.set_led(on)
                if code == 200:
                    self._emit_local_message(
                        dev_id,
                        {
                            "print": {
                                "command": "push_status",
                                "lights_report": [
                                    {
                                        "node": str(system_obj.get("led_node") or "chamber_light"),
                                        "mode": mode,
                                    }
                                ],
                            }
                        },
                    )
                    return BAMBU_NETWORK_SUCCESS
                return BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
            return BAMBU_NETWORK_SUCCESS

        print_obj = req.get("print")
        if not isinstance(print_obj, dict):
            return BAMBU_NETWORK_SUCCESS

        cmd = str(print_obj.get("command") or "")
        if cmd == "pause":
            code, _ = ctrl.pause_print()
            return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "resume":
            code, _ = ctrl.resume_print()
            return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "stop":
            code, _ = ctrl.cancel_print()
            return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "set_bed_temp" and "temp" in print_obj:
            code, _ = ctrl.set_bed_temp(print_obj["temp"])
            return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "set_nozzle_temp" and "target_temp" in print_obj:
            code, _ = ctrl.set_nozzle_temp(print_obj["target_temp"])
            return BAMBU_NETWORK_SUCCESS if code == 200 else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "print_speed" and "param" in print_obj:
            try:
                level = int(str(print_obj["param"]).strip())
            except ValueError:
                return BAMBU_NETWORK_ERR_INVALID_RESULT
            percent = _SPEED_LEVEL_TO_PERCENT.get(level)
            if percent is None:
                return BAMBU_NETWORK_ERR_INVALID_RESULT
            code, _ = ctrl.set_print_speed(percent)
            if code == 200:
                self._emit_local_message(
                    dev_id,
                    {
                        "print": {
                            "command": "push_status",
                            "spd_lvl": level,
                            "spd_mag": percent,
                        }
                    },
                )
                return BAMBU_NETWORK_SUCCESS
            return BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd in {"home", "back_to_center"}:
            code, _ = ctrl.home_axes()
            if code == 200:
                return BAMBU_NETWORK_SUCCESS
            if code == 409:
                return ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE
            return BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED
        if cmd == "gcode_line":
            return self._handle_gcode_line(dev_id, ctrl, print_obj)

        return BAMBU_NETWORK_SUCCESS

    def _handle_gcode_line(
        self, dev_id: str, ctrl: BridgeController, print_obj: dict[str, Any]
    ) -> int:
        param = str(print_obj.get("param") or "")
        ok = True

        m104 = _GCODE_M104_RE.search(param)
        if m104:
            code, _ = ctrl.set_nozzle_temp(int(round(float(m104.group(1)))))
            ok = ok and (code == 200)

        m140 = _GCODE_M140_RE.search(param)
        if m140:
            code, _ = ctrl.set_bed_temp(int(round(float(m140.group(1)))))
            ok = ok and (code == 200)

        m663 = _GCODE_M663_RE.search(param)
        if m663:
            code, _ = ctrl.set_print_speed(int(m663.group(1)))
            ok = ok and (code == 200)

        m107 = _GCODE_M107_RE.search(param)
        m106 = _GCODE_M106_RE.search(param)
        if m107:
            code, _ = ctrl.set_fan(False)
            ok = ok and (code == 200)
        elif m106:
            s_match = _GCODE_S_PARAM_RE.search(m106.group(1))
            fan_on = True if s_match is None else float(s_match.group(1)) > 0.0
            code, _ = ctrl.set_fan(fan_on)
            ok = ok and (code == 200)

        g28 = _GCODE_G28_RE.search(param)
        if g28:
            axes = g28.group(1).strip()
            code, _ = ctrl.home_axes(axes)
            ok = ok and (code == 200)

        resp: dict[str, Any] = {
            "print": {
                "command": "gcode_line",
                "param": param,
                "result": "success" if ok else "failed",
            }
        }
        if print_obj.get("sequence_id"):
            resp["print"]["sequence_id"] = str(print_obj["sequence_id"])
        self._emit_local_message(dev_id, resp)
        return BAMBU_NETWORK_SUCCESS if ok else BAMBU_NETWORK_ERR_CONNECTION_TO_PRINTER_FAILED

    # ------------------------------------------------------------------
    # Print Job Upload & Start
    # ------------------------------------------------------------------

    def _execute_file_transfer(
        self,
        params: Any,
        update_fn: Any,
        cancel_fn: Any,
        should_print: bool,
    ) -> int:
        if callable(update_fn):
            update_fn(PRINTING_STAGE_CREATE, 0, "Preparing G-code...")

        if callable(cancel_fn) and cancel_fn():
            return BAMBU_NETWORK_ERR_CANCELED

        ctrl = self._get_controller()
        if ctrl is None:
            return BAMBU_NETWORK_ERR_INVALID_HANDLE

        dst_file = str(getattr(params, "dst_file", "") or "").strip()
        filename = str(getattr(params, "filename", "") or "").strip()
        source = dst_file or filename
        if not source:
            return BAMBU_NETWORK_ERR_FILE_NOT_EXIST

        plate_index = int(getattr(params, "plate_index", 1) or 1)
        project_name = str(getattr(params, "project_name", "") or "")

        try:
            upload_name, gcode_bytes = extract_gcode_from_file(
                Path(source),
                plate_index=plate_index,
                project_name=project_name,
            )
        except (FileNotFoundError, ValueError, zipfile.BadZipFile) as exc:
            logger.error("PrinterAgent failed to read %s: %s", source, exc)
            return BAMBU_NETWORK_ERR_FILE_NOT_EXIST

        if callable(update_fn):
            update_fn(PRINTING_STAGE_UPLOAD, 0, f"Uploading {upload_name}...")

        def _on_progress(sent: int, total: int) -> None:
            if callable(update_fn) and total > 0:
                pct = max(0, min(100, int(sent * 100 / total)))
                update_fn(PRINTING_STAGE_UPLOAD, pct, f"Uploading {upload_name} ({pct}%)...")

        try:
            status_code, _ = ctrl.upload_local_file(
                filename=upload_name,
                file_bytes=gcode_bytes,
                should_print=should_print,
                progress_callback=_on_progress,
                cancel_check=cancel_fn if callable(cancel_fn) else None,
            )
        except FlashforgeTransferCancelledError:
            return BAMBU_NETWORK_ERR_CANCELED

        if status_code != 201:
            return (
                BAMBU_NETWORK_ERR_PRINT_LP_UPLOAD_FTP_FAILED
                if should_print
                else BAMBU_NETWORK_ERR_PRINT_SG_UPLOAD_FTP_FAILED
            )

        if callable(update_fn):
            msg = "Print started" if should_print else "File uploaded"
            update_fn(PRINTING_STAGE_FINISHED, 100, msg)
        return BAMBU_NETWORK_SUCCESS

    def start_local_print(self, params: Any, update_fn: Any, cancel_fn: Any) -> int:
        return self._execute_file_transfer(
            params=params,
            update_fn=update_fn,
            cancel_fn=cancel_fn,
            should_print=True,
        )

    def start_send_gcode_to_sdcard(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any
    ) -> int:
        del wait_fn
        return self._execute_file_transfer(
            params=params,
            update_fn=update_fn,
            cancel_fn=cancel_fn,
            should_print=False,
        )

    def start_print(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any
    ) -> int:
        del wait_fn
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_local_print_with_record(
        self, params: Any, update_fn: Any, cancel_fn: Any, wait_fn: Any
    ) -> int:
        del wait_fn
        return self.start_local_print(params, update_fn, cancel_fn)

    def start_sdcard_print(self, params: Any, update_fn: Any, cancel_fn: Any) -> int:
        del params, update_fn, cancel_fn
        return BAMBU_NETWORK_ERR_INVALID_HANDLE
