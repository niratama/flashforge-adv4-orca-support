"""Unit tests for app.py Web UI and OctoPrint-compatible endpoints."""

import io
import os
import unittest
from unittest.mock import MagicMock, patch

from app import create_app, load_config, parse_bool_flag
from flashforge_client import FlashforgeClient, FlashforgeError


class TestAppEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_client = MagicMock(spec=FlashforgeClient)
        self.test_config = {
            "printer": {
                "ip": "192.168.1.100",
                "port": 8899,
                "camera_url": "http://192.168.1.100:8080/?action=stream",
                "timeout": 5.0,
                "transfer_timeout": 30.0,
            },
            "server": {"host": "127.0.0.1", "port": 5000, "debug": False},
        }
        self.app = create_app(config=self.test_config, client=self.mock_client)
        self.client = self.app.test_client()

    def test_index_renders_dashboard(self) -> None:
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("192.168.1.100", body)
        self.assertIn("http://192.168.1.100:8080/?action=stream", body)

    def test_octoprint_version(self) -> None:
        resp = self.client.get("/api/version")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json(),
            {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0"},
        )

    def test_octoprint_settings(self) -> None:
        resp = self.client.get("/api/settings")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["api"]["allowCrossOrigin"])
        self.assertTrue(data["feature"]["sdSupport"])
        self.assertEqual(
            data["webcam"]["streamUrl"],
            "http://192.168.1.100:8080/?action=stream",
        )

    def test_printer_status_connected(self) -> None:
        expected = {
            "connected": True,
            "machine_status": "BUILDING_FROM_SD",
            "move_mode": "MOVING",
            "led": True,
            "current_file": "benchy.gx",
            "temperatures": {
                "nozzle": {"actual": 210, "target": 210},
                "bed": {"actual": 60, "target": 60},
            },
            "progress": {
                "is_printing": True,
                "bytes_printed": 500,
                "bytes_total": 1000,
                "percent": 50.0,
            },
        }
        self.mock_client.get_full_status.return_value = expected

        resp = self.client.get("/api/printer/status")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), expected)

    def test_printer_status_disconnected_returns_200(self) -> None:
        self.mock_client.get_full_status.side_effect = FlashforgeError("timed out")

        resp = self.client.get("/api/printer/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["connected"])
        self.assertEqual(data["machine_status"], "DISCONNECTED")
        self.assertIn("timed out", data["error"])

    def test_printer_info_success_and_error(self) -> None:
        self.mock_client.get_info.return_value = {"machine_type": "Adventurer 4"}
        resp = self.client.get("/api/printer/info")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.get_json(),
            {"connected": True, "info": {"machine_type": "Adventurer 4"}},
        )

        self.mock_client.get_info.side_effect = FlashforgeError("offline")
        resp_err = self.client.get("/api/printer/info")
        self.assertEqual(resp_err.status_code, 502)
        self.assertFalse(resp_err.get_json()["connected"])

    def test_control_endpoints(self) -> None:
        for action, method_name in [
            ("pause", "pause_print"),
            ("resume", "resume_print"),
            ("cancel", "cancel_print"),
        ]:
            resp = self.client.post(f"/api/printer/{action}")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.get_json(), {"ok": True, "action": action})
            getattr(self.mock_client, method_name).assert_called_once()

            getattr(self.mock_client, method_name).side_effect = FlashforgeError("fail")
            resp_err = self.client.post(f"/api/printer/{action}")
            self.assertEqual(resp_err.status_code, 502)
            self.assertFalse(resp_err.get_json()["ok"])

    def test_led_endpoint(self) -> None:
        resp = self.client.post("/api/printer/led", json={"on": False})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "led": False})
        self.mock_client.set_led.assert_called_once_with(False)

        self.mock_client.set_led.side_effect = FlashforgeError("led error")
        resp_err = self.client.post("/api/printer/led", json={"on": True})
        self.assertEqual(resp_err.status_code, 502)
        self.assertFalse(resp_err.get_json()["ok"])

    def test_octoprint_upload_local_missing_file(self) -> None:
        resp = self.client.post("/api/files/local", data={})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.get_json())

    def test_octoprint_upload_local_with_print(self) -> None:
        gcode = b"G28\nG1 X10 Y10\n"
        self.mock_client.send_file.return_value = "test_print.gcode"

        resp = self.client.post(
            "/api/files/local",
            data={
                "file": (io.BytesIO(gcode), "test_print.gcode"),
                "print": "true",
            },
            content_type="multipart/form-data",
        )

        self.assertEqual(resp.status_code, 201)
        self.mock_client.send_file.assert_called_once_with(
            filename="test_print.gcode",
            data=gcode,
            start_print=True,
        )
        data = resp.get_json()
        self.assertTrue(data["done"])
        self.assertEqual(
            data["files"]["local"],
            {
                "name": "test_print.gcode",
                "origin": "local",
                "path": "test_print.gcode",
                "size": len(gcode),
            },
        )

    def test_octoprint_upload_local_without_print_and_error(self) -> None:
        gcode = b"G28\n"
        self.mock_client.send_file.return_value = "upload_only.gcode"

        resp = self.client.post(
            "/api/files/local",
            data={"file": (io.BytesIO(gcode), "upload_only.gcode")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 201)
        self.mock_client.send_file.assert_called_once_with(
            filename="upload_only.gcode",
            data=gcode,
            start_print=False,
        )

        self.mock_client.send_file.side_effect = FlashforgeError("transfer failed")
        resp_err = self.client.post(
            "/api/files/local",
            data={"file": (io.BytesIO(gcode), "upload_only.gcode")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp_err.status_code, 502)
        self.assertIn("transfer failed", resp_err.get_json()["error"])

    def test_parse_bool_flag_and_load_config_env_overrides(self) -> None:
        self.assertTrue(parse_bool_flag("true"))
        self.assertTrue(parse_bool_flag("1"))
        self.assertFalse(parse_bool_flag("false"))
        self.assertFalse(parse_bool_flag(None))

        with patch.dict(
            os.environ,
            {
                "PRINTER_IP": "10.0.0.50",
                "PRINTER_PORT": "9999",
                "CAMERA_URL": "http://10.0.0.50:8081/stream",
                "SERVER_PORT": "5050",
            },
            clear=False,
        ):
            cfg = load_config()
            self.assertEqual(cfg["printer"]["ip"], "10.0.0.50")
            self.assertEqual(cfg["printer"]["port"], 9999)
            self.assertEqual(cfg["printer"]["camera_url"], "http://10.0.0.50:8081/stream")
            self.assertEqual(cfg["server"]["port"], 5050)


if __name__ == "__main__":
    unittest.main()
