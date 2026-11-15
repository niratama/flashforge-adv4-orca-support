"""TCP socket client for Flashforge Adventurer 4 (port 8899)."""

from __future__ import annotations

import logging
import os
import re
import socket
import struct
import threading
import time
import zlib
from typing import Any

logger = logging.getLogger(__name__)


class FlashforgeError(Exception):
    """Raised when communication with the Flashforge printer fails."""


class FlashforgeClient:
    """Thread-safe TCP client for Flashforge Adventurer 4."""

    CHUNK_SIZE = 4096
    PACKET_MAGIC = b"\x5a\x5a\xa5\xa5"

    def __init__(
        self,
        host: str,
        port: int = 8899,
        timeout: float = 5.0,
        transfer_timeout: float = 30.0,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.transfer_timeout = transfer_timeout
        self._lock = threading.RLock()
        self._sock: socket.socket | None = None
        self._session_active = False

    def _connect(self) -> None:
        """Open a TCP connection and start a control session (~M601 S1)."""
        self._close_socket()
        try:
            logger.debug("Connecting to printer at %s:%s (timeout=%ss)", self.host, self.port, self.timeout)
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            sock.settimeout(self.timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._sock = sock
        except OSError as exc:
            logger.warning("TCP connect failed (%s:%s): %s", self.host, self.port, exc)
            raise FlashforgeError(
                f"Failed to connect to printer at {self.host}:{self.port}: {exc}"
            ) from exc

        try:
            self._send_raw("~M601 S1\r\n")
            resp = self._read_until_ok()
            if "control failed" in resp.lower():
                logger.warning("Session handshake returned control failed (~M601 S1): %r", resp.strip())
            else:
                logger.debug("Session started (~M601 S1): %r", resp.strip())
            self._session_active = True
        except Exception as exc:
            logger.warning("Session start (~M601 S1) failed: %s", exc)
            self._close_socket()
            raise FlashforgeError(f"Failed to start control session: {exc}") from exc

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            finally:
                self._sock = None
                self._session_active = False

    def close(self) -> None:
        """Gracefully end the control session (~M602) and close the socket."""
        with self._lock:
            if self._sock is not None and self._session_active:
                try:
                    self._send_raw("~M602\r\n")
                    self._read_until_ok()
                except (OSError, FlashforgeError):
                    pass
            self._close_socket()

    def _ensure_connected(self) -> None:
        if self._sock is None or not self._session_active:
            self._connect()

    def _send_raw(self, payload: str | bytes) -> None:
        if self._sock is None:
            raise FlashforgeError("Socket is not connected.")
        data = payload.encode("utf-8") if isinstance(payload, str) else payload
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise FlashforgeError(f"Socket send failed: {exc}") from exc

    def _read_until_ok(self, timeout: float | None = None) -> str:
        """Read lines from the socket until a line equal to 'ok' is received."""
        if self._sock is None:
            raise FlashforgeError("Socket is not connected.")

        original_timeout = self._sock.gettimeout()
        if timeout is not None:
            self._sock.settimeout(timeout)

        chunks: list[bytes] = []
        try:
            while True:
                try:
                    chunk = self._sock.recv(4096)
                except socket.timeout as exc:
                    partial = b"".join(chunks).decode("utf-8", errors="replace")
                    if partial:
                        logger.warning("Timed out waiting for 'ok'; partial response=%r", partial)
                    raise FlashforgeError("Timed out waiting for printer response.") from exc
                except OSError as exc:
                    raise FlashforgeError(f"Socket receive failed: {exc}") from exc

                if not chunk:
                    raise FlashforgeError("Printer closed the connection.")

                chunks.append(chunk)
                decoded = b"".join(chunks).decode("utf-8", errors="replace")
                lines = [line.strip() for line in decoded.splitlines()]
                if any(line.lower() == "ok" for line in lines):
                    return decoded
        finally:
            if timeout is not None and self._sock is not None:
                try:
                    self._sock.settimeout(original_timeout)
                except OSError:
                    pass

    def send_command(
        self, command: str, timeout: float | None = None, quiet: bool = False
    ) -> str:
        """Send a Flashforge command and return the raw response text."""
        cmd = command.strip()
        if not cmd.startswith("~"):
            cmd = f"~{cmd}"
        wire_cmd = f"{cmd}\r\n"

        with self._lock:
            last_error: FlashforgeError | None = None
            for attempt in range(2):
                try:
                    self._ensure_connected()
                    if not quiet:
                        logger.debug("-> %s", cmd)
                    self._send_raw(wire_cmd)
                    resp = self._read_until_ok(timeout=timeout)
                    if not quiet:
                        logger.debug("<- %r", resp.strip())
                    return resp
                except FlashforgeError as exc:
                    last_error = exc
                    logger.warning("Command %s failed (attempt %d/2): %s", cmd, attempt + 1, exc)
                    self._close_socket()
                    if attempt == 1:
                        break
            raise last_error or FlashforgeError("Unknown communication error.")

    # ------------------------------------------------------------------
    # Response parsers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_temperatures(raw: str) -> dict[str, dict[str, float | None]]:
        """Parse ~M105 response into nozzle and bed actual/target temperatures."""
        nozzle_match = re.search(r"T0:\s*([\d.]+)\s*/\s*([\d.]+)", raw)
        bed_match = re.search(r"B:\s*([\d.]+)\s*/\s*([\d.]+)", raw)

        return {
            "nozzle": {
                "actual": float(nozzle_match.group(1)) if nozzle_match else None,
                "target": float(nozzle_match.group(2)) if nozzle_match else None,
            },
            "bed": {
                "actual": float(bed_match.group(1)) if bed_match else None,
                "target": float(bed_match.group(2)) if bed_match else None,
            },
        }

    @staticmethod
    def parse_progress(raw: str) -> dict[str, Any]:
        """Parse ~M27 response into byte counts and completion percentage."""
        match = re.search(r"SD printing byte\s+(\d+)\s*/\s*(\d+)", raw, re.IGNORECASE)
        if not match:
            return {
                "is_printing": False,
                "bytes_printed": 0,
                "bytes_total": 0,
                "percent": 0.0,
            }

        printed = int(match.group(1))
        total = int(match.group(2))
        percent = round((printed / total) * 100.0, 2) if total > 0 else 0.0
        return {
            "is_printing": total > 0,
            "bytes_printed": printed,
            "bytes_total": total,
            "percent": min(100.0, percent),
        }

    @staticmethod
    def parse_status(raw: str) -> dict[str, Any]:
        """Parse ~M119 response into machine status, move mode, LED, and file."""
        machine_match = re.search(r"MachineStatus:\s*(.+)", raw)
        move_match = re.search(r"MoveMode:\s*(.+)", raw)
        led_match = re.search(r"LED:\s*(\d+)", raw)
        file_match = re.search(r"CurrentFile:\s*(.+)", raw)

        current_file = file_match.group(1).strip() if file_match else None
        if current_file == "":
            current_file = None

        return {
            "machine_status": machine_match.group(1).strip() if machine_match else "UNKNOWN",
            "move_mode": move_match.group(1).strip() if move_match else "UNKNOWN",
            "led": bool(int(led_match.group(1))) if led_match else None,
            "current_file": current_file,
        }

    @staticmethod
    def parse_info(raw: str) -> dict[str, str]:
        """Parse ~M115 key-value lines into a dictionary."""
        info: dict[str, str] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.lower() == "ok" or line.startswith("CMD "):
                continue
            if ":" in line:
                key, value = line.split(":", 1)
                info[key.strip()] = value.strip()
        return info

    # ------------------------------------------------------------------
    # High-level printer operations
    # ------------------------------------------------------------------

    def get_info(self) -> dict[str, str]:
        return self.parse_info(self.send_command("~M115"))

    def get_temperatures(self, quiet: bool = False) -> dict[str, dict[str, float | None]]:
        return self.parse_temperatures(self.send_command("~M105", quiet=quiet))

    def get_progress(self, quiet: bool = False) -> dict[str, Any]:
        return self.parse_progress(self.send_command("~M27", quiet=quiet))

    def get_status(self, quiet: bool = False) -> dict[str, Any]:
        return self.parse_status(self.send_command("~M119", quiet=quiet))

    def get_full_status(self) -> dict[str, Any]:
        """Fetch temperature, progress, and machine status in one locked sequence."""
        with self._lock:
            temps = self.get_temperatures(quiet=True)
            progress = self.get_progress(quiet=True)
            status = self.get_status(quiet=True)

        return {
            "connected": True,
            "error": None,
            "machine_status": status["machine_status"],
            "move_mode": status["move_mode"],
            "led": status["led"],
            "current_file": status["current_file"],
            "temperatures": temps,
            "progress": progress,
        }

    def pause_print(self) -> str:
        return self.send_command("~M25")

    def resume_print(self) -> str:
        return self.send_command("~M24")

    def cancel_print(self) -> str:
        return self.send_command("~M26")

    def set_led(self, on: bool) -> str:
        if on:
            return self.send_command("~M146 r255 g255 b255 F0")
        return self.send_command("~M146 r0 g0 b0 F0")

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Keep only the safe basename for 0:/user/<filename>."""
        base = os.path.basename(filename.strip())
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", base)
        return sanitized or "print_job.gcode"

    @staticmethod
    def wrap_gcode_as_gx(gcode_bytes: bytes) -> bytes:
        """Wrap plain ASCII G-code in a minimal Flashforge xgcode 1.0 (.gx) container."""
        if gcode_bytes.startswith(b"xgcode 1.0"):
            return gcode_bytes

        bmp_width, bmp_height = 80, 60
        pixel_size = bmp_width * bmp_height * 3  # 14400 bytes (24-bit RGB)
        bmp_size = 54 + pixel_size  # 14454 bytes
        bmp_offset = 58  # 0x3A
        gcode_offset = bmp_offset + bmp_size  # 14512 (0x38B0)

        bmp_file_header = struct.pack("<2sIHHI", b"BM", bmp_size, 0, 0, 54)
        bmp_info_header = struct.pack(
            "<IiiHHIIiiII",
            40,
            bmp_width,
            bmp_height,
            1,
            24,
            0,
            pixel_size,
            2835,
            2835,
            0,
            0,
        )
        bmp_pixels = b"\x20\x20\x20" * (bmp_width * bmp_height)

        gx_header = (
            b"xgcode 1.0\n\x00"
            + struct.pack("<IIII", 0, bmp_offset, gcode_offset, gcode_offset)
            + struct.pack("<IIIHHHHHHHHH", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        )
        return gx_header + bmp_file_header + bmp_info_header + bmp_pixels + gcode_bytes

    @classmethod
    def _build_transfer_packet(
        cls, counter: int, chunk: bytes, crc_trailer: bool = False
    ) -> bytes:
        """Wrap a <=4096-byte payload chunk in Flashforge's 4112-byte packet format."""
        crc = zlib.crc32(chunk) & 0xFFFFFFFF
        padded = chunk.ljust(cls.CHUNK_SIZE, b"\x00")
        if crc_trailer:
            header = struct.pack(">4sII", cls.PACKET_MAGIC, counter, len(chunk))
            return header + padded + struct.pack(">I", crc)
        header = struct.pack(">4sIII", cls.PACKET_MAGIC, counter, len(chunk), crc)
        return header + padded

    def send_file(
        self,
        filename: str,
        data: bytes,
        start_print: bool = False,
        transfer_mode: str | None = None,
    ) -> str:
        """Transfer G-code/GX bytes via ~M28 ... ~M29 and optionally start with ~M23."""
        safe_name = self.sanitize_filename(filename)
        if not safe_name.lower().endswith(".gx"):
            stem, _ = os.path.splitext(safe_name)
            safe_name = f"{stem}.gx"
        data = self.wrap_gcode_as_gx(data)
        remote_path = f"0:/user/{safe_name}"
        file_size = len(data)
        mode = (transfer_mode or os.getenv("FLASHFORGE_TRANSFER_MODE", "raw")).strip().lower()

        with self._lock:
            # Reuse the active control session (closing without ~M602 causes 'Control failed.').
            self._ensure_connected()

            total_chunks = (file_size + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE if file_size else 0
            logger.info(
                "Starting file transfer: remote_path=%s size=%d bytes chunks=%d mode=%s start_print=%s",
                remote_path,
                file_size,
                total_chunks,
                mode,
                start_print,
            )

            try:
                m28_cmd = f"~M28 {file_size} {remote_path}"
                logger.debug("-> %s", m28_cmd)
                self._send_raw(f"{m28_cmd}\r\n")
                m28_resp = self._read_until_ok(timeout=self.transfer_timeout)
                logger.debug("<- %r", m28_resp.strip())

                for counter, offset in enumerate(range(0, file_size, self.CHUNK_SIZE)):
                    chunk = data[offset : offset + self.CHUNK_SIZE]
                    if mode == "packet":
                        payload = self._build_transfer_packet(counter, chunk)
                    elif mode == "trailer":
                        payload = self._build_transfer_packet(
                            counter, chunk, crc_trailer=True
                        )
                    else:
                        payload = chunk
                    self._send_raw(payload)
                    time.sleep(0.003)
                    if counter == 0 or (counter + 1) % 100 == 0 or (counter + 1) == total_chunks:
                        logger.debug(
                            "Sent chunk %d/%d (%d bytes, mode=%s)",
                            counter + 1,
                            total_chunks,
                            len(chunk),
                            mode,
                        )

                time.sleep(0.15)
                logger.debug("-> ~M29")
                self._send_raw("~M29\r\n")
                m29_resp = self._read_until_ok(timeout=self.transfer_timeout)
                logger.debug("<- %r", m29_resp.strip())

                if start_print:
                    m23_cmd = f"~M23 {remote_path}"
                    logger.debug("-> %s", m23_cmd)
                    self._send_raw(f"{m23_cmd}\r\n")
                    m23_resp = self._read_until_ok(timeout=self.transfer_timeout)
                    logger.debug("<- %r", m23_resp.strip())

                logger.info("File transfer completed: %s", remote_path)
            except Exception as exc:
                logger.error("File transfer failed (%s): %s", remote_path, exc)
                self._close_socket()
                if isinstance(exc, FlashforgeError):
                    raise
                raise FlashforgeError(f"File transfer failed: {exc}") from exc

        return safe_name
