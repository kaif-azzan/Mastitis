$ErrorActionPreference = 'Stop'
$project = 'D:\ThermalMaster-X3-Automation'
$python = Join-Path $project 'app\backend\.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Backend virtual environment is missing. Create it with: python -m venv $($project)\app\backend\.venv"
}

Set-Location (Join-Path $project 'app\backend')
& $python -m uvicorn main:app --host 127.0.0.1 --port 8000
