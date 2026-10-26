from __future__ import annotations

import ipaddress
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TopCameraAdbError(RuntimeError):
    pass


TOP_CAMERA_PACKAGE = "local.thermalmaster.topcamera"
TOP_CAMERA_ACTIVITY = f"{TOP_CAMERA_PACKAGE}/.MainActivity"
TOP_CAMERA_REVERSE_PORT = 8000
DIRECT_SERIAL_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d+$")


@dataclass
class TopCameraConfig:
    name: str
    ip_address: str
    connection_port: int
    serial: str
    model: str | None = None
    android_version: str | None = None
    verified_at: str | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.ip_address}:{self.connection_port}"


class TopCameraManager:
    """Manage one user-selected Android phone used as the overhead camera.

    The phone never opens a LAN camera server. Its sender app connects to the
    PC through an ADB reverse tunnel on the phone's loopback interface.
    """

    def __init__(self, adb_path: Path, config_path: Path, apk_path: Path) -> None:
        self.adb_path = adb_path
        self.config_path = config_path
        self.apk_path = apk_path
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self._config = self._load_config()

    def _load_config(self) -> TopCameraConfig | None:
        if not self.config_path.exists():
            return None
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top-camera configuration must be an object")
            return TopCameraConfig(**raw)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise TopCameraAdbError(f"Could not read top-camera configuration: {exc}") from exc

    def _save_config(self) -> None:
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self._config), indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    @staticmethod
    def _validate_private_ip(value: str) -> str:
        try:
            address = ipaddress.ip_address(value.strip())
        except ValueError as exc:
            raise ValueError("Enter a valid IPv4 address") from exc
        if address.version != 4 or not address.is_private or address.is_loopback:
            raise ValueError("The overhead phone must use a private IPv4 address")
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

    def _run_result(self, *args: str, serial: str | None = None, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        if not self.adb_path.exists():
            raise TopCameraAdbError(f"ADB was not found at {self.adb_path}")
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
            raise TopCameraAdbError(f"ADB command timed out: {' '.join(command[1:])}") from exc

    def _run(self, *args: str, serial: str | None = None, timeout: float = 20) -> str:
        result = self._run_result(*args, serial=serial, timeout=timeout)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).strip() or "no diagnostic output"
            raise TopCameraAdbError(f"ADB command failed ({result.returncode}) for {' '.join(args)}: {message}")
        return result.stdout

    def _connected_device(self, serial: str) -> dict[str, str] | None:
        output = self._run("devices", "-l")
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 2 or parts[0] != serial or parts[1] != "device":
                continue
            values = {"serial": parts[0], "state": parts[1]}
            for token in parts[2:]:
                if ":" in token:
                    key, value = token.split(":", 1)
                    values[key] = value
            return values
        return None

    def _connect_endpoint(
        self,
        ip_address: str,
        connection_port: int,
        pairing_port: int | str | None = None,
        pairing_code: str | None = None,
    ) -> str:
        serial = f"{ip_address}:{connection_port}"
        connect = self._run_result("connect", serial, timeout=10)
        if self._connected_device(serial) is not None:
            return serial

        pairing_message = ""
        if pairing_port and pairing_code:
            pair = self._run_result(
                "pair",
                f"{ip_address}:{self._validate_port(pairing_port, 'Pairing port')}",
                str(pairing_code),
                timeout=15,
            )
            pairing_message = (pair.stderr or pair.stdout).strip()
            self._run_result("connect", serial, timeout=10)

        if self._connected_device(serial) is None:
            details = (connect.stderr or connect.stdout).strip()
            if pairing_message:
                details = f"{details}; pairing: {pairing_message}"
            raise TopCameraAdbError(f"Could not connect to {serial}: {details}")
        return serial

    def _ensure_sender_installed(self, serial: str) -> bool:
        # `pm path` returns exit code 1 when the package is simply absent. That
        # is the normal branch for a new overhead phone, not a connection error.
        package_result = self._run_result("shell", "pm", "path", TOP_CAMERA_PACKAGE, serial=serial)
        existing_package = package_result.stdout.strip()
        if existing_package:
            return False
        if not self.apk_path.is_file():
            raise TopCameraAdbError(f"Top Camera Sender APK was not found at {self.apk_path}")
        install = self._run_result("install", "-r", str(self.apk_path), serial=serial, timeout=90)
        if install.returncode != 0:
            message = (install.stderr or install.stdout).strip() or "no diagnostic output"
            raise TopCameraAdbError(f"Could not install Top Camera Sender on {serial}: {message}")
        installed_package = self._run("shell", "pm", "path", TOP_CAMERA_PACKAGE, serial=serial).strip()
        if not installed_package:
            raise TopCameraAdbError(f"Top Camera Sender installation could not be verified on {serial}")
        return True

    def _deactivate_previous_sender(self, config: TopCameraConfig | None, new_serial: str) -> None:
        if config is None or config.serial == new_serial:
            return
        # The previous phone may already be offline. These are cleanup calls,
        # so a failure must not prevent the newly selected phone from working.
        for command in (
            ("shell", "am", "force-stop", TOP_CAMERA_PACKAGE),
            ("reverse", "--remove", f"tcp:{TOP_CAMERA_REVERSE_PORT}"),
        ):
            try:
                self._run_result(*command, serial=config.serial, timeout=8)
            except TopCameraAdbError:
                pass

    def _profile(self) -> dict[str, Any]:
        if self._config is None:
            return {
                "configured": False,
                "verified": False,
                "name": "",
                "ipAddress": "",
                "connectionPort": "",
                "deviceConnected": False,
            }
        connected = self._connected_device(self._config.serial) is not None
        return {
            "configured": True,
            "verified": bool(self._config.verified_at),
            "name": self._config.name,
            "ipAddress": self._config.ip_address,
            "connectionPort": self._config.connection_port,
            "endpoint": self._config.endpoint,
            "serial": self._config.serial,
            "model": self._config.model,
            "androidVersion": self._config.android_version,
            "verifiedAt": self._config.verified_at,
            "deviceConnected": connected,
        }

    def check_and_register(self, request: dict[str, Any]) -> dict[str, Any]:
        name = str(request.get("name", "")).strip() or "Overhead camera"
        ip_address = self._validate_private_ip(str(request.get("ipAddress", "")))
        connection_port = self._validate_port(request.get("connectionPort", ""), "Connection port")
        serial = self._connect_endpoint(
            ip_address,
            connection_port,
            request.get("pairingPort") or None,
            request.get("pairingCode") or None,
        )
        installed_now = self._ensure_sender_installed(serial)

        device = self._connected_device(serial) or {}
        config = TopCameraConfig(
            name=name,
            ip_address=ip_address,
            connection_port=connection_port,
            serial=serial,
            model=device.get("model"),
            android_version=self._run("shell", "getprop", "ro.build.version.release", serial=serial).strip(),
            verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        # The sender only targets 127.0.0.1:8000. This reverse mapping keeps
        # the stream private to this ADB-connected phone and local PC.
        self._run("reverse", f"tcp:{TOP_CAMERA_REVERSE_PORT}", f"tcp:{TOP_CAMERA_REVERSE_PORT}", serial=serial)
        previous_config = self._config
        self._deactivate_previous_sender(previous_config, serial)
        self._run("shell", "am", "start", "-n", TOP_CAMERA_ACTIVITY, serial=serial)

        self._config = config
        self._save_config()
        install_message = " APK installed." if installed_now else ""
        return {**self._profile(), "status": "verified", "installed": installed_now, "message": f"{name} verified.{install_message} Sender launched."}

    def health(self) -> dict[str, Any]:
        return self._profile()
