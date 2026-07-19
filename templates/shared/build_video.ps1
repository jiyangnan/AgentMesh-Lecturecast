[CmdletBinding()]
param(
    [string]$Slug = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
if ([string]::IsNullOrWhiteSpace($Slug)) {
    $Slug = Split-Path -Leaf $Root
}

function Assert-LastExit([string]$Description) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE"
    }
}

Write-Host "[1/6] Merge narration.mp3"
$AudioFiles = Get-ChildItem -LiteralPath (Join-Path $Root "audio") -Filter "*.mp3" | Sort-Object Name
if (-not $AudioFiles) {
    throw "No audio/*.mp3 files found."
}
$ConcatLines = foreach ($AudioFile in $AudioFiles) {
    $Normalized = $AudioFile.FullName.Replace("\", "/")
    if ($Normalized.Contains("'")) {
        throw "Audio paths containing a single quote are not supported: $Normalized"
    }
    "file '$Normalized'"
}
$ConcatPath = Join-Path $Root "audio\_concat.txt"
[System.IO.File]::WriteAllLines(
    $ConcatPath,
    $ConcatLines,
    (New-Object System.Text.UTF8Encoding($false))
)
& ffmpeg -y -f concat -safe 0 -i $ConcatPath -c copy (Join-Path $Root "remotion\public\narration.mp3")
Assert-LastExit "audio merge"
Copy-Item -LiteralPath (Join-Path $Root "remotion\public\narration.mp3") -Destination (Join-Path $Root "assets\narration.mp3") -Force

Write-Host "[2/6] Update scene timing"
& python (Join-Path $Root "update_theme.py")
Assert-LastExit "theme update"

Write-Host "[3/6] Generate SRT and ASS subtitles"
& python (Join-Path $Root "build_srt.py")
Assert-LastExit "SRT generation"
& python (Join-Path $Root "srt_to_ass_vertical.py")
Assert-LastExit "vertical ASS generation"
& python (Join-Path $Root "srt_to_ass.py")
Assert-LastExit "landscape ASS generation"

Write-Host "[4/6] Render both platform videos"
Push-Location (Join-Path $Root "remotion")
try {
    & npx.cmd remotion render VideoVertical out/video.mp4
    Assert-LastExit "vertical render"
    & npx.cmd remotion render VideoLandscape out/videoH.mp4
    Assert-LastExit "landscape render"
} finally {
    Pop-Location
}

Write-Host "[5/6] Burn subtitles locally"
$OutputDir = Join-Path $Root "output"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
& ffmpeg -y -i (Join-Path $Root "remotion\out\video.mp4") -vf "ass=assets/subtitle_vertical.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy (Join-Path $OutputDir "${Slug}-xiaohongshu.mp4")
Assert-LastExit "vertical subtitle burn"
& ffmpeg -y -i (Join-Path $Root "remotion\out\videoH.mp4") -vf "ass=assets/subtitle.ass" -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a copy (Join-Path $OutputDir "${Slug}-bilibili.mp4")
Assert-LastExit "landscape subtitle burn"

Write-Host "[6/6] Render both covers"
Push-Location (Join-Path $Root "remotion")
try {
    & npx.cmd remotion still CoverVertical (Join-Path $OutputDir "${Slug}-cover-xiaohongshu.png")
    Assert-LastExit "vertical cover"
    & npx.cmd remotion still CoverLandscape (Join-Path $OutputDir "${Slug}-cover-bilibili.png")
    Assert-LastExit "landscape cover"
} finally {
    Pop-Location
}

Write-Host "Complete: $OutputDir" -ForegroundColor Green
Write-Host "Next: run the compliance check and create publish-meta.md."
