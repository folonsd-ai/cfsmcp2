# Build Windows portable onedir bundle and pack dist/*.zip for release.
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Get-Process -Name "cfsmcp2" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Milliseconds 500

& (Join-Path $Root "packaging\build-portable.ps1")

$Venv = Join-Path $Root ".venv-portable"
$Py = Join-Path $Venv "Scripts\python.exe"
$Version = (& $Py -c "from app.core.version import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $Version) {
    throw "Could not read APP_VERSION"
}

$Dist = Join-Path $Root "dist\cfsmcp2-win-portable"
$ZipName = "cfsmcp2-win-portable-v$Version.zip"
$ZipPath = Join-Path $Root "dist\$ZipName"

New-Item -ItemType Directory -Force -Path (Join-Path $Root "dist") | Out-Null
if (Test-Path $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

Compress-Archive -LiteralPath $Dist -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "Portable ZIP ready:" (Resolve-Path $ZipPath)
