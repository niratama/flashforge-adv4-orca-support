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

    def test_snapshot_data_url_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = Path(tmpdir) / "snaps"
            mock_client = MagicMock(spec=FlashforgeClient)
            ctrl = BridgeController(
                config=_sample_config(snap_dir),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )
            saved = ctrl.snapshot_manager.save_frame(b"\xff\xd8\xff\xe0test\xff\xd9")

            resp = ctrl.handle_message(
                {
                    "id": 11,
                    "action": "camera.snapshots.get",
                    "payload": {"filename": saved["filename"]},
                }
            )
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["status"], 200)
            self.assertTrue(resp["data"]["data_url"].startswith("data:image/jpeg;base64,"))

            resp_missing = ctrl.handle_message(
                {
                    "id": 12,
                    "action": "camera.snapshots.get",
                    "payload": {"filename": "missing.jpg"},
                }
            )
            self.assertFalse(resp_missing["ok"])
            self.assertEqual(resp_missing["status"], 404)

    def test_job_metadata_attached_only_when_current_file_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.send_file.return_value = "part.gx"
            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            gcode = (
                b"M104 S215\nM140 S60\n"
                b"; estimated printing time (normal mode) = 10m 23s\n"
                b"; filament used [mm] = 530.0\n"
                b"; layer_height = 0.2\n"
                b"; outer_wall_speed = 60\n"
            )
            code, _ = ctrl.upload_local_file("part.gcode", gcode, should_print=True)
            self.assertEqual(code, 201)

            mock_client.get_full_status.return_value = {
                "connected": True,
                "machine_status": "BUILDING_FROM_SD",
                "current_file": "0:/user/part.gx",
                "temperatures": {},
                "progress": {"is_printing": True, "bytes_printed": 500, "bytes_total": 1000, "percent": 50.0},
            }
            status = ctrl.get_printer_status()
            self.assertIsNotNone(status["job_metadata"])
            self.assertEqual(status["job_metadata"]["print_time"], 623)
            self.assertEqual(status["job_metadata"]["filament_0_usage"], 530)
            self.assertEqual(status["job_metadata"]["layer_height"], 200)
            self.assertEqual(status["job_metadata"]["print_speed"], 60)
            self.assertTrue(status["job_metadata"]["thumbnail_data_url"].startswith("data:image/bmp;base64,"))

            # When printing another file started directly on the printer, job_metadata is None
            mock_client.get_full_status.return_value["current_file"] = "other_local.gx"
            status_other = ctrl.get_printer_status()
            self.assertIsNone(status_other["job_metadata"])
            self.assertEqual(status_other["current_file"], "other_local.gx")
            self.assertEqual(status_other["progress"]["bytes_printed"], 500)

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

    def test_storage_files_thumbnail_and_reprint_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.list_files.return_value = [
                {"path": "/data/benchy.gx", "name": "benchy.gx"},
                {"path": "/data/テスト.gx", "name": "テスト.gx"},
            ]
            mock_client.get_file_thumbnail.return_value = b"BM\x00\x01\x02\x03"
            mock_client.start_existing_print.return_value = "benchy.gx"
            mock_client.get_status.return_value = {"machine_status": "READY"}

            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            res_list = ctrl.handle_message({"id": 10, "action": "printer.files.list"})
            self.assertTrue(res_list["ok"])
            self.assertEqual(len(res_list["data"]["files"]), 2)
            self.assertEqual(res_list["data"]["files"][0]["name"], "benchy.gx")
            self.assertEqual(res_list["data"]["files"][1]["path"], "/data/テスト.gx")

            res_thumb = ctrl.handle_message(
                {"id": 11, "action": "printer.files.thumbnail", "payload": {"path": "/data/benchy.gx"}}
            )
            self.assertTrue(res_thumb["ok"])
            self.assertEqual(res_thumb["data"]["path"], "/data/benchy.gx")
            self.assertTrue(res_thumb["data"]["data_url"].startswith("data:image/bmp;base64,"))

            # Second request hits cache without calling client again
            res_thumb2 = ctrl.handle_message(
                {"id": 12, "action": "printer.files.thumbnail", "payload": {"path": "benchy.gx"}}
            )
            self.assertTrue(res_thumb2["ok"])
            self.assertEqual(res_thumb2["data"]["data_url"], res_thumb["data"]["data_url"])
            mock_client.get_file_thumbnail.assert_called_once()

            # Missing thumbnail returns 404
            mock_client.get_file_thumbnail.return_value = None
            res_missing = ctrl.handle_message(
                {"id": 120, "action": "printer.files.thumbnail", "payload": {"path": "/data/none.gx"}}
            )
            self.assertFalse(res_missing["ok"])
            self.assertEqual(res_missing["status"], 404)

            # Traversal path rejected with 400
            res_bad = ctrl.handle_message(
                {"id": 13, "action": "printer.files.thumbnail", "payload": {"path": "../secret.gx"}}
            )
            self.assertFalse(res_bad["ok"])
            self.assertEqual(res_bad["status"], 400)

            # Reprint allowed when READY
            res_print = ctrl.handle_message(
                {"id": 14, "action": "printer.files.print", "payload": {"filename": "/data/benchy.gx"}}
            )
            self.assertTrue(res_print["ok"])
            self.assertEqual(res_print["data"]["filename"], "benchy.gx")
            mock_client.start_existing_print.assert_called_once_with("/data/benchy.gx")

            # Reprint blocked with 409 when busy
            mock_client.get_status.return_value = {"machine_status": "BUILDING_FROM_SD"}
            res_busy = ctrl.handle_message(
                {"id": 15, "action": "printer.files.print", "payload": {"filename": "/data/benchy.gx"}}
            )
            self.assertFalse(res_busy["ok"])
            self.assertEqual(res_busy["status"], 409)

    def test_safety_controls_and_position_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_client = MagicMock(spec=FlashforgeClient)
            mock_client.get_status.return_value = {"machine_status": "READY"}
            mock_client.get_position.return_value = {"x": 10.0, "y": 20.0, "z": 5.5, "e": 0.0}

            ctrl = BridgeController(
                config=_sample_config(Path(tmpdir) / "snaps"),
                base_dir=Path(tmpdir),
                client=mock_client,
                start_background=False,
            )

            res_pos = ctrl.handle_message({"id": 20, "action": "printer.position"})
            self.assertTrue(res_pos["ok"])
            self.assertEqual(res_pos["data"]["position"]["z"], 5.5)

            res_off = ctrl.handle_message(
                {"id": 21, "action": "printer.steppers", "payload": {"enabled": False}}
            )
            self.assertTrue(res_off["ok"])
            mock_client.disable_steppers.assert_called_once()

            res_on = ctrl.handle_message(
                {"id": 22, "action": "printer.steppers", "payload": {"enabled": True}}
            )
            self.assertTrue(res_on["ok"])
            mock_client.enable_steppers.assert_called_once()

            # Steppers blocked with 409 when printing, but emergency stop always allowed
            mock_client.get_status.return_value = {"machine_status": "BUILDING_FROM_SD"}
            res_blocked = ctrl.handle_message(
                {"id": 23, "action": "printer.steppers", "payload": {"enabled": False}}
            )
            self.assertFalse(res_blocked["ok"])
            self.assertEqual(res_blocked["status"], 409)

            res_estop = ctrl.handle_message({"id": 24, "action": "printer.emergency_stop"})
            self.assertTrue(res_estop["ok"])
            mock_client.emergency_stop.assert_called_once()

    def test_gx_wrapping_and_filename_sanitization(self) -> None:
        gx = FlashforgeClient.wrap_gcode_as_gx(b"G28\nM104 S200\n")
        self.assertTrue(gx.startswith(b"xgcode 1.0\n\x00"))
        self.assertTrue(gx.endswith(b"G28\nM104 S200\n"))
        self.assertEqual(FlashforgeClient.wrap_gcode_as_gx(gx), gx)
        self.assertEqual(FlashforgeClient.sanitize_filename("dir/my test.gcode"), "my_test.gcode")
        self.assertEqual(FlashforgeClient.sanitize_filename("   "), "print_job.gcode")


if __name__ == "__main__":
    unittest.main()
