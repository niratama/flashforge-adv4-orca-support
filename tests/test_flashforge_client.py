"""Unit tests for flashforge_client.py using a local mock TCP server."""

import base64
import socket
import struct
import threading
import unittest
import zlib

from flashforge_client import (
    FlashforgeClient,
    FlashforgeError,
    XgcodeHeader,
    extract_xgcode_header,
)


class MockPrinterServer:
    """Minimal TCP mock speaking the Flashforge ~M... protocol."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self.port = self._sock.getsockname()[1]
        self.received_commands: list[str] = []
        self.uploaded_bytes: bytes = b""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.2)
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()

    def _handle_client(self, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            remaining_bytes = 0
            while not self._stop.is_set():
                data = conn.recv(8192)
                if not data:
                    break
                buffer += data

                if remaining_bytes > 0:
                    take = min(remaining_bytes, len(buffer))
                    self.uploaded_bytes += buffer[:take]
                    buffer = buffer[take:]
                    remaining_bytes -= take
                    if remaining_bytes > 0:
                        continue

                while b"\r\n" in buffer:
                    line_bytes, buffer = buffer.split(b"\r\n", 1)
                    cmd = line_bytes.decode("utf-8", errors="replace")
                    self.received_commands.append(cmd)

                    if cmd.startswith("~M28 "):
                        size_str = cmd.split()[1]
                        remaining_bytes = int(size_str)
                        self.uploaded_bytes = b""
                        conn.sendall(b"CMD M28 Received.\r\nok\r\n")
                        if remaining_bytes > 0:
                            take = min(remaining_bytes, len(buffer))
                            self.uploaded_bytes += buffer[:take]
                            buffer = buffer[take:]
                            remaining_bytes -= take
                            if remaining_bytes > 0:
                                break
                    elif cmd == "~M601 S1":
                        conn.sendall(b"CMD M601 Received.\r\nControl Success.\r\nok\r\n")
                    elif cmd == "~M105":
                        conn.sendall(b"CMD M105 Received.\r\nT0:205.5 /210.0 B:60.0 /60.0\r\nok\r\n")
                    elif cmd == "~M27":
                        conn.sendall(b"CMD M27 Received.\r\nSD printing byte 500/1000\r\nok\r\n")
                    elif cmd == "~M119":
                        conn.sendall(
                            b"CMD M119 Received.\r\n"
                            b"Endstop: X-max:0 Y-max:0 Z-min:0\r\n"
                            b"MachineStatus: BUILDING_FROM_SD\r\n"
                            b"MoveMode: MOVING\r\n"
                            b"LED: 1\r\n"
                            b"CurrentFile: benchy.gx\r\n"
                            b"ok\r\n"
                        )
                    elif cmd == "~M114":
                        conn.sendall(
                            b"CMD M114 Received.\r\n"
                            b"X:12.50 Y:-3.25 Z:45.00 A:1.20 B:0.00\r\n"
                            b"ok\r\n"
                        )
                    elif cmd == "~M661":
                        names = ["/data/benchy.gx", "/data/テスト造形.gx", "/data/cube.gcode"]
                        payload = struct.pack(">4sI", b"D\xaa\xaaD", len(names))
                        for name in names:
                            encoded = name.encode("utf-8")
                            payload += struct.pack(">4sI", b"::\xa3\xa3", len(encoded)) + encoded
                        conn.sendall(b"CMD M661 Received.\r\nok\r\n" + payload)
                    elif cmd.startswith("~M662 "):
                        target = cmd[len("~M662 ") :].strip()
                        if target == "/data/benchy.gx":
                            bmp = b"BM" + b"\x00" * 120
                            payload = struct.pack(">4sI", b"**\xa2\xa2", len(bmp)) + bmp
                            conn.sendall(b"CMD M662 Received.\r\nok\r\n" + payload)
                        else:
                            conn.sendall(b"CMD M662 Received.\r\nError: file not found\r\nok\r\n")
                    else:
                        conn.sendall(f"CMD {cmd} Received.\r\nok\r\n".encode("utf-8"))

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass


class TestFlashforgeClient(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockPrinterServer()
        self.client = FlashforgeClient("127.0.0.1", port=self.server.port, timeout=2.0)

    def tearDown(self) -> None:
        self.client.close()
        self.server.close()

    def test_get_full_status(self) -> None:
        status = self.client.get_full_status()
        self.assertTrue(status["connected"])
        self.assertEqual(status["machine_status"], "BUILDING_FROM_SD")
        self.assertEqual(status["move_mode"], "MOVING")
        self.assertTrue(status["led"])
        self.assertEqual(status["current_file"], "benchy.gx")
        self.assertEqual(status["temperatures"]["nozzle"], {"actual": 205.5, "target": 210.0})
        self.assertEqual(status["temperatures"]["bed"], {"actual": 60.0, "target": 60.0})
        self.assertEqual(status["progress"]["percent"], 50.0)
        self.assertTrue(status["progress"]["is_printing"])

    def test_send_file_and_start_print(self) -> None:
        payload = (b"; G-code test\nG28\nG1 X10 Y10\n" * 200)[:5000]
        saved = self.client.send_file("../dir/my model.gcode", payload, start_print=True)
        expected_gx = FlashforgeClient.wrap_gcode_as_gx(payload)
        self.assertEqual(saved, "my_model.gx")
        self.assertTrue(expected_gx.startswith(b"xgcode 1.0\n\x00"))
        self.assertEqual(self.server.uploaded_bytes, expected_gx)
        self.assertIn(
            f"~M28 {len(expected_gx)} 0:/user/my_model.gx",
            self.server.received_commands,
        )
        self.assertIn("~M29", self.server.received_commands)
        self.assertIn("~M23 0:/user/my_model.gx", self.server.received_commands)

    def test_control_commands(self) -> None:
        self.client.pause_print()
        self.client.resume_print()
        self.client.cancel_print()
        self.client.set_led(True)
        self.client.set_led(False)
        self.client.set_nozzle_temp(230)
        self.client.set_bed_temp(60)
        self.client.set_print_speed(90)
        self.assertIn("~M25", self.server.received_commands)
        self.assertIn("~M24", self.server.received_commands)
        self.assertIn("~M26", self.server.received_commands)
        self.assertIn("~M146 r255 g255 b255 F0", self.server.received_commands)
        self.assertIn("~M146 r0 g0 b0 F0", self.server.received_commands)
        self.assertIn("~M104 S230 T0", self.server.received_commands)
        self.assertIn("~M140 S60", self.server.received_commands)
        self.assertIn("~M663 S90", self.server.received_commands)

        with self.assertRaises(ValueError):
            self.client.set_nozzle_temp(350)
        with self.assertRaises(ValueError):
            self.client.set_bed_temp(150)
        with self.assertRaises(ValueError):
            self.client.set_print_speed(5)

    def test_wrap_gcode_as_gx_populates_orcaslicer_metadata(self) -> None:
        gcode = (
            b"; generated by OrcaSlicer\n"
            b"M140 S60\n"
            b"M104 S205\n"
            b"G28\n"
            b"M104 S0\n"
            b"M140 S0\n"
            b"; filament used [mm] = 1234.56, 0.00\n"
            b"; estimated printing time (normal mode) = 1h 2m 3s\n"
            b"; nozzle_temperature = 220\n"
            b"; hot_plate_temp = 70\n"
            b"; layer_height = 0.20\n"
            b"; outer_wall_speed = 65\n"
            b"; raft_layers = 2\n"
            b"; enable_support = 1\n"
            b"; wall_loops = 3\n"
        )
        gx = FlashforgeClient.wrap_gcode_as_gx(gcode)
        self.assertEqual(gx[:16], b"xgcode 1.0\n\x00\x00\x00\x00\x00")
        bmp_offset, gcode_offset1, gcode_offset2 = struct.unpack("<III", gx[16:28])
        self.assertEqual(bmp_offset, 0x3A)
        self.assertEqual(gcode_offset1, 14512)
        self.assertEqual(gcode_offset2, 14512)
        self.assertEqual(gx[bmp_offset : bmp_offset + 2], b"BM")
        self.assertEqual(gx[gcode_offset1:], gcode)

        fields = struct.unpack("<IIIHHHHHHHHH", gx[28:58])
        self.assertEqual(
            fields,
            (
                3723,  # print_time (1h 2m 3s)
                1235,  # filament_0_usage (rounded mm)
                0,     # filament_1_usage
                13,    # multi_extruder_type (0x01 | 0x04 raft | 0x08 support)
                200,   # layer_height (0.20mm -> 200um)
                0,     # reserved0
                3,     # perimeter_shells (wall_loops = 3)
                65,    # print_speed (outer_wall_speed = 65 mm/s)
                60,    # hotbed_temp (from M140 S60)
                205,   # extruder_0_temp (from M104 S205, prioritized over comment)
                0,     # extruder_1_temp
                0,     # reserved1
            ),
        )

        # Verify raft/support flag combinations
        self.assertEqual(extract_xgcode_header(b"; raft_layers = 0\n; enable_support = 0\n; wall_loops = 2\n").multi_extruder_type, 1)
        self.assertEqual(extract_xgcode_header(b"; raft_layers = 2\n; enable_support = 0\n").multi_extruder_type, 5)
        self.assertEqual(extract_xgcode_header(b"; raft_layers = 0\n; enable_support = 1\n").multi_extruder_type, 9)
        self.assertEqual(extract_xgcode_header(b"; wall_loops = 2\n").perimeter_shells, 2)

    def test_extract_xgcode_header_falls_back_to_comments_for_temps(self) -> None:
        gcode = (
            b"G28\n"
            b"M104 S0\n"
            b"M140 S0\n"
            b"; estimated printing time (silent mode) = 45m 10s\n"
            b"; total filament length [mm] : 890.2\n"
            b"; nozzle_temperature = 215\n"
            b"; textured_plate_temp = 65\n"
        )
        header = extract_xgcode_header(gcode)
        self.assertEqual(
            header,
            XgcodeHeader(
                print_time=2710,
                filament_0_usage=890,
                hotbed_temp=65,
                extruder_0_temp=215,
            ),
        )

    def test_extract_xgcode_header_scan_window_vs_full_scan(self) -> None:
        padding = b"G1 X10 Y10 E0.1\n" * 400  # ~6.4 KB
        head_tail_gcode = (
            b"M109 S210\nM190 S55\n"
            + padding
            + b"; estimated printing time (normal mode) = 2d 1h 0m 5s\n"
            + b"; filament used [mm] = 500\n"
        )
        bounded = extract_xgcode_header(head_tail_gcode, scan_bytes=1024)
        self.assertEqual(bounded.extruder_0_temp, 210)
        self.assertEqual(bounded.hotbed_temp, 55)
        self.assertEqual(bounded.print_time, 2 * 86400 + 3600 + 5)
        self.assertEqual(bounded.filament_0_usage, 500)

        # Metadata placed only in the middle is skipped when bounded, found when full-scan (0/None).
        middle_only = padding + b"M104 S200\nM140 S60\n" + padding
        self.assertEqual(extract_xgcode_header(middle_only, scan_bytes=1024), XgcodeHeader())
        self.assertEqual(
            extract_xgcode_header(middle_only, scan_bytes=0),
            XgcodeHeader(hotbed_temp=60, extruder_0_temp=200),
        )
        self.assertEqual(
            extract_xgcode_header(middle_only, scan_bytes=None),
            XgcodeHeader(hotbed_temp=60, extruder_0_temp=200),
        )

    @staticmethod
    def _make_test_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
        def chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
        row = b"\x00" + bytes(rgba) * width
        idat = zlib.compress(row * height)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")

    def test_wrap_gcode_as_gx_decodes_png_thumbnail_and_falls_back_on_invalid(self) -> None:
        # 80x60 solid orange RGBA PNG (with full alpha) -> BGR (0x10, 0x80, 0xFF)
        png = self._make_test_png(80, 60, (0xFF, 0x80, 0x10, 0xFF))
        b64 = base64.b64encode(png).decode("ascii")
        gcode = (
            b"; thumbnail begin 80x60 123\n"
            + b"; " + b64.encode("ascii") + b"\n"
            + b"; thumbnail end\n"
            + b"G28\n"
        )
        gx = FlashforgeClient.wrap_gcode_as_gx(gcode)
        self.assertEqual(len(gx), 14512 + len(gcode))
        pixel_data = gx[58 + 54 : 14512]
        self.assertEqual(pixel_data[:3], b"\x10\x80\xff")
        self.assertEqual(pixel_data[-3:], b"\x10\x80\xff")

        # Corrupted thumbnail falls back cleanly to default 0x20 gray BMP
        bad_gcode = (
            b"; thumbnail begin 80x60 10\n"
            b"; not-valid-base64!!!\n"
            b"; thumbnail end\n"
            b"G28\n"
        )
        bad_gx = FlashforgeClient.wrap_gcode_as_gx(bad_gcode)
        self.assertEqual(bad_gx[58 + 54 : 14512], b"\x20\x20\x20" * (80 * 60))

        # Oversized zlib stream (decompression bomb) is rejected safely
        def make_chunk(tag: bytes, data: bytes) -> bytes:
            crc = zlib.crc32(tag + data) & 0xFFFFFFFF
            return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

        ihdr_1x1 = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
        bomb_idat = zlib.compress(b"\x00" * 10000)
        bomb_png = (
            b"\x89PNG\r\n\x1a\n"
            + make_chunk(b"IHDR", ihdr_1x1)
            + make_chunk(b"IDAT", bomb_idat)
            + make_chunk(b"IEND", b"")
        )
        bomb_b64 = base64.b64encode(bomb_png).decode("ascii")
        bomb_gcode = (
            b"; thumbnail begin 1x1 10\n; "
            + bomb_b64.encode("ascii")
            + b"\n; thumbnail end\nG28\n"
        )
        bomb_gx = FlashforgeClient.wrap_gcode_as_gx(bomb_gcode)
        self.assertEqual(bomb_gx[58 + 54 : 14512], b"\x20\x20\x20" * (80 * 60))

    def test_list_files_thumbnail_and_reprint(self) -> None:
        files = self.client.list_files()
        self.assertEqual(
            files,
            [
                {"path": "/data/benchy.gx", "name": "benchy.gx"},
                {"path": "/data/テスト造形.gx", "name": "テスト造形.gx"},
                {"path": "/data/cube.gcode", "name": "cube.gcode"},
            ],
        )

        bmp = self.client.get_file_thumbnail("benchy.gx")
        self.assertIsNotNone(bmp)
        assert bmp is not None
        self.assertTrue(bmp.startswith(b"BM"))
        self.assertEqual(len(bmp), 122)

        missing = self.client.get_file_thumbnail("/data/missing.gx")
        self.assertIsNone(missing)

        saved = self.client.start_existing_print("/data/benchy.gx")
        self.assertEqual(saved, "benchy.gx")
        self.assertIn("~M23 0:/user/benchy.gx", self.server.received_commands)

    def test_normalize_storage_path_validation(self) -> None:
        self.assertEqual(FlashforgeClient.normalize_storage_path("benchy.gx"), "/data/benchy.gx")
        self.assertEqual(
            FlashforgeClient.normalize_storage_path("/data/テスト.gx"),
            "/data/テスト.gx",
        )
        self.assertEqual(
            FlashforgeClient.normalize_storage_path("0:/user/model.gcode"),
            "/data/model.gcode",
        )
        for bad in ["", "   ", "../secret.gx", "/data/../secret.gx", "sub/dir.gx", "bad\r\n~M26"]:
            with self.assertRaises(ValueError):
                FlashforgeClient.normalize_storage_path(bad)

    def test_position_steppers_and_emergency_stop(self) -> None:
        pos = self.client.get_position()
        self.assertEqual(pos, {"x": 12.5, "y": -3.25, "z": 45.0, "e": 1.2})

        full = self.client.get_full_status()
        self.assertEqual(full["position"], {"x": 12.5, "y": -3.25, "z": 45.0, "e": 1.2})

        self.client.disable_steppers()
        self.client.enable_steppers()
        self.client.emergency_stop()
        self.assertIn("~M18", self.server.received_commands)
        self.assertIn("~M17", self.server.received_commands)
        self.assertIn("~M112", self.server.received_commands)

    def test_connection_error(self) -> None:
        bad_client = FlashforgeClient("127.0.0.1", port=1, timeout=0.5)
        with self.assertRaises(FlashforgeError):
            bad_client.get_temperatures()


if __name__ == "__main__":
    unittest.main()
