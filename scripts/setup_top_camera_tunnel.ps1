param(
    [Parameter(Mandatory = $true)]
    [string]$Serial
)

$ErrorActionPreference = 'Stop'

$project = 'D:\ThermalMaster-X3-Automation'
$adb = Join-Path $project 'tools\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $adb)) { throw "ADB was not found at $adb" }

& $adb -s $Serial reverse tcp:8000 tcp:8000
if ($LASTEXITCODE -ne 0) {
    throw "Could not create the private ADB reverse tunnel for $Serial. Check that the selected overhead phone is paired and online."
}

Write-Output "Private overhead-camera tunnel ready: phone 127.0.0.1:8000 -> PC 127.0.0.1:8000"
