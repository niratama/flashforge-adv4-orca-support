"""Shared stdlib-only services for temperature history, camera MJPEG relay, and snapshots."""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
_SAFE_SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9_.-]+\.jpg$")


def resolve_snapshot_dir(raw_dir: str | Path, base_dir: Path) -> Path:
    """Resolve snapshot directory relative to base_dir when not absolute."""
    path = Path(raw_dir)
    return path if path.is_absolute() else (base_dir / path)


class TemperatureHistory:
    """Thread-safe in-memory ring buffer for nozzle and bed temperature samples."""

    def __init__(
        self,
        retention_hours: float = 4.0,
        sample_interval_seconds: float = 5.0,
    ) -> None:
        self.retention_hours = max(0.05, float(retention_hours))
        self.retention_seconds = self.retention_hours * 3600.0
        self.sample_interval_seconds = max(0.5, float(sample_interval_seconds))
        self._lock = threading.Lock()
        self._samples: deque[dict[str, Any]] = deque()
        self._last_recorded_at: float = 0.0

    def needs_sample(self, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        with self._lock:
            return (ts - self._last_recorded_at) >= self.sample_interval_seconds

    def record(self, status: dict[str, Any], now: float | None = None) -> bool:
        """Record a temperature sample if connected and sample interval has elapsed."""
        if not status.get("connected"):
            return False

        temps = status.get("temperatures") or {}
        nozzle = temps.get("nozzle") or {}
        bed = temps.get("bed") or {}
        nozzle_actual = nozzle.get("actual")
        bed_actual = bed.get("actual")
        if nozzle_actual is None and bed_actual is None:
            return False

        ts = time.time() if now is None else float(now)
        with self._lock:
            if (ts - self._last_recorded_at) < self.sample_interval_seconds:
                return False

            self._samples.append(
                {
                    "ts": int(ts * 1000),
                    "nozzle_actual": nozzle_actual,
                    "nozzle_target": nozzle.get("target"),
                    "bed_actual": bed_actual,
                    "bed_target": bed.get("target"),
                }
            )
            self._last_recorded_at = ts
            self._prune_locked(ts)
            return True

    def _prune_locked(self, now: float) -> None:
        cutoff_ms = int((now - self.retention_seconds) * 1000)
        while self._samples and self._samples[0]["ts"] < cutoff_ms:
            self._samples.popleft()

    def get_samples(self, now: float | None = None) -> list[dict[str, Any]]:
        ts = time.time() if now is None else float(now)
        with self._lock:
            self._prune_locked(ts)
            return list(self._samples)

    def to_payload(self, now: float | None = None) -> dict[str, Any]:
        return {
            "retention_hours": self.retention_hours,
            "sample_interval_seconds": self.sample_interval_seconds,
            "samples": self.get_samples(now=now),
        }


class SnapshotManager:
    """Persist and prune JPEG camera snapshots on disk."""

    def __init__(
        self,
        snapshot_dir: Path,
        enabled: bool = True,
        only_while_printing: bool = True,
        interval_seconds: float = 60.0,
        max_count: int = 200,
    ) -> None:
        self.snapshot_dir = Path(snapshot_dir)
        self.enabled = bool(enabled)
        self.only_while_printing = bool(only_while_printing)
        self.interval_seconds = max(1.0, float(interval_seconds))
        self.max_count = max(1, int(max_count))
        self._lock = threading.Lock()
        self._last_saved_at: float = 0.0

    def is_due(self, now: float | None = None) -> bool:
        ts = time.time() if now is None else now
        with self._lock:
            return (ts - self._last_saved_at) >= self.interval_seconds

    def save_frame(
        self, jpeg_bytes: bytes, timestamp: float | None = None
    ) -> dict[str, Any]:
        """Save a JPEG frame to snapshot_dir and prune old files beyond max_count."""
        if not jpeg_bytes:
            raise ValueError("Empty JPEG frame")

        ts = time.time() if timestamp is None else float(timestamp)
        dt = datetime.fromtimestamp(ts)
        millis = int((ts % 1.0) * 1000)
        filename = f"{dt.strftime('%Y%m%d_%H%M%S')}_{millis:03d}.jpg"

        with self._lock:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            target = self.snapshot_dir / filename
            # Avoid rare same-millisecond collision
            counter = 1
            while target.exists():
                filename = f"{dt.strftime('%Y%m%d_%H%M%S')}_{millis:03d}_{counter}.jpg"
                target = self.snapshot_dir / filename
                counter += 1

            tmp_path = target.with_suffix(".jpg.tmp")
            tmp_path.write_bytes(jpeg_bytes)
            tmp_path.replace(target)
            self._last_saved_at = ts
            self._prune_locked()

            return {
                "filename": filename,
                "url": f"/api/camera/snapshots/{filename}",
                "timestamp": int(ts * 1000),
                "label": dt.strftime("%Y-%m-%d %H:%M:%S"),
                "size": len(jpeg_bytes),
            }

    def _prune_locked(self) -> None:
        if not self.snapshot_dir.exists():
            return
        files = sorted(
            (p for p in self.snapshot_dir.iterdir() if p.is_file() and p.suffix.lower() == ".jpg"),
            key=lambda p: p.name,
        )
        excess = len(files) - self.max_count
        for old_file in files[: max(0, excess)]:
            try:
                old_file.unlink()
            except OSError as exc:
                logger.warning("Failed to prune snapshot %s: %s", old_file, exc)

    def list_snapshots(self) -> list[dict[str, Any]]:
        """Return snapshots sorted newest-first."""
        with self._lock:
            if not self.snapshot_dir.exists():
                return []
            files = sorted(
                (
                    p
                    for p in self.snapshot_dir.iterdir()
                    if p.is_file() and _SAFE_SNAPSHOT_RE.match(p.name)
                ),
                key=lambda p: p.name,
                reverse=True,
            )
            items: list[dict[str, Any]] = []
            for p in files:
                stat = p.stat()
                dt = datetime.fromtimestamp(stat.st_mtime)
                items.append(
                    {
                        "filename": p.name,
                        "url": f"/api/camera/snapshots/{p.name}",
                        "timestamp": int(stat.st_mtime * 1000),
                        "label": dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "size": stat.st_size,
                    }
                )
            return items

    def get_snapshot_path(self, filename: str) -> Path | None:
        if not _SAFE_SNAPSHOT_RE.match(filename) or Path(filename).name != filename:
            return None
        candidate = (self.snapshot_dir / filename).resolve()
        try:
            candidate.relative_to(self.snapshot_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def to_payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "only_while_printing": self.only_while_printing,
            "interval_seconds": self.interval_seconds,
            "max_count": self.max_count,
            "snapshots": self.list_snapshots(),
        }


class CameraRelay:
    """Single-connection MJPEG relay that fans out one upstream camera stream to many clients."""

    def __init__(
        self,
        camera_url: str,
        opener: Callable[..., Any] = urllib.request.urlopen,
        idle_grace_seconds: float = 5.0,
        connect_timeout: float = 5.0,
    ) -> None:
        parsed = urlparse(camera_url)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"Unsupported camera URL scheme or host: {camera_url!r}")
        self.camera_url = camera_url
        self._opener = opener
        self.idle_grace_seconds = idle_grace_seconds
        self.connect_timeout = connect_timeout

        self._cond = threading.Condition()
        self._latest_frame: bytes | None = None
        self._latest_frame_at: float = 0.0
        self._frame_seq: int = 0
        self._active_clients: int = 0
        self._auto_snapshot_active: bool = False
        self._last_demand_at: float = 0.0
        self._last_error: str | None = None
        self._upstream_connected: bool = False
        self._reconnect_requested: bool = False
        self._stopped: bool = False
        self._thread: threading.Thread | None = None
        self._active_response: Any = None

    def set_auto_snapshot_active(self, active: bool) -> None:
        with self._cond:
            self._auto_snapshot_active = active
            if active:
                self._last_demand_at = time.monotonic()
                self._ensure_thread_locked()
            self._cond.notify_all()

    def request_reconnect(self) -> None:
        with self._cond:
            self._reconnect_requested = True
            self._last_demand_at = time.monotonic()
            resp = self._active_response
            self._ensure_thread_locked()
            self._cond.notify_all()
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            resp = self._active_response
            self._cond.notify_all()
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def get_status(self) -> dict[str, Any]:
        """Return current upstream/client relay status for UI visualization."""
        with self._cond:
            age = (
                round(max(0.0, time.time() - self._latest_frame_at), 1)
                if self._latest_frame_at > 0
                else None
            )
            return {
                "upstream_connected": self._upstream_connected,
                "active_clients": self._active_clients,
                "auto_snapshot_active": self._auto_snapshot_active,
                "has_frame": self._latest_frame is not None,
                "last_frame_age_seconds": age,
                "last_error": self._last_error,
            }

    def _has_demand_locked(self) -> bool:
        if self._active_clients > 0 or self._auto_snapshot_active:
            return True
        return (time.monotonic() - self._last_demand_at) < self.idle_grace_seconds

    def _ensure_thread_locked(self) -> None:
        if self._stopped:
            return
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._reader_loop,
                name="camera-mjpeg-relay",
                daemon=True,
            )
            self._thread.start()

    def get_latest_frame(self, wait_timeout: float = 0.0) -> bytes | None:
        """Return the latest JPEG frame, optionally waiting up to wait_timeout seconds."""
        deadline = time.monotonic() + wait_timeout
        with self._cond:
            if self._latest_frame is not None and (time.time() - self._latest_frame_at) < 5.0:
                return self._latest_frame

            if wait_timeout <= 0:
                return self._latest_frame

            self._last_demand_at = time.monotonic()
            self._ensure_thread_locked()
            start_seq = self._frame_seq
            while not self._stopped and self._frame_seq == start_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cond.wait(timeout=remaining)
            return self._latest_frame

    def iter_mjpeg_frames(self, frame_timeout: float = 5.0) -> Iterator[bytes]:
        """Yield multipart MJPEG chunks for HTTP streaming responses."""
        with self._cond:
            self._active_clients += 1
            self._last_demand_at = time.monotonic()
            self._ensure_thread_locked()
            last_seq = 0

        try:
            while True:
                with self._cond:
                    if self._stopped:
                        break
                    if self._frame_seq == last_seq:
                        self._cond.wait(timeout=frame_timeout)
                    if self._stopped:
                        break
                    frame = self._latest_frame
                    if self._frame_seq != last_seq:
                        last_seq = self._frame_seq
                    elif frame is None:
                        continue

                if not frame:
                    continue

                header = (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(frame)).encode("ascii") + b"\r\n\r\n"
                )
                yield header + frame + b"\r\n"
        finally:
            with self._cond:
                self._active_clients = max(0, self._active_clients - 1)
                self._last_demand_at = time.monotonic()
                self._cond.notify_all()

    def _publish_frame(self, jpeg_bytes: bytes) -> None:
        with self._cond:
            self._latest_frame = jpeg_bytes
            self._latest_frame_at = time.time()
            self._frame_seq += 1
            self._last_error = None
            self._cond.notify_all()

    def _reader_loop(self) -> None:
        while True:
            with self._cond:
                if self._stopped or not self._has_demand_locked():
                    self._thread = None
                    return
                self._reconnect_requested = False

            response = None
            try:
                logger.info("Connecting camera relay to %s", self.camera_url)
                response = self._opener(self.camera_url, timeout=self.connect_timeout)
                with self._cond:
                    self._active_response = response
                    self._upstream_connected = True
                    self._last_error = None
                buf = bytearray()
                while True:
                    with self._cond:
                        if self._stopped or self._reconnect_requested or not self._has_demand_locked():
                            break
                    chunk = response.read(8192)
                    if not chunk:
                        logger.warning("Camera relay upstream EOF (%s)", self.camera_url)
                        break
                    buf.extend(chunk)
                    while True:
                        start = buf.find(JPEG_SOI)
                        if start < 0:
                            if len(buf) > 2:
                                del buf[:-2]
                            break
                        if start > 0:
                            del buf[:start]
                        end = buf.find(JPEG_EOI, 2)
                        if end < 0:
                            if len(buf) > 4 * 1024 * 1024:
                                buf.clear()
                            break
                        frame = bytes(buf[: end + 2])
                        del buf[: end + 2]
                        self._publish_frame(frame)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                with self._cond:
                    self._last_error = str(exc)
                logger.warning("Camera relay connection ended (%s): %s", self.camera_url, exc)
            finally:
                with self._cond:
                    self._upstream_connected = False
                    if self._active_response is response:
                        self._active_response = None
                if response is not None:
                    try:
                        response.close()
                    except Exception:
                        pass

            with self._cond:
                if self._stopped:
                    self._thread = None
                    return
                if not self._reconnect_requested:
                    self._cond.wait(timeout=1.5)


