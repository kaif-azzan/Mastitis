# Thermal Master X3 Capture, Top Camera, and QR Processing

Local-only React and Python application for displaying a user-selected Android overhead camera, generating registered cow QR codes, recognizing them on the PC, and coordinating Thermal Master X3 captures across verified Android phones.

## First-time setup (Windows)

The current PowerShell launch scripts expect the project at `D:\ThermalMaster-X3-Automation`. Clone it there, or update the `$project` / `$frontend` paths in `scripts\run_app.ps1` and `scripts\run_frontend_dev.ps1` if you use another location.

```powershell
git clone https://github.com/kaif-azzan/Mastitis.git D:\ThermalMaster-X3-Automation
Set-Location D:\ThermalMaster-X3-Automation
```

Install Python 3.13 and Node.js (including npm), then from the project root install the backend and frontend dependencies:

```powershell
py -3.13 -m venv .\app\backend\.venv
& .\app\backend\.venv\Scripts\python.exe -m pip install -r .\app\backend\requirements.txt
Push-Location .\app\frontend
npm ci
Pop-Location
```

### Overhead-camera phone setup

The top-camera Android app is source code in `app\top-camera-android`; the APK is not committed. To build it, install JDK 17, Android SDK Platform 34, and Gradle 9.1. Configure `ANDROID_HOME` (or Android Studio's SDK path) and make sure `gradle` is available in PowerShell, then run:

```powershell
Push-Location .\app\top-camera-android
gradle assembleDebug
Pop-Location
```

The build creates `app\top-camera-android\app\build\outputs\apk\debug\app-debug.apk`, which is the path the backend expects. Install Android SDK Platform-Tools and place `adb.exe` and its companion DLLs in `tools\platform-tools` (or update `ADB_PATH` in `app\backend\main.py`). Connect the phone using Android Wireless debugging. When you choose `Check overhead camera` in Setup, the backend installs the APK if needed, creates the ADB reverse tunnel, and launches the sender app. Approve Android's camera permission prompt if shown.

## Start

After installing dependencies and building the overhead-camera APK if needed, run each command in a separate PowerShell window from the project root:

```powershell
& '.\scripts\run_app.ps1'
```

Open `http://127.0.0.1:8000/` in a browser on the same PC.

The backend is intentionally bound to `127.0.0.1`. It does not listen on the home Wi-Fi network.

## Configurable overhead-camera preview

The top-camera prototype is at `app/top-camera-android`. It requests only Camera and Internet permissions. Camera frames are sent as throttled JPEG previews to the PC through an ADB reverse tunnel; frames are kept in PC memory and are not saved on the selected phone or PC.

On the Setup page, enter the selected phone's private IP address, ADB connection port, and optional wireless-debugging pairing details, then choose `Check overhead camera`. The backend connects only to that explicit private endpoint, installs the locally built `Top Camera Sender` APK if it is missing, creates the private reverse tunnel, and launches the sender. The selected phone's name and endpoint are saved in `data\top_camera.json`.

The manual tunnel script is also generic. With the selected phone paired and online over ADB, run:

```powershell
& '.\scripts\setup_top_camera_tunnel.ps1' -Serial '<PHONE_IP>:<ADB_CONNECTION_PORT>'
& '.\tools\platform-tools\adb.exe' -s '<PHONE_IP>:<ADB_CONNECTION_PORT>' shell am start -n 'local.thermalmaster.topcamera/.MainActivity'
```

Install/update the locally built APK with:

```powershell
& '.\tools\platform-tools\adb.exe' -s '<PHONE_IP>:<ADB_CONNECTION_PORT>' install -r '.\app\top-camera-android\app\build\outputs\apk\debug\app-debug.apk'
```

The current sender build uses the existing Android SDK 34 and project-local Gradle 9.1. The selected phone must be disconnected from the X3 while it is being used as the overhead camera.

## QR generation and processing

Open the `Generate QR codes` page, enter a new site code and the number of cows, then choose `Generate and save`. The application writes one high-error-correction PNG per cow to:

```text
qr_codes\<SITE_CODE>\cow_001.png
```

The QR payload is plain text in the form `SITE=<SITE_CODE>|COW=<3-digit cow number>`. This is a standard QR payload and can be decoded directly by OpenCV's built-in QR detector; no custom machine-learning model is used. Existing site codes are rejected rather than overwritten.

On the `Processing` page, select a generated site and start a session. Only then does the PC activate the overhead feed and QR recognition. It requires two consistent detections before registering a cow, ignores repeated frames while the QR remains visible, and completes the visit after the QR has been absent for the exit grace period. A new visit schedules an automatic synchronized thermal capture after 10 seconds in normal mode or 2 seconds in test mode. Stop session disables the feed and QR recognition. A completed visit can be overridden from the UI when the same cow is intentionally brought through again.

The local SQLite database is stored at `data\thermalmaster.sqlite3`. It stores site registrations, generated QR file paths, processing sessions, and visit state. QR images are never shown in the UI.

## Development

Backend dependencies are isolated in `app/backend/.venv`. The React dependencies are local to `app/frontend/node_modules`.

```powershell
& '.\scripts\run_frontend_dev.ps1'
```

The Vite development UI runs at `http://127.0.0.1:5173/` and proxies `/api` to the backend.

## Capture behavior

The Setup page verifies each thermal phone through its explicit wireless ADB endpoint and stores its model, Android version, Thermal Master version, and udder-quarter assignment in `data\thermal_devices.json`. Pairing credentials are used only when a connection cannot be established; the resulting ADB endpoint is what capture uses.

The backend uses `uiautomator2` inside the project virtual environment. It disables UI Automator's idle wait because the Thermal Master preview continuously updates, finds `ivMediaMenu` and `imgMenuTakePhoto` by resource ID, and never relies on screen coordinates. Before a batch, every configured phone must prepare its photo control. A barrier then releases all photo clicks concurrently. Each new JPEG is verified, pulled into `captures`, and reported independently; originals on the phones are never moved or deleted.

Thermal capture is automatic during Processing. After the PC confirms a newly registered QR code, it waits 10 seconds in normal mode or 2 seconds in test mode, then triggers every verified thermal phone in parallel. Repeated frames for the same QR do not create duplicate captures. The synchronized capture endpoint remains available for backend diagnostics, but there is no manual capture button in the normal Processing UI.

## Security boundary

The PC listener is localhost-only. The selected sender connects to `127.0.0.1:8000` through `adb reverse`; it does not open a Wi-Fi camera server. The ADB reverse mapping can be removed with:

```powershell
& '.\scripts\remove_top_camera_tunnel.ps1' -Serial '<PHONE_IP>:<ADB_CONNECTION_PORT>'
```
