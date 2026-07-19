[CmdletBinding()]
param(
    [ValidateSet("install", "uninstall")]
    [string]$Action = "install"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallDir = if ($env:LECTURECAST_DIR) {
    [System.IO.Path]::GetFullPath($env:LECTURECAST_DIR)
} else {
    Join-Path $HOME ".lecturecast\app"
}

function Write-Ok([string]$Message) {
    Write-Host "  [ok] $Message" -ForegroundColor Green
}

function Write-Warn([string]$Message) {
    Write-Host "  [warn] $Message" -ForegroundColor Yellow
}

function Get-OwnedTarget([string]$Path) {
    $Item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if (-not $Item) {
        return $null
    }
    $LinkTypeProperty = $Item.PSObject.Properties["LinkType"]
    $TargetProperty = $Item.PSObject.Properties["Target"]
    if (-not $LinkTypeProperty -or
        $LinkTypeProperty.Value -notin @("Junction", "SymbolicLink") -or
        -not $TargetProperty) {
        return $null
    }
    $Target = [string]$TargetProperty.Value
    if ([string]::IsNullOrWhiteSpace($Target)) {
        return $null
    }
    return [System.IO.Path]::GetFullPath($Target).TrimEnd("\")
}

function Manage-One(
    [string]$Agent,
    [string]$Base,
    [string]$Source
) {
    $Target = Join-Path $Base "lecturecast"
    if (-not (Test-Path -LiteralPath $Base -PathType Container)) {
        $HostRoot = Split-Path -Parent $Base
        if (Test-Path -LiteralPath $HostRoot -PathType Container) {
            Write-Warn "$Agent adapter skipped: $Base is missing; create it and rerun this installer"
        } else {
            Write-Warn "$Agent adapter skipped: host not detected"
        }
        return
    }

    $Expected = [System.IO.Path]::GetFullPath($Source).TrimEnd("\")
    $OwnedTarget = Get-OwnedTarget $Target
    if ($Action -eq "install") {
        if ($OwnedTarget) {
            if ($OwnedTarget -ieq $Expected) {
                Write-Ok "$Agent adapter already registered"
            } else {
                Write-Warn "$Agent already has a custom lecturecast link; left unchanged"
            }
            return
        }
        if (Test-Path -LiteralPath $Target) {
            Write-Warn "$Agent already has a custom lecturecast skill; left unchanged"
            return
        }
        New-Item -ItemType Junction -Path $Target -Target $Expected | Out-Null
        Write-Ok "$Agent adapter registered"
        return
    }

    if ($OwnedTarget -and $OwnedTarget -ieq $Expected) {
        Remove-Item -LiteralPath $Target -Force
        Write-Ok "$Agent adapter unregistered"
    } elseif ($OwnedTarget -or (Test-Path -LiteralPath $Target)) {
        Write-Warn "$Agent lecturecast skill is not installer-owned; left unchanged"
    }
}

Manage-One "Codex" (Join-Path $HOME ".codex\skills") (Join-Path $InstallDir "skills\codex")
Manage-One "Claude Code" (Join-Path $HOME ".claude\skills") (Join-Path $InstallDir "skills\claude-code")

$OpenClawGlobal = Join-Path $HOME ".openclaw\skills"
$OpenClawWorkspace = Join-Path $HOME ".openclaw\workspace\skills"
$OpenClawRoot = Join-Path $HOME ".openclaw"
if (Test-Path -LiteralPath $OpenClawGlobal -PathType Container) {
    Manage-One "OpenClaw" $OpenClawGlobal (Join-Path $InstallDir "skills\openclaw")
} elseif (Test-Path -LiteralPath $OpenClawWorkspace -PathType Container) {
    Manage-One "OpenClaw" $OpenClawWorkspace (Join-Path $InstallDir "skills\openclaw")
} elseif (Test-Path -LiteralPath $OpenClawRoot -PathType Container) {
    Write-Warn "OpenClaw adapter skipped: no skills directory detected; create one and rerun this installer"
} else {
    Write-Warn "OpenClaw adapter skipped: host not detected"
}
