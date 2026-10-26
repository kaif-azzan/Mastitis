from __future__ import annotations

import asyncio
import json
import re
import subprocess
import threading
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import database
from qr_processing import QRProcessingWorker
from thermal_capture import AdbError, ThermalCaptureManager, validate_production_layout
from top_camera import TopCameraAdbError, TopCameraManager


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADB_PATH = PROJECT_ROOT / "tools" / "platform-tools" / "adb.exe"
CAPTURES_DIR = PROJECT_ROOT / "captures"
TEST_DIR = PROJECT_ROOT / "test"
THERMAL_CONFIG_PATH = PROJECT_ROOT / "data" / "thermal_devices.json"
TOP_CAMERA_CONFIG_PATH = PROJECT_ROOT / "data" / "top_camera.json"
TOP_CAMERA_APK_PATH = PROJECT_ROOT / "app" / "top-camera-android" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
FRONTEND_DIST = PROJECT_ROOT / "app" / "frontend" / "dist"

IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(?:jpg|jpeg|png)$", re.IGNORECASE)
TOP_CAMERA_TOKEN = "thermal-master-top-camera-local"

CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
TEST_DIR.mkdir(parents=True, exist_ok=True)
database.initialize_database()

thermal_manager = ThermalCaptureManager(
    adb_path=ADB_PATH,
    captures_dir=CAPTURES_DIR,
    test_dir=TEST_DIR,
    config_path=THERMAL_CONFIG_PATH,
)
top_camera_manager = TopCameraManager(
    adb_path=ADB_PATH,
    config_path=TOP_CAMERA_CONFIG_PATH,
    apk_path=TOP_CAMERA_APK_PATH,
)


