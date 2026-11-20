"""Unit tests for the OrcaSlicer PrinterAgentBase capability (Pattern B Phase 2)."""

import importlib
import json
import sys
import tempfile
import threading
import types
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "orca_plugin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "orca_plugin"))

import plugin as orca_plugin_mod  # noqa: E402
from bridge_controller import BridgeController  # noqa: E402
from flashforge_client import FlashforgeClient, FlashforgeTransferCancelledError  # noqa: E402
from printer_agent import (  # noqa: E402
    AGENT_ID,
    AGENT_NAME,
    BAMBU_NETWORK_ERR_CANCELED,
    BAMBU_NETWORK_ERR_FILE_NOT_EXIST,
    BAMBU_NETWORK_ERR_INVALID_RESULT,
    BAMBU_NETWORK_SUCCESS,
    ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE,
    PRINTING_STAGE_FINISHED,
    PRINTING_STAGE_UPLOAD,
    PrinterAgentMixin,
    build_push_status_payload,
    extract_gcode_from_file,
)


def _sample_config(snapshot_dir: Path) -> dict:
    return {
        "printer": {
            "ip": "192.168.1.77",
            "port": 8899,
            "timeout": 2.0,
            "transfer_timeout": 5.0,
            "camera_url": "http://192.168.1.77:8080/?action=stream",
        },
        "server": {"enabled": False, "host": "127.0.0.1", "port": 5000},
        "camera": {"snapshot_dir": str(snapshot_dir), "snapshot_enabled": False},
        "history": {"retention_hours": 1.0, "sample_interval_seconds": 5.0},
    }


