[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectRoot,
    [string]$Capabilities = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDir "..\.."))
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$RemotionDir = if ($env:LECTURECAST_REMOTION_DIR) {
    [System.IO.Path]::GetFullPath($env:LECTURECAST_REMOTION_DIR)
} else {
    Join-Path $ProjectRoot "remotion"
}
if ([string]::IsNullOrWhiteSpace($Capabilities)) {
    $Capabilities = Join-Path $ProjectRoot ".lecturecast\client-capabilities.json"
}
$Manifest = Join-Path $ProjectRoot ".lecturecast\production-manifest.json"
$Overrides = Join-Path $ProjectRoot ".lecturecast\local-overrides.json"
$BuildDir = Join-Path $ProjectRoot ".lecturecast\build"
$Timing = Join-Path $BuildDir "audio-timing.json"
$OutputDir = Join-Path $ProjectRoot "output"
$InstallerPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$InstallerLectureCast = Join-Path $RepoRoot ".venv\Scripts\lecturecast.exe"
$PythonBin = if ($env:PYTHON_BIN) {
    $env:PYTHON_BIN
} elseif (Test-Path -LiteralPath $InstallerPython) {
    $InstallerPython
} else {
    "python"
}
$LectureCastBin = if ($env:LECTURECAST_BIN) {
    $env:LECTURECAST_BIN
} elseif (Test-Path -LiteralPath $InstallerLectureCast) {
    $InstallerLectureCast
} else {
    "lecturecast"
}

if (-not (Test-Path -LiteralPath (Join-Path $RemotionDir "package.json"))) {
    throw "Episode Remotion runtime missing: $RemotionDir. Copy templates/remotion into PROJECT_ROOT/remotion and run npm install first."
}

function Assert-LastExit([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

New-Item -ItemType Directory -Force -Path $BuildDir, $OutputDir, (Join-Path $RemotionDir "public\director") | Out-Null

Write-Host "[1/8] Verify full-script approval receipt"
& $LectureCastBin manifest approval $ProjectRoot --json
Assert-LastExit "manifest script approval"

Write-Host "[2/8] Verify signature, capabilities, narration timing, and component contract"
& $LectureCastBin manifest preflight $Manifest --capabilities $Capabilities --project-root $ProjectRoot --json
Assert-LastExit "manifest preflight"

Write-Host "[3/8] Build section narration and measure the execution timeline"
& $PythonBin (Join-Path $ScriptDir "build_manifest_audio.py") $Manifest (Join-Path $BuildDir "narration.mp3") --timing-out $Timing --reuse
Assert-LastExit "manifest audio"
Copy-Item -LiteralPath (Join-Path $BuildDir "narration.mp3") -Destination (Join-Path $RemotionDir "public\director\narration.mp3") -Force
$AudioSrc = "director/narration.mp3"
& $PythonBin (Join-Path $ScriptDir "build_manifest_subtitles.py") $Manifest $BuildDir --timing $Timing
Assert-LastExit "manifest subtitles"

Write-Host "[4/8] Prepare local execution props from signed plan and measured audio"
$PropsVertical = Join-Path $BuildDir "props-vertical.json"
$PropsLandscape = Join-Path $BuildDir "props-landscape.json"
& $PythonBin (Join-Path $ScriptDir "prepare_manifest_render.py") --manifest $Manifest --overrides $Overrides --variant vertical --audio-src $AudioSrc --timing $Timing --project-root $ProjectRoot --public-root (Join-Path $RemotionDir "public") --output $PropsVertical
Assert-LastExit "vertical render props"
& $PythonBin (Join-Path $ScriptDir "prepare_manifest_render.py") --manifest $Manifest --overrides $Overrides --variant landscape --audio-src $AudioSrc --timing $Timing --project-root $ProjectRoot --public-root (Join-Path $RemotionDir "public") --output $PropsLandscape
Assert-LastExit "landscape render props"

Write-Host "[5/8] Render Director videos"
Push-Location $RemotionDir
try {
    & npx.cmd remotion render DirectorVertical (Join-Path $BuildDir "video-vertical-raw.mp4") "--props=$PropsVertical"
    Assert-LastExit "Director vertical render"
    & npx.cmd remotion render DirectorLandscape (Join-Path $BuildDir "video-landscape-raw.mp4") "--props=$PropsLandscape"
    Assert-LastExit "Director landscape render"
} finally {
    Pop-Location
}

$VideoVertical = (& $PythonBin (Join-Path $ScriptDir "manifest_output_name.py") $Manifest video "9:16").Trim()
Assert-LastExit "vertical output name"
$VideoLandscape = (& $PythonBin (Join-Path $ScriptDir "manifest_output_name.py") $Manifest video "16:9").Trim()
Assert-LastExit "landscape output name"
$CoverVertical = (& $PythonBin (Join-Path $ScriptDir "manifest_output_name.py") $Manifest cover "3:4").Trim()
Assert-LastExit "vertical cover name"
$CoverLandscape = (& $PythonBin (Join-Path $ScriptDir "manifest_output_name.py") $Manifest cover "16:9").Trim()
Assert-LastExit "landscape cover name"

Write-Host "[6/8] Burn subtitles locally"
$ManifestData = Get-Content -LiteralPath $Manifest -Raw | ConvertFrom-Json
if ($ManifestData.subtitles.burn_in) {
    Push-Location $BuildDir
    try {
        & ffmpeg -y -i video-vertical-raw.mp4 -vf "ass=subtitle_vertical.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy (Join-Path $OutputDir $VideoVertical)
        Assert-LastExit "vertical subtitle burn"
        & ffmpeg -y -i video-landscape-raw.mp4 -vf "ass=subtitle_landscape.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy (Join-Path $OutputDir $VideoLandscape)
        Assert-LastExit "landscape subtitle burn"
    } finally {
        Pop-Location
    }
} else {
    Copy-Item -LiteralPath (Join-Path $BuildDir "video-vertical-raw.mp4") -Destination (Join-Path $OutputDir $VideoVertical) -Force
    Copy-Item -LiteralPath (Join-Path $BuildDir "video-landscape-raw.mp4") -Destination (Join-Path $OutputDir $VideoLandscape) -Force
}

Write-Host "[7/8] Render both covers"
Push-Location $RemotionDir
try {
    & npx.cmd remotion still DirectorCoverVertical (Join-Path $OutputDir $CoverVertical) "--props=$PropsVertical"
    Assert-LastExit "Director vertical cover"
    & npx.cmd remotion still DirectorCoverLandscape (Join-Path $OutputDir $CoverLandscape) "--props=$PropsLandscape"
    Assert-LastExit "Director landscape cover"
} finally {
    Pop-Location
}

Write-Host "[8/8] Validate dimensions, measured duration, narration coverage, and files"
& $PythonBin (Join-Path $ScriptDir "validate_manifest_outputs.py") $Manifest $OutputDir --timing $Timing --narration (Join-Path $BuildDir "narration.mp3")
Assert-LastExit "manifest output validation"
Write-Host "Complete: $OutputDir" -ForegroundColor Green