class BackgroundMonitor:
    """Periodically records temperature history and triggers automatic camera snapshots."""

    def __init__(
        self,
        ff_client: Any,
        temp_history: TemperatureHistory,
        camera_relay: CameraRelay,
        snapshot_manager: SnapshotManager,
    ) -> None:
        self._ff_client = ff_client
        self._temp_history = temp_history
        self._camera_relay = camera_relay
        self._snapshot_manager = snapshot_manager
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_is_printing: bool = False

    def observe_status(self, status: dict[str, Any]) -> None:
        """Update temperature history and printing state from a fresh status response."""
        self._temp_history.record(status)
        if status.get("connected"):
            machine_status = str(status.get("machine_status") or "").upper()
            progress = status.get("progress") or {}
            self._last_is_printing = bool(
                progress.get("is_printing") or machine_status == "BUILDING_FROM_SD"
            )
        else:
            self._last_is_printing = False

        should_record = self._snapshot_manager.enabled and (
            not self._snapshot_manager.only_while_printing or self._last_is_printing
        )
        self._camera_relay.set_auto_snapshot_active(should_record)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="bridge-background-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._camera_relay.stop()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self._temp_history.needs_sample():
                    status = self._ff_client.get_full_status()
                    self.observe_status(status)
            except Exception as exc:
                logger.debug("Background status poll skipped: %s", exc)
                self._camera_relay.set_auto_snapshot_active(
                    self._snapshot_manager.enabled and not self._snapshot_manager.only_while_printing
                )

            should_record = self._snapshot_manager.enabled and (
                not self._snapshot_manager.only_while_printing or self._last_is_printing
            )
            if should_record and self._snapshot_manager.is_due():
                frame = self._camera_relay.get_latest_frame(wait_timeout=2.0)
                if frame:
                    try:
                        saved = self._snapshot_manager.save_frame(frame)
                        logger.info("Saved camera snapshot: %s", saved["filename"])
                    except Exception as exc:
                        logger.warning("Failed to save camera snapshot: %s", exc)

            wait_sec = min(2.0, self._temp_history.sample_interval_seconds)
            self._stop_event.wait(timeout=wait_sec)


