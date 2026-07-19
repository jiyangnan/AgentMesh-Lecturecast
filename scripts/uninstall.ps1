[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallDir = if ($env:LECTURECAST_DIR) {
    [System.IO.Path]::GetFullPath($env:LECTURECAST_DIR)
} else {
    Join-Path $HOME ".lecturecast\app"
}
$Shim = Join-Path $HOME ".local\bin\lecturecast.cmd"
$ExpectedExe = Join-Path $InstallDir ".venv\Scripts\lecturecast.exe"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "scripts\manage_adapters.ps1") -Action uninstall
if ($LASTEXITCODE -ne 0) {
    throw "Adapter unregistration failed with exit code $LASTEXITCODE"
}

if (Test-Path -LiteralPath $Shim -PathType Leaf) {
    $Content = Get-Content -LiteralPath $Shim -Raw
    if ($Content.Contains($ExpectedExe)) {
        Remove-Item -LiteralPath $Shim -Force
        Write-Host "  [ok] LectureCast shim removed" -ForegroundColor Green
    } else {
        Write-Host "  [warn] custom lecturecast shim left unchanged" -ForegroundColor Yellow
    }
}

Write-Host "LectureCast adapters are unregistered."
Write-Host "The app checkout and all local projects were preserved: $InstallDir"
Write-Host "Remove that checkout manually only after backing up custom files."