class TestPrinterAgent(unittest.TestCase):
    def test_extract_gcode_from_plain_and_3mf_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            gcode_path = root / "cube.gcode"
            gcode_path.write_bytes(b"G28\nM104 S200\n")

            name, data = extract_gcode_from_file(gcode_path)
            self.assertEqual(name, "cube.gcode")
            self.assertEqual(data, b"G28\nM104 S200\n")

            bundle_path = root / "benchy.gcode.3mf"
            with zipfile.ZipFile(bundle_path, "w") as zf:
                zf.writestr("Metadata/plate_1.gcode", b"; plate 1\nG28\n")
                zf.writestr("Metadata/plate_2.gcode", b"; plate 2\nG28\n")

            name2, data2 = extract_gcode_from_file(
                bundle_path, plate_index=2, project_name="benchy"
            )
            self.assertEqual(name2, "benchy.gcode")
            self.assertEqual(data2, b"; plate 2\nG28\n")

            # Sole plate fallback when requested plate_index is missing
            single_bundle = root / "single.gcode.3mf"
            with zipfile.ZipFile(single_bundle, "w") as zf:
                zf.writestr("Metadata/plate_1.gcode", b"; only plate\n")
            name3, data3 = extract_gcode_from_file(single_bundle, plate_index=9)
            self.assertEqual(name3, "single.gcode")
            self.assertEqual(data3, b"; only plate\n")

            with self.assertRaises(FileNotFoundError):
                extract_gcode_from_file(root / "missing.gcode")

    def test_build_push_status_payload_sets_sdcard_and_home_flag(self) -> None:
        payload = build_push_status_payload(
            {
                "connected": True,
                "machine_status": "BUILDING_FROM_SD",
                "current_file": "part.gx",
                "led": True,
                "temperatures": {
                    "nozzle": {"actual": 205.5, "target": 210.0},
                    "bed": {"actual": 59.0, "target": 60.0},
                },
                "progress": {"is_printing": True, "percent": 67.4},
            }
        )
        p = payload["print"]
        self.assertEqual(p["command"], "push_status")
        self.assertEqual(p["gcode_state"], "RUNNING")
        self.assertEqual(p["mc_print_stage"], 1)
        self.assertTrue(p["sdcard"])
        self.assertEqual(p["home_flag"] & (0x3 << 8), 1 << 8)
        self.assertEqual(p["home_flag"] & 0x07, 0x07)
        self.assertTrue(p["support_send_to_sd"])
        self.assertEqual(p["nozzle_temper"], 205.5)
        self.assertEqual(p["bed_target_temper"], 60.0)
        self.assertEqual(p["mc_percent"], 67)
        self.assertEqual(p["subtask_name"], "part.gx")
        self.assertEqual(p["lights_report"], [{"node": "chamber_light", "mode": "on"}])

    def test_discovery_connect_status_push_and_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.get_full_status.return_value = {
                "connected": True,
                "machine_status": "READY",
                "current_file": None,
                "temperatures": {
                    "nozzle": {"actual": 25.0, "target": 0.0},
                    "bed": {"actual": 24.0, "target": 0.0},
                },
                "progress": {"is_printing": False, "percent": 0.0},
            }
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )
            agent = PrinterAgentMixin(
                controller_provider=lambda: ctrl,
                status_poll_interval=0.2,
            )

            ssdp_msgs: list[dict] = []
            agent.set_on_ssdp_msg_fn(lambda raw: ssdp_msgs.append(json.loads(raw)))
            self.assertTrue(agent.start_discovery(True, False))
            self.assertEqual(len(ssdp_msgs), 1)
            self.assertEqual(ssdp_msgs[0]["dev_ip"], "192.168.1.77")
            self.assertEqual(ssdp_msgs[0]["dev_type"], "Flashforge Adventurer 4 Series")
            self.assertEqual(ssdp_msgs[0]["connect_type"], "lan")

            detect = SimpleNamespace()
            self.assertEqual(
                agent.bind_detect("192.168.1.77", "secure", detect),
                BAMBU_NETWORK_SUCCESS,
            )
            self.assertEqual(detect.dev_name, AGENT_NAME)
            self.assertEqual(detect.model_id, "Flashforge Adventurer 4 Series")

            connected_evt = threading.Event()
            msg_evt = threading.Event()
            received_msgs: list[tuple[str, dict]] = []

            agent.set_on_local_connect_fn(lambda state, dev_id, msg: connected_evt.set())
            agent.set_on_local_message_fn(
                lambda dev_id, raw: (received_msgs.append((dev_id, json.loads(raw))), msg_evt.set())
            )
            self.assertEqual(
                agent.connect_printer(SimpleNamespace(dev_id="ff_dev_1", host="192.168.1.77")),
                BAMBU_NETWORK_SUCCESS,
            )
            self.assertTrue(connected_evt.wait(timeout=2.0))
            self.assertTrue(msg_evt.wait(timeout=2.0))
            self.assertEqual(received_msgs[0][0], "ff_dev_1")
            self.assertEqual(received_msgs[0][1]["print"]["gcode_state"], "IDLE")
            self.assertEqual(agent.disconnect_printer(), BAMBU_NETWORK_SUCCESS)

    def test_commands_and_gcode_line_translation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )
            agent = PrinterAgentMixin(controller_provider=lambda: ctrl)
            acks: list[dict] = []
            agent.set_on_local_message_fn(lambda dev_id, raw: acks.append(json.loads(raw)))

            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps({"print": {"command": "pause"}}), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.pause_print.assert_called_once()

            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps({"print": {"command": "resume"}}), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.resume_print.assert_called_once()

            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps({"print": {"command": "stop"}}), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.cancel_print.assert_called_once()

            self.assertEqual(agent.command_set_nozzle("dev", 215, 1, True), BAMBU_NETWORK_SUCCESS)
            mock_client.set_nozzle_temp.assert_called_with(215)

            self.assertEqual(agent.command_set_bed("dev", 60, False, 2, True), BAMBU_NETWORK_SUCCESS)
            mock_client.set_bed_temp.assert_called_with(60)

            gcode_req = {
                "print": {
                    "command": "gcode_line",
                    "sequence_id": "42",
                    "param": "M104 S220\nM140 S65\nM663 S110\n",
                }
            }
            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps(gcode_req), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.set_nozzle_temp.assert_called_with(220)
            mock_client.set_bed_temp.assert_called_with(65)
            mock_client.set_print_speed.assert_called_with(110)
            self.assertEqual(acks[-1]["print"]["sequence_id"], "42")
            self.assertEqual(acks[-1]["print"]["result"], "success")

            led_req = {
                "system": {
                    "command": "ledctrl",
                    "led_node": "chamber_light",
                    "led_mode": "on",
                }
            }
            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps(led_req), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.set_led.assert_called_with(True)
            self.assertEqual(
                acks[-1]["print"]["lights_report"],
                [{"node": "chamber_light", "mode": "on"}],
            )

            # Speed meter (print_speed level 3 -> 124%)
            speed_req = {"print": {"command": "print_speed", "param": "3"}}
            self.assertEqual(
                agent.send_message_to_printer("dev", json.dumps(speed_req), 0, 0),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.set_print_speed.assert_called_with(124)
            self.assertEqual(acks[-1]["print"]["spd_lvl"], 3)
            self.assertEqual(acks[-1]["print"]["spd_mag"], 124)

            # Fan via M106 / M107 gcode_line
            self.assertEqual(
                agent.send_message_to_printer(
                    "dev",
                    json.dumps({"print": {"command": "gcode_line", "param": "M106 P1 S255\n"}}),
                    0,
                    0,
                ),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.set_fan.assert_called_with(True)

            self.assertEqual(
                agent.send_message_to_printer(
                    "dev",
                    json.dumps({"print": {"command": "gcode_line", "param": "M106 P1 S0\n"}}),
                    0,
                    0,
                ),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.set_fan.assert_called_with(False)

            # Home & Axis control allowed only when READY
            mock_client.get_status.return_value = {"machine_status": "READY"}
            self.assertEqual(
                agent.command_go_home("dev", False, False, 10, True),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.home_axes.assert_called_with("")

            self.assertEqual(
                agent.send_message_to_printer(
                    "dev",
                    json.dumps({"print": {"command": "gcode_line", "param": "G28 X Y\n"}}),
                    0,
                    0,
                ),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.home_axes.assert_called_with("X Y")

            self.assertEqual(
                agent.command_axis_control("dev", "Z", 10.0, 1.0, 1200, False, False, 11, True),
                BAMBU_NETWORK_SUCCESS,
            )
            mock_client.move_axis_relative.assert_called_once_with("Z", 10.0, feedrate=1200)

            # Extruder axis 'E' is blocked for safety
            self.assertEqual(
                agent.command_axis_control("dev", "E", 10.0, 1.0, 300, False, False, 12, True),
                ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE,
            )

            # Motion blocked when printing
            self.assertEqual(
                agent.command_go_home("dev", True, False, 13, True),
                ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE,
            )
            mock_client.get_status.return_value = {"machine_status": "BUILDING_FROM_SD"}
            self.assertEqual(
                agent.command_axis_control("dev", "X", 10.0, 1.0, 3000, False, False, 14, True),
                ORCA_NETWORK_ERR_CAP_NOT_AVAILABLE,
            )

    def test_start_local_print_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            gcode_path = root / "model.gcode"
            gcode_path.write_bytes(b"G28\nG1 X10 Y10\n")

            mock_client = MagicMock(spec=FlashforgeClient)

            def _fake_send_file(filename, data, start_print=False, progress_callback=None, cancel_check=None):
                if progress_callback:
                    progress_callback(len(data), len(data))
                return "model.gx"

            mock_client.send_file.side_effect = _fake_send_file
            ctrl = BridgeController(
                config=_sample_config(root / "snaps"),
                base_dir=root,
                client=mock_client,
                start_background=False,
            )
            agent = PrinterAgentMixin(controller_provider=lambda: ctrl)

            stages: list[tuple[int, int]] = []
            params = SimpleNamespace(
                filename=str(gcode_path),
                dst_file="",
                plate_index=1,
                project_name="model",
            )
            rc = agent.start_local_print(
                params,
                update_fn=lambda stage, pct, msg: stages.append((stage, pct)),
                cancel_fn=lambda: False,
            )
            self.assertEqual(rc, BAMBU_NETWORK_SUCCESS)
            self.assertTrue(any(s == PRINTING_STAGE_UPLOAD and p == 100 for s, p in stages))
            self.assertEqual(stages[-1], (PRINTING_STAGE_FINISHED, 100))

            # Pre-cancelled
            rc_cancel = agent.start_local_print(params, None, lambda: True)
            self.assertEqual(rc_cancel, BAMBU_NETWORK_ERR_CANCELED)

            # Mid-transfer cancellation
            mock_client.send_file.side_effect = FlashforgeTransferCancelledError("cancelled")
            rc_mid = agent.start_send_gcode_to_sdcard(params, None, lambda: False, None)
            self.assertEqual(rc_mid, BAMBU_NETWORK_ERR_CANCELED)

            # Missing file
            rc_missing = agent.start_local_print(
                SimpleNamespace(filename=str(root / "nope.gcode"), dst_file="", plate_index=1, project_name=""),
                None,
                None,
            )
            self.assertEqual(rc_missing, BAMBU_NETWORK_ERR_FILE_NOT_EXIST)

    def test_registers_printer_agent_when_orca_printer_agent_present(self) -> None:
        registered: list[type] = []

        class _Base:
            pass

        class _ScriptBase:
            pass

        class _PagesBase:
            def post_message(self, data: dict) -> None:
                pass

        class _PrinterAgentBase:
            def __init__(self) -> None:
                pass

        class _AgentInfo:
            def __init__(self, id_: str, name: str, version: str, description: str) -> None:
                self.id = id_
                self.name = name
                self.version = version
                self.description = description

        fake_orca = types.ModuleType("orca")
        fake_orca.base = _Base
        fake_orca.plugin = lambda cls: cls
        fake_orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=_ScriptBase)
        fake_orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=_PagesBase)
        fake_orca.printer_agent = types.SimpleNamespace(
            PrinterAgentBase=_PrinterAgentBase,
            AgentInfo=_AgentInfo,
            CameraStreamMode=SimpleNamespace(HTTP=1),
            FilamentSyncMode=SimpleNamespace(None_=0),
        )
        fake_orca.register_capability = lambda cap_cls: registered.append(cap_cls)
        fake_orca.request_permissions = lambda **kwargs: None

        orig_orca = sys.modules.get("orca")
        sys.modules["orca"] = fake_orca
        try:
            reloaded = importlib.reload(orca_plugin_mod)
            orig_start = reloaded._start_bridge
            reloaded._start_bridge = lambda *a, **kw: (None, "mocked")
            try:
                pkg = reloaded.FlashforgeAdv4BridgePlugin()
                pkg.register_capabilities()
            finally:
                reloaded._start_bridge = orig_start

            self.assertIn(reloaded.FlashforgePrinterAgent, registered)
            agent_instance = reloaded.FlashforgePrinterAgent()
            info = agent_instance.get_agent_info()
            self.assertEqual(info.id, AGENT_ID)
            self.assertEqual(info.name, AGENT_NAME)
        finally:
            if orig_orca is not None:
                sys.modules["orca"] = orig_orca
            else:
                sys.modules.pop("orca", None)
            importlib.reload(orca_plugin_mod)

    def test_extract_3mf_enforces_streaming_max_bytes_and_invalid_command_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / "big.3mf"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("Metadata/plate_1.gcode", b"G1 X1 Y1\n" * 100)

            with self.assertRaises(ValueError):
                extract_gcode_from_file(archive, plate_index=1, max_bytes=32)

        mock_ctrl = MagicMock(spec=BridgeController)
        agent = PrinterAgentMixin(controller_provider=lambda: mock_ctrl)
        self.assertEqual(
            agent.command_set_nozzle("dev1", "not-a-number", 1, True),  # type: ignore[arg-type]
            BAMBU_NETWORK_ERR_INVALID_RESULT,
        )
        self.assertEqual(
            agent.command_set_bed("dev1", None, True, 1, True),  # type: ignore[arg-type]
            BAMBU_NETWORK_ERR_INVALID_RESULT,
        )
        self.assertEqual(
            agent.command_axis_control("dev1", "X", 10.0, "bad", 3000, False, True, 1, True),  # type: ignore[arg-type]
            BAMBU_NETWORK_ERR_INVALID_RESULT,
        )


if __name__ == "__main__":
    unittest.main()
