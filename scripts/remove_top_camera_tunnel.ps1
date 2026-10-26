param(
    [Parameter(Mandatory = $true)]
    [string]$Serial
)

$ErrorActionPreference = 'Stop'

$project = 'D:\ThermalMaster-X3-Automation'
$adb = Join-Path $project 'tools\platform-tools\adb.exe'
if (-not (Test-Path -LiteralPath $adb)) { throw "ADB was not found at $adb" }
& $adb -s $Serial reverse --remove tcp:8000
Write-Output "Private overhead-camera tunnel removed from $Serial"
