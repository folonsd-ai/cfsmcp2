# Point this repo at tracked git hooks in .githooks/
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

git config core.hooksPath .githooks
Write-Host "Git hooks enabled: core.hooksPath=.githooks"
Write-Host "Before push: packaging\package-portable.ps1 (build + zip)"
Write-Host "Skip once: set SKIP_PORTABLE_BUILD=1"
