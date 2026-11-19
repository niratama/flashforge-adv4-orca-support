"""Unit tests for the OrcaSlicer Pages capability (Phase 1 Pattern B)."""

import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "orca_plugin") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "orca_plugin"))

import plugin as orca_plugin_mod  # noqa: E402
from server import render_dashboard_html  # noqa: E402


class _FakePagesCapability(orca_plugin_mod.PagesBridgeMixin):
    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post_message(self, data: dict) -> None:
        self.posted.append(data)


class TestPluginPagesCapability(unittest.TestCase):
    def test_render_dashboard_html_embeds_http_base_url(self) -> None:
        tpl = (REPO_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        rendered = render_dashboard_html(
            tpl,
            camera_url="http://192.168.1.55:8080/?action=stream",
            printer_ip="192.168.1.55",
            http_base_url="http://127.0.0.1:5123",
        )
        self.assertIn('const HTTP_BASE_URL = "http://127.0.0.1:5123";', rendered)
        self.assertIn("BridgeTransport.loadSnapshotImage", rendered)
        self.assertNotIn('<img id="camera-img" src="/api/camera/stream"', rendered)

    def test_pages_mixin_get_ui_and_on_message_with_mock_bridge(self) -> None:
        mock_bridge = MagicMock()
        mock_bridge.is_running = True
        mock_bridge.port = 5432
        mock_bridge.controller.camera_url = "http://192.168.1.55:8080/?action=stream"
        mock_bridge.controller.printer_ip = "192.168.1.55"
        mock_bridge.controller.handle_message.return_value = {
            "id": 42,
            "action": "printer.status",
            "ok": True,
            "status": 200,
            "data": {"connected": True},
        }

        prev = orca_plugin_mod._bridge_instance
        orca_plugin_mod._bridge_instance = mock_bridge
        try:
            cap = _FakePagesCapability()
            self.assertEqual(cap.get_name(), "Adventurer 4")
            html = cap.get_ui()
            self.assertIn('const HTTP_BASE_URL = "http://127.0.0.1:5432";', html)

            cap.on_message({"id": 42, "action": "printer.status"})
            mock_bridge.controller.handle_message.assert_called_once_with(
                {"id": 42, "action": "printer.status"}
            )
            self.assertEqual(len(cap.posted), 1)
            self.assertEqual(cap.posted[0]["id"], 42)
            self.assertTrue(cap.posted[0]["ok"])

            # Non-dict messages are ignored safely
            cap.on_message("invalid")
            self.assertEqual(len(cap.posted), 1)
        finally:
            orca_plugin_mod._bridge_instance = prev

    def test_pages_mixin_on_message_when_bridge_unavailable(self) -> None:
        cap = _FakePagesCapability()
        cap._get_controller = lambda: None  # type: ignore[method-assign]
        cap.on_message({"id": 9, "action": "printer.status"})
        self.assertEqual(len(cap.posted), 1)
        self.assertFalse(cap.posted[0]["ok"])
        self.assertEqual(cap.posted[0]["status"], 503)

    def test_start_bridge_respects_server_enabled_false(self) -> None:
        import urllib.error
        import urllib.request

        prev_bridge = orca_plugin_mod._bridge_instance
        prev_cam = orca_plugin_mod._camera_server_instance
        prev_ctrl = orca_plugin_mod._controller_instance
        prev_cfg = dict(orca_plugin_mod._cached_orca_config)
        orca_plugin_mod._bridge_instance = None
        orca_plugin_mod._camera_server_instance = None
        orca_plugin_mod._controller_instance = None
        orca_plugin_mod._cached_orca_config = {}
        try:
            bridge, info = orca_plugin_mod._start_bridge({"server": {"enabled": False}})
            self.assertIsNone(bridge)
            self.assertIn("disabled", info)
            self.assertIsNotNone(orca_plugin_mod._controller_instance)
            self.assertIsNotNone(orca_plugin_mod._camera_server_instance)
            self.assertTrue(orca_plugin_mod._camera_server_instance.is_running)

            cam_port = orca_plugin_mod._camera_server_instance.port
            self.assertGreater(cam_port, 0)
            self.assertEqual(
                orca_plugin_mod._resolve_camera_url_for_agent(),
                f"http://127.0.0.1:{cam_port}/api/camera/stream",
            )

            cap = _FakePagesCapability()
            html = cap.get_ui()
            self.assertIn(f'const HTTP_BASE_URL = "http://127.0.0.1:{cam_port}";', html)

            # CameraRelayServer must NOT expose OctoPrint / Web UI endpoints
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"http://127.0.0.1:{cam_port}/api/version", timeout=2)
            self.assertEqual(ctx.exception.code, 404)
        finally:
            orca_plugin_mod._stop_bridge()
            orca_plugin_mod._bridge_instance = prev_bridge
            orca_plugin_mod._camera_server_instance = prev_cam
            orca_plugin_mod._controller_instance = prev_ctrl
            orca_plugin_mod._cached_orca_config = prev_cfg

    def test_registers_pages_capability_when_orca_pages_is_present(self) -> None:
        registered: list[type] = []

        class _Base:
            pass

        class _ScriptBase:
            pass

        class _PagesBase:
            def post_message(self, data: dict) -> None:
                pass

        fake_orca = types.ModuleType("orca")
        fake_orca.base = _Base
        fake_orca.plugin = lambda cls: cls
        fake_orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=_ScriptBase)
        fake_orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=_PagesBase)
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

            self.assertIn(reloaded.FlashforgeBridgeCapability, registered)
            self.assertIn(reloaded.FlashforgePagesCapability, registered)
            self.assertTrue(issubclass(reloaded.FlashforgePagesCapability, _PagesBase))
        finally:
            if orig_orca is not None:
                sys.modules["orca"] = orig_orca
            else:
                sys.modules.pop("orca", None)
            importlib.reload(orca_plugin_mod)


if __name__ == "__main__":
    unittest.main()
