param(
    [Parameter(Mandatory = $true)][string]$Source,
    [Parameter(Mandatory = $true)][string]$Target,
    [int]$WaitPid = 0,
    [string]$StagingVersionDir = "",
    [string]$LogFile = ""
)

$ErrorActionPreference = "Stop"

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    if ($LogFile) {
        Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
    }
}

function Wait-ProcessExit {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return
    }
    Write-Log "Waiting for PID $ProcessId"
    while ($true) {
        try {
            Get-Process -Id $ProcessId -ErrorAction Stop | Out-Null
            Start-Sleep -Milliseconds 250
        }
        catch {
            Write-Log "PID $ProcessId exited"
            return
        }
    }
}

function Wait-TargetUnlocked {
    param(
        [string]$ExePath,
        [int]$TimeoutSec = 180
    )
    $resolved = (Resolve-Path -LiteralPath $ExePath).Path
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    Write-Log "Waiting for unlock: $resolved"
    while ((Get-Date) -lt $deadline) {
        $running = @(
            Get-Process -Name "cfsmcp2" -ErrorAction SilentlyContinue | Where-Object {
                try {
                    $_.Path -and ((Resolve-Path -LiteralPath $_.Path).Path -eq $resolved)
                }
                catch {
                    $false
                }
            }
        )
        if ($running.Count -eq 0) {
            Write-Log "Target unlocked"
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "cfsmcp2.exe still running after ${TimeoutSec}s: $resolved"
}

function Invoke-CopyWithRetry {
    param(
        [string]$From,
        [string]$To,
        [switch]$Recurse,
        [int]$MaxAttempts = 30
    )
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            if ($Recurse) {
                Copy-Item -LiteralPath $From -Destination $To -Recurse -Force
            }
            else {
                Copy-Item -LiteralPath $From -Destination $To -Force
            }
            return
        }
        catch {
            if ($attempt -ge $MaxAttempts) {
                throw
            }
            Write-Log "Copy retry ${attempt}/${MaxAttempts}: ${From} -> ${To} ($($_.Exception.Message))"
            Start-Sleep -Milliseconds 500
        }
    }
}

$ApplyLock = Join-Path $Target "_updates\.apply-in-progress"

function Remove-ApplyLock {
    if (Test-Path -LiteralPath $ApplyLock) {
        Remove-Item -LiteralPath $ApplyLock -Force -ErrorAction SilentlyContinue
    }
}

try {
    Write-Log "apply-update start Source=$Source Target=$Target WaitPid=$WaitPid"

    New-Item -ItemType Directory -Force -Path (Join-Path $Target "_updates") | Out-Null
    Set-Content -LiteralPath $ApplyLock -Value $PID -Encoding ASCII -NoNewline

    Wait-ProcessExit -Pid $WaitPid

    $exePath = Join-Path $Target "cfsmcp2.exe"
    if (-not (Test-Path -LiteralPath $exePath)) {
        throw "Target missing cfsmcp2.exe: $Target"
    }
    Wait-TargetUnlocked -ExePath $exePath

    $preserve = @("data", "_updates", "cfsmcp2.ini", "apply-update.ps1", "update-portable.ps1", "update-portable.cmd")
    Get-ChildItem -LiteralPath $Source | ForEach-Object {
        if ($preserve -contains $_.Name) {
            return
        }
        $dest = Join-Path $Target $_.Name
        Write-Log "Updating $($_.Name)"
        if ($_.PSIsContainer) {
            if (Test-Path -LiteralPath $dest) {
                Remove-Item -LiteralPath $dest -Recurse -Force
            }
            Invoke-CopyWithRetry -From $_.FullName -To $dest -Recurse
        }
        else {
            Invoke-CopyWithRetry -From $_.FullName -To $dest
        }
    }

    if ($StagingVersionDir -and (Test-Path -LiteralPath $StagingVersionDir)) {
        Write-Log "Cleanup staging: $StagingVersionDir"
        Remove-Item -LiteralPath $StagingVersionDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    foreach ($legacy in @("apply-update.ps1", "update-portable.ps1", "update-portable.cmd")) {
        $legacyPath = Join-Path $Target $legacy
        if (Test-Path -LiteralPath $legacyPath) {
            Write-Log "Remove legacy helper from install root: $legacy"
            Remove-Item -LiteralPath $legacyPath -Force -ErrorAction SilentlyContinue
        }
    }

    $updatesDir = Join-Path $Target "_updates"
    Get-ChildItem -LiteralPath $updatesDir -Filter "cfsmcp2-win-portable-v*.zip" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Log "Remove downloaded zip: $($_.Name)"
        Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
    }

    Write-Log "Starting $exePath"
    Start-Process -FilePath $exePath -WorkingDirectory $Target
    Write-Log "apply-update done"
}
catch {
    Write-Log "apply-update failed: $($_.Exception.Message)"
    throw
}
finally {
    Remove-ApplyLock
}
