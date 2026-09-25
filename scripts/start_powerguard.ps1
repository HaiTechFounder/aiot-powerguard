# One command: broker + backend + dashboard, then the browser.
#
#   START_POWERGUARD.cmd                      (double-click, or from any shell)
#   powershell -File scripts\start_powerguard.ps1 -NoBroker -NoBrowser
#
# Windows PowerShell 5.1 compatible: no `&&`, no ternary, no null-coalescing.
#
# What this script will NOT do:
#   * install anything (setup_all.ps1 owns that, and reinstalling on every
#     start is how a 5-second launch becomes a 5-minute one),
#   * print, log or pass a credential on a command line,
#   * kill a process it did not start - a port held by something else is
#     reported, never seized,
#   * call a service healthy on the strength of having launched it.

[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 5173,
    # Skip Docker entirely. The dashboard still runs; MQTT reads "disconnected",
    # which is a true state and not a failure.
    [switch]$NoBroker,
    # Fail instead of degrading when the broker cannot be started.
    [switch]$RequireBroker,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Run-state helpers shared with stop_powerguard.ps1 (and tested by
# test_launcher.ps1): ownership is a pid AND its start time, never a pid alone.
Import-Module (Join-Path $PSScriptRoot "PowerGuardRunState.psm1") -Force

$root      = Split-Path -Parent $PSScriptRoot
$backend   = Join-Path $root "backend"
$frontend  = Join-Path $root "frontend"
$logDir    = Join-Path $root "logs"
$stateFile = Join-Path $logDir "powerguard-run.json"
$python    = Join-Path $backend ".venv\Scripts\python.exe"

$backendUrl  = "http://127.0.0.1:$BackendPort"
$healthUrl   = "$backendUrl/api/v1/health"
$frontendUrl = "http://127.0.0.1:$FrontendPort"

# Processes this run owns, and therefore this run may stop. Anything already
# listening when we arrived is somebody else's and is left alone.
$owned = New-Object System.Collections.ArrayList
$brokerStartedHere = $false
$launched = $false
# Non-zero when an owned process dies on its own, so START_POWERGUARD.cmd
# pauses and the reason stays on screen instead of the window vanishing.
$exitCode = 0

function Write-Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }
function Write-Ok($message)   { Write-Host "    $message" -ForegroundColor Green }
function Write-Warn($message) { Write-Host "    $message" -ForegroundColor Yellow }
function Write-Fail($message) { Write-Host "    $message" -ForegroundColor Red }

function Fail-With {
    param([string]$Problem, [string[]]$Fix)
    Write-Host ""
    Write-Host "PowerGuard could not start." -ForegroundColor Red
    Write-Host "  $Problem" -ForegroundColor Red
    if ($Fix) {
        Write-Host ""
        Write-Host "  To fix it:" -ForegroundColor Yellow
        foreach ($line in $Fix) { Write-Host "    $line" -ForegroundColor Yellow }
    }
    Write-Host ""
    # Whatever this run had already started must not be left behind.
    Stop-Owned
    exit 1
}

# An HTTP probe that answers "is it up?" without throwing on a 4xx/5xx.
function Test-Http {
    param([string]$Url, [int]$TimeoutSec = 2)
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec $TimeoutSec -UseBasicParsing
        return [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 500
    } catch {
        # A reachable server that answered 4xx/5xx still counts as listening.
        $webResponse = $null
        try { $webResponse = $_.Exception.Response } catch { }
        if ($webResponse) { return $true }
        return $false
    }
}

function Wait-ForHttp {
    param([string]$Url, [int]$TimeoutSec = 60, [string]$What = "service")
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Http -Url $Url) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Get-Health {
    try { return Invoke-RestMethod -Uri $healthUrl -TimeoutSec 3 } catch { return $null }
}

function Start-Owned {
    param(
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$WorkingDirectory,
        [string]$LogName
    )
    # Separate -FilePath/-ArgumentList so a path containing spaces is never
    # handed to a shell that could split it.
    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments `
        -WorkingDirectory $WorkingDirectory -PassThru -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir "$LogName.out.log") `
        -RedirectStandardError  (Join-Path $logDir "$LogName.err.log")
    # The start time is read from the process itself, now, while the handle is
    # fresh: it is what later proves this pid is still the process we started.
    $startTime = $null
    try { $startTime = $process.StartTime } catch { $startTime = $null }
    if ($null -eq $startTime) { $startTime = Get-Date }
    [void]$owned.Add([pscustomobject]@{
        Name = $LogName; Id = $process.Id; Process = $process; StartTime = $startTime
    })
    return $process
}

# A dev server on the dashboard port is only reused if it is this dashboard.
# Anything else there is a conflict to report, not a server to adopt.
function Test-PowerGuardDashboard {
    param([string]$Url)
    try {
        $response = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing
        return [string]$response.Content -match "<title>AIoT PowerGuard</title>"
    } catch {
        return $false
    }
}

# Native tools (alembic, docker) write progress to stderr. Under
# $ErrorActionPreference = "Stop", PowerShell 5.1 promotes any stderr line to a
# terminating error - so an ordinary "INFO alembic..." line would abort the
# launch. Native calls therefore run with the preference relaxed and are judged
# on their exit code, which is the only thing that actually reports success.
function Invoke-Native {
    param([scriptblock]$Command, [switch]$Quiet)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) { & $Command 2>&1 | Out-Null }
        else { & $Command 2>&1 | ForEach-Object { Write-Host "    $_" } }
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Stop-Owned {
    foreach ($entry in $owned) {
        if (-not $entry) { continue }
        $exited = $false
        try { $exited = $entry.Process.HasExited } catch { $exited = $true }
        if ($exited) { continue }
        Write-Host "    stopping $($entry.Name) (pid $($entry.Id))"
        # npm.cmd is a wrapper: killing it alone leaves node/vite on the port.
        try {
            Invoke-Native -Quiet { & taskkill.exe /PID $entry.Id /T /F } | Out-Null
            Stop-Process -Id $entry.Id -Force -ErrorAction SilentlyContinue
        } catch { }
    }
}

# A stack trace is not an error message a non-technical user can act on, so
# an unforeseen failure is reported in one line - and still cleans up.
trap {
    Write-Host ""
    Write-Host "PowerGuard stopped on an unexpected error:" -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "  Logs: $logDir" -ForegroundColor Yellow
    Stop-Owned
    exit 1
}

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

Write-Host "AIoT PowerGuard" -ForegroundColor Cyan
Write-Host "  project: $root"

# ---------------------------------------------------------------------------
# 1. Dependencies. Verified, never installed - installing here would run on
#    every single launch.
# ---------------------------------------------------------------------------
Write-Step "checking the installation"

if (-not (Test-Path $python)) {
    Fail-With "The backend virtualenv is missing (backend\.venv)." @(
        "powershell -ExecutionPolicy Bypass -File `"$root\scripts\setup_all.ps1`""
    )
}
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Fail-With "Frontend dependencies are missing (frontend\node_modules)." @(
        "powershell -ExecutionPolicy Bypass -File `"$root\scripts\setup_all.ps1`"",
        "or: cd `"$frontend`"  then  npm ci"
    )
}

$envFile = Join-Path $backend ".env"
if (-not (Test-Path $envFile)) {
    Fail-With "backend\.env is missing, so there is no configuration to start from." @(
        "powershell -ExecutionPolicy Bypass -File `"$root\scripts\setup_all.ps1`"",
        "or: copy `"$backend\.env.example`" `"$envFile`"  then edit it"
    )
}

# The file stays on disk and out of Git; it is read for a placeholder check
# only, and no value from it is ever printed.
$placeholderPassword = $false
foreach ($line in (Get-Content -LiteralPath $envFile)) {
    if ($line -match '^\s*POWERGUARD_MQTT_PASSWORD\s*=\s*change-me\s*$') { $placeholderPassword = $true }
}

# Inference is opt-in and is read from backend\.env like everything else. The
# launcher only reports what is configured; it never enables anything, and it
# never prints a value from that file.
$inferenceEnabled = $false
foreach ($line in (Get-Content -LiteralPath $envFile)) {
    if ($line -match '^\s*POWERGUARD_INFERENCE_ENABLED\s*=\s*(?i:true|1|yes)\s*$') {
        $inferenceEnabled = $true
    }
}

$npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $npm) {
    Fail-With "npm was not found on PATH." @(
        "Install Node.js 20 or newer, then open a new terminal."
    )
}
Write-Ok "backend venv, frontend packages, backend\.env and npm all present"

