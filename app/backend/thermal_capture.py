from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AdbError(RuntimeError):
    pass


DIRECT_SERIAL_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")
IMAGE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.(?:jpg|jpeg|png)$", re.IGNORECASE)
PACKAGE_NAME = "com.thermalmaster.p2telephoto"
LAUNCHER_ACTIVITY = f"{PACKAGE_NAME}/.splash.SplashActivity"
# Thermal Master versions and Android storage implementations do not all use
# the same public media directory.  Keep the scan limited to public image
# locations rather than assuming one directory exists on every phone.
REMOTE_IMAGE_ROOTS = (
    "/sdcard/DCIM",
    "/sdcard/Pictures",
    "/sdcard/Download",
    "/sdcard/Movies",
    "/sdcard/Android/media",
)
RESOURCE_MEDIA_MENU = f"{PACKAGE_NAME}:id/ivMediaMenu"
RESOURCE_MEDIA_PANEL = f"{PACKAGE_NAME}:id/llMediaMenu"
RESOURCE_TAKE_PHOTO = f"{PACKAGE_NAME}:id/imgMenuTakePhoto"
PHOTO_CONTROL_TIMEOUT = 6
MEDIA_MENU_OPEN_ATTEMPTS = 2
MAX_PRODUCTION_CAMERAS = 5
PRODUCTION_UDDER_AREAS = frozenset(
    {"left-upper", "left-lower", "right-upper", "right-lower"}
)
PRODUCTION_EYE_AREAS = frozenset({"left-eye", "right-eye"})
BURST_CAPTURE_COUNT = 5
BURST_INTERVAL_SECONDS = 2.0

# uiautomator2 uses adbutils, which must be pointed at the project's bundled
# platform-tools because adb is intentionally not installed globally.


@dataclass
class ThermalDeviceConfig:
    slot: int
    ip_address: str
    connection_port: int
    quarter: str
    serial: str
    model: str | None = None
    android_version: str | None = None
    app_version: str | None = None
    verified_at: str | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.ip_address}:{self.connection_port}"


def validate_production_layout(configs: list[ThermalDeviceConfig]) -> None:
    if len(configs) != MAX_PRODUCTION_CAMERAS:
        raise ValueError(f"Standard mode requires exactly {MAX_PRODUCTION_CAMERAS} thermal cameras")

    areas = [str(config.quarter).strip().lower() for config in configs]
    eye_count = sum(area in PRODUCTION_EYE_AREAS for area in areas)
    if (
        not PRODUCTION_UDDER_AREAS.issubset(areas)
        or eye_count != 1
        or any(area not in PRODUCTION_UDDER_AREAS | PRODUCTION_EYE_AREAS for area in areas)
    ):
        raise ValueError(
            "Standard mode requires left-upper, left-lower, right-upper, right-lower, "
            "and exactly one eye camera (left-eye or right-eye)"
        )


