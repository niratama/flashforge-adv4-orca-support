"""TCP socket client for Flashforge Adventurer 4 (port 8899)."""

from __future__ import annotations

import base64
from dataclasses import dataclass
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

DEFAULT_METADATA_SCAN_BYTES = 131072  # 128 KiB head + 128 KiB tail
_U16_MAX = 0xFFFF
_U32_MAX = 0xFFFFFFFF

_PRINT_TIME_LINE_RE = re.compile(
    r"^\s*;\s*estimated printing time(?:\s*\(([^)]*)\))?\s*=\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_TIME_TOKEN_RE = re.compile(r"(\d+)\s*([dhms])", re.IGNORECASE)
_FILAMENT_MM_RE = re.compile(
    r"^\s*;\s*(?:filament used \[mm\]|total filament length \[mm\])\s*[=:]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_TEMP_CMD_RE = re.compile(r"^(?:N\d+\s+)?M(104|109|140|190)\b(.*)$", re.IGNORECASE)
_S_PARAM_RE = re.compile(r"\bS(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_T_PARAM_RE = re.compile(r"\bT(\d+)\b", re.IGNORECASE)
_NOZZLE_COMMENT_RE = re.compile(
    r"^\s*;\s*(?:nozzle_temperature|first_layer_temperature)\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_BED_COMMENT_RE = re.compile(
    r"^\s*;\s*(?:hot_plate_temp|textured_plate_temp|cool_plate_temp|eng_plate_temp|bed_temperature|first_layer_bed_temperature)\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_LAYER_HEIGHT_RE = re.compile(
    r"^\s*;\s*layer_height\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_PRINT_SPEED_RE = re.compile(
    r"^\s*;\s*(default_print_speed|outer_wall_speed|inner_wall_speed)\s*=\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE | re.MULTILINE,
)
_RAFT_LAYERS_RE = re.compile(
    r"^\s*;\s*raft_layers\s*=\s*(\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_ENABLE_SUPPORT_RE = re.compile(
    r"^\s*;\s*enable_support\s*=\s*(\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_WALL_LOOPS_RE = re.compile(
    r"^\s*;\s*wall_loops\s*=\s*(\d+)",
    re.IGNORECASE | re.MULTILINE,
)
_THUMBNAIL_BLOCK_RE = re.compile(
    r";\s*thumbnail\s+begin\s+(\d+)x(\d+)\s+\d+\s*\r?\n(.*?);\s*thumbnail\s+end",
    re.IGNORECASE | re.DOTALL,
)
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BMP_BG_BYTE = 0x20
_THUMB_WIDTH = 80
_THUMB_HEIGHT = 60


def _clamp_int(value: int, upper: int) -> int:
    return max(0, min(int(value), upper))


def _decode_scan_window(gcode_bytes: bytes, scan_bytes: int | None) -> str:
    """Decode either the bounded head/tail window or the entire G-code payload."""
    if scan_bytes is None or scan_bytes <= 0 or len(gcode_bytes) <= scan_bytes * 2:
        return gcode_bytes.decode("utf-8", errors="ignore")
    head = gcode_bytes[:scan_bytes].decode("utf-8", errors="ignore")
    tail = gcode_bytes[-scan_bytes:].decode("utf-8", errors="ignore")
    return f"{head}\n{tail}"


def _parse_print_time_seconds(text: str) -> int:
    matches = _PRINT_TIME_LINE_RE.findall(text)
    if not matches:
        return 0
    selected = matches[0][1]
    for mode, value in matches:
        if mode and mode.strip().lower() == "normal mode":
            selected = value
            break

    multipliers = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0
    for amount, unit in _TIME_TOKEN_RE.findall(selected):
        total += int(amount) * multipliers[unit.lower()]
    return _clamp_int(total, _U32_MAX)


def _parse_filament_mm(text: str) -> int:
    match = _FILAMENT_MM_RE.search(text)
    if not match:
        return 0
    try:
        return _clamp_int(round(float(match.group(1))), _U32_MAX)
    except ValueError:
        return 0


def _parse_first_comment_temp(pattern: re.Pattern[str], text: str) -> int:
    for match in pattern.finditer(text):
        try:
            temp = round(float(match.group(1)))
        except ValueError:
            continue
        if temp > 0:
            return _clamp_int(temp, _U16_MAX)
    return 0


def _parse_temperatures(text: str) -> tuple[int, int]:
    """Return (extruder_0_temp, hotbed_temp) prioritizing M104/M109 and M140/M190."""
    nozzle_temp = 0
    bed_temp = 0

    for raw_line in text.splitlines():
        code_part = raw_line.split(";", 1)[0].strip()
        if not code_part:
            continue
        cmd_match = _TEMP_CMD_RE.match(code_part)
        if not cmd_match:
            continue
        code = cmd_match.group(1)
        args = cmd_match.group(2)
        s_match = _S_PARAM_RE.search(args)
        if not s_match:
            continue
        try:
            temp = round(float(s_match.group(1)))
        except ValueError:
            continue
        if temp <= 0:
            continue

        if code in {"104", "109"} and nozzle_temp == 0:
            t_match = _T_PARAM_RE.search(args)
            if t_match and int(t_match.group(1)) != 0:
                continue
            nozzle_temp = _clamp_int(temp, _U16_MAX)
        elif code in {"140", "190"} and bed_temp == 0:
            bed_temp = _clamp_int(temp, _U16_MAX)

        if nozzle_temp > 0 and bed_temp > 0:
            break

    if nozzle_temp == 0:
        nozzle_temp = _parse_first_comment_temp(_NOZZLE_COMMENT_RE, text)
    if bed_temp == 0:
        bed_temp = _parse_first_comment_temp(_BED_COMMENT_RE, text)

    return nozzle_temp, bed_temp


@dataclass(frozen=True)
class XgcodeHeader:
    """Metadata fields stored in the 58-byte Flashforge xgcode 1.0 header."""

    print_time: int = 0
    filament_0_usage: int = 0
    filament_1_usage: int = 0
    multi_extruder_type: int = 0
    layer_height: int = 0
    reserved0: int = 0
    perimeter_shells: int = 0
    print_speed: int = 0
    hotbed_temp: int = 0
    extruder_0_temp: int = 0
    extruder_1_temp: int = 0
    reserved1: int = 0

    def to_bytes(self, bmp_offset: int, gcode_offset: int) -> bytes:
        """Serialize the 58-byte xgcode 1.0 binary header."""
        return (
            b"xgcode 1.0\n\x00"
            + struct.pack("<IIII", 0, bmp_offset, gcode_offset, gcode_offset)
            + struct.pack(
                "<IIIHHHHHHHHH",
                _clamp_int(self.print_time, _U32_MAX),
                _clamp_int(self.filament_0_usage, _U32_MAX),
                _clamp_int(self.filament_1_usage, _U32_MAX),
                _clamp_int(self.multi_extruder_type, _U16_MAX),
                _clamp_int(self.layer_height, _U16_MAX),
                _clamp_int(self.reserved0, _U16_MAX),
                _clamp_int(self.perimeter_shells, _U16_MAX),
                _clamp_int(self.print_speed, _U16_MAX),
                _clamp_int(self.hotbed_temp, _U16_MAX),
                _clamp_int(self.extruder_0_temp, _U16_MAX),
                _clamp_int(self.extruder_1_temp, _U16_MAX),
                _clamp_int(self.reserved1, _U16_MAX),
            )
        )


def _parse_layer_height_um(text: str) -> int:
    match = _LAYER_HEIGHT_RE.search(text)
    if not match:
        return 0
    try:
        return _clamp_int(round(float(match.group(1)) * 1000.0), _U16_MAX)
    except ValueError:
        return 0


def _parse_print_speed_mms(text: str) -> int:
    found: dict[str, int] = {}
    for key, raw_val in _PRINT_SPEED_RE.findall(text):
        key_lower = key.lower()
        if key_lower in found:
            continue
        try:
            speed = round(float(raw_val))
        except ValueError:
            continue
        if speed > 0:
            found[key_lower] = _clamp_int(speed, _U16_MAX)
    for preferred in ("default_print_speed", "outer_wall_speed", "inner_wall_speed"):
        if preferred in found:
            return found[preferred]
    return 0


def _parse_multi_extruder_type(text: str) -> int:
    raft_match = _RAFT_LAYERS_RE.search(text)
    support_match = _ENABLE_SUPPORT_RE.search(text)
    if not raft_match and not support_match:
        return 0
    flags = 0x01
    if raft_match and int(raft_match.group(1)) > 0:
        flags |= 0x04
    if support_match and int(support_match.group(1)) > 0:
        flags |= 0x08
    return flags


def _parse_perimeter_shells(text: str) -> int:
    match = _WALL_LOOPS_RE.search(text)
    if not match:
        return 0
    return _clamp_int(int(match.group(1)), _U16_MAX)


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _unfilter_png_scanlines(
    raw: bytes, width: int, height: int, channels: int
) -> bytearray | None:
    stride = width * channels
    expected_len = height * (1 + stride)
    if len(raw) != expected_len:
        return None

    pixels = bytearray(height * stride)
    prev_row = bytearray(stride)
    pos = 0
    for y in range(height):
        filter_type = raw[pos]
        pos += 1
        row = bytearray(raw[pos : pos + stride])
        pos += stride

        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(channels, stride):
                row[i] = (row[i] + row[i - channels]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                row[i] = (row[i] + prev_row[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                row[i] = (row[i] + ((left + prev_row[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = row[i - channels] if i >= channels else 0
                up = prev_row[i]
                up_left = prev_row[i - channels] if i >= channels else 0
                row[i] = (row[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            return None

        out_offset = y * stride
        pixels[out_offset : out_offset + stride] = row
        prev_row = row

    return pixels


def _blend_on_bg(channel: int, alpha: int) -> int:
    return (channel * alpha + _BMP_BG_BYTE * (255 - alpha)) // 255


def _decode_png_rgb(png_bytes: bytes) -> tuple[int, int, bytearray] | None:
    """Decode an 8-bit non-interlaced PNG into packed RGB bytes (top-down)."""
    if not png_bytes.startswith(_PNG_SIGNATURE):
        return None

    width = 0
    height = 0
    bit_depth = 0
    color_type = 0
    palette = b""
    trns = b""
    idat_chunks: list[bytes] = []

    offset = len(_PNG_SIGNATURE)
    total_len = len(png_bytes)
    while offset + 8 <= total_len:
        chunk_len, chunk_type = struct.unpack(">I4s", png_bytes[offset : offset + 8])
        offset += 8
        if offset + chunk_len + 4 > total_len:
            return None
        chunk_data = png_bytes[offset : offset + chunk_len]
        offset += chunk_len + 4  # skip CRC

        if chunk_type == b"IHDR":
            if chunk_len != 13:
                return None
            width, height, bit_depth, color_type, comp, flt, interlace = struct.unpack(
                ">IIBBBBB", chunk_data
            )
            if (
                width == 0
                or height == 0
                or width * height > 2048 * 2048
                or bit_depth != 8
                or comp != 0
                or flt != 0
                or interlace != 0
            ):
                return None
        elif chunk_type == b"PLTE":
            palette = chunk_data
        elif chunk_type == b"tRNS":
            trns = chunk_data
        elif chunk_type == b"IDAT":
            idat_chunks.append(chunk_data)
        elif chunk_type == b"IEND":
            break

    channels_map = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
    channels = channels_map.get(color_type)
    if channels is None or not idat_chunks:
        return None
    if color_type == 3 and len(palette) < 3:
        return None

    expected_len = height * (1 + width * channels)
    try:
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(b"".join(idat_chunks), max_length=expected_len + 1)
    except zlib.error:
        return None
    if decompressor.unconsumed_tail or not decompressor.eof or len(raw) != expected_len:
        return None

    unfiltered = _unfilter_png_scanlines(raw, width, height, channels)
    if unfiltered is None:
        return None

    rgb = bytearray(width * height * 3)
    if color_type == 2:
        rgb[:] = unfiltered
        return width, height, rgb

    pixel_count = width * height
    if color_type == 6:
        for i in range(pixel_count):
            src = i * 4
            dst = i * 3
            a = unfiltered[src + 3]
            rgb[dst] = _blend_on_bg(unfiltered[src], a)
            rgb[dst + 1] = _blend_on_bg(unfiltered[src + 1], a)
            rgb[dst + 2] = _blend_on_bg(unfiltered[src + 2], a)
    elif color_type == 0:
        for i in range(pixel_count):
            v = unfiltered[i]
            dst = i * 3
            rgb[dst] = v
            rgb[dst + 1] = v
            rgb[dst + 2] = v
    elif color_type == 4:
        for i in range(pixel_count):
            src = i * 2
            dst = i * 3
            v = _blend_on_bg(unfiltered[src], unfiltered[src + 1])
            rgb[dst] = v
            rgb[dst + 1] = v
            rgb[dst + 2] = v
    else:  # color_type == 3 (indexed palette)
        pal_entries = len(palette) // 3
        for i in range(pixel_count):
            idx = unfiltered[i]
            if idx >= pal_entries:
                return None
            p = idx * 3
            a = trns[idx] if idx < len(trns) else 255
            dst = i * 3
            rgb[dst] = _blend_on_bg(palette[p], a)
            rgb[dst + 1] = _blend_on_bg(palette[p + 1], a)
            rgb[dst + 2] = _blend_on_bg(palette[p + 2], a)

    return width, height, rgb


def _resize_rgb_to_bmp_pixels(
    src_w: int, src_h: int, rgb: bytearray
) -> bytes:
    """Fit top-down RGB image into 80x60 bottom-up 24-bit BGR BMP pixel buffer."""
    scale = min(_THUMB_WIDTH / src_w, _THUMB_HEIGHT / src_h)
    fit_w = max(1, min(_THUMB_WIDTH, round(src_w * scale)))
    fit_h = max(1, min(_THUMB_HEIGHT, round(src_h * scale)))
    offset_x = (_THUMB_WIDTH - fit_w) // 2
    offset_y = (_THUMB_HEIGHT - fit_h) // 2

    out = bytearray([_BMP_BG_BYTE] * (_THUMB_WIDTH * _THUMB_HEIGHT * 3))
    for dy in range(fit_h):
        sy = min(src_h - 1, (dy * src_h) // fit_h)
        bmp_y = _THUMB_HEIGHT - 1 - (offset_y + dy)
        bmp_row_base = bmp_y * _THUMB_WIDTH * 3
        src_row_base = sy * src_w * 3
        for dx in range(fit_w):
            sx = min(src_w - 1, (dx * src_w) // fit_w)
            src_idx = src_row_base + sx * 3
            dst_idx = bmp_row_base + (offset_x + dx) * 3
            out[dst_idx] = rgb[src_idx + 2]      # B
            out[dst_idx + 1] = rgb[src_idx + 1]  # G
            out[dst_idx + 2] = rgb[src_idx]      # R
    return bytes(out)


def _extract_thumbnail_bmp_pixels_from_text(text: str) -> bytes:
    """Extract OrcaSlicer PNG thumbnail from G-code comments and convert to 80x60 BGR BMP."""
    default_pixels = bytes([_BMP_BG_BYTE] * (_THUMB_WIDTH * _THUMB_HEIGHT * 3))
    candidates: list[tuple[int, str]] = []
    for match in _THUMBNAIL_BLOCK_RE.finditer(text):
        w, h = int(match.group(1)), int(match.group(2))
        candidates.append((w * h, match.group(3)))

    # Prefer the largest thumbnail block (or first valid PNG if decoding fails)
    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, body in candidates:
        b64_lines = [line.lstrip().lstrip(";").strip() for line in body.splitlines()]
        b64_data = "".join(b64_lines)
        if not b64_data:
            continue
        try:
            raw_bytes = base64.b64decode(b64_data, validate=True)
        except Exception:
            continue
        decoded = _decode_png_rgb(raw_bytes)
        if decoded is not None:
            src_w, src_h, rgb = decoded
            return _resize_rgb_to_bmp_pixels(src_w, src_h, rgb)

    return default_pixels


def _extract_xgcode_header_from_text(text: str) -> XgcodeHeader:
    extruder_0_temp, hotbed_temp = _parse_temperatures(text)
    return XgcodeHeader(
        print_time=_parse_print_time_seconds(text),
        filament_0_usage=_parse_filament_mm(text),
        multi_extruder_type=_parse_multi_extruder_type(text),
        layer_height=_parse_layer_height_um(text),
        perimeter_shells=_parse_perimeter_shells(text),
        print_speed=_parse_print_speed_mms(text),
        hotbed_temp=hotbed_temp,
        extruder_0_temp=extruder_0_temp,
    )


def _build_bmp_bytes(bmp_pixels: bytes) -> bytes:
    """Wrap 80x60 24-bit BGR pixel buffer in a 54-byte BMP container."""
    pixel_size = _THUMB_WIDTH * _THUMB_HEIGHT * 3
    bmp_size = 54 + pixel_size
    bmp_file_header = struct.pack("<2sIHHI", b"BM", bmp_size, 0, 0, 54)
    bmp_info_header = struct.pack(
        "<IiiHHIIiiII",
        40,
        _THUMB_WIDTH,
        _THUMB_HEIGHT,
        1,
        24,
        0,
        pixel_size,
        2835,
        2835,
        0,
        0,
    )
    return bmp_file_header + bmp_info_header + bmp_pixels


def extract_xgcode_header(
    gcode_bytes: bytes,
    scan_bytes: int | None = DEFAULT_METADATA_SCAN_BYTES,
) -> XgcodeHeader:
    """Extract xgcode header metadata from OrcaSlicer G-code bytes."""
    return _extract_xgcode_header_from_text(_decode_scan_window(gcode_bytes, scan_bytes))


def extract_job_metadata(
    filename: str,
    data: bytes,
    scan_bytes: int | None = DEFAULT_METADATA_SCAN_BYTES,
) -> dict[str, Any]:
    """Extract xgcode header fields and BMP thumbnail Data URL from G-code or .gx bytes."""
    bmp_bytes: bytes | None = None
    if data.startswith(b"xgcode 1.0") and len(data) >= 58:
        bmp_offset, gcode_offset = struct.unpack("<II", data[16:24])
        fields = struct.unpack("<IIIHHHHHHHHH", data[28:58])
        header = XgcodeHeader(
            print_time=fields[0],
            filament_0_usage=fields[1],
            filament_1_usage=fields[2],
            multi_extruder_type=fields[3],
            layer_height=fields[4],
            reserved0=fields[5],
            perimeter_shells=fields[6],
            print_speed=fields[7],
            hotbed_temp=fields[8],
            extruder_0_temp=fields[9],
            extruder_1_temp=fields[10],
            reserved1=fields[11],
        )
        if (
            0x3A <= bmp_offset < gcode_offset <= len(data)
            and data[bmp_offset : bmp_offset + 2] == b"BM"
            and (gcode_offset - bmp_offset) <= 262144
        ):
            bmp_bytes = data[bmp_offset:gcode_offset]
    else:
        text = _decode_scan_window(data, scan_bytes)
        header = _extract_xgcode_header_from_text(text)
        bmp_bytes = _build_bmp_bytes(_extract_thumbnail_bmp_pixels_from_text(text))

    return {
        "filename": filename,
        "print_time": header.print_time,
        "filament_0_usage": header.filament_0_usage,
        "layer_height": header.layer_height,
        "print_speed": header.print_speed,
        "hotbed_temp": header.hotbed_temp,
        "extruder_0_temp": header.extruder_0_temp,
        "thumbnail_data_url": (
            f"data:image/bmp;base64,{base64.b64encode(bmp_bytes).decode('ascii')}"
            if bmp_bytes is not None
            else None
        ),
    }


class FlashforgeError(Exception):
    """Raised when communication with the Flashforge printer fails."""


class FlashforgeTransferCancelledError(FlashforgeError):
    """Raised when an in-progress file transfer is cancelled by the caller."""


class FlashforgeClient:
    """Thread-safe TCP client for Flashforge Adventurer 4."""

    CHUNK_SIZE = 4096
    PACKET_MAGIC = b"\x5a\x5a\xa5\xa5"
    MAX_RESPONSE_BYTES = 1024 * 1024

    def __init__(
        self,
        host: str,
        port: int = 8899,
        timeout: float = 5.0,
        transfer_timeout: float = 30.0,
        metadata_scan_bytes: int | None = DEFAULT_METADATA_SCAN_BYTES,
    ) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.transfer_timeout = transfer_timeout
        self.metadata_scan_bytes = (
            None if metadata_scan_bytes is None else int(metadata_scan_bytes)
        )
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
        total_bytes = 0
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

                total_bytes += len(chunk)
                if total_bytes > self.MAX_RESPONSE_BYTES:
                    raise FlashforgeError("Printer response exceeded maximum allowed size.")

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

    @staticmethod
    def parse_position(raw: str) -> dict[str, float | None]:
        """Parse ~M114 response (e.g. 'X:0.00 Y:0.00 Z:0.00 A:0.00 B:0.00')."""
        def _axis(label: str) -> float | None:
            match = re.search(rf"\b{label}:\s*(-?\d+(?:\.\d+)?)", raw)
            return float(match.group(1)) if match else None

        x = _axis("X")
        y = _axis("Y")
        z = _axis("Z")
        e = _axis("E")
        if e is None:
            e = _axis("A")
        return {"x": x, "y": y, "z": z, "e": e}

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

    def get_position(self, quiet: bool = False) -> dict[str, float | None]:
        return self.parse_position(self.send_command("~M114", quiet=quiet))

    def get_full_status(self) -> dict[str, Any]:
        """Fetch temperature, progress, machine status, and position in one locked sequence."""
        with self._lock:
            temps = self.get_temperatures(quiet=True)
            progress = self.get_progress(quiet=True)
            status = self.get_status(quiet=True)
            try:
                position: dict[str, float | None] | None = self.get_position(quiet=True)
            except FlashforgeError:
                position = None

        return {
            "connected": True,
            "error": None,
            "machine_status": status["machine_status"],
            "move_mode": status["move_mode"],
            "led": status["led"],
            "current_file": status["current_file"],
            "temperatures": temps,
            "progress": progress,
            "position": position,
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

    def set_nozzle_temp(self, temp: int) -> str:
        target = int(temp)
        if not 0 <= target <= 300:
            raise ValueError("Nozzle temperature must be between 0 and 300")
        return self.send_command(f"~M104 S{target} T0")

    def set_bed_temp(self, temp: int) -> str:
        target = int(temp)
        if not 0 <= target <= 120:
            raise ValueError("Bed temperature must be between 0 and 120")
        return self.send_command(f"~M140 S{target}")

    def set_print_speed(self, percent: int) -> str:
        speed = int(percent)
        if not 10 <= speed <= 200:
            raise ValueError("Print speed must be between 10 and 200%")
        return self.send_command(f"~M663 S{speed}")

    def set_fan(self, on: bool) -> str:
        """Turn the part cooling fan ON (~M106) or OFF (~M107)."""
        return self.send_command("~M106" if on else "~M107")

    def enable_steppers(self) -> str:
        """Enable stepper motors (~M17)."""
        return self.send_command("~M17")

    def disable_steppers(self) -> str:
        """Disable stepper motors to allow manual movement (~M18)."""
        return self.send_command("~M18")

    def emergency_stop(self) -> str:
        """Immediately halt all printer operations (~M112)."""
        return self.send_command("~M112")

    def home_axes(self, axes: str = "") -> str:
        """Home all axes (~G28 X Y Z) or specified X/Y/Z axes."""
        valid = [ch for ch in axes.upper() if ch in {"X", "Y", "Z"}]
        unique_axes = list(dict.fromkeys(valid))
        cmd = f"~G28 {' '.join(unique_axes)}" if unique_axes else "~G28 X Y Z"
        return self.send_command(cmd, timeout=self.transfer_timeout)

    def move_axis_relative(self, axis: str, delta: float, feedrate: int = 3000) -> str:
        """Move X/Y/Z axis by relative delta (mm), always restoring absolute mode (~G90)."""
        ax = axis.strip().upper()
        if ax not in {"X", "Y", "Z"}:
            raise ValueError(f"Unsupported axis: {axis!r}")
        dist = float(delta)
        if not -200.0 <= dist <= 200.0:
            raise ValueError("Relative move distance must be between -200 and 200 mm")
        speed = max(60, min(12000, int(feedrate)))

        with self._lock:
            self.send_command("~G91")
            try:
                return self.send_command(f"~G1 {ax}{dist:.2f} F{speed}")
            finally:
                try:
                    self.send_command("~G90")
                except FlashforgeError as exc:
                    logger.warning("Failed to restore absolute mode (~G90): %s", exc)

    def _recv_until_marker(self, buf: bytearray, marker: bytes) -> bytes:
        assert self._sock is not None
        while marker not in buf:
            try:
                chunk = self._sock.recv(8192)
            except socket.timeout as exc:
                raise FlashforgeError("Timed out waiting for printer binary response header.") from exc
            except OSError as exc:
                raise FlashforgeError(f"Socket receive failed: {exc}") from exc
            if not chunk:
                raise FlashforgeError("Printer closed the connection.")
            buf.extend(chunk)
            if len(buf) > self.MAX_RESPONSE_BYTES:
                raise FlashforgeError("Printer response exceeded maximum allowed size.")
        idx = buf.find(marker) + len(marker)
        out = bytes(buf[:idx])
        del buf[:idx]
        return out

    def _recv_exact_buffered(self, buf: bytearray, count: int) -> bytes:
        assert self._sock is not None
        while len(buf) < count:
            try:
                chunk = self._sock.recv(max(4096, count - len(buf)))
            except socket.timeout as exc:
                raise FlashforgeError("Timed out reading binary payload from printer.") from exc
            except OSError as exc:
                raise FlashforgeError(f"Socket receive failed: {exc}") from exc
            if not chunk:
                raise FlashforgeError("Printer closed the connection during binary read.")
            buf.extend(chunk)
        out = bytes(buf[:count])
        del buf[:count]
        return out

    def list_files(self) -> list[dict[str, str]]:
        """Fetch internal storage file list via ~M661 binary response."""
        with self._lock:
            self._ensure_connected()
            assert self._sock is not None
            orig_timeout = self._sock.gettimeout()
            self._sock.settimeout(max(self.timeout, 10.0))
            buf = bytearray()
            try:
                self._send_raw("~M661\r\n")
                self._recv_until_marker(buf, b"ok\r\n")
                magic, count = struct.unpack(">4sI", self._recv_exact_buffered(buf, 8))
                if magic != b"D\xaa\xaaD":
                    raise FlashforgeError(f"Unexpected M661 magic: {magic!r}")
                if count > 5000:
                    raise FlashforgeError(f"Unexpected M661 file count: {count}")

                files: list[dict[str, str]] = []
                for _ in range(count):
                    delim, length = struct.unpack(">4sI", self._recv_exact_buffered(buf, 8))
                    if delim != b"::\xa3\xa3" or length > 1024:
                        raise FlashforgeError(f"Invalid M661 entry header: delim={delim!r} len={length}")
                    path = self._recv_exact_buffered(buf, length).decode("utf-8", errors="replace")
                    name = path.rsplit("/", 1)[-1]
                    files.append({"path": path, "name": name})
                return files
            except Exception as exc:
                self._close_socket()
                if isinstance(exc, FlashforgeError):
                    raise
                raise FlashforgeError(f"Failed to list files (~M661): {exc}") from exc
            finally:
                if self._sock is not None:
                    try:
                        self._sock.settimeout(orig_timeout)
                    except OSError:
                        pass

    @staticmethod
    def normalize_storage_path(file_path: str) -> str:
        """Validate and normalize an Adventurer 4 internal storage path (/data/<name>)."""
        raw = file_path.strip()
        if not raw or "\r" in raw or "\n" in raw or "\x00" in raw:
            raise ValueError("Invalid storage file path")
        if raw.startswith("0:/user/"):
            raw = "/data/" + raw[len("0:/user/") :]
        elif not raw.startswith("/data/"):
            raw = "/data/" + raw.lstrip("/")
        name = raw[len("/data/") :]
        if not name or "/" in name or "\\" in name or name in {".", ".."} or ".." in name:
            raise ValueError(f"Unsafe storage file path: {file_path!r}")
        return f"/data/{name}"

    def get_file_thumbnail(self, file_path: str) -> bytes | None:
        """Fetch embedded 80x60 BMP thumbnail for an internal file via ~M662."""
        norm_path = self.normalize_storage_path(file_path)
        with self._lock:
            self._ensure_connected()
            assert self._sock is not None
            orig_timeout = self._sock.gettimeout()
            self._sock.settimeout(max(self.timeout, 10.0))
            buf = bytearray()
            try:
                self._send_raw(f"~M662 {norm_path}\r\n")
                header = self._recv_until_marker(buf, b"ok\r\n")
                if b"error" in header.lower():
                    return None
                if len(buf) < 8:
                    self._sock.settimeout(min(self.timeout, 1.5))
                    try:
                        header_bin = self._recv_exact_buffered(buf, 8)
                    except FlashforgeError:
                        return None
                    self._sock.settimeout(max(self.timeout, 10.0))
                else:
                    header_bin = self._recv_exact_buffered(buf, 8)
                magic, bmp_len = struct.unpack(">4sI", header_bin)
                if magic != b"**\xa2\xa2" or bmp_len == 0 or bmp_len > 256 * 1024:
                    return None
                bmp_bytes = self._recv_exact_buffered(buf, bmp_len)
                return bmp_bytes if bmp_bytes.startswith(b"BM") else None
            except Exception as exc:
                self._close_socket()
                if isinstance(exc, FlashforgeError):
                    raise
                raise FlashforgeError(f"Failed to fetch thumbnail (~M662): {exc}") from exc
            finally:
                if self._sock is not None:
                    try:
                        self._sock.settimeout(orig_timeout)
                    except OSError:
                        pass

    def start_existing_print(self, file_path: str) -> str:
        """Start printing an existing file on internal storage via ~M23 0:/user/<name>."""
        norm_path = self.normalize_storage_path(file_path)
        name = norm_path[len("/data/") :]
        self.send_command(f"~M23 0:/user/{name}", timeout=self.transfer_timeout)
        return name

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Keep only the safe basename for 0:/user/<filename>."""
        base = os.path.basename(filename.strip())
        sanitized = re.sub(r"[^A-Za-z0-9._-]", "_", base)
        return sanitized or "print_job.gcode"

    @staticmethod
    def wrap_gcode_as_gx(
        gcode_bytes: bytes,
        metadata_scan_bytes: int | None = DEFAULT_METADATA_SCAN_BYTES,
    ) -> bytes:
        """Wrap plain ASCII G-code in a Flashforge xgcode 1.0 (.gx) container."""
        if gcode_bytes.startswith(b"xgcode 1.0"):
            return gcode_bytes

        bmp_offset = 58  # 0x3A
        text = _decode_scan_window(gcode_bytes, metadata_scan_bytes)
        bmp_bytes = _build_bmp_bytes(_extract_thumbnail_bmp_pixels_from_text(text))
        gcode_offset = bmp_offset + len(bmp_bytes)  # 14512 (0x38B0)

        header = _extract_xgcode_header_from_text(text)
        logger.debug(
            "xgcode header metadata: print_time=%ds filament_0=%dmm layer=%dum speed=%dmm/s nozzle=%dC bed=%dC",
            header.print_time,
            header.filament_0_usage,
            header.layer_height,
            header.print_speed,
            header.extruder_0_temp,
            header.hotbed_temp,
        )
        gx_header = header.to_bytes(bmp_offset=bmp_offset, gcode_offset=gcode_offset)
        return gx_header + bmp_bytes + gcode_bytes

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
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
    ) -> str:
        """Transfer G-code/GX bytes via ~M28 ... ~M29 and optionally start with ~M23."""
        safe_name = self.sanitize_filename(filename)
        if not safe_name.lower().endswith(".gx"):
            stem, _ = os.path.splitext(safe_name)
            safe_name = f"{stem}.gx"
        data = self.wrap_gcode_as_gx(data, metadata_scan_bytes=self.metadata_scan_bytes)
        remote_path = f"0:/user/{safe_name}"
        file_size = len(data)
        mode = (transfer_mode or os.getenv("FLASHFORGE_TRANSFER_MODE", "raw")).strip().lower()

        with self._lock:
            if cancel_check is not None and cancel_check():
                raise FlashforgeTransferCancelledError("File transfer cancelled before start")
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

                bytes_sent = 0
                for counter, offset in enumerate(range(0, file_size, self.CHUNK_SIZE)):
                    if cancel_check is not None and cancel_check():
                        raise FlashforgeTransferCancelledError("File transfer cancelled")
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
                    bytes_sent += len(chunk)
                    if progress_callback is not None and (
                        counter == 0 or (counter + 1) % 8 == 0 or bytes_sent >= file_size
                    ):
                        progress_callback(bytes_sent, file_size)
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
                    if cancel_check is not None and cancel_check():
                        raise FlashforgeTransferCancelledError("Print start cancelled")
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
