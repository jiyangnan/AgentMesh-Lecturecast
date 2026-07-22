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
$PythonInfoText = & $PythonExe -c 'import platform, sys; print(sys.version_info.major, sys.version_info.minor, platform.machine(), sep=chr(124))'
Assert-LastExit "Python inspection"
$PythonInfo = $PythonInfoText.Trim().Split("|")
if ($PythonInfo.Count -ne 3) {
    throw "Python inspection returned an unexpected value: $PythonInfoText"
}
$PythonMajor = [int]$PythonInfo[0]
$PythonMinor = [int]$PythonInfo[1]
$PythonArch = $PythonInfo[2]
if ($PythonMajor -lt 3 -or ($PythonMajor -eq 3 -and $PythonMinor -lt 11)) {
    throw "Python 3.11+ is required (found $PythonMajor.$PythonMinor)."
}
$PythonSignature = "$PythonMajor.$PythonMinor/$PythonArch"
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
        $VenvSignature = (& $VenvPython -c 'import platform, sys; print(sys.version_info.major, sys.version_info.minor, platform.machine(), sep=chr(47))' 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $VenvParts = $VenvSignature.Trim().Split("/")
            if ($VenvParts.Count -eq 3) {
                $VenvSignature = "$($VenvParts[0]).$($VenvParts[1])/$($VenvParts[2])"
            }
        }
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
& $LectureCastExe agent adapters --json
Assert-LastExit "adapter inspection"

$DoctorJson = & $LectureCastExe doctor --json
Assert-LastExit "lecturecast doctor"
$Doctor = $DoctorJson | ConvertFrom-Json
if ($Doctor.ready) {
    Write-Ok "CLI installed; renderer ready"
} else {
    Write-Warn "CLI installed; renderer not ready"
    $Missing = @($Doctor.missing) -join ", "
    Write-Warn "missing local render capabilities: $Missing"
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
Write-Host "Commercial and host-session onboarding gate:" -ForegroundColor Cyan
& $LectureCastExe onboard --json
Assert-LastExit "commercial onboarding"
Write-Host ""
Write-Host "Start a NEW host-agent task and run its exact Skill command:" -ForegroundColor Cyan
Write-Host "    Codex:       lecturecast onboard --adapter codex --host-contract 1.0.0 --json"
Write-Host "    Claude Code: lecturecast onboard --adapter claude-code --host-contract 1.0.0 --json"
Write-Host "    OpenClaw:    lecturecast onboard --adapter openclaw --host-contract 1.0.0 --json"
Write-Host "    lecturecast auth login    # when onboarding asks for an API Key"
Write-Host ""
Write-Host "A paid AgentMesh360 account and at least 10 shared credits are required."
Write-Host "Account center: https://agentmesh360.com/app/"
Write-Host "Original media, voice, rendering and exports remain on this machine."
Write-Host "The installer cannot attest the already-running agent session. Always open a new session."
Write-Host "    Read the current LectureCast Skill and execute only the machine-returned next_action."
