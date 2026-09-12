# Build Windows portable onedir bundle. Run from repository root.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Venv = Join-Path $Root ".venv-portable"
if (-not (Test-Path $Venv)) {
    python -m venv $Venv
}

$Py = Join-Path $Venv "Scripts\python.exe"
$Pip = Join-Path $Venv "Scripts\pip.exe"

& $Pip install --upgrade pip
& $Pip install -r requirements.txt
& $Pip install pyinstaller pystray pillow

& $Py packaging\make_icon.py
& $Py -m PyInstaller --noconfirm packaging\cfsmcp2.spec
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE"
}

$Dist = Join-Path $Root "dist\cfsmcp2-win-portable"
if (-not (Test-Path (Join-Path $Dist "cfsmcp2.exe"))) {
    throw "Expected cfsmcp2.exe in $Dist"
}

New-Item -ItemType Directory -Force -Path (Join-Path $Dist "data") | Out-Null
Copy-Item -Force packaging\cfsmcp2.ini.example (Join-Path $Dist "cfsmcp2.ini")
Copy-Item -Force README-portable.txt $Dist

Write-Host ""
Write-Host "Portable build ready:" (Resolve-Path $Dist)