class ThermalCaptureManager:
    def __init__(self, adb_path: Path, captures_dir: Path, test_dir: Path, config_path: Path) -> None:
        self.adb_path = adb_path
        self.captures_dir = captures_dir
        self.test_dir = test_dir
        self.config_path = config_path
        self.captures_dir.mkdir(parents=True, exist_ok=True)
        self.test_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config_lock = threading.RLock()
        self._configs: dict[int, ThermalDeviceConfig] = self._load_configs()

        os.environ.setdefault("ADBUTILS_ADB_PATH", str(self.adb_path))
        try:
            import uiautomator2 as u2
        except ImportError as exc:  # pragma: no cover - dependency is pinned in requirements
            raise AdbError(
                "uiautomator2 is missing from the backend virtual environment"
            ) from exc
        self._u2 = u2

    def _load_configs(self) -> dict[int, ThermalDeviceConfig]:
        if not self.config_path.exists():
            return {}
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            return {
                int(item["slot"]): ThermalDeviceConfig(**item)
                for item in raw
                if isinstance(item, dict)
            }
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AdbError(f"Could not read thermal camera configuration: {exc}") from exc

    def _save_configs(self) -> None:
        payload = [asdict(self._configs[slot]) for slot in sorted(self._configs)]
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    def configured_devices(self) -> list[ThermalDeviceConfig]:
        with self._config_lock:
            return [self._configs[slot] for slot in sorted(self._configs)]

    def sync_verified_slots(self, verified_slots: list[int]) -> dict[str, list[int]]:
        """Keep persisted thermal devices aligned with the current setup UI.

        The setup page is the source of truth for which camera slots are
        active.  Any previously saved slot that is not currently verified is
        removed so an old phone cannot silently participate in a later batch.
        """
        normalized = sorted({int(slot) for slot in verified_slots})
        if any(slot < 1 for slot in normalized):
            raise ValueError("Camera slots must be positive integers")
        with self._config_lock:
            removed = sorted(set(self._configs) - set(normalized))
            self._configs = {
                slot: config
                for slot, config in self._configs.items()
                if slot in normalized and config.verified_at
            }
            if removed or len(self._configs) != len(normalized):
                self._save_configs()
            return {
                "removedSlots": removed,
                "activeSlots": sorted(self._configs),
            }

    def _run_adb_result(
        self,
        *args: str,
        serial: str | None = None,
        timeout: float = 20,
    ) -> subprocess.CompletedProcess[str]:
        if not self.adb_path.exists():
            raise AdbError(f"ADB was not found at {self.adb_path}")
        command = [str(self.adb_path)]
        if serial:
            command.extend(["-s", serial])
        command.extend(args)
        try:
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
                shell=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise AdbError(f"ADB command timed out: {' '.join(command[1:])}") from exc

    def _run_adb(self, *args: str, serial: str | None = None, timeout: float = 20) -> str:
        completed = self._run_adb_result(*args, serial=serial, timeout=timeout)
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout).strip()
            raise AdbError(f"ADB command failed ({completed.returncode}): {message}")
        return completed.stdout

    def _parse_devices(self) -> list[dict[str, str]]:
        output = self._run_adb("devices", "-l")
        devices: list[dict[str, str]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices") or line.startswith("*"):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            values = {"serial": parts[0], "state": parts[1]}
            for token in parts[2:]:
                if ":" in token:
                    key, value = token.split(":", 1)
                    values[key] = value
            devices.append(values)
        return devices

    def _connected_device(self, serial: str) -> dict[str, str] | None:
        matches = [
            device
            for device in self._parse_devices()
            if device.get("serial") == serial and device.get("state") == "device"
        ]
        return matches[0] if matches else None

    @staticmethod
    def _validate_private_ip(value: str) -> str:
        try:
            address = ipaddress.ip_address(value.strip())
        except ValueError as exc:
            raise ValueError("Enter a valid IPv4 address") from exc
        if address.version != 4 or not address.is_private or address.is_loopback:
            raise ValueError("Thermal phones must use a private IPv4 address")
        return str(address)

    @staticmethod
    def _validate_port(value: int | str, label: str) -> int:
        try:
            port = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a valid TCP port") from exc
        if not 1 <= port <= 65535:
            raise ValueError(f"{label} must be between 1 and 65535")
        return port

    def _connect_endpoint(
        self,
        ip_address: str,
        connection_port: int,
        pairing_port: int | str | None = None,
        pairing_code: str | None = None,
    ) -> str:
        ip_address = self._validate_private_ip(ip_address)
        connection_port = self._validate_port(connection_port, "Connection port")
        serial = f"{ip_address}:{connection_port}"

        connect = self._run_adb_result("connect", serial, timeout=10)
        if not self._connected_device(serial):
            pairing_message = ""
            if pairing_port and pairing_code:
                pairing_port = self._validate_port(pairing_port, "Pairing port")
                pair = self._run_adb_result(
                    "pair",
                    f"{ip_address}:{pairing_port}",
                    str(pairing_code),
                    timeout=15,
                )
                pairing_message = (pair.stderr or pair.stdout).strip()
                self._run_adb_result("connect", serial, timeout=10)
            device = self._connected_device(serial)
            if device is None:
                details = (connect.stderr or connect.stdout).strip()
                if pairing_message:
                    details = f"{details}; pairing: {pairing_message}"
                raise AdbError(f"Could not connect to {serial}: {details}")
        return serial

    def _foreground_package(self, serial: str) -> str | None:
        output = self._run_adb("shell", "dumpsys", "activity", "activities", serial=serial)
        match = re.search(r"(?:mResumedActivity|ResumedActivity):.*?\bu\d+\s+([A-Za-z0-9._]+)/", output)
        return match.group(1) if match else None

    def _ensure_foreground(self, serial: str) -> None:
        current = self._foreground_package(serial)
        if current == PACKAGE_NAME:
            return
        self._run_adb("shell", "am", "start", "-n", LAUNCHER_ACTIVITY, serial=serial)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            current = self._foreground_package(serial)
            if current == PACKAGE_NAME:
                return
            if current == "com.android.permissioncontroller":
                raise AdbError("Thermal Master is waiting for an Android permission decision")
            time.sleep(0.35)
        raise AdbError(f"Thermal Master did not reach its main screen on {serial}")

    def _app_version(self, serial: str) -> str | None:
        output = self._run_adb("shell", "dumpsys", "package", PACKAGE_NAME, serial=serial)
        match = re.search(r"versionName=([^\s]+)", output)
        return match.group(1) if match else None

    def _open_ui(self, serial: str):
        device = self._u2.connect(serial)
        device.settings["wait_timeout"] = 3
        device.jsonrpc.setConfigurator(
            {"waitForIdleTimeout": 0, "waitForSelectorTimeout": 0}
        )
        return device

    def _wait_for_ui_package(self, device: Any, timeout: float = 6) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if device.info.get("currentPackageName") == PACKAGE_NAME:
                return
            time.sleep(0.25)
        current = device.info.get("currentPackageName")
        raise AdbError(f"Thermal Master is not the active package (current: {current or 'unknown'})")

    @staticmethod
    def _is_clickable_enabled(obj: Any) -> bool:
        info = obj.info
        return bool(info.get("exists", True) and info.get("clickable") and info.get("enabled"))

    @staticmethod
    def _is_present(obj: Any) -> bool:
        try:
            return bool(obj.exists)
        except Exception:
            return False

    def _wait_resource(self, device: Any, resource_id: str, timeout: float = 6):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            obj = device(resourceId=resource_id)
            if obj.exists and self._is_clickable_enabled(obj):
                return obj
            time.sleep(0.2)
        raise AdbError(f"Thermal Master control was not found or is not clickable: {resource_id}")

    def _prepare_photo_control(self, device: Any):
        """Return a semantic capture control regardless of menu state.

        Thermal Master can expose the photo action immediately or only after
        its media menu has been opened.  We detect the current state instead
        of assuming a particular phone or screen layout, and retry the menu
        transition once if the first click does not produce the action.
        """
        photo = device(resourceId=RESOURCE_TAKE_PHOTO)
        if self._is_present(photo) and self._is_clickable_enabled(photo):
            return photo

        menu = self._wait_resource(device, RESOURCE_MEDIA_MENU)
        last_error: Exception | None = None

        for attempt in range(MEDIA_MENU_OPEN_ATTEMPTS):
            try:
                panel = device(resourceId=RESOURCE_MEDIA_PANEL)
                if not self._is_present(panel):
                    menu.click()

                photo = self._wait_resource(
                    device,
                    RESOURCE_TAKE_PHOTO,
                    timeout=PHOTO_CONTROL_TIMEOUT,
                )
                return photo
            except Exception as exc:
                last_error = exc
                if attempt + 1 < MEDIA_MENU_OPEN_ATTEMPTS:
                    # If the menu transition did not become visible, give
                    # the app a short settling period before trying again.
                    time.sleep(0.35)

        raise AdbError(
            "Thermal Master photo control did not become available after "
            f"opening the media menu ({MEDIA_MENU_OPEN_ATTEMPTS} attempts): "
            f"{last_error}"
        ) from last_error

    def _stop_ui(self, device: Any, serial: str) -> None:
        try:
            device.stop_uiautomator()
        except Exception:
            pass
        for path in ("/data/local/tmp/u2.jar", "/data/local/tmp/u2"):
            try:
                self._run_adb("shell", "rm", "-rf", path, serial=serial, timeout=5)
            except AdbError:
                pass

    def _list_remote_images(self, serial: str) -> set[str]:
        """Return public image paths currently visible on the phone.

        A missing directory is normal on a new phone or before the first
        capture, so it must not abort the shutter workflow.  `find` is used
        instead of a fixed directory listing because Android and Thermal
        Master versions may save through different public media locations.
        """
        images: set[str] = set()
        for root in REMOTE_IMAGE_ROOTS:
            result = self._run_adb_result(
                "shell",
                "find",
                root,
                "-maxdepth",
                "5",
                "-type",
                "f",
                serial=serial,
                timeout=10,
            )
            if result.returncode != 0:
                # The root may not exist on this Android build.  Continue
                # scanning the other public media locations.
                continue
            for path in result.stdout.splitlines():
                normalized = path.strip()
                filename = normalized.rsplit("/", 1)[-1]
                if normalized and IMAGE_NAME_RE.fullmatch(filename):
                    images.add(normalized)
        return images

    def _remote_size(self, serial: str, remote_path: str) -> int:
        output = self._run_adb("shell", "stat", "-c", "%s", remote_path, serial=serial)
        try:
            return int(output.strip())
        except ValueError as exc:
            raise AdbError(f"Could not read the size of {remote_path}") from exc

    def _wait_for_new_image(
        self,
        serial: str,
        before: set[str],
        timeout: float = 20,
    ) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self._list_remote_images(serial)
            new_images = current - before
            if new_images:
                candidate = sorted(new_images)[-1]
                remote_path = candidate
                size = self._remote_size(serial, remote_path)
                time.sleep(0.25)
                if size > 0 and self._remote_size(serial, remote_path) == size:
                    return candidate
            time.sleep(0.35)
        raise AdbError(f"No new Thermal Master image appeared on {serial}")

    @staticmethod
    def _safe_token(value: str, fallback: str) -> str:
        token = re.sub(r"[^A-Za-z0-9-]+", "-", str(value or "").strip()).strip("-")
        return token or fallback

    def _local_name(
        self,
        config: ThermalDeviceConfig,
        remote_name: str,
        request: dict[str, Any],
        timestamp: int,
        cycle: int | None = None,
    ) -> str:
        extension = Path(remote_name).suffix.lower()
        site_code = self._safe_token(str(request.get("siteCode", "")), "capture")
        cow_id = self._safe_token(str(request.get("cowId", "")), f"slot-{config.slot}")
        quarter = self._safe_token(config.quarter, f"camera-{config.slot}")
        cycle_suffix = f"_cycle-{cycle:02d}" if cycle is not None else ""
        return f"{site_code}_{cow_id}_{timestamp}_{quarter}{cycle_suffix}{extension}"

    def _local_path(
        self,
        config: ThermalDeviceConfig,
        request: dict[str, Any],
        local_name: str,
    ) -> Path:
        site_code = self._safe_token(str(request.get("siteCode", "")), "site")
        cow_id = self._safe_token(str(request.get("cowId", "")), f"slot-{config.slot}")
        root = self.test_dir if bool(request.get("testMode")) else self.captures_dir
        output_dir = root / site_code / cow_id
        output_dir.mkdir(parents=True, exist_ok=True)
        return output_dir / local_name

    def _store_remote_image(
        self,
        config: ThermalDeviceConfig,
        request: dict[str, Any],
        serial: str,
        remote_path: str,
        timestamp: int,
        trigger_time: int,
        cycle: int | None = None,
    ) -> dict[str, Any]:
        remote_name = remote_path.rsplit("/", 1)[-1]
        local_name = self._local_name(config, remote_name, request, timestamp, cycle)
        local_path = self._local_path(config, request, local_name)
        if local_path.exists():
            local_name = f"{local_path.stem}_{uuid.uuid4().hex[:8]}{local_path.suffix}"
            local_path = local_path.with_name(local_name)
        storage_root = self.test_dir if bool(request.get("testMode")) else self.captures_dir
        relative_path = local_path.relative_to(storage_root).as_posix()
        self._run_adb("pull", remote_path, str(local_path), serial=serial, timeout=45)
        if not local_path.is_file() or local_path.stat().st_size == 0:
            raise AdbError("ADB reported a successful pull, but the local image is empty")
        result = {
            "filename": local_name,
            "path": relative_path,
            "imageUrl": f"/api/captures/{relative_path}",
            "bytes": local_path.stat().st_size,
            "phonePath": remote_path,
            "triggeredAt": datetime.fromtimestamp(
                trigger_time / 1_000_000_000, timezone.utc
            ).isoformat(timespec="milliseconds"),
        }
        if cycle is not None:
            result["cycle"] = cycle
        return result

    def _wait_for_new_images(
        self,
        serial: str,
        before: set[str],
        expected_count: int,
        timeout: float = 25,
    ) -> list[str]:
        deadline = time.monotonic() + timeout
        last_sizes: dict[str, int] = {}
        stable: set[str] = set()
        while time.monotonic() < deadline:
            for path in sorted(self._list_remote_images(serial) - before):
                try:
                    size = self._remote_size(serial, path)
                except AdbError:
                    continue
                if size > 0 and last_sizes.get(path) == size:
                    stable.add(path)
                else:
                    last_sizes[path] = size
            if len(stable) >= expected_count:
                break
            time.sleep(0.25)
        return sorted(stable)[:expected_count]

    def _capture_worker(
        self,
        config: ThermalDeviceConfig,
        request: dict[str, Any],
        timestamp: int,
        barrier: threading.Barrier,
    ) -> dict[str, Any]:
        device = None
        try:
            serial = self._connect_endpoint(config.ip_address, config.connection_port)
            self._ensure_foreground(serial)
            before = self._list_remote_images(serial)
            device = self._open_ui(serial)
            self._wait_for_ui_package(device)
            photo = self._prepare_photo_control(device)

            # Every device reaches this barrier with its photo control ready.
            # No phone is clicked if another configured phone cannot prepare.
            barrier.wait(timeout=20)
            trigger_time = time.time_ns()
            photo.click()
            remote_path = self._wait_for_new_image(serial, before)
            image = self._store_remote_image(config, request, serial, remote_path, timestamp, trigger_time)
            return {
                "slot": config.slot,
                "quarter": config.quarter,
                "serial": serial,
                "model": config.model,
                "status": "completed",
                **image,
            }
        except Exception as exc:
            try:
                barrier.abort()
            except Exception:
                pass
            return {
                "slot": config.slot,
                "quarter": config.quarter,
                "serial": config.endpoint,
                "model": config.model,
                "status": "failed",
                "error": str(exc),
            }
        finally:
            if device is not None:
                self._stop_ui(device, config.endpoint)

    def _capture_configs(
        self,
        configs: list[ThermalDeviceConfig],
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = request or {}
        if not configs:
            raise AdbError("No verified thermal cameras are configured")
        if not request.get("testMode"):
            try:
                validate_production_layout(configs)
            except ValueError as exc:
                raise AdbError(str(exc)) from exc
        timestamp = int(request.get("epochTimestamp") or time.time())
        batch_id = uuid.uuid4().hex
        barrier = threading.Barrier(len(configs))
        results: list[dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=len(configs), thread_name_prefix="thermal-capture") as pool:
            futures = [
                pool.submit(self._capture_worker, config, request, timestamp, barrier)
                for config in configs
            ]
            for future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"status": "failed", "error": str(exc)})

        results.sort(key=lambda item: item.get("slot", 999))
        successful = sum(1 for result in results if result.get("status") == "completed")
        status = "completed" if successful == len(configs) else "partial" if successful else "failed"
        return {
            "batchId": batch_id,
            "status": status,
            "capturedAt": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "total": len(configs),
            "successful": successful,
            "results": results,
        }

    def _capture_burst_worker(
        self,
        config: ThermalDeviceConfig,
        request: dict[str, Any],
        timestamp: int,
        barrier: threading.Barrier,
        cycle_starts: list[float],
    ) -> dict[str, Any]:
        device = None
        serial = config.endpoint
        before: set[str] = set()
        trigger_times: list[int] = []
        error: str | None = None
        try:
            serial = self._connect_endpoint(config.ip_address, config.connection_port)
            self._ensure_foreground(serial)
            before = self._list_remote_images(serial)
            device = self._open_ui(serial)
            self._wait_for_ui_package(device)

            for cycle in range(BURST_CAPTURE_COUNT):
                photo = self._prepare_photo_control(device)
                barrier.wait(timeout=20)
                deadline = cycle_starts[0] + cycle * BURST_INTERVAL_SECONDS
                delay = deadline - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                trigger_time = time.time_ns()
                photo.click()
                trigger_times.append(trigger_time)
        except Exception as exc:
            error = str(exc)
            try:
                barrier.abort()
            except Exception:
                pass
        finally:
            if device is not None:
                self._stop_ui(device, serial)

        remote_images: list[str] = []
        if trigger_times:
            try:
                remote_images = self._wait_for_new_images(
                    serial, before, len(trigger_times), timeout=25 if len(trigger_times) == BURST_CAPTURE_COUNT else 8
                )
            except Exception as exc:
                error = f"{error}; {exc}" if error else str(exc)

        captures: list[dict[str, Any]] = []
        for cycle, remote_path in enumerate(remote_images, start=1):
            try:
                captures.append({
                    **self._store_remote_image(
                        config, request, serial, remote_path, timestamp,
                        trigger_times[min(cycle - 1, len(trigger_times) - 1)], cycle,
                    ),
                })
            except Exception as exc:
                error = f"{error}; {exc}" if error else str(exc)

        if len(captures) < BURST_CAPTURE_COUNT and not error:
            error = f"Only {len(captures)} of {BURST_CAPTURE_COUNT} images were returned"
        status = "completed" if len(captures) == BURST_CAPTURE_COUNT and not error else "partial" if captures else "failed"
        return {
            "slot": config.slot,
            "quarter": config.quarter,
            "serial": serial,
            "model": config.model,
            "status": status,
            "captureCount": len(captures),
            "captureTotal": BURST_CAPTURE_COUNT,
            "captures": captures,
            **(captures[-1] if captures else {}),
            **({"error": error} if error else {}),
        }

    def _capture_burst_configs(
        self,
        configs: list[ThermalDeviceConfig],
        request: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = request or {}
        if not configs:
            raise AdbError("No verified thermal cameras are configured")
        if not request.get("testMode"):
            try:
                validate_production_layout(configs)
            except ValueError as exc:
                raise AdbError(str(exc)) from exc

        timestamp = int(request.get("epochTimestamp") or time.time())
        batch_id = uuid.uuid4().hex
        cycle_starts: list[float] = []
        barrier = threading.Barrier(len(configs), action=lambda: cycle_starts.append(time.monotonic()))
        with ThreadPoolExecutor(max_workers=len(configs), thread_name_prefix="thermal-burst") as pool:
            futures = [
                pool.submit(self._capture_burst_worker, config, request, timestamp, barrier, cycle_starts)
                for config in configs
            ]
            results = []
            for config, future in zip(configs, futures):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append({"slot": config.slot, "quarter": config.quarter, "status": "failed", "error": str(exc)})

        results.sort(key=lambda item: item.get("slot", 999))
        successful = sum(result.get("status") == "completed" for result in results)
        images_captured = sum(result.get("captureCount", 0) for result in results)
        status = "completed" if successful == len(configs) else "partial" if images_captured else "failed"
        return {
            "batchId": batch_id,
            "status": status,
            "capturedAt": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "total": len(configs),
            "successful": successful,
            "imagesCaptured": images_captured,
            "cycles": BURST_CAPTURE_COUNT,
            "cycleIntervalSeconds": BURST_INTERVAL_SECONDS,
            "results": results,
        }

    def capture_burst(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._capture_burst_configs(self.configured_devices(), request)

    def capture_all(self, request: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._capture_configs(self.configured_devices(), request)

    def check_and_register(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            slot = int(request.get("slot"))
        except (TypeError, ValueError) as exc:
            raise ValueError("Camera slot must be an integer") from exc
        test_mode = bool(request.get("testMode"))
        if slot < 1:
            raise ValueError("Camera slot must be a positive integer")
        if not test_mode and slot > MAX_PRODUCTION_CAMERAS:
            raise ValueError(f"Standard mode supports camera slots 1 through {MAX_PRODUCTION_CAMERAS}")
        ip_address = self._validate_private_ip(str(request.get("ipAddress", "")))
        connection_port = self._validate_port(request.get("connectionPort", ""), "Connection port")
        quarter = str(request.get("quarter", "")).strip()
        if not quarter:
            raise ValueError("Choose a quarter assignment")
        with self._config_lock:
            duplicate = next(
                (item.slot for item in self._configs.values() if item.slot != slot and item.quarter == quarter),
                None,
            )
        if duplicate is not None and not test_mode:
            raise ValueError(f"Quarter {quarter} is already assigned to camera {duplicate}")

        serial = self._connect_endpoint(
            ip_address,
            connection_port,
            request.get("pairingPort") or None,
            request.get("pairingCode") or None,
        )
        package_path = self._run_adb("shell", "pm", "path", PACKAGE_NAME, serial=serial).strip()
        if not package_path:
            raise AdbError(f"Thermal Master is not installed on {serial}")
        self._ensure_foreground(serial)
        self._list_remote_images(serial)
        device = None
        try:
            device = self._open_ui(serial)
            self._wait_for_ui_package(device)
            photo = self._prepare_photo_control(device)
            app_version = self._app_version(serial)
            model = self._connected_device(serial) or {}
            config = ThermalDeviceConfig(
                slot=slot,
                ip_address=ip_address,
                connection_port=connection_port,
                quarter=quarter,
                serial=serial,
                model=model.get("model"),
                android_version=self._run_adb("shell", "getprop", "ro.build.version.release", serial=serial).strip(),
                app_version=app_version,
                verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
            # Verify the control is still present after preparation, without
            # taking a test image. The real capture path performs the same
            # click and verifies a new file.
            if not self._is_clickable_enabled(photo):
                raise AdbError(f"Photo control is not enabled on {serial}")
        finally:
            if device is not None:
                self._stop_ui(device, serial)

        test_result = self._capture_configs(
            [config],
            {
                "siteCode": "setup-test",
                "cowId": f"camera-{slot}",
                "epochTimestamp": int(time.time()),
                "testMode": True,
            },
        )
        if test_result["status"] != "completed":
            failure = next(
                (item.get("error") for item in test_result["results"] if item.get("status") == "failed"),
                "Thermal Master did not create a test image",
            )
            raise AdbError(f"Setup test capture failed: {failure}")

        with self._config_lock:
            self._configs[slot] = config
            self._save_configs()
        return {
            "slot": slot,
            "quarter": quarter,
            "serial": serial,
            "model": config.model,
            "androidVersion": config.android_version,
            "appVersion": config.app_version,
            "status": "verified",
            "message": f"Camera {slot} verified with a test capture.",
            "testCapture": test_result["results"][0],
        }

    def health(self) -> dict[str, Any]:
        configured = self.configured_devices()
        devices: list[dict[str, Any]] = []
        for config in configured:
            connected = self._connected_device(config.endpoint) is not None
            output_directory = False
            if connected:
                try:
                    self._list_remote_images(config.endpoint)
                    output_directory = True
                except AdbError:
                    output_directory = False
            devices.append(
                {
                    "slot": config.slot,
                    "quarter": config.quarter,
                    "serial": config.endpoint,
                    "model": config.model,
                    "connected": connected,
                    "verified": bool(config.verified_at),
                    "outputDirectory": output_directory,
                    "appVersion": config.app_version,
                }
            )
        ready = bool(configured) and all(
            item["connected"] and item["verified"] and item["outputDirectory"]
            for item in devices
        )
        return {
            "connected": any(item["connected"] for item in devices),
            "ready": ready,
            "verifiedCount": sum(1 for item in devices if item["verified"]),
            "thermalMasterInstalled": bool(configured),
            "thermalOutputDirectory": bool(configured) and all(item["outputDirectory"] for item in devices),
            "devices": devices,
        }
