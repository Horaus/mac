$ErrorActionPreference = "Stop"

# Windows installer for MAC. Run from PowerShell with:
#   powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

function Find-Python {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return @("py", "-3") }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return @("python") }
    throw "Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/ and run this script again."
}

$Python = Find-Python
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    if ($Python.Length -gt 1) { & $Python[0] $Python[1] -m venv (Join-Path $Root ".venv") }
    else { & $Python[0] -m venv (Join-Path $Root ".venv") }
}
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $Root
& $VenvPython -m agent_control_plane --project $Root init

if (-not (Test-Path (Join-Path $Root ".agent-control-plane\config.json"))) {
    & $VenvPython -m agent_control_plane --project $Root setup
} else {
    Write-Host "Giữ nguyên cấu hình MAC hiện có. Mở 'mac' để chỉnh nếu cần."
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
Write-Host "MAC đã cài đặt thành công cho Windows."
Write-Host "Mở MAC trong cửa sổ PowerShell hiện tại bằng: mac"
Write-Host "Nếu cửa sổ hiện tại chưa nhận PATH, mở PowerShell mới rồi chạy: mac"
