param(
    [string]$Target = ""
)

$ErrorActionPreference = "Stop"

function Resolve-InstallRoot {
    param([string]$Start)
    $dir = $Start.Trim().TrimEnd('\', '/')
    if (-not $dir) {
        throw "Install root path is empty"
    }
    while ($dir) {
        if (Test-Path -LiteralPath (Join-Path $dir "cfsmcp2.exe")) {
            return (Resolve-Path -LiteralPath $dir).Path
        }
        $parent = Split-Path -Parent $dir
        if (-not $parent -or $parent -eq $dir) {
            break
        }
        $dir = $parent
    }
    throw "Install root not found (no cfsmcp2.exe near $Start)"
}

if (-not $Target) {
    $Target = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$Target = Resolve-InstallRoot -Start $Target

function Find-StagingRoot {
    param([string]$InstallRoot)
    $pending = Join-Path $InstallRoot "_updates\pending"
    if (-not (Test-Path -LiteralPath $pending)) {
        throw "Pending update not found: $pending"
    }
    $found = @()
    Get-ChildItem -LiteralPath $pending -Directory | ForEach-Object {
        $exe = Join-Path $_.FullName "cfsmcp2.exe"
        if (Test-Path -LiteralPath $exe) {
            $found += [PSCustomObject]@{ Path = $_.FullName; Time = $_.LastWriteTimeUtc }
            return
        }
        Get-ChildItem -LiteralPath $_.FullName -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $nestedExe = Join-Path $_.FullName "cfsmcp2.exe"
            if (Test-Path -LiteralPath $nestedExe) {
                $found += [PSCustomObject]@{ Path = $_.FullName; Time = $_.LastWriteTimeUtc }
            }
        }
    }
    if ($found.Count -eq 0) {
        throw "No staged portable build in $pending"
    }
    return ($found | Sort-Object Time -Descending | Select-Object -First 1).Path
}

$source = Find-StagingRoot -InstallRoot $Target
$script = Join-Path $Target "_updates\apply-update.ps1"
if (-not (Test-Path -LiteralPath $script)) {
    $bundled = Join-Path $Target "apply-update.ps1"
    if (Test-Path -LiteralPath $bundled) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Target "_updates") | Out-Null
        Copy-Item -LiteralPath $bundled -Destination $script -Force
    }
    else {
        throw "apply-update.ps1 not found in $Target\_updates or $Target"
    }
}

$versionDir = Split-Path -Parent $source
$log = Join-Path $Target "_updates\apply-update.log"

Write-Host "Applying update"
Write-Host "  from: $source"
Write-Host "  to:   $Target"

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $script `
    -Source $source `
    -Target $Target `
    -WaitPid 0 `
    -StagingVersionDir $versionDir `
    -LogFile $log
