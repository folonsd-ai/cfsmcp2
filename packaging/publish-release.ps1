# Create GitHub Release with portable ZIP after git push (idempotent).
param(
    [int]$WaitSeconds = 120
)

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

function Invoke-RepoGit {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GitArgs)
    $output = & git -C $Root -c "safe.directory=$Root" @GitArgs 2>$null
    if ($null -eq $output) {
        return ""
    }
    if ($output -is [System.Array]) {
        return ($output -join [Environment]::NewLine).Trim()
    }
    return [string]$output
}

if ($env:SKIP_GITHUB_RELEASE -eq "1") {
    Write-Host "publish-release: SKIP_GITHUB_RELEASE=1, skipped"
    exit 0
}

$Branch = (Invoke-RepoGit rev-parse --abbrev-ref HEAD).Trim()
if ($Branch -notin @("master", "main")) {
    Write-Host "publish-release: skip (branch $Branch)"
    exit 0
}

$Venv = Join-Path $Root ".venv-portable"
$Py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $Py)) {
    $Py = "python"
}
$Version = (& $Py -c "from app.core.version import APP_VERSION; print(APP_VERSION)").Trim()
if (-not $Version) {
    throw "Could not read APP_VERSION"
}

$Tag = "v$Version"
$ZipPath = Join-Path $Root "dist\cfsmcp2-win-portable-v$Version.zip"
if (-not (Test-Path -LiteralPath $ZipPath)) {
    Write-Warning "publish-release: ZIP not found: $ZipPath (run package-portable.ps1 first)"
    exit 1
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    Write-Warning "publish-release: GitHub CLI (gh) not found - install gh and run: gh auth login"
    exit 1
}

Write-Host "publish-release: waiting for origin/$Branch ..."
$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    Invoke-RepoGit fetch origin $Branch | Out-Null
    $local = (Invoke-RepoGit rev-parse HEAD).Trim()
    $remote = (Invoke-RepoGit rev-parse "origin/$Branch").Trim()
    if ($LASTEXITCODE -ne 0) {
        $remote = ""
    }
    if ($remote -and $local -eq $remote) {
        break
    }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)

if (-not $remote -or $local -ne $remote) {
    Write-Warning "publish-release: push to origin/$Branch not confirmed - release skipped"
    exit 1
}

gh release view $Tag 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) {
    Write-Host "publish-release: $Tag already exists"
    exit 0
}

$Notes = @"
Windows portable: cfsmcp2.exe + launcher (tray, auto-update).

Скачайте ``cfsmcp2-win-portable-v$Version.zip``, распакуйте, запустите ``cfsmcp2.exe``.
"@

$NotesPath = Join-Path $Root "dist\.release-notes-$Version.md"
[System.IO.File]::WriteAllText($NotesPath, $Notes, [System.Text.UTF8Encoding]::new($false))

Write-Host "publish-release: creating $Tag ..."
& gh release create $Tag $ZipPath --title $Version --notes-file $NotesPath
if ($LASTEXITCODE -ne 0) {
    throw "gh release create failed with exit code $LASTEXITCODE"
}

Write-Host "publish-release: done - $Tag"
