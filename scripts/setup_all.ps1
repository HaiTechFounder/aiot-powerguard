# Clean checkout -> everything installed, migrated and ready to run.
#
# Offline-safe: no broker, no hardware, no cloud. Creates nothing secret.
#
#   pwsh -File scripts/setup_all.ps1
#
# Then:  pwsh -File scripts/verify_all.ps1   (all gates)
#        pwsh -File scripts/start_demo.ps1   (backend + dashboard + data)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

function Step($message) { Write-Host "`n==> $message" -ForegroundColor Cyan }

# -- backend ---------------------------------------------------------------
Step "backend: virtualenv, dependencies, configuration, migrations"
Push-Location (Join-Path $root "backend")
if (-not (Test-Path ".venv")) { py -3.11 -m venv .venv }
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -r requirements-dev.lock
& $py -m pip install --quiet --no-build-isolation --no-deps -e .

if (-not (Test-Path ".env")) {
    Copy-Item .env.example .env
    Write-Host "  created backend/.env from the example - edit it before using a real broker." -ForegroundColor Yellow
    Write-Host "  POWERGUARD_MQTT_PASSWORD is a placeholder, not a credential." -ForegroundColor Yellow
}
& $py -m powerguard check-config | Out-Null   # fails loudly on a bad .env
& $py -m alembic upgrade head                 # creates data/ itself
& $py -m powerguard check-db
Pop-Location

# -- frontend --------------------------------------------------------------
Step "frontend: dependencies"
Push-Location (Join-Path $root "frontend")
npm ci
if (-not (Test-Path ".env")) { Copy-Item .env.example .env }
Pop-Location

# -- ML (optional) ---------------------------------------------------------
Step "ml: virtualenv and dependencies (optional; the backend runs without it)"
Push-Location (Join-Path $root "ml")
if (-not (Test-Path ".venv")) { python -m venv .venv }
& ".\.venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install --quiet -e ".[dev]"
Pop-Location

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "  gates:  pwsh -File scripts/verify_all.ps1"
Write-Host "  demo:   pwsh -File scripts/start_demo.ps1"