def build_services(
    cfg: dict[str, Any],
    base_dir: Path,
    ff_client: Any,
) -> tuple[TemperatureHistory, CameraRelay, SnapshotManager, BackgroundMonitor]:
    """Create configured TemperatureHistory, CameraRelay, SnapshotManager, and BackgroundMonitor."""
    printer_cfg = cfg.get("printer", {})
    camera_cfg = cfg.get("camera", {})
    history_cfg = cfg.get("history", {})

    camera_url = printer_cfg.get(
        "camera_url", f"http://{printer_cfg.get('ip', '127.0.0.1')}:8080/?action=stream"
    )
    snapshot_dir = resolve_snapshot_dir(
        camera_cfg.get("snapshot_dir", "snapshots"),
        base_dir=base_dir,
    )

    temp_history = TemperatureHistory(
        retention_hours=float(history_cfg.get("temperature_hours", 4.0)),
        sample_interval_seconds=float(history_cfg.get("sample_interval_seconds", 5.0)),
    )
    camera_relay = CameraRelay(camera_url=camera_url)
    snapshot_manager = SnapshotManager(
        snapshot_dir=snapshot_dir,
        enabled=bool(camera_cfg.get("snapshot_enabled", True)),
        only_while_printing=bool(camera_cfg.get("snapshot_only_while_printing", True)),
        interval_seconds=float(camera_cfg.get("snapshot_interval_seconds", 60)),
        max_count=int(camera_cfg.get("snapshot_max_count", 200)),
    )
    monitor = BackgroundMonitor(
        ff_client=ff_client,
        temp_history=temp_history,
        camera_relay=camera_relay,
        snapshot_manager=snapshot_manager,
    )
    return temp_history, camera_relay, snapshot_manager, monitor
