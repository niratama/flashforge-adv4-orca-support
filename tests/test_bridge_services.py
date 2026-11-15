"""Tests for bridge_services (TemperatureHistory, SnapshotManager, CameraRelay) and endpoints."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock

from app import create_app
from bridge_services import CameraRelay, SnapshotManager, TemperatureHistory
from orca_plugin.server import BridgeServer


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.closed = False

    def read(self, _n: int = 8192) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def close(self) -> None:
        self.closed = True


class TestBridgeServices(unittest.TestCase):
    def test_temperature_history_records_throttles_and_prunes(self) -> None:
        hist = TemperatureHistory(retention_hours=1.0, sample_interval_seconds=5.0)
        connected_status = {
            "connected": True,
            "temperatures": {
                "nozzle": {"actual": 205.2, "target": 210.0},
                "bed": {"actual": 59.8, "target": 60.0},
            },
        }
        disconnected_status = {
            "connected": False,
            "temperatures": {
                "nozzle": {"actual": None, "target": None},
                "bed": {"actual": None, "target": None},
            },
        }

        self.assertFalse(hist.record(disconnected_status, now=1000.0))
        self.assertTrue(hist.record(connected_status, now=1000.0))
        # Within 5s interval -> throttled
        self.assertFalse(hist.record(connected_status, now=1003.0))
        # After 5s -> recorded
        self.assertTrue(hist.record(connected_status, now=1006.0))
        self.assertEqual(len(hist.get_samples(now=1006.0)), 2)

        # After >1h (3600s) from t=1000 -> first sample pruned, second remains
        samples = hist.get_samples(now=4602.0)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["ts"], 1006000)

    def test_snapshot_manager_save_prune_and_path_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            mgr = SnapshotManager(snapshot_dir=Path(tmpdir), max_count=2)
            s1 = mgr.save_frame(b"\xff\xd8frame1\xff\xd9", timestamp=1700000001.100)
            s2 = mgr.save_frame(b"\xff\xd8frame2\xff\xd9", timestamp=1700000002.200)
            s3 = mgr.save_frame(b"\xff\xd8frame3\xff\xd9", timestamp=1700000003.300)

            listed = mgr.list_snapshots()
            self.assertEqual(len(listed), 2)
            self.assertEqual([item["filename"] for item in listed], [s3["filename"], s2["filename"]])
            self.assertIsNone(mgr.get_snapshot_path(s1["filename"]))
            self.assertIsNotNone(mgr.get_snapshot_path(s3["filename"]))
            self.assertIsNone(mgr.get_snapshot_path("../escape.jpg"))
            self.assertIsNone(mgr.get_snapshot_path("sub/file.jpg"))

    def test_camera_relay_parses_chunked_mjpeg_and_streams(self) -> None:
        jpeg1 = b"\xff\xd8\xff\xe0FRAME1\xff\xd9"
        jpeg2 = b"\xff\xd8\xff\xe0FRAME2\xff\xd9"
        raw_stream = (
            b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
            + jpeg1
            + b"\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n"
            + jpeg2
            + b"\r\n"
        )
        # Split across odd byte boundaries
        chunks = [raw_stream[i : i + 7] for i in range(0, len(raw_stream), 7)]
        calls = 0

        def fake_opener(_url: str, timeout: float = 5.0) -> _FakeStreamResponse:
            nonlocal calls
            calls += 1
            return _FakeStreamResponse(chunks if calls == 1 else [])

        relay = CameraRelay("http://127.0.0.1:8080/?action=stream", opener=fake_opener, idle_grace_seconds=0.2)
        try:
            frame = relay.get_latest_frame(wait_timeout=2.0)
            self.assertIn(frame, (jpeg1, jpeg2))

            gen = relay.iter_mjpeg_frames(frame_timeout=0.5)
            first_part = next(gen)
            self.assertIn(b"Content-Type: image/jpeg", first_part)
            self.assertTrue(first_part.startswith(b"--frame\r\n"))
            gen.close()
        finally:
            relay.stop()

    def test_flask_endpoints_for_history_and_snapshots(self) -> None:
        mock_client = MagicMock()
        mock_client.get_full_status.return_value = {
            "connected": True,
            "machine_status": "BUILDING_FROM_SD",
            "move_mode": "MOVING",
            "led": True,
            "current_file": "test.gx",
            "temperatures": {
                "nozzle": {"actual": 210.0, "target": 210.0},
                "bed": {"actual": 60.0, "target": 60.0},
            },
            "progress": {"is_printing": True, "bytes_printed": 50, "bytes_total": 100, "percent": 50.0},
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {
                "printer": {"ip": "192.168.1.100", "camera_url": "http://192.168.1.100:8080/?action=stream"},
                "server": {"host": "127.0.0.1", "port": 5000},
                "camera": {"snapshot_dir": tmpdir, "snapshot_max_count": 10},
                "history": {"temperature_hours": 4.0, "sample_interval_seconds": 1.0},
            }
            app = create_app(config=cfg, client=mock_client, start_background=False)
            services = app.config["BRIDGE_SERVICES"]
            services["camera_relay"]._publish_frame(b"\xff\xd8TESTJPEG\xff\xd9")

            client = app.test_client()
            # Trigger status poll -> records temperature history & includes camera relay status
            status_res = client.get("/api/printer/status")
            self.assertEqual(status_res.status_code, 200)
            status_json = status_res.get_json()
            self.assertIn("camera", status_json)
            self.assertTrue(status_json["camera"]["has_frame"])
            hist_res = client.get("/api/history/temperature")
            self.assertEqual(hist_res.status_code, 200)
            hist_data = hist_res.get_json()
            self.assertEqual(hist_data["retention_hours"], 4.0)
            self.assertEqual(len(hist_data["samples"]), 1)

            # Capture snapshot via POST
            cap_res = client.post("/api/camera/snapshots")
            self.assertEqual(cap_res.status_code, 201)
            snap_name = cap_res.get_json()["snapshot"]["filename"]

            # List and fetch snapshot
            list_res = client.get("/api/camera/snapshots")
            self.assertEqual(len(list_res.get_json()["snapshots"]), 1)
            img_res = client.get(f"/api/camera/snapshots/{snap_name}")
            self.assertEqual(img_res.status_code, 200)
            self.assertEqual(img_res.data, b"\xff\xd8TESTJPEG\xff\xd9")
            img_res.close()

            # Reconnect endpoint
            self.assertEqual(client.post("/api/camera/reconnect").status_code, 200)
            services["monitor"].stop()

    def test_plugin_server_endpoints_for_history_and_snapshots(self) -> None:
        mock_client = MagicMock()
        mock_client.get_full_status.return_value = {
            "connected": True,
            "machine_status": "READY",
            "move_mode": "READY",
            "led": False,
            "current_file": None,
            "temperatures": {
                "nozzle": {"actual": 25.0, "target": 0.0},
                "bed": {"actual": 24.0, "target": 0.0},
            },
            "progress": {"is_printing": False, "bytes_printed": 0, "bytes_total": 0, "percent": 0.0},
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = {
                "printer": {"ip": "127.0.0.1", "camera_url": "http://127.0.0.1:8080/?action=stream"},
                "server": {"host": "127.0.0.1", "port": 0},
                "camera": {"snapshot_dir": tmpdir, "snapshot_max_count": 10},
                "history": {"temperature_hours": 4.0, "sample_interval_seconds": 1.0},
            }
            server = BridgeServer(config=cfg, client=mock_client, start_background=False)
            port = server.start()
            try:
                server.camera_relay._publish_frame(b"\xff\xd8PLUGINJPEG\xff\xd9")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/printer/status", timeout=3) as resp:
                    self.assertEqual(resp.status, 200)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/history/temperature", timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    self.assertEqual(len(data["samples"]), 1)

                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/camera/snapshots",
                    data=b"",
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    self.assertEqual(resp.status, 201)
                    snap_name = json.loads(resp.read().decode("utf-8"))["snapshot"]["filename"]

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/api/camera/snapshots/{snap_name}", timeout=3
                ) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.read(), b"\xff\xd8PLUGINJPEG\xff\xd9")
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
