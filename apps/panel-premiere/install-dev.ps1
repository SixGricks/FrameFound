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

$icons = Join-Path $PSScriptRoot "icons"
if (Test-Path $icons) {
    Copy-Item $icons -Destination $dest -Recurse
    Write-Host "  copied icons"
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
