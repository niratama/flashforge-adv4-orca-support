import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from bridge_controller import BridgeController, load_config, parse_bool_flag
from flashforge_client import FlashforgeClient, FlashforgeError


def _sample_config(snapshot_dir: Path) -> dict:
    return {
        "printer": {
            "ip": "192.168.1.50",
            "port": 8899,
            "timeout": 2.0,
            "transfer_timeout": 5.0,
            "camera_url": "http://192.168.1.50:8080/?action=stream",
        },
        "server": {"host": "127.0.0.1", "port": 5000},
        "camera": {
            "snapshot_dir": str(snapshot_dir),
            "snapshot_enabled": False,
        },
        "history": {"retention_hours": 1.0, "sample_interval_seconds": 5.0},
    }


class TestBridgeController(unittest.TestCase):
    def test_load_config_falls_back_to_example_and_parses_bool_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            example = root / "config-example.json"
            example.write_text(
                '{"printer": {"ip": "10.0.0.2", "port": 8899}, "server": {"port": 5000}}',
                encoding="utf-8",
            )
            cfg = load_config(search_dirs=(root,))
            self.assertEqual(cfg["printer"]["ip"], "10.0.0.2")

        self.assertTrue(parse_bool_flag("true"))
        self.assertTrue(parse_bool_flag(" 1 "))
        self.assertFalse(parse_bool_flag("false"))
        self.assertFalse(parse_bool_flag(None))

    def test_status_connected_and_disconnected(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.get_full_status.return_value = {
                "connected": True,
                "machine_status": "BUILDING_FROM_SD",
                "temperatures": {
                    "nozzle": {"actual": 205.0, "target": 210.0},
                    "bed": {"actual": 60.0, "target": 60.0},
                },
                "progress": {"is_printing": True, "percent": 42.0},
            }
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            status = ctrl.get_printer_status()
            self.assertTrue(status["connected"])
            self.assertIn("camera", status)

            mock_client.get_full_status.side_effect = FlashforgeError("timeout")
            disconnected = ctrl.get_printer_status()
            self.assertFalse(disconnected["connected"])
            self.assertEqual(disconnected["machine_status"], "DISCONNECTED")
            self.assertEqual(disconnected["error"], "timeout")

    def test_live_tuning_validation_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            code, data = ctrl.set_nozzle_temp(215)
            self.assertEqual(code, 200)
            self.assertEqual(data, {"ok": True, "target": 215})
            mock_client.set_nozzle_temp.assert_called_once_with(215)

            for bad in (None, True, "abc"):
                code, data = ctrl.set_bed_temp(bad)
                self.assertEqual(code, 400)
                self.assertFalse(data["ok"])

            mock_client.set_print_speed.side_effect = FlashforgeError("busy")
            code, data = ctrl.set_print_speed(120)
            self.assertEqual(code, 502)
            self.assertFalse(data["ok"])

    def test_upload_local_file_and_handle_message_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.send_file.return_value = "part.gx"
            mock_client.get_full_status.return_value = {
                "connected": True,
                "machine_status": "READY",
                "temperatures": {},
                "progress": {},
            }
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            code, data = ctrl.upload_local_file("", b"G28", should_print=True)
            self.assertEqual(code, 400)

            code, data = ctrl.upload_local_file("part.gcode", b"G28\n", should_print=True)
            self.assertEqual(code, 201)
            self.assertEqual(data["files"]["local"]["name"], "part.gx")

            # Pattern B Pages RPC dispatch
            resp = ctrl.handle_message({"id": 7, "action": "ready"})
            self.assertEqual(resp["id"], 7)
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["status"], 200)
            self.assertTrue(resp["data"]["connected"])

            resp_unknown = ctrl.handle_message({"id": 8, "action": "nope"})
            self.assertFalse(resp_unknown["ok"])
            self.assertEqual(resp_unknown["status"], 404)

    def test_ownership_closes_owned_client_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            injected = MagicMock(spec=FlashforgeClient)
            ctrl_injected = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=injected,
                start_background=False,
            )
            ctrl_injected.stop()
            injected.close.assert_not_called()

            ctrl_owned = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=None,
                start_background=False,
            )
            ctrl_owned.client = MagicMock(spec=FlashforgeClient)
            ctrl_owned.stop()
            ctrl_owned.client.close.assert_called_once()

    def test_gx_wrapping_and_filename_sanitization(self) -> None:
        gx = FlashforgeClient.wrap_gcode_as_gx(b"G28\nM104 S200\n")
        self.assertTrue(gx.startswith(b"xgcode 1.0\n\x00"))
        self.assertTrue(gx.endswith(b"G28\nM104 S200\n"))
        self.assertEqual(FlashforgeClient.wrap_gcode_as_gx(gx), gx)
        self.assertEqual(FlashforgeClient.sanitize_filename("dir/my test.gcode"), "my_test.gcode")
        self.assertEqual(FlashforgeClient.sanitize_filename("   "), "print_job.gcode")


if __name__ == "__main__":
    unittest.main()
