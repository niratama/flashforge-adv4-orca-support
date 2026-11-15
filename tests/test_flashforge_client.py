"""Unit tests for flashforge_client.py using a local mock TCP server."""

import socket
import struct
import threading
import unittest
import zlib

from flashforge_client import FlashforgeClient, FlashforgeError


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
        self.assertIn("~M25", self.server.received_commands)
        self.assertIn("~M24", self.server.received_commands)
        self.assertIn("~M26", self.server.received_commands)
        self.assertIn("~M146 r255 g255 b255 F0", self.server.received_commands)
        self.assertIn("~M146 r0 g0 b0 F0", self.server.received_commands)

    def test_connection_error(self) -> None:
        bad_client = FlashforgeClient("127.0.0.1", port=1, timeout=0.5)
        with self.assertRaises(FlashforgeError):
            bad_client.get_temperatures()


if __name__ == "__main__":
    unittest.main()
