# Every gate this repository can run offline, in one pass.
#
# No broker, no hardware, no network. Anything that needs those is NOT_RUN by
# design and is listed in FINAL_MVP_HANDOFF.md rather than faked here.
#
#   pwsh -File scripts/verify_all.ps1
#
# Exits non-zero if any gate fails, naming each one.

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$results = [System.Collections.ArrayList]::new()

function Invoke-Gate {
    param([string]$Name, [string]$Directory, [scriptblock]$Command)

    Write-Host "`n=== $Name ===" -ForegroundColor Cyan
    Push-Location (Join-Path $root $Directory)
    try {
        & $Command
        $ok = $LASTEXITCODE -eq 0
    } catch {
        Write-Host $_ -ForegroundColor Red
        $ok = $false
    } finally {
        Pop-Location
    }
    [void]$results.Add([pscustomobject]@{ Gate = $Name; Passed = $ok })
    if (-not $ok) { Write-Host "FAILED: $Name" -ForegroundColor Red }
}

$backendPy = Join-Path $root "backend\.venv\Scripts\python.exe"
$mlPy = Join-Path $root "ml\.venv\Scripts\python.exe"

# -- contracts across all four components ---------------------------------
Invoke-Gate "contracts (firmware/MQTT/backend/REST/WS/frontend/ML)" "." {
    python scripts/check_contracts.py
}

# -- backend ---------------------------------------------------------------
if (Test-Path $backendPy) {
    Invoke-Gate "backend pytest" "backend" { & $backendPy -m pytest -q -p no:warnings }
    Invoke-Gate "backend ruff"   "backend" { & $backendPy -m ruff check . }
    Invoke-Gate "backend mypy"   "backend" { & $backendPy -m mypy --strict src }
} else {
    Write-Host "backend/.venv missing - see FINAL_MVP_HANDOFF.md" -ForegroundColor Yellow
}

# -- frontend --------------------------------------------------------------
if (Test-Path (Join-Path $root "frontend\node_modules")) {
    Invoke-Gate "frontend typecheck" "frontend" { npm run typecheck }
    Invoke-Gate "frontend lint"      "frontend" { npm run lint }
    Invoke-Gate "frontend test"      "frontend" { npm run test }
    Invoke-Gate "frontend coverage"  "frontend" { npm run test:coverage }
    Invoke-Gate "frontend build"     "frontend" { npm run build }
} else {
    Write-Host "frontend/node_modules missing - run 'npm ci' in frontend/" -ForegroundColor Yellow
}

# -- ML --------------------------------------------------------------------
if (Test-Path $mlPy) {
    Invoke-Gate "ml pytest" "ml" { & $mlPy -m pytest -q }
    Invoke-Gate "ml ruff"   "ml" { & $mlPy -m ruff check . }
    Invoke-Gate "ml mypy"   "ml" { & $mlPy -m mypy }
    Invoke-Gate "ml train/evaluate smoke" "ml" {
        $env:PYTHONPATH = "src"
        & $mlPy -m powerguard_ml.train --synthetic 600 --artifacts artifacts
        if ($LASTEXITCODE -ne 0) { return }
        & $mlPy -m powerguard_ml.evaluate --artifact artifacts/pg-synthetic-01/v1 --synthetic 400
    }
} else {
    Write-Host "ml/.venv missing - see FINAL_MVP_HANDOFF.md" -ForegroundColor Yellow
}

# -- firmware --------------------------------------------------------------
# The ESP8266 build is the real gate. `pio test -e native` additionally needs a
# host g++ on a path WITHOUT spaces; where that is not the case it is reported
# NOT_RUN rather than skipped silently.
$hasPio = [bool](Get-Command pio -ErrorAction SilentlyContinue)
if (-not $hasPio) {
    python -c "import platformio" 2>$null
    $hasPioModule = $LASTEXITCODE -eq 0
} else {
    $hasPioModule = $false
}

if ($hasPio) {
    Invoke-Gate "firmware build (nodemcuv2)" "firmware" { pio run -e nodemcuv2 }
} elseif ($hasPioModule) {
    Invoke-Gate "firmware build (nodemcuv2)" "firmware" { python -m platformio run -e nodemcuv2 }
} else {
    Write-Host "PlatformIO not installed - firmware build NOT_RUN" -ForegroundColor Yellow
}

# -- summary ---------------------------------------------------------------
Write-Host "`n=== SUMMARY ===" -ForegroundColor Cyan
$results | Format-Table -AutoSize
$failed = @($results | Where-Object { -not $_.Passed })
if ($failed.Count -gt 0) {
    Write-Host "$($failed.Count) gate(s) failed." -ForegroundColor Red
    exit 1
}
Write-Host "All $($results.Count) gate(s) passed. Broker/hardware gates remain NOT_RUN." -ForegroundColor Green
exit 0
