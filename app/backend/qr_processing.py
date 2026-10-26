from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Any

try:
    import cv2
    import numpy as np

    OPENCV_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    OPENCV_AVAILABLE = False

import database
from qr_codec import parse_payload


CONFIRM_FRAMES = 2
CONFIRM_WINDOW_SECONDS = 0.8
EXIT_GRACE_SECONDS = 1.5
TOUCH_INTERVAL_SECONDS = 0.5
TEST_CAPTURE_DELAY_SECONDS = 2.0
NORMAL_CAPTURE_DELAY_SECONDS = 10.0


def iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Track:
    payload: str
    first_seen: float
    last_seen: float
    seen_count: int = 1
    confirmed: bool = False
    visit_id: int | None = None
    last_touched: float = 0.0
    capture_scheduled: bool = False


class QRProcessingWorker:
    def __init__(
        self,
        frame_snapshot: Callable[[], tuple[bytes | None, float | None, int, int, int]],
        capture_callback: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._frame_snapshot = frame_snapshot
        self._capture_callback = capture_callback
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="qr-processing", daemon=True)
        self._lock = threading.Lock()
        self._tracks: dict[str, Track] = {}
        self._last_event: dict[str, Any] | None = None
        self._frame_count = 0
        self._last_version = -1
        self._last_session_check = 0.0
        self._session_id: int | None = None
        self._session: sqlite3.Row | None = None
        self._last_invalid_event = 0.0
        self._multiple_qr_active = False
        self._decoder = cv2.QRCodeDetector() if OPENCV_AVAILABLE else None
        self._last_capture: dict[str, Any] | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.is_set():
            frame, _, _, _, version = self._frame_snapshot()
            now = time.time()
            if now - self._last_session_check >= 0.25:
                self._refresh_session()
                self._last_session_check = now

            if frame is None or version == self._last_version:
                # Treat a stalled/disconnected feed as an exit from the frame
                # after the same grace period used for an observed blank frame.
                # This prevents a visit from staying active forever when the
                # phone stops sending frames.
                if self._session is not None:
                    self._expire_stale_tracks(now)
                self._stop.wait(0.05)
                continue
            self._last_version = version
            self._frame_count += 1

            if self._session is None:
                self._tracks.clear()
                continue

            if bool(self._session["paused"]):
                # Keep the overhead stream alive, but do not decode frames or
                # create/touch visits while the operator has paused the run.
                self._tracks.clear()
                self._stop.wait(0.05)
                continue

            raw_values = self._decode(frame)
            self._observe(raw_values, now)

    def _refresh_session(self) -> None:
        session = database.get_active_session()
        next_id = None if session is None else int(session["id"])
        was_paused = bool(self._session["paused"]) if self._session is not None else False
        is_paused = bool(session["paused"]) if session is not None else False
        if next_id != self._session_id or was_paused != is_paused:
            self._tracks.clear()
            if next_id != self._session_id:
                self._last_capture = None
            self._session_id = next_id
        self._session = session

    def _decode(self, frame: bytes) -> list[str]:
        if self._decoder is None or np is None or cv2 is None:
            return []
        try:
            encoded = np.frombuffer(frame, dtype=np.uint8)
            image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
            if image is None:
                return []
            result = self._decoder.detectAndDecodeMulti(image)
            decoded = result[1] if isinstance(result, tuple) and len(result) >= 2 else []
            return [str(value).strip() for value in (decoded or []) if str(value).strip()]
        except Exception as exc:  # The live feed must continue if one frame is malformed.
            self._set_event("decoder_error", f"QR decoder skipped a frame: {type(exc).__name__}")
            return []

    def _observe(self, raw_values: list[str], now: float) -> None:
        visible_values = [str(value).strip() for value in raw_values if str(value).strip()]
        if len(visible_values) > 1:
            # Do not associate either code with a cow while the view is
            # ambiguous. Requiring a fresh single-code confirmation also
            # prevents a code seen before the warning from being processed
            # immediately when the second code leaves the frame.
            self._tracks.clear()
            with self._lock:
                self._multiple_qr_active = True
            self._set_event(
                "multiple_qr",
                "Only one QR code may be in view at a time",
            )
            return

        if self._multiple_qr_active:
            self._tracks.clear()
            with self._lock:
                self._multiple_qr_active = False
            self._clear_event_type("multiple_qr")

        valid_payloads: set[str] = set()
        for raw_value in visible_values:
            parsed = parse_payload(raw_value)
            if parsed is None:
                if now - self._last_invalid_event > 1.0:
                    self._set_event("invalid", "A QR code was detected but its format is invalid", raw_value)
                    self._last_invalid_event = now
                continue
            valid_payloads.add(parsed["payload"])

        for payload in valid_payloads:
            track = self._tracks.get(payload)
            if track is None or now - track.last_seen > CONFIRM_WINDOW_SECONDS:
                track = Track(payload=payload, first_seen=now, last_seen=now)
                self._tracks[payload] = track
            else:
                track.seen_count += 1
                track.last_seen = now

            if not track.confirmed and track.seen_count >= CONFIRM_FRAMES:
                self._confirm_track(track, now)
            elif track.confirmed and track.visit_id is None:
                self._attach_overridden_visit(track, now)
            elif track.confirmed and track.visit_id is not None and now - track.last_touched >= TOUCH_INTERVAL_SECONDS:
                database.touch_visit(track.visit_id, now, track.seen_count)
                track.last_touched = now

        self._expire_stale_tracks(now)

    def _expire_stale_tracks(self, now: float) -> None:
        expired = [payload for payload, track in self._tracks.items() if now - track.last_seen > EXIT_GRACE_SECONDS]
        for payload in expired:
            track = self._tracks.pop(payload)
            if track.confirmed and track.visit_id is not None:
                database.complete_visit(track.visit_id, now)
                self._set_event("completed", "Cow QR left the camera view", payload, track.visit_id)
            else:
                # A duplicate QR does not have a new visit to complete. Clear
                # its warning when the tracked QR has actually left the view.
                # This lets the UI keep the warning visible while the QR is
                # present without leaving a stale override prompt forever.
                self._clear_qr_warning(payload)

    def _confirm_track(self, track: Track, now: float) -> None:
        track.confirmed = True
        track.last_touched = now
        if self._session is None:
            return

        cow = database.get_cow_by_payload(track.payload)
        if cow is None:
            self._set_event("unknown", "This QR code is not registered", track.payload)
            return
        if int(cow["site_id"]) != int(self._session["site_id"]):
            self._set_event("wrong_site", "This QR code belongs to another site", track.payload)
            return

        existing = database.get_current_visit(int(self._session["id"]), int(cow["id"]))
        if existing is not None:
            if existing["status"] == "active":
                track.visit_id = int(existing["id"])
                database.touch_visit(track.visit_id, now, track.seen_count)
                self._set_event("active", "Cow QR is being tracked", track.payload, track.visit_id)
            else:
                self._set_event("duplicate", "This cow has already been scanned in this session", track.payload, int(existing["id"]))
            return

        try:
            created = database.create_active_visit(int(self._session["id"]), int(cow["id"]), now, track.seen_count)
        except sqlite3.IntegrityError:
            existing = database.get_current_visit(int(self._session["id"]), int(cow["id"]))
            if existing is not None:
                self._set_event("duplicate", "This cow has already been scanned in this session", track.payload, int(existing["id"]))
            return
        track.visit_id = int(created["id"])
        self._set_event("active", "New cow registered and being tracked", track.payload, track.visit_id)
        self._schedule_capture(track, cow, now)

    def _capture_delay_seconds(self) -> float:
        if self._session is not None and bool(self._session["test_mode"]):
            return TEST_CAPTURE_DELAY_SECONDS
        return NORMAL_CAPTURE_DELAY_SECONDS

    def _schedule_capture(self, track: Track, cow: sqlite3.Row, now: float) -> None:
        if self._capture_callback is None or track.capture_scheduled or self._session is None:
            return
        track.capture_scheduled = True
        delay = self._capture_delay_seconds()
        database.set_visit_capture_status(track.visit_id, "scheduled")
        request = {
            "siteCode": str(cow["site_code"]),
            "cowId": f"{int(cow['number']):03d}",
            "epochTimestamp": None,
            "testMode": bool(self._session["test_mode"]),
            "sessionId": int(self._session["id"]),
        }
        scheduled_at = iso_timestamp()
        with self._lock:
            self._last_capture = {
                "status": "scheduled",
                "payload": track.payload,
                "visitId": track.visit_id,
                "siteCode": request["siteCode"],
                "cowId": request["cowId"],
                "delaySeconds": delay,
                "scheduledAt": scheduled_at,
            }
        self._set_event(
            "capture_scheduled",
            f"New cow confirmed; thermal capture scheduled in {int(delay)} seconds",
            track.payload,
            track.visit_id,
        )
        threading.Thread(
            target=self._run_scheduled_capture,
            args=(track.payload, track.visit_id, int(self._session["id"]), request, delay, scheduled_at),
            name="qr-thermal-capture",
            daemon=True,
        ).start()

    def _run_scheduled_capture(
        self,
        payload: str,
        visit_id: int | None,
        session_id: int,
        request: dict[str, Any],
        delay: float,
        scheduled_at: str,
    ) -> None:
        wait_result = self._wait_for_capture_window(session_id, delay)
        if wait_result != "ready":
            database.set_visit_capture_status(visit_id, "failed")
            reason = (
                "Capture cancelled because the processing session stopped."
                if wait_result == "session-ended"
                else "Capture cancelled because the processing worker stopped."
            )
            with self._lock:
                self._last_capture = {
                    "status": "cancelled",
                    "payload": payload,
                    "visitId": visit_id,
                    "siteCode": request["siteCode"],
                    "cowId": request["cowId"],
                    "delaySeconds": delay,
                    "scheduledAt": scheduled_at,
                    "message": reason,
                }
            return

        capture_started = iso_timestamp()
        database.set_visit_capture_status(visit_id, "capturing")
        with self._lock:
            self._last_capture = {
                "status": "capturing",
                "payload": payload,
                "visitId": visit_id,
                "siteCode": request["siteCode"],
                "cowId": request["cowId"],
                "delaySeconds": delay,
                "scheduledAt": scheduled_at,
                "startedAt": capture_started,
            }
        try:
            request["epochTimestamp"] = int(time.time())
            result = self._capture_callback(request) if self._capture_callback else {
                "status": "failed",
                "error": "Automatic capture is not configured",
            }
            status = str(result.get("status", "failed"))
            with self._lock:
                self._last_capture = {
                    **result,
                    "status": status,
                    "payload": payload,
                    "visitId": visit_id,
                    "siteCode": request["siteCode"],
                    "cowId": request["cowId"],
                    "delaySeconds": delay,
                    "scheduledAt": scheduled_at,
                    "startedAt": capture_started,
                    "completedAt": iso_timestamp(),
                }
            if status == "completed":
                database.set_visit_capture_status(visit_id, "completed")
                self._set_event("capture_completed", "Automatic thermal capture completed", payload, visit_id)
            elif status == "skipped":
                database.set_visit_capture_status(visit_id, "skipped")
                self._set_event("capture_skipped", "Thermal capture skipped", payload, visit_id)
            else:
                database.set_visit_capture_status(visit_id, "failed")
                self._set_event("capture_failed", "Automatic thermal capture did not complete", payload, visit_id)
        except Exception as exc:
            database.set_visit_capture_status(visit_id, "failed")
            with self._lock:
                self._last_capture = {
                    "status": "failed",
                    "payload": payload,
                    "visitId": visit_id,
                    "siteCode": request["siteCode"],
                    "cowId": request["cowId"],
                    "delaySeconds": delay,
                    "scheduledAt": scheduled_at,
                    "startedAt": capture_started,
                    "completedAt": iso_timestamp(),
                    "error": str(exc),
                }
            self._set_event("capture_failed", "Automatic thermal capture failed", payload, visit_id)

    def _wait_for_capture_window(self, session_id: int, delay: float) -> str:
        """Wait for the delay while holding it whenever the session is paused."""
        remaining = max(0.0, delay)
        while remaining > 0:
            active_session = database.get_active_session()
            if active_session is None or int(active_session["id"]) != session_id:
                return "session-ended"
            if bool(active_session["paused"]):
                if self._stop.wait(0.2):
                    return "worker-stopped"
                continue
            started = time.monotonic()
            if self._stop.wait(min(0.2, remaining)):
                return "worker-stopped"
            remaining -= time.monotonic() - started

        # A capture that was already scheduled must not fire while the QR
        # view is ambiguous. Resume only after the multi-QR warning clears.
        while self._is_multiple_qr_active():
            active_session = database.get_active_session()
            if active_session is None or int(active_session["id"]) != session_id:
                return "session-ended"
            if bool(active_session["paused"]):
                if self._stop.wait(0.2):
                    return "worker-stopped"
                continue
            if self._stop.wait(0.2):
                return "worker-stopped"
        return "ready"

    def _attach_overridden_visit(self, track: Track, now: float) -> None:
        if self._session is None:
            return
        cow = database.get_cow_by_payload(track.payload)
        if cow is None or int(cow["site_id"]) != int(self._session["site_id"]):
            return
        existing = database.get_current_visit(int(self._session["id"]), int(cow["id"]))
        if existing is not None and existing["status"] == "active":
            track.visit_id = int(existing["id"])
            database.touch_visit(track.visit_id, now, track.seen_count)
            self._set_event("active", "Override accepted; cow is being tracked again", track.payload, track.visit_id)

    def _set_event(self, event_type: str, message: str, payload: str | None = None, visit_id: int | None = None) -> None:
        event = {
            "type": event_type,
            "message": message,
            "payload": payload,
            "visitId": visit_id,
            "at": iso_timestamp(),
        }
        with self._lock:
            if self._multiple_qr_active and event_type != "multiple_qr":
                return
            self._last_event = event

    def _clear_event_type(self, event_type: str) -> None:
        with self._lock:
            if self._last_event and self._last_event.get("type") == event_type:
                self._last_event = None

    def _is_multiple_qr_active(self) -> bool:
        with self._lock:
            return self._multiple_qr_active

    def _clear_qr_warning(self, payload: str) -> None:
        with self._lock:
            warning_types = {"duplicate", "wrong_site", "unknown"}
            if (
                self._last_event
                and self._last_event.get("type") in warning_types
                and self._last_event.get("payload") == payload
            ):
                self._last_event = None

    def mark_override(self, payload: str | None, visit_id: int) -> None:
        if payload:
            track = self._tracks.get(payload)
            if track is not None:
                track.visit_id = visit_id
                track.confirmed = True
                self._set_event("active", "Override accepted; cow is being tracked again", payload, visit_id)
            else:
                self._clear_qr_warning(payload)

    def status(self) -> dict[str, Any]:
        with self._lock:
            last_event = dict(self._last_event) if self._last_event else None
        session = database.get_active_session()
        session_data = None
        session_id = None
        if session is not None:
            session_id = int(session["id"])
            test_mode = bool(session["test_mode"])
            paused = bool(session["paused"])
            session_data = {
                "id": session_id,
                "siteCode": session["site_code"],
                "status": "paused" if paused else session["status"],
                "paused": paused,
                "startedAt": session["started_at"],
                "testMode": test_mode,
                "captureDelaySeconds": TEST_CAPTURE_DELAY_SECONDS if test_mode else NORMAL_CAPTURE_DELAY_SECONDS,
            }
        with self._lock:
            last_capture = dict(self._last_capture) if self._last_capture else None
        return {
            "decoderAvailable": OPENCV_AVAILABLE,
            "decoder": "OpenCV QRCodeDetector" if OPENCV_AVAILABLE else "Unavailable",
            "frameCount": self._frame_count,
            "session": session_data,
            "activeDetections": [
                {
                    "payload": track.payload,
                    "seenCount": track.seen_count,
                    "confirmed": track.confirmed,
                    "visitId": track.visit_id,
                }
                for track in self._tracks.values()
            ],
            "lastEvent": last_event,
            "lastCapture": last_capture,
            "visits": database.session_summary(session_id),
        }
