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

from bridge_services import (
    BackgroundMonitor,
    CameraRelay,
    SnapshotManager,
    TemperatureHistory,
    build_services,
)
from flashforge_client import FlashforgeClient, FlashforgeError

logger = logging.getLogger(__name__)

PLUGIN_DIR = Path(__file__).resolve().parent
REPO_ROOT = PLUGIN_DIR.parent


def _resolve_asset(relative_path: str) -> Path:
    """Locate an asset in the plugin directory first, then the repo root."""
    local = PLUGIN_DIR / relative_path
    if local.exists():
        return local
    return REPO_ROOT / relative_path


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load configuration from config.json (falling back to config-example.json) with env overrides."""
    if path is None:
        cfg_path = _resolve_asset("config.json")
        if not cfg_path.exists():
            cfg_path = _resolve_asset("config-example.json")
    else:
        cfg_path = path

    with cfg_path.open("r", encoding="utf-8") as f:
        config = json.load(f)

    printer_cfg = config.setdefault("printer", {})
    server_cfg = config.setdefault("server", {})

    if os.getenv("PRINTER_IP"):
        printer_cfg["ip"] = os.environ["PRINTER_IP"]
    if os.getenv("PRINTER_PORT"):
        printer_cfg["port"] = int(os.environ["PRINTER_PORT"])
    if os.getenv("CAMERA_URL"):
        printer_cfg["camera_url"] = os.environ["CAMERA_URL"]
    if os.getenv("SERVER_PORT"):
        server_cfg["port"] = int(os.environ["SERVER_PORT"])

    return config


def parse_bool_flag(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def render_dashboard_html(template_text: str, camera_url: str, printer_ip: str) -> str:
    """Render templates/index.html without requiring Jinja2."""
    rendered = template_text.replace("{{ printer_ip }}", html.escape(printer_ip, quote=True))
    rendered = rendered.replace("{{ camera_url }}", html.escape(camera_url, quote=True))
    rendered = rendered.replace("{{ camera_url | tojson }}", json.dumps(camera_url))
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
    printer_cfg: dict[str, Any],
    ff_client: FlashforgeClient,
    template_path: Path,
    temp_history: TemperatureHistory,
    camera_relay: CameraRelay,
    snapshot_manager: SnapshotManager,
    monitor: BackgroundMonitor,
) -> type[BaseHTTPRequestHandler]:
    camera_url = printer_cfg.get(
        "camera_url", f"http://{printer_cfg['ip']}:8080/?action=stream"
    )
    printer_ip = printer_cfg["ip"]

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
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, status: int, data: Any) -> None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self._send_bytes(status, "application/json; charset=utf-8", body)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(length) if length > 0 else b""

        def do_OPTIONS(self) -> None:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Api-Key")
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
                cam_status = camera_relay.get_status()
                try:
                    status = ff_client.get_full_status()
                    monitor.observe_status(status)
                    status["camera"] = cam_status
                    self._send_json(HTTPStatus.OK, status)
                except FlashforgeError as exc:
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "connected": False,
                            "error": str(exc),
                            "machine_status": "DISCONNECTED",
                            "move_mode": "UNKNOWN",
                            "led": None,
                            "current_file": None,
                            "temperatures": {
                                "nozzle": {"actual": None, "target": None},
                                "bed": {"actual": None, "target": None},
                            },
                            "progress": {
                                "is_printing": False,
                                "bytes_printed": 0,
                                "bytes_total": 0,
                                "percent": 0.0,
                            },
                            "camera": cam_status,
                        },
                    )
                return

            if path == "/api/printer/info":
                try:
                    self._send_json(HTTPStatus.OK, {"connected": True, "info": ff_client.get_info()})
                except FlashforgeError as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"connected": False, "error": str(exc)})
                return

            if path == "/api/version":
                self._send_json(
                    HTTPStatus.OK,
                    {"api": "0.1", "server": "1.9.0", "text": "OctoPrint 1.9.0"},
                )
                return

            if path == "/api/settings":
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "api": {"allowCrossOrigin": True},
                        "feature": {"sdSupport": True},
                        "webcam": {"streamUrl": camera_url},
                    },
                )
                return

            if path == "/api/camera/stream":
                self.close_connection = True
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                try:
                    for chunk in camera_relay.iter_mjpeg_frames():
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                return

            if path == "/api/camera/snapshots":
                self._send_json(HTTPStatus.OK, snapshot_manager.to_payload())
                return

            if path.startswith("/api/camera/snapshots/"):
                filename = path[len("/api/camera/snapshots/") :]
                snap_path = snapshot_manager.get_snapshot_path(filename)
                if snap_path is None:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Snapshot not found"})
                    return
                self._send_bytes(HTTPStatus.OK, "image/jpeg", snap_path.read_bytes())
                return

            if path == "/api/history/temperature":
                self._send_json(HTTPStatus.OK, temp_history.to_payload())
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            body = self._read_body()

            if path == "/api/printer/pause":
                self._handle_control("pause", ff_client.pause_print)
                return
            if path == "/api/printer/resume":
                self._handle_control("resume", ff_client.resume_print)
                return
            if path == "/api/printer/cancel":
                self._handle_control("cancel", ff_client.cancel_print)
                return

            if path == "/api/printer/led":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except json.JSONDecodeError:
                    payload = {}
                on = bool(payload.get("on", True))
                try:
                    ff_client.set_led(on)
                    self._send_json(HTTPStatus.OK, {"ok": True, "led": on})
                except FlashforgeError as exc:
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})
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
                logger.info(
                    "Received /api/files/local upload: filename=%s size=%d bytes print=%s",
                    filename,
                    len(file_bytes),
                    should_print,
                )
                try:
                    saved_name = ff_client.send_file(
                        filename=filename,
                        data=file_bytes,
                        start_print=should_print,
                    )
                except FlashforgeError as exc:
                    logger.error("Upload/print failed for %s: %s", filename, exc)
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                    return

                self._send_json(
                    HTTPStatus.CREATED,
                    {
                        "files": {
                            "local": {
                                "name": saved_name,
                                "origin": "local",
                                "path": saved_name,
                                "size": len(file_bytes),
                            }
                        },
                        "done": True,
                    },
                )
                return

            if path == "/api/camera/reconnect":
                camera_relay.request_reconnect()
                self._send_json(HTTPStatus.OK, {"ok": True})
                return

            if path == "/api/camera/snapshots":
                frame = camera_relay.get_latest_frame(wait_timeout=5.0)
                if not frame:
                    self._send_json(
                        HTTPStatus.BAD_GATEWAY,
                        {"ok": False, "error": "カメラフレームを取得できませんでした"},
                    )
                    return
                saved = snapshot_manager.save_frame(frame)
                self._send_json(HTTPStatus.CREATED, {"ok": True, "snapshot": saved})
                return

            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"Not found: {path}"})

        def _handle_control(self, action: str, fn: Any) -> None:
            try:
                fn()
                self._send_json(HTTPStatus.OK, {"ok": True, "action": action})
            except FlashforgeError as exc:
                self._send_json(HTTPStatus.BAD_GATEWAY, {"ok": False, "error": str(exc)})

    return BridgeRequestHandler


class BridgeServer:
    """Managed background HTTP server wrapping ThreadingHTTPServer + FlashforgeClient."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        client: FlashforgeClient | None = None,
        template_path: Path | None = None,
        start_background: bool | None = None,
    ) -> None:
        self.config = config or load_config()
        printer_cfg = self.config["printer"]
        server_cfg = self.config["server"]

        self.host: str = server_cfg.get("host", "127.0.0.1")
        self.port: int = int(server_cfg.get("port", 5000))
        self.client = client or FlashforgeClient(
            host=printer_cfg["ip"],
            port=int(printer_cfg.get("port", 8899)),
            timeout=float(printer_cfg.get("timeout", 5.0)),
            transfer_timeout=float(printer_cfg.get("transfer_timeout", 30.0)),
        )
        self._owns_client = client is None
        self._start_background = (client is None) if start_background is None else start_background
        self._template_path = template_path or _resolve_asset("templates/index.html")
        (
            self.temp_history,
            self.camera_relay,
            self.snapshot_manager,
            self.monitor,
        ) = build_services(
            cfg=self.config,
            base_dir=PLUGIN_DIR,
            ff_client=self.client,
        )
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
            printer_cfg=self.config["printer"],
            ff_client=self.client,
            template_path=self._template_path,
            temp_history=self.temp_history,
            camera_relay=self.camera_relay,
            snapshot_manager=self.snapshot_manager,
            monitor=self.monitor,
        )
        ThreadingHTTPServer.allow_reuse_address = True
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="flashforge-adv4-bridge",
            daemon=True,
        )
        self._thread.start()
        if self._start_background:
            self.monitor.start()
        logger.info("BridgeServer listening on http://%s:%d", self.host, self.port)
        return self.port

    def stop(self) -> None:
        """Shut down the HTTP server and release the printer control session (~M602)."""
        self.monitor.stop()
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
        if self._owns_client:
            self.client.close()


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
