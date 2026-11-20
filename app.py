"""Local bridge server (Web UI + OctoPrint-compatible API) for Flashforge Adventurer 4."""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, render_template, request, send_file

from bridge_controller import (
    BridgeController,
    is_same_origin_request,
    load_config as _load_config,
    parse_bool_flag,
    verify_api_key,
)
from flashforge_client import FlashforgeClient

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load configuration from config.json with optional environment overrides."""
    return _load_config(path=path)


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
    controller = BridgeController(
        config=cfg,
        base_dir=BASE_DIR,
        client=client,
        start_background=start_background,
    )
    controller.start()
    atexit.register(controller.stop)

    app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
    app.config["MAX_CONTENT_LENGTH"] = int(
        controller.server_cfg.get("max_upload_bytes", 100 * 1024 * 1024)
    )
    app.config["PRINTER_CONFIG"] = controller.printer_cfg
    app.config["BRIDGE_CONTROLLER"] = controller
    app.config["BRIDGE_SERVICES"] = {
        "temp_history": controller.temp_history,
        "camera_relay": controller.camera_relay,
        "snapshot_manager": controller.snapshot_manager,
        "monitor": controller.monitor,
    }

    @app.before_request
    def _enforce_security_guards():
        if request.method == "POST":
            if not is_same_origin_request(
                request.headers.get("Origin"),
                request.headers.get("Referer"),
                request.headers.get("Host"),
            ):
                return jsonify({"ok": False, "error": "Cross-origin POST request rejected"}), 403
            if not verify_api_key(
                controller.api_key,
                request.headers.get("X-Api-Key"),
                request.headers.get("Authorization"),
            ):
                return jsonify({"ok": False, "error": "Unauthorized"}), 401
        return None

    # ------------------------------------------------------------------
    # Device Tab Web UI
    # ------------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            camera_url=controller.camera_url,
            printer_ip=controller.printer_ip,
        )

    @app.get("/api/printer/status")
    def printer_status():
        return jsonify(controller.get_printer_status())

    @app.get("/api/printer/info")
    def printer_info():
        status_code, data = controller.get_printer_info()
        return jsonify(data), status_code

    @app.post("/api/printer/pause")
    def printer_pause():
        status_code, data = controller.pause_print()
        return jsonify(data), status_code

    @app.post("/api/printer/resume")
    def printer_resume():
        status_code, data = controller.resume_print()
        return jsonify(data), status_code

    @app.post("/api/printer/cancel")
    def printer_cancel():
        status_code, data = controller.cancel_print()
        return jsonify(data), status_code

    def _parse_optional_json() -> tuple[dict[str, Any] | None, tuple[Any, int] | None]:
        raw = request.get_data()
        if not raw or not raw.strip():
            return {}, None
        parsed = request.get_json(silent=True)
        if parsed is None:
            return None, (jsonify({"ok": False, "error": "Invalid JSON body"}), 400)
        if not isinstance(parsed, dict):
            return None, (jsonify({"ok": False, "error": "JSON body must be an object"}), 400)
        return parsed, None

    @app.post("/api/printer/led")
    def printer_led():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        status_code, data = controller.set_led(bool(payload.get("on", True)))
        return jsonify(data), status_code

    @app.post("/api/printer/temperature/nozzle")
    def printer_nozzle_temp():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        status_code, data = controller.set_nozzle_temp(payload.get("target"))
        return jsonify(data), status_code

    @app.post("/api/printer/temperature/bed")
    def printer_bed_temp():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        status_code, data = controller.set_bed_temp(payload.get("target"))
        return jsonify(data), status_code

    @app.post("/api/printer/speed")
    def printer_speed():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        status_code, data = controller.set_print_speed(payload.get("percent"))
        return jsonify(data), status_code

    @app.get("/api/printer/position")
    def printer_position():
        status_code, data = controller.get_printer_position()
        return jsonify(data), status_code

    @app.post("/api/printer/steppers")
    def printer_steppers():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        status_code, data = controller.set_steppers(bool(payload.get("enabled", False)))
        return jsonify(data), status_code

    @app.post("/api/printer/emergency_stop")
    def printer_emergency_stop():
        status_code, data = controller.emergency_stop()
        return jsonify(data), status_code

    @app.get("/api/printer/files")
    def printer_files_list():
        status_code, data = controller.list_printer_files()
        return jsonify(data), status_code

    @app.get("/api/printer/files/thumbnail")
    def printer_file_thumbnail():
        target = request.args.get("path") or request.args.get("filename") or ""
        status_code, data = controller.get_printer_file_thumbnail(target)
        return jsonify(data), status_code

    @app.post("/api/printer/files/print")
    def printer_files_print():
        payload, err = _parse_optional_json()
        if err is not None:
            return err
        assert payload is not None
        target = str(payload.get("filename") or payload.get("path") or "")
        status_code, data = controller.start_printer_file(target)
        return jsonify(data), status_code

    # ------------------------------------------------------------------
    # Camera Relay, Snapshots & Temperature History
    # ------------------------------------------------------------------

    @app.get("/api/camera/stream")
    def camera_stream():
        return Response(
            controller.camera_relay.iter_mjpeg_frames(),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )

    @app.post("/api/camera/reconnect")
    def camera_reconnect():
        return jsonify(controller.reconnect_camera())

    @app.get("/api/camera/snapshots")
    def camera_snapshots_list():
        return jsonify(controller.list_snapshots())

    @app.post("/api/camera/snapshots")
    def camera_snapshots_capture():
        status_code, data = controller.capture_snapshot()
        return jsonify(data), status_code

    @app.get("/api/camera/snapshots/<filename>")
    def camera_snapshot_file(filename: str):
        path = controller.get_snapshot_path(filename)
        if path is None:
            return jsonify({"error": "Snapshot not found"}), 404
        return send_file(path, mimetype="image/jpeg")

    @app.get("/api/history/temperature")
    def temperature_history():
        return jsonify(controller.get_temperature_history())

    # ------------------------------------------------------------------
    # OctoPrint Compatibility API (for OrcaSlicer)
    # ------------------------------------------------------------------

    @app.get("/api/version")
    def octoprint_version():
        return jsonify(controller.get_octoprint_version())

    @app.get("/api/settings")
    def octoprint_settings():
        return jsonify(controller.get_octoprint_settings())

    @app.post("/api/files/local")
    def octoprint_upload_local():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify({"error": "No file provided in 'file' field."}), 400

        should_print = parse_bool_flag(request.form.get("print"))
        file_bytes = uploaded.read()
        status_code, data = controller.upload_local_file(
            filename=uploaded.filename,
            file_bytes=file_bytes,
            should_print=should_print,
        )
        return jsonify(data), status_code

    return app


if __name__ == "__main__":
    config = load_config()
    server_cfg = config["server"]
    debug_enabled = bool(server_cfg.get("debug", False))
    configure_logging(debug=debug_enabled)
    application = create_app(config=config)
    application.run(
        host=server_cfg.get("host", "127.0.0.1"),
        port=int(server_cfg.get("port", 5000)),
        debug=debug_enabled,
        threaded=True,
    )
