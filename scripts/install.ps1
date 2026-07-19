[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "LectureCast install.ps1 supports native Windows only. Linux and WSL are not supported."
}

$Repo = if ($env:LECTURECAST_REPO) {
    $env:LECTURECAST_REPO
} else {
    "https://github.com/jiyangnan/AgentMesh-Lecturecast.git"
}
$Branch = if ($env:LECTURECAST_BRANCH) { $env:LECTURECAST_BRANCH } else { "main" }
$InstallDir = if ($env:LECTURECAST_DIR) {
    [System.IO.Path]::GetFullPath($env:LECTURECAST_DIR)
} else {
    Join-Path $HOME ".lecturecast\app"
}

$HomeDir = [System.IO.Path]::GetFullPath($HOME).TrimEnd("\")
$InstallRoot = [System.IO.Path]::GetPathRoot($InstallDir).TrimEnd("\")
if ([string]::IsNullOrWhiteSpace($InstallDir) -or
    $InstallDir.TrimEnd("\") -eq $HomeDir -or
    $InstallDir.TrimEnd("\") -eq $InstallRoot) {
    throw "Unsafe LECTURECAST_DIR: $InstallDir"
}

function Write-Ok([string]$Message) {
    Write-Host "  [ok] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "  [warn] $Message" -ForegroundColor Yellow
}

function Assert-LastExit([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Write-Host "LectureCast installer (Windows)" -ForegroundColor Cyan

$PythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $PythonCommand) {
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $PythonCommand) {
    throw "Python 3.11+ is required and must be on PATH."
}
$GitCommand = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $GitCommand) {
    $GitCommand = Get-Command git -ErrorAction SilentlyContinue
}
if (-not $GitCommand) {
    throw "Git for Windows is required and must be on PATH."
}

$PythonExe = $PythonCommand.Source
$PythonInfoJson = & $PythonExe -c 'import json, platform, sys; print(json.dumps({"major": sys.version_info.major, "minor": sys.version_info.minor, "arch": platform.machine()}))'
Assert-LastExit "Python inspection"
$PythonInfo = $PythonInfoJson | ConvertFrom-Json
if ($PythonInfo.major -lt 3 -or ($PythonInfo.major -eq 3 -and $PythonInfo.minor -lt 11)) {
    throw "Python 3.11+ is required (found $($PythonInfo.major).$($PythonInfo.minor))."
}
$PythonSignature = "$($PythonInfo.major).$($PythonInfo.minor)/$($PythonInfo.arch)"
Write-Ok "python $PythonSignature"

if (Test-Path -LiteralPath (Join-Path $InstallDir ".git")) {
    Write-Ok "updating $InstallDir"
    & $GitCommand.Source -C $InstallDir fetch --quiet origin $Branch
    Assert-LastExit "git fetch"
    & $GitCommand.Source -C $InstallDir reset --hard "origin/$Branch" --quiet
    Assert-LastExit "git reset"
} else {
    if (Test-Path -LiteralPath $InstallDir) {
        throw "$InstallDir exists but is not a LectureCast git checkout; left unchanged."
    }
    Write-Ok "cloning to $InstallDir"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    & $GitCommand.Source clone --quiet --depth 1 --branch $Branch $Repo $InstallDir
    Assert-LastExit "git clone"
}

$Venv = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$VenvPip = Join-Path $Venv "Scripts\pip.exe"
$LectureCastExe = Join-Path $Venv "Scripts\lecturecast.exe"
if (Test-Path -LiteralPath $Venv) {
    $VenvSignature = ""
    if (Test-Path -LiteralPath $VenvPython) {
        $VenvSignature = (& $VenvPython -c 'import platform, sys; print(f"{sys.version_info.major}.{sys.version_info.minor}/{platform.machine()}")' 2>$null)
    }
    if ($VenvSignature -ne $PythonSignature) {
        Write-Warn "recreating incomplete or mismatched installer-owned venv ($VenvSignature -> $PythonSignature)"
        Remove-Item -LiteralPath $Venv -Recurse -Force
    }
}
if (-not (Test-Path -LiteralPath $Venv)) {
    & $PythonExe -m venv $Venv
    Assert-LastExit "venv creation"
    Write-Ok "venv created"
}

if ($env:LECTURECAST_SKIP_PIP_UPGRADE -ne "1") {
    & $VenvPip install --quiet --upgrade pip
    Assert-LastExit "pip upgrade"
}
$InstallSpec = $InstallDir
if ($env:LECTURECAST_INSTALL_DIRECTOR -eq "1") {
    $InstallSpec = "${InstallDir}[director]"
}
& $VenvPip install --quiet -e $InstallSpec
if ($LASTEXITCODE -ne 0) {
    Write-Warn "package installation failed; retrying with full diagnostics"
    & $VenvPip install -e $InstallSpec
    Assert-LastExit "package installation"
}
Write-Ok "lecturecast package installed"

$ShimDir = Join-Path $HOME ".local\bin"
$Shim = Join-Path $ShimDir "lecturecast.cmd"
New-Item -ItemType Directory -Force -Path $ShimDir | Out-Null
$ShimContent = "@`"$LectureCastExe`" %*`r`n"
[System.IO.File]::WriteAllText($Shim, $ShimContent, [System.Text.Encoding]::ASCII)
Write-Ok "shim at $Shim"

$PreviousInstallDir = $env:LECTURECAST_DIR
$env:LECTURECAST_DIR = $InstallDir
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallDir "scripts\manage_adapters.ps1") -Action install
    Assert-LastExit "adapter registration"
} finally {
    $env:LECTURECAST_DIR = $PreviousInstallDir
}

$DoctorJson = & $LectureCastExe doctor --json
Assert-LastExit "lecturecast doctor"
$Doctor = $DoctorJson | ConvertFrom-Json
if ($Doctor.ready) {
    Write-Ok "CLI installed; renderer ready"
} else {
    Write-Warn "CLI installed; renderer not ready"
    & $LectureCastExe doctor
    Assert-LastExit "lecturecast doctor report"
}

$PathEntries = $env:Path -split ";"
if ($PathEntries -contains $ShimDir) {
    Write-Ok "$ShimDir is on PATH"
} else {
    Write-Warn "add $ShimDir to your user PATH, then open a new PowerShell window"
    $PathHint = '[Environment]::SetEnvironmentVariable("Path", [Environment]::GetEnvironmentVariable("Path", "User") + ";{0}", "User")' -f $ShimDir
    Write-Host "    $PathHint"
}

Write-Host ""
Write-Host "CLI installed. Next:" -ForegroundColor Cyan
Write-Host "    lecturecast workflow"
Write-Host "    lecturecast project resume <project-path> --json"
Write-Host ""
Write-Host "Community remains fully local. Director is optional; media and rendering stay local."
Write-Host "Director signature verification is optional; install it only when needed:"
Write-Host "    `"$VenvPip`" install cryptography>=43"
Write-Host "If this agent session started before installation, open a new session and paste:"
Write-Host "    请读取 LectureCast Skill，并从项目路径 <project-path> 继续。"
