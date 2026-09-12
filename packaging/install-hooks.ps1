# Point this repo at tracked git hooks in .githooks/
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

git config core.hooksPath .githooks
Write-Host "Git hooks enabled: core.hooksPath=.githooks"
Write-Host "Before push: build portable ZIP, then GitHub Release (needs gh auth login)"
Write-Host "Skip build:  `$env:SKIP_PORTABLE_BUILD=1"
Write-Host "Skip release: `$env:SKIP_GITHUB_RELEASE=1"
