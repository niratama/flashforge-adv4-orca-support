"""Zero-dependency HTTP bridge server for Flashforge Adventurer 4 (OrcaSlicer plugin & standalone)."""

from __future__ import annotations

import html
import json
import logging
import os
import threading
from email.parser import BytesParser
from email.policy import default as email_default_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from bridge_controller import (
    BridgeController,
    is_same_origin_request,
    load_config as _load_config,
    parse_bool_flag,
    verify_api_key,
)
from flashforge_client import FlashforgeClient

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = PLUGIN_DIR.parent if PLUGIN_DIR.name == "lib" else PLUGIN_DIR
REPO_ROOT = PLUGIN_DIR.parent


def _resolve_asset(relative_path: str) -> Path:
    """Locate an asset in the plugin root directory first, then the repo root."""
    local = PLUGIN_ROOT / relative_path
    if local.exists():
        return local
    return REPO_ROOT / relative_path


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load configuration from config.json (falling back to config-example.json) with env overrides."""
    return _load_config(path=path, search_dirs=(PLUGIN_ROOT, REPO_ROOT))


def _safe_json_for_script(value: Any) -> str:
    """Serialize JSON safely for inline <script> embedding."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_dashboard_html(
    template_text: str,
    camera_url: str,
    printer_ip: str,
    http_base_url: str = "",
) -> str:
    """Render templates/index.html without requiring Jinja2."""
    rendered = template_text.replace("{{ printer_ip }}", html.escape(printer_ip, quote=True))
    rendered = rendered.replace("{{ camera_url }}", html.escape(camera_url, quote=True))
    rendered = rendered.replace("{{ camera_url | tojson }}", _safe_json_for_script(camera_url))
    rendered = rendered.replace(
        '{{ http_base_url | default("") | tojson }}',
        _safe_json_for_script(http_base_url),
    )
    return rendered


def parse_multipart_form(
    content_type: str, body: bytes
) -> tuple[dict[str, str], dict[str, tuple[str, bytes]]]:
    """Parse multipart/form-data using Python's stdlib email parser."""
    if "multipart/form-data" not in content_type.lower():
        return {}, {}

    raw_message = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("latin-1") + body
    )
    msg = BytesParser(policy=email_default_policy).parsebytes(raw_message)
    if not msg.is_multipart():
        return {}, {}

    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in msg.iter_parts():
        disp_name = part.get_param("name", header="content-disposition")
        if not disp_name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename is not None:
            files[disp_name] = (filename, payload)
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[disp_name] = payload.decode(charset, errors="replace")

    return fields, files