if ($inferenceEnabled) {
    # The model package is an optional backend dependency. If it is absent the
    # backend still starts and simply reports the model unavailable - but the
    # operator asked for inference, so say exactly how to get it.
    $mlImportExit = Invoke-Native -Quiet { & $python -c "import powerguard_ml" }
    if ($mlImportExit -ne 0) {
        Write-Warn "POWERGUARD_INFERENCE_ENABLED is true but powerguard_ml is not installed"
        Write-Warn "in the backend virtualenv, so no model can load. Install it with:"
        Write-Warn "  `"$python`" -m pip install -e `"$root\ml`""
    } else {
        Write-Ok "anomaly inference is enabled in backend\.env; the artifact is validated at startup"
    }
}

# ---------------------------------------------------------------------------
# 2. Broker. Optional by design: the REST API, the database and the dashboard
#    all work without it, and /health reports mqtt=disconnected honestly.
# ---------------------------------------------------------------------------
$brokerRunning = $false

if ($NoBroker) {
    Write-Step "broker: skipped (-NoBroker)"
} else {
    Write-Step "broker (Docker mosquitto)"
    $dockerCmd = Get-Command docker -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $engineUp = $false
    if ($dockerCmd) {
        $engineUp = (Invoke-Native -Quiet { & $dockerCmd.Source "info" "--format" "{{.ServerVersion}}" }) -eq 0
    }

    if (-not $dockerCmd) {
        Write-Warn "Docker is not installed or not on PATH."
    } elseif (-not $engineUp) {
        Write-Warn "Docker Engine is not responding - Docker Desktop is probably not running."
        Write-Warn "Start Docker Desktop, wait for the whale icon to settle, then run this again."
    } else {
        $passwd = Join-Path $backend "deploy\mosquitto\passwd"
        if (-not (Test-Path $passwd)) {
            Write-Warn "deploy\mosquitto\passwd is missing; Compose mounts it read-only and would fail."
            Write-Warn "See backend\deploy\mosquitto\README.md to create it."
        } else {
            # Already up? Then it is not ours to start, stop or duplicate.
            $previousPreference = $ErrorActionPreference
            $ErrorActionPreference = "Continue"
            $existing = & $dockerCmd.Source "ps" "--filter" "name=powerguard-mosquitto" `
                "--filter" "status=running" "--format" "{{.Names}}" 2>$null
            $ErrorActionPreference = $previousPreference
            if ($existing -match "powerguard-mosquitto") {
                Write-Ok "powerguard-mosquitto is already running; reusing it"
                $brokerRunning = $true
            } else {
                Push-Location $backend
                try {
                    # Port mapping stays exactly as compose.yaml declares it.
                    $composeExit = Invoke-Native { & $dockerCmd.Source "compose" "up" "-d" "mosquitto" }
                    if ($composeExit -eq 0) {
                        $brokerRunning = $true
                        $brokerStartedHere = $true
                        # compose.yaml binds 127.0.0.1 unless POWERGUARD_MQTT_BIND
                        # names a LAN address; report what was actually asked for.
                        $bind = $env:POWERGUARD_MQTT_BIND
                        if (-not $bind) { $bind = "127.0.0.1" }
                        Write-Ok "mosquitto started ($($bind):1883)"
                    } else {
                        Write-Warn "docker compose could not start mosquitto (see the output above)."
                    }
                } finally {
                    Pop-Location
                }
            }
        }
    }

    if (-not $brokerRunning) {
        if ($RequireBroker) {
            Fail-With "The broker is required (-RequireBroker) but could not be started." @(
                "Start Docker Desktop and try again, or drop -RequireBroker to run without it."
            )
        }
        Write-Warn "Continuing without a broker. The dashboard will show MQTT disconnected,"
        Write-Warn "which is the true state - no device telemetry can arrive."
    } elseif ($placeholderPassword) {
        Write-Warn "backend\.env still holds the example MQTT password, so broker auth will fail."
        Write-Warn "Set a real POWERGUARD_MQTT_PASSWORD in backend\.env (it is Git-ignored)."
    }
}

# ---------------------------------------------------------------------------
# 3. Backend.
# ---------------------------------------------------------------------------
Write-Step "backend ($backendUrl)"

$health = Get-Health
if ($health) {
    Write-Ok "a backend is already answering on port $BackendPort; reusing it"
} else {
    if (Test-Http -Url $backendUrl) {
        Fail-With "Port $BackendPort is held by something that is not the PowerGuard API." @(
            "Close the program using port $BackendPort, then run this again.",
            "To find it:  netstat -ano | findstr :$BackendPort",
            "(-BackendPort starts the API elsewhere, but the dashboard's dev proxy",
            " is wired to 127.0.0.1:8000 in frontend\vite.config.ts, so the UI",
            " would not reach it.)"
        )
    }

    Push-Location $backend
    try {
        $migrationExit = Invoke-Native { & $python -m alembic upgrade head }
        if ($migrationExit -ne 0) {
            Fail-With "Database migrations failed; the backend was not started." @(
                "Read the output above, then see backend\README.md"
            )
        }
    } finally {
        Pop-Location
    }

    # Configuration comes from backend\.env and the environment. The only
    # value set here is the transport switch, and only when we have proved the
    # broker is not there - so the backend does not spend the session retrying
    # a connection that cannot succeed. No credential is set, read out or logged.
    $env:POWERGUARD_HTTP_PORT = "$BackendPort"
    if (-not $brokerRunning) { $env:POWERGUARD_MQTT_ENABLED = "false" }
    else { Remove-Item Env:\POWERGUARD_MQTT_ENABLED -ErrorAction SilentlyContinue }

    Start-Owned -FilePath $python -Arguments @("-m", "powerguard", "serve") `
        -WorkingDirectory $backend -LogName "backend" | Out-Null
    $launched = $true

    if (-not (Wait-ForHttp -Url $healthUrl -TimeoutSec 60)) {
        Fail-With "The backend did not become healthy at $healthUrl within 60s." @(
            "Log: $logDir\backend.err.log"
        )
    }
    $health = Get-Health
    if (-not $health) {
        Fail-With "The backend answered but /api/v1/health could not be read." @(
            "Log: $logDir\backend.err.log"
        )
    }
}

Write-Ok ("health: status={0} database={1} mqtt={2} model={3}" -f `
    $health.status, $health.database, $health.mqtt, $health.model)
if ($health.status -ne "ok" -or $health.database -ne "ready") {
    Fail-With "The backend reports it is not healthy; it will not be presented as ready." @(
        "Log: $logDir\backend.err.log"
    )
}

# ---------------------------------------------------------------------------
# 4. Frontend. Vite is pinned to 5173 with strictPort in vite.config.ts, so a
#    busy port is an error rather than a silent move to 5174/5175.
# ---------------------------------------------------------------------------
Write-Step "dashboard ($frontendUrl)"

if (Test-PowerGuardDashboard -Url $frontendUrl) {
    Write-Ok "the PowerGuard dashboard is already serving port $FrontendPort; reusing it"
} elseif (Test-Http -Url $frontendUrl) {
    Fail-With "Port $FrontendPort is held by something that is not the PowerGuard dashboard." @(
        "Close the program using port $FrontendPort, then run this again.",
        "To find it:  netstat -ano | findstr :$FrontendPort"
    )
} else {
    # npm.cmd, not npm.ps1: Start-Process needs something CreateProcess can run.
    Start-Owned -FilePath $npm.Source -Arguments @("run", "dev") `
        -WorkingDirectory $frontend -LogName "frontend" | Out-Null
    $launched = $true

    if (-not (Wait-ForHttp -Url $frontendUrl -TimeoutSec 90)) {
        Fail-With "The dashboard did not start serving $frontendUrl within 90s." @(
            "Log: $logDir\frontend.err.log",
            "If the port is taken, Vite fails by design rather than moving to another one."
        )
    }
    Write-Ok "dashboard is serving"
}

# ---------------------------------------------------------------------------
# 5. Record what this run owns, so stop_powerguard.ps1 stops that and no more.
# ---------------------------------------------------------------------------
# Only a run that started something owns something, and it adds to the file
# rather than replacing it: an earlier run whose window was closed may still
# own a live backend, and overwriting its entry would leave that backend with
# nothing able to stop it. Entries that no longer name a live process of ours
# (same pid AND same start time) are dropped on the way through.
$ownIds = @($owned | ForEach-Object { [int]$_.Id })
if ($launched) {
    $previous = Read-RunState -Path $stateFile
    if (-not $previous.Valid) {
        Write-Warn "The previous run file was unreadable; it is replaced, and nothing it named is stopped."
    }
    $others = @(Get-OwnedRunEntries -Entries $previous.Entries -Exclude $ownIds)
    $mine = @($owned | ForEach-Object { New-RunEntry -Name $_.Name -Id $_.Id -StartTime $_.StartTime })
    Write-RunState -Path $stateFile -Entries (@($others) + @($mine)) `
        -BrokerStartedHere ($brokerStartedHere -or ($others.Count -gt 0 -and $previous.BrokerStartedHere))
}

# ---------------------------------------------------------------------------
# 6. Summary, then the browser.
# ---------------------------------------------------------------------------
# "disconnected" has two very different causes, and the operator needs to know
# which one they are looking at: no broker at all, or a broker that refused us.
if ($health.mqtt -eq "connected") {
    $mqttNote = "connected"
} elseif ($brokerRunning) {
    $mqttNote = "disconnected (broker is up - check the POWERGUARD_MQTT_* settings in backend\.env)"
} else {
    $mqttNote = "disconnected (no broker - no device telemetry can arrive)"
}

Write-Host ""
Write-Host "  PowerGuard is running" -ForegroundColor Green
Write-Host "  ---------------------------------------------"
Write-Host "  Dashboard   $frontendUrl"
Write-Host "  API         $backendUrl/api/v1/health"
Write-Host "  Database    $($health.database)"
Write-Host "  MQTT        $mqttNote"
# ML Lite is deliberately unpromoted: no model is loaded until real-data
# validation is done, so the dashboard says "detection unavailable" rather
# than passing synthetic-trained inference off as production.
if ($health.model -eq "ready") {
    $modelNote = "ready (an artifact passed validation and is active)"
} elseif ($inferenceEnabled) {
    # Shadow deliberately reports unavailable: its verdicts never leave the log.
    $modelNote = "unavailable (enabled, but shadow or refused - see $logDir\backend.err.log)"
} else {
    $modelNote = "unavailable (expected - inference is off and no model is promoted)"
}
Write-Host "  Model       $modelNote"
Write-Host "  Logs        $logDir"
Write-Host "  ---------------------------------------------"

if (-not $NoBrowser) { Start-Process $frontendUrl | Out-Null }

if (-not $launched) {
    Write-Host ""
    Write-Host "  Everything was already running; nothing new was started." -ForegroundColor Yellow
    Write-Host "  This window can be closed." -ForegroundColor Yellow
    return
}

Write-Host ""
Write-Host "  Keep this window open. Press Ctrl+C to stop PowerGuard." -ForegroundColor Yellow

try {
    :watch while ($true) {
        Start-Sleep -Seconds 2
        foreach ($entry in $owned) {
            $exited = $false
            try { $exited = $entry.Process.HasExited } catch { $exited = $true }
            if ($exited) {
                Write-Fail "$($entry.Name) exited unexpectedly - see $logDir\$($entry.Name).err.log"
                $exitCode = 1
                break watch
            }
        }
    }
} finally {
    # Runs on Ctrl+C too. Stops this run's processes and removes only this
    # run's entries from the run file; another run's live entries stay.
    Write-Step "stopping what this run started"
    Stop-Owned
    $current = Read-RunState -Path $stateFile
    $remaining = @(Get-OwnedRunEntries -Entries $current.Entries -Exclude $ownIds)
    $brokerStillClaimed = $current.BrokerStartedHere -and $remaining.Count -gt 0
    if ($brokerStartedHere -and $remaining.Count -eq 0) {
        Push-Location $backend
        try {
            Invoke-Native -Quiet { & $dockerCmd.Source "compose" "stop" "mosquitto" } | Out-Null
        } finally {
            Pop-Location
        }
        Write-Host "    stopped mosquitto"
    } elseif ($brokerStartedHere) {
        # Another live run is using the broker this run started; stopping it
        # would cut that run's telemetry. The run file keeps the claim, so
        # stop_powerguard.ps1 can still stop it later.
        Write-Host "    left mosquitto running: another PowerGuard run is still using it"
    }
    Write-RunState -Path $stateFile -Entries $remaining -BrokerStartedHere $brokerStillClaimed
    Write-Host "  Stopped." -ForegroundColor Green
}
exit $exitCode
