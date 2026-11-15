"""Local bridge server (Web UI + OctoPrint-compatible API) for Flashforge Adventurer 4."""

from __future__ import annotations

import atexit
import json
import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

from bridge_services import build_services
from flashforge_client import FlashforgeClient, FlashforgeError

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load configuration from config.json with optional environment overrides."""
    with path.open("r", encoding="utf-8") as f:
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


class _StatusPollingFilter(logging.Filter):
    """Suppress noisy Werkzeug access logs for successful /api/printer/status polls."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return not ("GET /api/printer/status " in msg and '" 200 ' in msg)


def configure_logging(debug: bool = False) -> None:
    """Configure application logging (LOG_LEVEL env var overrides server.debug)."""
    env_level = os.getenv("LOG_LEVEL", "").strip().upper()
    if env_level and hasattr(logging, env_level):
        level = getattr(logging, env_level)
    else:
        level = logging.DEBUG if debug else logging.INFO

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("werkzeug").addFilter(_StatusPollingFilter())


def create_app(
    config: dict[str, Any] | None = None,
    client: FlashforgeClient | None = None,
    start_background: bool | None = None,
) -> Flask:
    cfg = config or load_config()
    printer_cfg = cfg["printer"]

    ff_client = client or FlashforgeClient(
        host=printer_cfg["ip"],
        port=int(printer_cfg.get("port", 8899)),
        timeout=float(printer_cfg.get("timeout", 5.0)),
        transfer_timeout=float(printer_cfg.get("transfer_timeout", 30.0)),
    )

    temp_history, camera_relay, snapshot_manager, monitor = build_services(
        cfg=cfg,
        base_dir=BASE_DIR,
        ff_client=ff_client,
    )
    should_start_bg = (client is None) if start_background is None else start_background
    if should_start_bg:
        monitor.start()

    if client is None:
        atexit.register(ff_client.close)
    atexit.register(monitor.stop)

    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
    app.config["PRINTER_CONFIG"] = printer_cfg
    app.config["BRIDGE_SERVICES"] = {
        "temp_history": temp_history,
        "camera_relay": camera_relay,
        "snapshot_manager": snapshot_manager,
        "monitor": monitor,
    }

    # ------------------------------------------------------------------
    # Device Tab Web UI
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            camera_url=printer_cfg.get(
                "camera_url", f"http://{printer_cfg['ip']}:8080/?action=stream"
            ),
            printer_ip=printer_cfg["ip"],
        )

    @app.get("/api/printer/status")
    def printer_status():
        try:
            status = ff_client.get_full_status()
            monitor.observe_status(status)
            return jsonify(status)
        except FlashforgeError as exc:
            return jsonify(
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
                }
            )

    @app.get("/api/printer/info")
    def printer_info():
        try:
            return jsonify({"connected": True, "info": ff_client.get_info()})
        except FlashforgeError as exc:
            return jsonify({"connected": False, "error": str(exc)}), 502

    @app.post("/api/printer/pause")
    def printer_pause():
        try:
            ff_client.pause_print()
            return jsonify({"ok": True, "action": "pause"})
        except FlashforgeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/api/printer/resume")
    def printer_resume():
        try:
            ff_client.resume_print()
            return jsonify({"ok": True, "action": "resume"})
        except FlashforgeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/api/printer/cancel")
    def printer_cancel():
        try:
            ff_client.cancel_print()
            return jsonify({"ok": True, "action": "cancel"})
        except FlashforgeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    @app.post("/api/printer/led")
    def printer_led():
        payload = request.get_json(silent=True) or {}
        on = bool(payload.get("on", True))
        try:
            ff_client.set_led(on)
            return jsonify({"ok": True, "led": on})
        except FlashforgeError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 502

    # ------------------------------------------------------------------
    # Camera Relay, Snapshots & Temperature History
    # ------------------------------------------------------------------

    @app.get("/api/camera/stream")
    def camera_stream():
        return Response(
            camera_relay.iter_mjpeg_frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.post("/api/camera/reconnect")
    def camera_reconnect():
        camera_relay.request_reconnect()
        return jsonify({"ok": True})

    @app.get("/api/camera/snapshots")
    def camera_snapshots_list():
        return jsonify(snapshot_manager.to_payload())

    @app.post("/api/camera/snapshots")
    def camera_snapshots_capture():
        frame = camera_relay.get_latest_frame(wait_timeout=5.0)
        if not frame:
            return jsonify({"ok": False, "error": "カメラフレームを取得できませんでした"}), 502
        saved = snapshot_manager.save_frame(frame)
        return jsonify({"ok": True, "snapshot": saved}), 201

    @app.get("/api/camera/snapshots/<filename>")
    def camera_snapshot_file(filename: str):
        path = snapshot_manager.get_snapshot_path(filename)
        if path is None:
            return jsonify({"error": "Snapshot not found"}), 404
        return send_file(path, mimetype="image/jpeg")

    @app.get("/api/history/temperature")
    def temperature_history():
        return jsonify(temp_history.to_payload())

    # ------------------------------------------------------------------
    # OctoPrint Compatibility API (for OrcaSlicer)
    # ------------------------------------------------------------------

    @app.get("/api/version")
    def octoprint_version():
        return jsonify(
            {
                "api": "0.1",
                "server": "1.9.0",
                "text": "OctoPrint 1.9.0",
            }
        )

    @app.get("/api/settings")
    def octoprint_settings():
        return jsonify(
            {
                "api": {"allowCrossOrigin": True},
                "feature": {"sdSupport": True},
                "webcam": {
                    "streamUrl": printer_cfg.get(
                        "camera_url", f"http://{printer_cfg['ip']}:8080/?action=stream"
                    )
                },
            }
        )

    @app.post("/api/files/local")
    def octoprint_upload_local():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "No file provided in 'file' field."}), 400

        should_print = parse_bool_flag(request.form.get("print"))
        file_bytes = uploaded.read()
        logger.info(
            "Received /api/files/local upload: filename=%s size=%d bytes print=%s",
            uploaded.filename,
            len(file_bytes),
            should_print,
        )

        try:
            saved_name = ff_client.send_file(
                filename=uploaded.filename,
                data=file_bytes,
                start_print=should_print,
            )
        except FlashforgeError as exc:
            logger.error("Upload/print failed for %s: %s", uploaded.filename, exc)
            return jsonify({"error": str(exc)}), 502

        return (
            jsonify(
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
                }
            ),
            201,
        )

    return app


if __name__ == "__main__":
    config = load_config()
    server_cfg = config["server"]
    debug_enabled = bool(server_cfg.get("debug", False))
    configure_logging(debug=debug_enabled)
    application = create_app(config=config)
    application.run(
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 5000)),
        debug=debug_enabled,
        threaded=True,
    )
