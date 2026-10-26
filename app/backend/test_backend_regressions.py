from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import database
import thermal_capture
from thermal_capture import AdbError, ThermalCaptureManager, ThermalDeviceConfig


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class BackendRegressionTests(unittest.TestCase):
    def test_burst_starts_five_synchronized_cycles_two_seconds_apart(self) -> None:
        manager = object.__new__(ThermalCaptureManager)
        config = ThermalDeviceConfig(1, "192.168.1.2", 5555, "left-upper", "serial")
        clock = _Clock()
        click_times: list[float] = []

        class _Photo:
            def click(self) -> None:
                click_times.append(clock.monotonic())

        paths = [f"/phone/image-{index}.jpg" for index in range(5)]
        with (
            patch.object(manager, "configured_devices", return_value=[config]),
            patch.object(manager, "_connect_endpoint", return_value="serial"),
            patch.object(manager, "_ensure_foreground"),
            patch.object(manager, "_list_remote_images", return_value=set()),
            patch.object(manager, "_open_ui", return_value=object()),
            patch.object(manager, "_wait_for_ui_package"),
            patch.object(manager, "_prepare_photo_control", return_value=_Photo()),
            patch.object(manager, "_stop_ui"),
            patch.object(manager, "_wait_for_new_images", return_value=paths),
            patch.object(
                manager,
                "_store_remote_image",
                side_effect=lambda _c, _r, _s, path, _t, _trigger, cycle: {
                    "filename": Path(path).name,
                    "path": path,
                    "imageUrl": f"/api/captures/{path}",
                    "bytes": 1,
                    "cycle": cycle,
                },
            ),
            patch.object(thermal_capture.time, "monotonic", clock.monotonic),
            patch.object(thermal_capture.time, "sleep", clock.sleep),
        ):
            result = manager.capture_burst({"testMode": True})

        self.assertEqual(click_times, [100.0, 102.0, 104.0, 106.0, 108.0])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["results"][0]["captureCount"], 5)

    def test_deleting_site_removes_records_qrs_and_both_capture_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch.object(database, "DATA_DIR", root / "data"),
                patch.object(database, "DATABASE_PATH", root / "data" / "test.sqlite3"),
                patch.object(database, "QR_CODES_DIR", root / "qr_codes"),
            ):
                database.initialize_database()
                database.create_site("MUSCAT", 1)
                session = database.start_session("MUSCAT")
                cow = database.get_cow_by_payload("SITE=MUSCAT|COW=001")
                visit = database.create_active_visit(session["id"], cow["id"], 1.0)
                database.complete_visit(visit["id"], 2.0)
                database.override_visit(visit["id"], 3.0)

                captures = root / "captures" / "MUSCAT" / "001"
                test_captures = root / "test" / "MUSCAT" / "001"
                captures.mkdir(parents=True)
                test_captures.mkdir(parents=True)
                (captures / "capture.jpg").write_bytes(b"production")
                (test_captures / "capture.jpg").write_bytes(b"test")

                with self.assertRaises(database.SiteActiveError):
                    database.delete_site("MUSCAT", (root / "captures", root / "test"))
                database.stop_active_session()
                result = database.delete_site("MUSCAT", (root / "captures", root / "test"))

                self.assertEqual(result, {"siteCode": "MUSCAT", "deleted": True})
                self.assertEqual(database.list_sites(), [])
                self.assertFalse((root / "qr_codes" / "MUSCAT").exists())
                self.assertFalse((root / "captures" / "MUSCAT").exists())
                self.assertFalse((root / "test" / "MUSCAT").exists())
                with database.db_session() as db:
                    for table in ("cows", "processing_sessions", "cow_visits"):
                        self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)

    def test_standard_capture_requires_four_udder_areas_and_one_eye(self) -> None:
        manager = object.__new__(ThermalCaptureManager)
        areas = ["left-upper", "left-lower", "right-upper", "right-lower", "left-eye"]
        configs = [
            ThermalDeviceConfig(slot, "192.168.1.2", 5555, area, f"s{slot}")
            for slot, area in enumerate(areas, start=1)
        ]
        with patch.object(manager, "_capture_worker", side_effect=lambda config, *_: {"slot": config.slot, "status": "completed"}):
            self.assertEqual(manager._capture_configs(configs, {"testMode": False})["total"], 5)
        with self.assertRaisesRegex(AdbError, "exactly 5"):
            manager._capture_configs(configs[:4], {"testMode": False})

        invalid_layout = [*configs[:-1], ThermalDeviceConfig(5, "192.168.1.2", 5555, "behind", "s5")]
        with self.assertRaisesRegex(AdbError, "exactly one eye camera"):
            manager._capture_burst_configs(invalid_layout, {"testMode": False})

    def test_production_setup_has_five_slots_but_test_setup_is_unlimited(self) -> None:
        manager = object.__new__(ThermalCaptureManager)
        manager._configs = {}
        manager._config_lock = threading.RLock()
        request = {
            "slot": 6,
            "ipAddress": "192.168.1.2",
            "connectionPort": "5555",
            "quarter": "left-eye",
        }
        with self.assertRaisesRegex(ValueError, "slots 1 through 5"):
            manager.check_and_register({**request, "testMode": False})

        with (
            patch.object(manager, "_connect_endpoint", return_value="192.168.1.2:5555"),
            patch.object(manager, "_run_adb", side_effect=["package:/base.apk", "14"]),
            patch.object(manager, "_ensure_foreground"),
            patch.object(manager, "_list_remote_images"),
            patch.object(manager, "_open_ui", return_value=object()),
            patch.object(manager, "_wait_for_ui_package"),
            patch.object(manager, "_prepare_photo_control", return_value=object()),
            patch.object(manager, "_is_clickable_enabled", return_value=True),
            patch.object(manager, "_app_version", return_value="1.0"),
            patch.object(manager, "_connected_device", return_value={"model": "test"}),
            patch.object(manager, "_stop_ui"),
            patch.object(
                manager,
                "_capture_configs",
                return_value={"status": "completed", "results": [{"status": "completed"}]},
            ),
            patch.object(manager, "_save_configs"),
        ):
            result = manager.check_and_register({**request, "testMode": True})
        self.assertEqual(result["slot"], 6)

    def test_test_mode_burst_accepts_more_than_five_cameras(self) -> None:
        manager = object.__new__(ThermalCaptureManager)
        configs = [ThermalDeviceConfig(slot, "192.168.1.2", 5555, f"q{slot}", f"s{slot}") for slot in range(1, 8)]
        with (
            patch.object(manager, "configured_devices", return_value=configs),
            patch.object(
                manager,
                "_capture_burst_worker",
                side_effect=lambda config, *_: {"slot": config.slot, "status": "completed", "captureCount": 5},
            ),
        ):
            result = manager.capture_burst({"testMode": True})
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["imagesCaptured"], 35)


if __name__ == "__main__":
    unittest.main()
