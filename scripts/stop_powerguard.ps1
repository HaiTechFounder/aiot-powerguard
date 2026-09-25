# Stop what start_powerguard.ps1 started - and only that.
#
#   powershell -ExecutionPolicy Bypass -File scripts\stop_powerguard.ps1
#
# Needed when the launcher window was closed without Ctrl+C, which leaves the
# backend and Vite running. The run file records each pid this project started
# together with that process's own start time. A pid that Windows has since
# recycled onto an unrelated program has a different start time and is left
# alone, as is any entry whose identity cannot be proved.
#
# Windows PowerShell 5.1 compatible: no `&&`, no ternary, no null-coalescing.

[CmdletBinding()]
param([switch]$KeepBroker)

$ErrorActionPreference = "Stop"

Import-Module (Join-Path $PSScriptRoot "PowerGuardRunState.psm1") -Force

$root      = Split-Path -Parent $PSScriptRoot
$backend   = Join-Path $root "backend"
$stateFile = Join-Path $root "logs\powerguard-run.json"

if (-not (Test-Path -LiteralPath $stateFile)) {
    Write-Host "No run file at $stateFile - nothing was started by this project, or it already stopped." -ForegroundColor Yellow
    return
}

$state = Read-RunState -Path $stateFile
if (-not $state.Valid) {
    # Guessing which processes an unreadable file meant is how the wrong one
    # gets killed. Say so, and stop nothing.
    Write-Host "The run file $stateFile could not be read; nothing was stopped." -ForegroundColor Red
    Write-Host "Stop the backend and dashboard windows yourself, then delete that file." -ForegroundColor Yellow
    exit 1
}

$stopped = 0
foreach ($entry in @($state.Entries)) {
    if (-not (Test-RunEntryOwned -Entry $entry)) {
        Write-Host "pid $($entry.id) ($($entry.name)) is gone or is no longer the process this project started; leaving it alone." -ForegroundColor Yellow
        continue
    }
    Write-Host "stopping $($entry.name) (pid $($entry.id))"
    # npm.cmd is a wrapper: /T takes node/vite with it.
    & taskkill.exe /PID $entry.id /T /F *> $null
    Stop-Process -Id $entry.id -Force -ErrorAction SilentlyContinue
    $stopped++
}

if ($state.BrokerStartedHere -and -not $KeepBroker) {
    $docker = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $docker) {
        Write-Host "The broker was started by a run, but docker is not on PATH; leaving it." -ForegroundColor Yellow
    } else {
        Push-Location $backend
        try {
            $previous = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            & $docker.Source compose stop mosquitto *> $null
            $composeExit = $LASTEXITCODE
            $ErrorActionPreference = $previous
            if ($composeExit -eq 0) {
                Write-Host "stopped mosquitto"
            } else {
                Write-Host "docker compose could not stop mosquitto (exit $composeExit)." -ForegroundColor Yellow
            }
        } finally {
            Pop-Location
        }
    }
}

Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
Write-Host "Stopped $stopped process(es)." -ForegroundColor Green
