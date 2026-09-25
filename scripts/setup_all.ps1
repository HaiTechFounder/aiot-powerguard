# Clean checkout -> everything installed, migrated and ready to run.
#
# Offline-safe: no broker, no hardware, no cloud. Creates nothing secret.
#
#   powershell -ExecutionPolicy Bypass -File scripts\setup_all.ps1
#   pwsh -File scripts/setup_all.ps1
#
# Then:  START_POWERGUARD.cmd               (broker if available + backend + dashboard)
#        pwsh -File scripts/verify_all.ps1   (all gates)
#
# Windows PowerShell 5.1 compatible. Every native step is judged on its exit
# code: a failed pip or npm install stops setup here, rather than letting it
# print "Setup complete" over a half-installed tree.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

# Runs a native command and stops setup if it fails. Stderr is left alone:
# pip and npm print warnings there that are not failures.
function Invoke-Checked {
    param([string]$What, [scriptblock]$Command)
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit code $LASTEXITCODE). Fix the error above and run setup again."
    }
}

$startLocation = Get-Location
try {
    # -- backend -----------------------------------------------------------
    Step "backend: virtualenv, dependencies, configuration, migrations"
    Set-Location (Join-Path $root "backend")
    if (-not (Test-Path ".venv")) { Invoke-Checked "creating backend\.venv (py -3.11)" { py -3.11 -m venv .venv } }
    $py = ".\.venv\Scripts\python.exe"
    Invoke-Checked "upgrading pip" { & $py -m pip install --quiet --upgrade pip }
    Invoke-Checked "installing backend dependencies" { & $py -m pip install --quiet -r requirements-dev.lock }
    Invoke-Checked "installing the backend package" { & $py -m pip install --quiet --no-build-isolation --no-deps -e . }

    if (-not (Test-Path ".env")) {
        Copy-Item .env.example .env
        Write-Host "  created backend/.env from the example - edit it before using a real broker." -ForegroundColor Yellow
        Write-Host "  POWERGUARD_MQTT_PASSWORD is a placeholder, not a credential." -ForegroundColor Yellow
    }
    # The anomaly package is an OPTIONAL backend dependency. Installing it here
    # means enabling inference later is a config change, not another terminal.
    # Inference itself stays off (POWERGUARD_INFERENCE_ENABLED=false).
    Invoke-Checked "installing powerguard_ml into the backend" { & $py -m pip install --quiet --no-build-isolation -e ../ml }
    # check-config prints secrets as <set>, never their values.
    Invoke-Checked "checking backend/.env" { & $py -m powerguard check-config | Out-Null }
    Invoke-Checked "database migrations" { & $py -m alembic upgrade head }   # creates data/ itself
    Invoke-Checked "database check" { & $py -m powerguard check-db }

    # -- frontend ----------------------------------------------------------
    Step "frontend: dependencies"
    Set-Location (Join-Path $root "frontend")
    Invoke-Checked "npm ci" { npm ci }
    if (-not (Test-Path ".env")) { Copy-Item .env.example .env }

    # -- ML (optional) -----------------------------------------------------
    Step "ml: virtualenv and dependencies (optional; the backend runs without it)"
    Set-Location (Join-Path $root "ml")
    if (-not (Test-Path ".venv")) { Invoke-Checked "creating ml\.venv" { python -m venv .venv } }
    Invoke-Checked "upgrading pip (ml)" { & ".\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip }
    Invoke-Checked "installing ml dependencies" { & ".\.venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]" }
} catch {
    Write-Host ""
    Write-Host "Setup stopped: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Set-Location $startLocation
}

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "  start:  START_POWERGUARD.cmd"
Write-Host "  gates:  pwsh -File scripts/verify_all.ps1"