app = FastAPI(title="Thermal Master Capture", version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

capture_lock = threading.Lock()


class TopCameraState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.last_frame_at: float | None = None
        self.frame_count = 0
        self.connection_count = 0
        self.version = 0
        self.processing_enabled = False
        self.socket: WebSocket | None = None
        self.controls: dict[str, Any] = {
            "ready": False,
            "focusSupported": False,
            "exposureSupported": False,
            "focusMode": "auto",
            "focusPosition": 0.5,
            "exposureIndex": 0,
            "exposureMin": -6,
            "exposureMax": 6,
        }

    def update(self, frame: bytes) -> None:
        with self.lock:
            if not self.processing_enabled:
                return
            self.frame = frame
            self.last_frame_at = time.time()
            self.frame_count += 1
            self.version += 1

    def set_processing_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.processing_enabled = enabled
            if not enabled:
                self.frame = None
                self.last_frame_at = None
                self.frame_count = 0
                self.version += 1

    def is_processing_enabled(self) -> bool:
        with self.lock:
            return self.processing_enabled

    def set_connection(self, connected: bool) -> None:
        with self.lock:
            self.connection_count = max(0, self.connection_count + (1 if connected else -1))

    def set_socket(self, websocket: WebSocket | None) -> None:
        with self.lock:
            self.socket = websocket
            if websocket is None:
                self.controls["ready"] = False

    def update_control_message(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        message_type = payload.get("type")
        with self.lock:
            if message_type == "capabilities":
                for key in (
                    "ready",
                    "focusSupported",
                    "exposureSupported",
                    "focusMode",
                    "focusPosition",
                    "exposureIndex",
                    "exposureMin",
                    "exposureMax",
                ):
                    if key in payload:
                        self.controls[key] = payload[key]
                self.controls["ready"] = True
            elif message_type == "control_applied":
                for key in ("focusMode", "focusPosition", "exposureIndex"):
                    if key in payload:
                        self.controls[key] = payload[key]

    def control_status(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.controls)

    async def send_control(self, payload: dict[str, Any]) -> bool:
        with self.lock:
            websocket = self.socket
        if websocket is None:
            return False
        try:
            await websocket.send_text(json.dumps(payload))
            return True
        except Exception:
            return False

    def snapshot(self) -> tuple[bytes | None, float | None, int, int, int]:
        with self.lock:
            return self.frame, self.last_frame_at, self.frame_count, self.connection_count, self.version


def automatic_thermal_capture(request: dict[str, Any]) -> dict[str, Any]:
    # Automatic captures are serialized at the batch level. Each batch still
    # prepares and triggers its configured thermal phones in parallel inside
    # ThermalCaptureManager.
    expected_session_id = request.get("sessionId")
    session = database.get_active_session()
    if expected_session_id is not None and (
        session is None or int(session["id"]) != int(expected_session_id)
    ):
        return {"status": "skipped", "message": "Capture cancelled because its processing session ended."}
    if session is not None and bool(session["test_mode"]) and not thermal_manager.configured_devices():
        return {
            "status": "skipped",
            "message": "Thermal capture skipped; no thermal cameras are configured in test mode.",
            "total": 0,
            "successful": 0,
            "results": [],
        }
    with capture_lock:
        session = database.get_active_session()
        if expected_session_id is not None and (
            session is None or int(session["id"]) != int(expected_session_id)
        ):
            return {"status": "skipped", "message": "Capture cancelled because its processing session ended."}
        return thermal_manager.capture_burst(request)


top_camera = TopCameraState()
top_camera.set_processing_enabled(database.get_active_session() is not None)
qr_worker = QRProcessingWorker(top_camera.snapshot, automatic_thermal_capture)
qr_worker.start()


@app.on_event("shutdown")
def shutdown_background_workers() -> None:
    qr_worker.stop()


class GenerateSiteRequest(BaseModel):
    siteCode: str
    cowCount: int


class StartSessionRequest(BaseModel):
    siteCode: str
    testMode: bool = False


class ThermalSetupCheckRequest(BaseModel):
    slot: int
    ipAddress: str
    pairingPort: str = ""
    pairingCode: str = ""
    connectionPort: str
    quarter: str
    testMode: bool = False


class ThermalSetupSyncRequest(BaseModel):
    verifiedSlots: list[int] = Field(default_factory=list)


class TopCameraSetupRequest(BaseModel):
    name: str = "Overhead camera"
    ipAddress: str
    pairingPort: str = ""
    pairingCode: str = ""
    connectionPort: str


class TopCameraControlRequest(BaseModel):
    focusPosition: float | None = Field(default=None, ge=0.0, le=1.0)
    autofocus: bool | None = None
    exposureIndex: int | None = Field(default=None, ge=-100, le=100)


class CaptureRequest(BaseModel):
    siteCode: str | None = None
    cowId: str | None = None
    epochTimestamp: int | None = None
    testMode: bool = False


@app.get("/api/health")
def health() -> dict[str, Any]:
    try:
        return thermal_manager.health()
    except (AdbError, subprocess.TimeoutExpired) as exc:
        return {"connected": False, "error": str(exc)}


@app.get("/api/thermal/devices")
def thermal_devices() -> dict[str, Any]:
    return thermal_manager.health()


@app.post("/api/thermal/setup/check")
def check_thermal_setup(request: ThermalSetupCheckRequest) -> dict[str, Any]:
    try:
        return thermal_manager.check_and_register(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (AdbError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/thermal/setup/sync")
def sync_thermal_setup(request: ThermalSetupSyncRequest) -> dict[str, Any]:
    try:
        return thermal_manager.sync_verified_slots(request.verifiedSlots)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid thermal camera slot list") from exc


@app.delete("/api/sites/{site_code}")
def delete_site(site_code: str) -> dict[str, Any]:
    try:
        # Serialize deletion with capture writes so a completed site removal
        # cannot be followed by a late file from an in-flight capture.
        with capture_lock:
            return database.delete_site(site_code, (CAPTURES_DIR, TEST_DIR))
    except database.SiteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except database.SiteActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Site database records were deleted, but stored files could not be fully removed: {exc}") from exc


@app.post("/api/top-camera/setup/check")
def check_top_camera_setup(request: TopCameraSetupRequest) -> dict[str, Any]:
    try:
        return top_camera_manager.check_and_register(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (TopCameraAdbError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/top-camera/status")
def top_camera_status() -> dict[str, Any]:
    frame, last_frame_at, frame_count, connection_count, _ = top_camera.snapshot()
    stream_active = top_camera.is_processing_enabled()
    age = None if last_frame_at is None else max(0.0, time.time() - last_frame_at)
    try:
        profile = top_camera_manager.health()
    except TopCameraAdbError as exc:
        profile = {
            "configured": False,
            "verified": False,
            "name": "",
            "ipAddress": "",
            "connectionPort": "",
            "deviceConnected": False,
            "error": str(exc),
        }
    return {
        **profile,
        "streamActive": stream_active,
        "connected": stream_active and connection_count > 0 and age is not None and age < 3.0,
        "socketConnected": stream_active and connection_count > 0,
        "lastFrameAgeSeconds": age,
        "frameCount": frame_count,
        "hasFrame": frame is not None,
        "transport": "ADB reverse / localhost",
        "controls": top_camera.control_status(),
    }


@app.post("/api/top-camera/control")
async def control_top_camera(request: TopCameraControlRequest) -> dict[str, Any]:
    if not top_camera.is_processing_enabled():
        raise HTTPException(status_code=409, detail="Start a processing session before changing camera controls")
    payload: dict[str, Any] = {"type": "camera_settings"}
    if request.focusPosition is not None:
        payload["focusPosition"] = request.focusPosition
        payload["focusMode"] = "manual"
    if request.autofocus is not None:
        payload["focusMode"] = "auto" if request.autofocus else "manual"
    if request.exposureIndex is not None:
        payload["exposureIndex"] = request.exposureIndex
    if len(payload) == 1:
        raise HTTPException(status_code=422, detail="Provide a focus or exposure setting")
    if not await top_camera.send_control(payload):
        raise HTTPException(status_code=503, detail="The overhead camera sender is not connected")
    return {"status": "sent", "controls": top_camera.control_status()}


@app.websocket("/ws/top-camera")
async def top_camera_socket(websocket: WebSocket) -> None:
    token = websocket.query_params.get("token")
    if token != TOP_CAMERA_TOKEN or not top_camera.is_processing_enabled():
        await websocket.close(code=1008)
        return

    await websocket.accept()
    top_camera.set_socket(websocket)
    top_camera.set_connection(True)
    try:
        await websocket.send_text("ready")
        while True:
            if not top_camera.is_processing_enabled():
                break
            message = await websocket.receive()
            if not top_camera.is_processing_enabled():
                break
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("text") is not None:
                top_camera.update_control_message(message["text"])
                continue
            frame = message.get("bytes")
            if frame is None:
                continue
            if len(frame) < 4 or not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
                continue
            top_camera.update(frame)
    except WebSocketDisconnect:
        pass
    finally:
        top_camera.set_socket(None)
        top_camera.set_connection(False)


async def mjpeg_frames():
    last_version = -1
    while True:
        if not top_camera.is_processing_enabled():
            return
        frame, _, _, _, version = top_camera.snapshot()
        if frame is not None and version != last_version:
            last_version = version
            yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n"
        else:
            await asyncio.sleep(0.05)


@app.get("/api/top-camera/mjpeg")
async def top_camera_mjpeg() -> StreamingResponse:
    return StreamingResponse(
        mjpeg_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.get("/api/sites")
def sites() -> list[dict[str, Any]]:
    return database.list_sites()


@app.post("/api/sites/generate")
def generate_site(request: GenerateSiteRequest) -> dict[str, Any]:
    try:
        return database.create_site(request.siteCode, request.cowCount)
    except database.SiteAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="This site already exists. Please create a new site.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/processing/sessions")
def start_processing_session(request: StartSessionRequest) -> dict[str, Any]:
    try:
        if not request.testMode:
            validate_production_layout(thermal_manager.configured_devices())
        result = database.start_session(request.siteCode, request.testMode)
        top_camera.set_processing_enabled(True)
        return result
    except database.SiteNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/processing/sessions/stop")
def stop_processing_session() -> dict[str, str]:
    database.stop_active_session("completed")
    top_camera.set_processing_enabled(False)
    return {"status": "completed"}


@app.post("/api/processing/sessions/pause")
def pause_processing_session() -> dict[str, str]:
    if not database.pause_active_session():
        raise HTTPException(status_code=409, detail="No active processing session to pause")
    return {"status": "paused"}


@app.post("/api/processing/sessions/resume")
def resume_processing_session() -> dict[str, str]:
    if not database.resume_active_session():
        raise HTTPException(status_code=409, detail="No paused processing session to resume")
    return {"status": "active"}


@app.get("/api/processing/status")
def processing_status() -> dict[str, Any]:
    return qr_worker.status()


@app.post("/api/processing/visits/{visit_id}/override")
def override_processing_visit(visit_id: int) -> dict[str, Any]:
    try:
        result = database.override_visit(visit_id, time.time())
        qr_worker.mark_override(result.get("payload"), int(result["id"]))
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/capture")
def capture(request: CaptureRequest | None = None) -> dict[str, Any]:
    if not capture_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="A capture is already in progress")
    try:
        try:
            return thermal_manager.capture_all(request.model_dump() if request else {})
        except (AdbError, subprocess.TimeoutExpired) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        capture_lock.release()


def _capture_path(capture_path: str) -> Path:
    relative = Path(capture_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HTTPException(status_code=400, detail="Invalid capture path")
    if not IMAGE_NAME_RE.fullmatch(relative.name):
        raise HTTPException(status_code=400, detail="Invalid capture filename")

    for root in (CAPTURES_DIR, TEST_DIR):
        root_path = root.resolve()
        path = (root_path / relative).resolve()
        if root_path == path or root_path not in path.parents:
            continue
        if path.is_file():
            return path
    raise HTTPException(status_code=404, detail="Capture not found")


@app.get("/api/captures/{capture_path:path}")
def get_capture(capture_path: str) -> FileResponse:
    path = _capture_path(capture_path)
    media_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(path, media_type=media_type, filename=path.name)


@app.get("/api/captures")
def list_captures() -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    for root, category in ((CAPTURES_DIR, "production"), (TEST_DIR, "test")):
        for path in root.rglob("*"):
            if not path.is_file() or not IMAGE_NAME_RE.fullmatch(path.name):
                continue
            relative = path.relative_to(root).as_posix()
            captures.append(
                {
                    "filename": path.name,
                    "path": relative,
                    "category": category,
                    "imageUrl": f"/api/captures/{quote(relative)}",
                    "bytes": path.stat().st_size,
                }
            )
    captures.sort(key=lambda item: item["filename"], reverse=True)
    return captures[:20]


if FRONTEND_DIST.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
