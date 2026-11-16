"""Unit tests for the zero-dependency OrcaSlicer plugin HTTP bridge server."""

import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

from flashforge_client import FlashforgeClient, FlashforgeError

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "orca_plugin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "orca_plugin"))

from server import BridgeServer, render_dashboard_html  # noqa: E402
from scripts.build_orca_plugin import build_plugin  # noqa: E402


class TestPluginBridgeServer(unittest.TestCase):
    def setUp(self) -> None:
        self.mock_client = MagicMock(spec=FlashforgeClient)
        self.config = {
            "printer": {
                "ip": "192.168.1.100",
                "port": 8899,
                "camera_url": "http://192.168.1.100:8080/?action=stream",
                "timeout": 5.0,
                "transfer_timeout": 30.0,
            },
            "server": {"host": "127.0.0.1", "port": 0, "debug": False},
        }
        self.bridge = BridgeServer(config=self.config, client=self.mock_client)
        self.port = self.bridge.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.bridge.stop()

    def _get_json(self, path: str) -> tuple[int, dict]:
        req = urllib.request.Request(f"{self.base_url}{path}", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def _post_json(self, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"} if payload is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read().decode("utf-8"))

    def test_dashboard_and_octoprint_meta(self) -> None:
        with urllib.request.urlopen(f"{self.base_url}/", timeout=3) as resp:
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
            self.assertIn("192.168.1.100", body)
            self.assertIn("http://192.168.1.100:8080/?action=stream", body)

        status, ver = self._get_json("/api/version")
        self.assertEqual(status, 200)
        self.assertEqual(ver["text"], "OctoPrint 1.9.0")

        status, settings = self._get_json("/api/settings")
        self.assertEqual(status, 200)
        self.assertTrue(settings["api"]["allowCrossOrigin"])

    def test_status_and_controls(self) -> None:
        self.mock_client.get_full_status.return_value = {
            "connected": True,
            "machine_status": "READY",
        }
        status, data = self._get_json("/api/printer/status")
        self.assertEqual(status, 200)
        self.assertTrue(data["connected"])

        self.mock_client.get_full_status.side_effect = FlashforgeError("offline")
        status, data = self._get_json("/api/printer/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["connected"])
        self.assertEqual(data["machine_status"], "DISCONNECTED")

        status, pause_resp = self._post_json("/api/printer/pause")
        self.assertEqual(status, 200)
        self.assertEqual(pause_resp, {"ok": True, "action": "pause"})

        status, led_resp = self._post_json("/api/printer/led", {"on": False})
        self.assertEqual(status, 200)
        self.assertEqual(led_resp, {"ok": True, "led": False})
        self.mock_client.set_led.assert_called_once_with(False)

    def test_multipart_upload_and_error(self) -> None:
        boundary = "----TestBoundary123"
        gcode = b"G28\nG1 X10 Y10\n"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="print"\r\n\r\ntrue\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="cube.gcode"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8") + gcode + f"\r\n--{boundary}--\r\n".encode("utf-8")

        self.mock_client.send_file.return_value = "cube.gx"
        req = urllib.request.Request(
            f"{self.base_url}/api/files/local",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            self.assertEqual(resp.status, 201)
            result = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(result["done"])
            self.assertEqual(result["files"]["local"]["name"], "cube.gx")
            self.mock_client.send_file.assert_called_once_with(
                filename="cube.gcode", data=gcode, start_print=True
            )

        bad_req = urllib.request.Request(
            f"{self.base_url}/api/files/local",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad_req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)

    def test_build_plugin_creates_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            out = build_plugin(Path(tmpdir) / "flashforge_adv4_bridge")
            self.assertEqual([p.name for p in out.glob("*.py")], ["plugin.py"])
            self.assertTrue((out / "plugin.py").is_file())
            self.assertTrue((out / "orca-plugin.json").is_file())
            self.assertTrue((out / "lib" / "server.py").is_file())
            self.assertTrue((out / "lib" / "bridge_controller.py").is_file())
            self.assertTrue((out / "lib" / "bridge_services.py").is_file())
            self.assertTrue((out / "lib" / "flashforge_client.py").is_file())
            self.assertTrue((out / "templates" / "index.html").is_file())
            self.assertTrue((out / "config-example.json").is_file())

    def test_stop_does_not_hang_with_open_camera_stream(self) -> None:
        import time

        self.assertTrue(self.bridge._httpd.daemon_threads)
        self.assertFalse(self.bridge._httpd.block_on_close)

        resp = urllib.request.urlopen(f"{self.base_url}/api/camera/stream", timeout=2)
        try:
            start = time.monotonic()
            self.bridge.stop()
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 2.0)
        finally:
            resp.close()

    def test_security_cors_origin_api_key_limits_and_html_escape(self) -> None:
        # No wildcard CORS header
        with urllib.request.urlopen(f"{self.base_url}/api/version", timeout=3) as resp:
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

        # Cross-origin POST rejected with 403
        cross_req = urllib.request.Request(
            f"{self.base_url}/api/printer/pause",
            data=b"",
            method="POST",
            headers={"Origin": "https://evil.example"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(cross_req, timeout=3)
        self.assertEqual(ctx.exception.code, 403)

        # Invalid JSON on /api/printer/led returns 400
        bad_json_req = urllib.request.Request(
            f"{self.base_url}/api/printer/led",
            data=b"{bad",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(bad_json_req, timeout=3)
        self.assertEqual(ctx.exception.code, 400)

        # render_dashboard_html escapes <script> breakout characters
        rendered = render_dashboard_html(
            "<script>const u = {{ camera_url | tojson }};</script>",
            camera_url="http://127.0.0.1:8080/?x=</script><script>alert(1)</script>",
            printer_ip="127.0.0.1",
        )
        self.assertNotIn("</script><script>", rendered)
        self.assertIn("\\u003c/script\\u003e", rendered)

        # API key + upload limit enforcement
        secured_cfg = {
            "printer": self.config["printer"],
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "api_key": "plugin-secret",
                "max_upload_bytes": 16,
            },
        }
        secured = BridgeServer(config=secured_cfg, client=self.mock_client, start_background=False)
        s_port = secured.start()
        try:
            s_url = f"http://127.0.0.1:{s_port}"
            unauth_req = urllib.request.Request(f"{s_url}/api/printer/pause", data=b"", method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(unauth_req, timeout=3)
            self.assertEqual(ctx.exception.code, 401)

            auth_req = urllib.request.Request(
                f"{s_url}/api/printer/pause",
                data=b"",
                method="POST",
                headers={"X-Api-Key": "plugin-secret"},
            )
            with urllib.request.urlopen(auth_req, timeout=3) as resp:
                self.assertEqual(resp.status, 200)

            big_req = urllib.request.Request(
                f"{s_url}/api/files/local",
                data=b"A" * 64,
                method="POST",
                headers={"X-Api-Key": "plugin-secret"},
            )
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(big_req, timeout=3)
            self.assertEqual(ctx.exception.code, 413)
        finally:
            secured.stop()


class TestPluginConfigUI(unittest.TestCase):
    def test_parse_and_merge_orca_config(self) -> None:
        import plugin as plugin_mod

        self.assertEqual(plugin_mod.parse_orca_config_json(None), {})
        self.assertEqual(plugin_mod.parse_orca_config_json(""), {})
        self.assertEqual(plugin_mod.parse_orca_config_json("not-json"), {})
        self.assertEqual(
            plugin_mod.parse_orca_config_json('{"printer": {"ip": "10.0.0.5"}}'),
            {"printer": {"ip": "10.0.0.5"}},
        )

        base = {
            "printer": {"ip": "192.168.1.100", "port": 8899},
            "server": {"host": "127.0.0.1", "port": 5000},
        }
        merged = plugin_mod.resolve_effective_config(
            orca_config={"printer": {"ip": "192.168.1.250"}},
            base_config=base,
        )
        self.assertEqual(merged["printer"]["ip"], "192.168.1.250")
        self.assertEqual(merged["printer"]["port"], 8899)
        self.assertEqual(merged["server"]["port"], 5000)

    def test_build_config_ui_html(self) -> None:
        import plugin as plugin_mod

        html_out = plugin_mod.build_config_ui_html(
            {
                "printer": {"ip": "192.168.1.100</script>", "port": 8899},
                "server": {"host": "127.0.0.1", "port": 5000, "debug": False},
            }
        )
        self.assertIn("window.orca.getConfig", html_out)
        self.assertIn("window.orca.saveConfig", html_out)
        self.assertIn('id="printer_ip"', html_out)
        self.assertIn('id="server_port"', html_out)
        self.assertIn('id="camera_snapshot_enabled"', html_out)
        self.assertIn('id="history_temperature_hours"', html_out)
        self.assertIn("\\u003c/script\\u003e", html_out)


if __name__ == "__main__":
    unittest.main()
