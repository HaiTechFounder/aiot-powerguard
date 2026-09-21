# The demo: backend + dashboard, with data and without hardware.
#
#   pwsh -File scripts/start_demo.ps1            # no broker; REST/WS only
#   pwsh -File scripts/start_demo.ps1 -WithBroker  # + Docker mosquitto + live publisher
#
# Ctrl+C stops everything this script started.

param(
    [switch]$WithBroker,
    [int]$BackendPort = 8000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$py = Join-Path $backend ".venv\Scripts\python.exe"
$jobs = @()

if (-not (Test-Path $py)) { throw "backend/.venv missing - run scripts/setup_all.ps1 first" }

try {
    if ($WithBroker) {
        Write-Host "==> mosquitto (Docker)" -ForegroundColor Cyan
        if (-not (Test-Path (Join-Path $backend "deploy\mosquitto\passwd"))) {
            throw "deploy/mosquitto/passwd is missing - see backend/deploy/mosquitto/README.md"
        }
        Push-Location $backend
        docker compose up -d mosquitto
        Pop-Location
    } else {
        # No broker: the API, the database and the dashboard all still work.
        # `mqtt` simply reports "disconnected" in /health, which is a state,
        # not a failure.
        $env:POWERGUARD_MQTT_ENABLED = "false"
    }

    Write-Host "==> migrations" -ForegroundColor Cyan
    Push-Location $backend
    & $py -m alembic upgrade head
    Pop-Location

    Write-Host "==> backend on http://127.0.0.1:$BackendPort" -ForegroundColor Cyan
    $env:POWERGUARD_HTTP_PORT = "$BackendPort"
    $jobs += Start-Process -PassThru -NoNewWindow -FilePath $py `
        -ArgumentList "-m", "powerguard", "serve" -WorkingDirectory $backend

    # Wait for /health rather than guessing with a sleep.
    $health = "http://127.0.0.1:$BackendPort/api/v1/health"
    $ready = $false
    foreach ($attempt in 1..40) {
        Start-Sleep -Milliseconds 500
        try {
            $response = Invoke-RestMethod -Uri $health -TimeoutSec 2
            Write-Host ("    health: status={0} database={1} mqtt={2} model={3}" -f `
                $response.status, $response.database, $response.mqtt, $response.model) -ForegroundColor Green
            $ready = $true
            break
        } catch { }
    }
    if (-not $ready) { throw "backend did not become healthy at $health" }

    if ($WithBroker) {
        Write-Host "==> synthetic publisher (live)" -ForegroundColor Cyan
        Write-Host "    set POWERGUARD_DEVICE_PASSWORD first; see backend/README.md" -ForegroundColor Yellow
        $jobs += Start-Process -PassThru -NoNewWindow -FilePath $py `
            -ArgumentList "scripts\publish_synthetic.py", "--host", "127.0.0.1", `
                          "--username", "powerguard-01", "--password-env", "POWERGUARD_DEVICE_PASSWORD", `
                          "--scenario", "all" -WorkingDirectory $backend
    } else {
        Write-Host "==> synthetic publisher (dry run - no broker)" -ForegroundColor Cyan
        Push-Location $backend
        & $py scripts\publish_synthetic.py --dry-run --scenario all --count 3 --seed 7 | Select-Object -First 6
        Pop-Location
        Write-Host "    (dry run only: with no broker the dashboard shows an empty device list,"
        Write-Host "     which is the correct empty state, not a failure)" -ForegroundColor Yellow
    }

    Write-Host "==> dashboard on http://127.0.0.1:5173" -ForegroundColor Cyan
    $frontend = Join-Path $root "frontend"
    if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
        throw "frontend/node_modules missing - run scripts/setup_all.ps1 (or 'npm ci' in frontend/) first"
    }
    # There is no npm.exe on Windows. `Get-Command npm` resolves to npm.ps1, a
    # PowerShell script, and Start-Process needs something CreateProcess can
    # actually launch -- so ask for npm.cmd by name and use its full path.
    # Passing -FilePath/-ArgumentList separately keeps "C:\Program Files\..."
    # out of any shell's hands, so the space in it cannot split the command.
    $npm = Get-Command npm.cmd -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $npm) {
        $npm = Get-Command npm -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if (-not $npm) {
        throw "npm not found on PATH - install Node.js 20+ and reopen the shell"
    }
    $jobs += Start-Process -PassThru -NoNewWindow -FilePath $npm.Source `
        -ArgumentList "run", "dev" -WorkingDirectory $frontend

    Write-Host "`nRunning. Ctrl+C to stop." -ForegroundColor Green
    Wait-Process -Id $jobs[0].Id
} finally {
    Write-Host "`n==> stopping" -ForegroundColor Cyan
    foreach ($job in $jobs) {
        if (-not $job) { continue }
        try { if ($job.HasExited) { continue } } catch { }
        # npm.cmd is a wrapper: killing it alone leaves node/vite holding 5173.
        # taskkill /T takes the whole tree; Stop-Process is the fallback.
        if ($env:OS -eq 'Windows_NT') {
            & taskkill.exe /PID $job.Id /T /F *> $null
        }
        Stop-Process -Id $job.Id -Force -ErrorAction SilentlyContinue
    }
    if ($WithBroker) {
        Push-Location $backend; docker compose down; Pop-Location
    }
}
