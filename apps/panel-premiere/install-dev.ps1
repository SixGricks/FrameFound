# Install the FrameFound panel for developer loading.
#
# The UXP Developer Tool is still the supported route, but it needs the plugin
# on disk somewhere it can be pointed at, and on a first run it needs one thing
# that is easy to miss:
#
#   Premiere > Edit > Preferences > Plugins > "Enable developer mode"
#
# Without that, UDT loads report "No applications are connected to the service"
# and Premiere never appears as a target. It requires a Premiere restart.
#
# Usage:  right-click this file > Run with PowerShell

$ErrorActionPreference = "Stop"

$source = Join-Path $PSScriptRoot "."
$dest = Join-Path $env:APPDATA "Adobe\UXP\Plugins\External\com.sixgricks.framefound"

Write-Host "Source     : $source"
Write-Host "Destination: $dest"
Write-Host ""

if (Test-Path $dest) {
    Write-Host "Replacing the existing copy."
    Remove-Item -Recurse -Force $dest
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null

# Only the files the panel actually needs. Copying this script into the plugin
# folder would make UXP parse it looking for a manifest entry.
foreach ($name in @("manifest.json", "index.html", "main.js")) {
    $from = Join-Path $PSScriptRoot $name
    if (Test-Path $from) {
        Copy-Item $from -Destination $dest
        Write-Host ("  copied {0}" -f $name)
    } else {
        Write-Warning ("  missing {0}" -f $name)
    }
}

# Every asset the manifest names must exist. The first version of this script
# copied icons only "if (Test-Path)" and skipped in silence — which is how a
# manifest declaring icons/icon-24.png, a file that has never existed in this
# repository, got deployed and loaded repeatedly without anyone noticing. A
# manifest that references a missing asset is a structural defect, and UXP
# reports structural defects as unrelated-looking permission errors.
$manifest = Get-Content (Join-Path $PSScriptRoot "manifest.json") -Raw | ConvertFrom-Json
$missing = @()

if ($manifest.PSObject.Properties.Name -contains "icons") {
    foreach ($icon in $manifest.icons) {
        $iconPath = Join-Path $PSScriptRoot $icon.path
        if (Test-Path $iconPath) {
            $target = Join-Path $dest (Split-Path $icon.path -Parent)
            New-Item -ItemType Directory -Force -Path $target | Out-Null
            Copy-Item $iconPath -Destination $target
            Write-Host ("  copied {0}" -f $icon.path)
        } else {
            $missing += $icon.path
        }
    }
}

if ($manifest.main -and -not (Test-Path (Join-Path $PSScriptRoot $manifest.main))) {
    $missing += $manifest.main
}

if ($missing.Count -gt 0) {
    Write-Host ""
    Write-Warning "The manifest names files that do not exist:"
    foreach ($m in $missing) { Write-Warning ("  {0}" -f $m) }
    Write-Warning "Remove them from manifest.json or add the files. UXP can report"
    Write-Warning "a dangling reference as a permission error somewhere unrelated."
}

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1. Premiere: Edit menu, Preferences, Plugins, tick Enable developer mode"
Write-Host "  2. Restart Premiere Pro"
Write-Host "  3. UXP Developer Tool: Add Plugin, choose"
Write-Host "     $dest\manifest.json"
Write-Host "  4. Press Load. The panel appears under Window, Extensions, FrameFound."
Write-Host ""
Read-Host "Press Enter to close"
