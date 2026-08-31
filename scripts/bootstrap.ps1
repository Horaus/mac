$ErrorActionPreference = "Stop"

# Windows installer for MAC. Run from PowerShell with:
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$UvCommand = Join-Path $HOME ".local\bin\uv.exe"
if (-not (Test-Path $UvCommand)) {
    $uv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($uv) { $UvCommand = $uv.Source }
}
if (-not (Test-Path $VenvPython)) {
    if (Test-Path $UvCommand) {
        Write-Host "No system Python found; using uv-managed Python 3.12."
        & $UvCommand venv --python 3.12 (Join-Path $Root ".venv")
    } else {
        $PythonCommand = $null
        $PythonArgs = @()
        if (Get-Command py.exe -ErrorAction SilentlyContinue) {
            $PythonCommand = "py.exe"
            $PythonArgs = @("-3")
        } elseif (Get-Command python.exe -ErrorAction SilentlyContinue) {
            $PythonCommand = "python.exe"
        }
        if ($PythonCommand) {
            & $PythonCommand @PythonArgs -m venv (Join-Path $Root ".venv")
        } else {
            throw "Python 3.11 or newer, or uv, is required. Install Python or uv and run this script again."
        }
    }
}
if (-not (Test-Path $VenvPython)) {
    throw "The Python virtual environment could not be created. Install Python 3.11+ or uv, then run this script again."
}
if (Test-Path $UvCommand) {
    & $UvCommand pip install --python $VenvPython -e $Root
} else {
    & $VenvPython -m pip install -e $Root
}
if ($LASTEXITCODE -ne 0) {
    throw "MAC package installation failed. Check Python/uv output and run the installer again."
}
& $VenvPython -m agent_control_plane --project $Root init
if ($LASTEXITCODE -ne 0) {
    throw "MAC initialization failed. Run the installer again after fixing the reported error."
}

if (-not (Test-Path (Join-Path $Root ".agent-control-plane\config.json"))) {
    & $VenvPython -m agent_control_plane --project $Root setup
    if ($LASTEXITCODE -ne 0) {
        throw "MAC setup failed. Fix the reported error and run the installer again."
    }
} else {
    Write-Host "Keeping the existing MAC configuration. Run 'mac' to edit it if needed."
}

$Bin = Join-Path $HOME ".mac\bin"
New-Item -ItemType Directory -Force -Path $Bin | Out-Null
$LauncherLines = @(
    "@echo off"
    ("set MAC_PROJECT_ROOT=" + $Root)
    ([char]34 + $VenvPython + [char]34 + " -m agent_control_plane %*")
)
$LauncherPaths = @(
    (Join-Path $Bin "mac.cmd"),
    (Join-Path $HOME ".local\bin\mac.cmd")
)
foreach ($Launcher in $LauncherPaths) {
    $LauncherDirectory = Split-Path $Launcher -Parent
    New-Item -ItemType Directory -Force -Path $LauncherDirectory | Out-Null
    Set-Content -Encoding ASCII -Path $Launcher -Value $LauncherLines
}

$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$Entries = @($UserPath -split ";" | Where-Object { $_ })
if ($Entries -notcontains $Bin) {
    [Environment]::SetEnvironmentVariable("Path", (($Entries + $Bin) -join ";"), "User")
}
$env:Path = "$Bin;$env:Path"

Write-Host ""
Write-Host "MAC was installed successfully on Windows."
Write-Host "Close this PowerShell window, open a new one, then run: mac"
