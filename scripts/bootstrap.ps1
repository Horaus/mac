$ErrorActionPreference = "Stop"

# Windows installer for MAC. Run from PowerShell with:
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$PythonCommand = $null
$PythonArgs = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCommand = "py"
    $PythonArgs = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCommand = "python"
} else {
    throw "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ and run this script again."
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    & $PythonCommand @PythonArgs -m venv (Join-Path $Root ".venv")
}
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $Root
& $VenvPython -m agent_control_plane --project $Root init

if (-not (Test-Path (Join-Path $Root ".agent-control-plane\config.json"))) {
    & $VenvPython -m agent_control_plane --project $Root setup
} else {
    Write-Host "Keeping the existing MAC configuration. Run 'mac' to edit it if needed."
}

$Bin = Join-Path $HOME ".mac\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$Launcher = Join-Path $Bin "mac.cmd"
$LauncherLines = @(
    "@echo off"
    ([char]34 + $VenvPython + [char]34 + " -m agent_control_plane %*")
)
Set-Content -Encoding ASCII -Path $Launcher -Value $LauncherLines

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Entries = @($UserPath -split ";" | Where-Object { $_ })
if ($Entries -notcontains $Bin) {
    [Environment]::SetEnvironmentVariable("Path", (($Entries + $Bin) -join ";"), "User")
}
$env:Path = "$Bin;$env:Path"

Write-Host ""
Write-Host "MAC was installed successfully on Windows."
Write-Host "Close this PowerShell window, open a new one, then run: mac"
