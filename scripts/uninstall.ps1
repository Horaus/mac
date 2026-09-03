param([switch]$Yes)
$ErrorActionPreference = "Stop"
if (-not $Yes) { throw "Usage: .\scripts\uninstall.ps1 -Yes" }
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Remove-Item (Join-Path $Root ".venv"), (Join-Path $Root ".agent-control-plane") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $HOME ".mac\bin\mac.cmd"), (Join-Path $HOME ".local\bin\mac.cmd") -Force -ErrorAction SilentlyContinue
Write-Host "MAC runtime, state and launchers removed. Source files were kept."