def _make_handler(
    controller: BridgeController,
    template_path: Path,
) -> type[BaseHTTPRequestHandler]:
    camera_url = controller.camera_url
    printer_ip = controller.printer_ip

    class BridgeRequestHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            if args and isinstance(args[0], str) and "GET /api/printer/status " in args[0]:
                if len(args) > 1 and str(args[1]) == "200":
                    return
            logger.info("%s - %s", self.address_string(), format % args)

        def _send_bytes(self, status: int, content_type: str, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", body)

        def _read_body(self, max_bytes: int) -> bytes:
            raw_len = self.headers.get("Content-Length", "0") or "0"
            try:
                length = int(raw_len)
            except ValueError as exc:
                raise ValueError("Invalid Content-Length header") from exc
            if length < 0:
                raise ValueError("Negative Content-Length header")
            if length > max_bytes:
                raise OverflowError(f"Request body exceeds limit ({length} > {max_bytes} bytes)")
            return self.rfile.read(length) if length > 0 else b""

        def _read_json_body(self, body: bytes) -> dict[str, Any]:
            if not body:
                return {}
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON body must be an object")
            return parsed

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path

            if path == "/":
                tpl = template_path.read_text(encoding="utf-8")
                rendered = render_dashboard_html(tpl, camera_url=camera_url, printer_ip=printer_ip)
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", rendered.encode("utf-8"))
                return

            if path == "/api/printer/status":
                self._send_json(HTTPStatus.OK, controller.get_printer_status())
                return

            if path == "/api/printer/info":
                status, data = controller.get_printer_info()
                self._send_json(status, data)
                return

            if path == "/api/version":
                self._send_json(HTTPStatus.OK, controller.get_octoprint_version())
                return

            if path == "/api/settings":
                self._send_json(HTTPStatus.OK, controller.get_octoprint_settings())
                return

            if path == "/api/camera/stream":
                self.close_connection = True
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    for chunk in controller.camera_relay.iter_mjpeg_frames():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            if path == "/api/camera/snapshots":
                self._send_json(HTTPStatus.OK, controller.list_snapshots())
                return

            if path.startswith("/api/camera/snapshots/"):
                filename = path[len("/api/camera/snapshots/") :]
                snap_path = controller.get_snapshot_path(filename)
                if snap_path is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                self._send_bytes(HTTPStatus.OK, "image/jpeg", snap_path.read_bytes())
                return

            if path == "/api/history/temperature":
                self._send_json(HTTPStatus.OK, controller.get_temperature_history())
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:
            if not is_same_origin_request(
                self.headers.get("Origin"),
                self.headers.get("Referer"),
                self.headers.get("Host"),
            ):
                self._send_json(
                    HTTPStatus.FORBIDDEN,
                    {"ok": False, "error": "Cross-origin POST request rejected"},
                )
                return

            if not verify_api_key(
                controller.api_key,
                self.headers.get("X-Api-Key"),
                self.headers.get("Authorization"),
            ):
                self._send_json(
                    HTTPStatus.UNAUTHORIZED,
                    {"ok": False, "error": "Unauthorized"},
                )
                return

            path = urlparse(self.path).path
            max_upload = int(controller.server_cfg.get("max_upload_bytes", 100 * 1024 * 1024))
            max_bytes = max_upload if path == "/api/files/local" else 1024 * 1024
            try:
                body = self._read_body(max_bytes=max_bytes)
            except OverflowError as exc:
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": str(exc)})
                return
            except ValueError as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                return

            if path == "/api/printer/pause":
                self._send_json(*controller.pause_print())
                return
            if path == "/api/printer/resume":
                self._send_json(*controller.resume_print())
                return
            if path == "/api/printer/cancel":
                self._send_json(*controller.cancel_print())
                return

            if path in {
                "/api/printer/led",
                "/api/printer/temperature/nozzle",
                "/api/printer/temperature/bed",
                "/api/printer/speed",
            }:
                try:
                    payload = self._read_json_body(body)
                except (json.JSONDecodeError, ValueError) as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)})
                    return

                if path == "/api/printer/led":
                    self._send_json(*controller.set_led(bool(payload.get("on", True))))
                elif path == "/api/printer/temperature/nozzle":
                    self._send_json(*controller.set_nozzle_temp(payload.get("target")))
                elif path == "/api/printer/temperature/bed":
                    self._send_json(*controller.set_bed_temp(payload.get("target")))
                else:
                    self._send_json(*controller.set_print_speed(payload.get("percent")))
                return

            if path == "/api/files/local":
                content_type = self.headers.get("Content-Type", "")
                fields, files = parse_multipart_form(content_type, body)
                uploaded = files.get("file")
                if uploaded is None or not uploaded[0]:
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "No file provided in 'file' field."},
                    )
                    return

                filename, file_bytes = uploaded
                should_print = parse_bool_flag(fields.get("print"))
                self._send_json(
                    *controller.upload_local_file(
                        filename=filename,
                        file_bytes=file_bytes,
                        should_print=should_print,
                    )
                )
                return

            if path == "/api/camera/reconnect":
                self._send_json(HTTPStatus.OK, controller.reconnect_camera())
                return

            if path == "/api/camera/snapshots":
                self._send_json(*controller.capture_snapshot())
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"})

    return BridgeRequestHandler


class _DaemonThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with daemon request threads so shutdown never hangs on open streams."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False


class BridgeServer:
    """Managed background HTTP server wrapping ThreadingHTTPServer + BridgeController."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        client: FlashforgeClient | None = None,
        template_path: Path | None = None,
        start_background: bool | None = None,
        controller: BridgeController | None = None,
    ) -> None:
        self.config = config or (controller.config if controller is not None else load_config())
        server_cfg = self.config["server"]

        self.host: str = server_cfg.get("host", "127.0.0.1")
        self.port: int = int(server_cfg.get("port", 5000))
        self._owns_controller = controller is None
        self.controller = controller or BridgeController(
            config=self.config,
            base_dir=PLUGIN_ROOT,
            client=client,
            start_background=start_background,
        )
        self.client = self.controller.client
        self.temp_history = self.controller.temp_history
        self.camera_relay = self.controller.camera_relay
        self.snapshot_manager = self.controller.snapshot_manager
        self.monitor = self.controller.monitor
        self._template_path = template_path or _resolve_asset("templates/index.html")
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def is_running(self) -> bool:
        return self._httpd is not None and self._thread is not None and self._thread.is_alive()

    def start(self) -> int:
        """Start the HTTP server in a daemon thread and return the bound port."""
        if self.is_running and self._httpd is not None:
            return int(self._httpd.server_address[1])

        handler_cls = _make_handler(
            controller=self.controller,
            template_path=self._template_path,
        )
        self._httpd = _DaemonThreadingHTTPServer((self.host, self.port), handler_cls)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="flashforge-adv4-bridge",
            daemon=True,
        )
        self._thread.start()
        if self._owns_controller:
            self.controller.start()
        logger.info("BridgeServer listening on http://%s:%d", self.host, self.port)
        return self.port

    def stop(self) -> None:
        """Shut down the HTTP server and release the printer control session (~M602)."""
        if self._owns_controller:
            self.controller.stop()
        if self._httpd is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except OSError:
                pass
            finally:
                self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if os.getenv("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    bridge = BridgeServer()
    bound_port = bridge.start()
    print(f"Serving zero-dependency bridge on http://{bridge.host}:{bound_port}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        bridge.stop()
