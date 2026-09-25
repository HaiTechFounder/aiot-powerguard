# Offline checks for the Windows launcher. No broker, no Docker, no ports.
#
#   powershell -ExecutionPolicy Bypass -File scripts\test_launcher.ps1
#   pwsh -File scripts/test_launcher.ps1
#
# Run it under both shells when both exist: each parses the scripts with its
# own parser, so a pass under powershell.exe is the Windows PowerShell 5.1
# syntax check and a pass under pwsh is the PowerShell 7 one.
#
# 1. Every launcher script parses, with no error.
# 2. No script carries a hidden control character. A "\v" or "\b" typed into a
#    path once became a literal vertical tab and backspace inside a message.
# 3. A file with non-ASCII text carries a UTF-8 BOM - without one, Windows
#    PowerShell 5.1 reads it in the ANSI code page and mangles it.
# 4. The run-state helpers only ever claim a process whose pid AND start time
#    match, so nothing the launcher did not start can be stopped by it.
#
# Exits 1 if anything fails, naming each failure.

$ErrorActionPreference = "Stop"
$scripts = $PSScriptRoot
$root = Split-Path -Parent $scripts
$failures = New-Object System.Collections.ArrayList
$passed = 0

function Check {
    param([string]$Name, [bool]$Condition)
    if ($Condition) {
        $script:passed++
    } else {
        [void]$script:failures.Add($Name)
        Write-Host "  FAIL  $Name" -ForegroundColor Red
    }
}

Write-Host "PowerShell $($PSVersionTable.PSVersion) ($($PSVersionTable.PSEdition))"

$files = @(Get-ChildItem -LiteralPath $scripts -File |
    Where-Object { $_.Extension -in @(".ps1", ".psm1") })
$files += Get-Item -LiteralPath (Join-Path $root "START_POWERGUARD.cmd")

# -- 1. parse ----------------------------------------------------------------
foreach ($file in $files) {
    if ($file.Extension -eq ".cmd") { continue }
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $file.FullName, [ref]$tokens, [ref]$errors)
    foreach ($problem in @($errors)) {
        Write-Host "    $($file.Name):$($problem.Extent.StartLineNumber) $($problem.Message)" -ForegroundColor Red
    }
    Check "$($file.Name) parses" (@($errors).Count -eq 0)
}

# -- 2 and 3. bytes ----------------------------------------------------------
foreach ($file in $files) {
    $bytes = [System.IO.File]::ReadAllBytes($file.FullName)
    $bad = @()
    $line = 1
    $nonAscii = $false
    for ($index = 0; $index -lt $bytes.Length; $index++) {
        $byte = $bytes[$index]
        if ($byte -eq 10) { $line++ }
        if (($byte -lt 32 -and $byte -ne 9 -and $byte -ne 10 -and $byte -ne 13) -or $byte -eq 127) {
            $bad += ("line {0}: 0x{1:X2}" -f $line, $byte)
        }
        if ($byte -gt 127) { $nonAscii = $true }
    }
    foreach ($hit in $bad) { Write-Host "    $($file.Name) $hit" -ForegroundColor Red }
    Check "$($file.Name) has no control characters" ($bad.Count -eq 0)

    $hasBom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    Check "$($file.Name) is ASCII or carries a UTF-8 BOM" ((-not $nonAscii) -or $hasBom)
}

# -- 4. run-state helpers ----------------------------------------------------
Import-Module (Join-Path $scripts "PowerGuardRunState.psm1") -Force

$started = Get-Date -Year 2026 -Month 9 -Day 25 -Hour 10 -Minute 0 -Second 0 -Millisecond 0
$entry = New-RunEntry -Name "backend" -Id 4242 -StartTime $started

# A fake process table: pid -> start time. $null means "no such process".
function New-Table([hashtable]$Map) {
    return { param($Id) $Map[[int]$Id] }.GetNewClosure()
}

Check "a timestamp round-trips through its ISO form" `
    ((ConvertTo-RunTimestamp $started.ToString("o")) -eq $started)
Check "a DateTime (as PowerShell 7 JSON yields) is accepted" `
    ((ConvertTo-RunTimestamp $started) -eq $started)
Check "an unparseable timestamp is null, not a guess" `
    ($null -eq (ConvertTo-RunTimestamp "not-a-date"))

Check "the process we started is ours" `
    (Test-RunEntryOwned -Entry $entry -StartTimeOf (New-Table @{ 4242 = $started.AddMilliseconds(400) }))
Check "a recycled pid (other start time) is not ours" `
    (-not (Test-RunEntryOwned -Entry $entry -StartTimeOf (New-Table @{ 4242 = $started.AddMinutes(7) })))
Check "a pid whose process began earlier is not ours" `
    (-not (Test-RunEntryOwned -Entry $entry -StartTimeOf (New-Table @{ 4242 = $started.AddHours(-3) })))
Check "a vanished process is not ours" `
    (-not (Test-RunEntryOwned -Entry $entry -StartTimeOf (New-Table @{})))
$legacy = [pscustomobject]@{ name = "backend"; id = 4242 }
Check "an entry with no recorded start time is never claimed" `
    (-not (Test-RunEntryOwned -Entry $legacy -StartTimeOf (New-Table @{ 4242 = $started })))
$badId = [pscustomobject]@{ name = "backend"; id = "x"; startTime = $started.ToString("o") }
Check "an entry with a malformed pid is never claimed" `
    (-not (Test-RunEntryOwned -Entry $badId -StartTimeOf (New-Table @{ 4242 = $started })))

# The real process table, through this very process.
$self = Get-Process -Id $PID
$selfEntry = New-RunEntry -Name "self" -Id $PID -StartTime $self.StartTime
Check "the live process table confirms a matching pid and start time" `
    (Test-RunEntryOwned -Entry $selfEntry)
$recycled = New-RunEntry -Name "self" -Id $PID -StartTime $self.StartTime.AddHours(-1)
Check "the live process table rejects the same pid with another start time" `
    (-not (Test-RunEntryOwned -Entry $recycled))

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("pg-launcher-test-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $stateFile = Join-Path $temp "powerguard-run.json"

    $missing = Read-RunState -Path $stateFile
    Check "a missing run file is an empty, valid state" `
        ($missing.Valid -and @($missing.Entries).Count -eq 0 -and -not $missing.BrokerStartedHere)

    Set-Content -LiteralPath $stateFile -Value "{ not json" -Encoding UTF8
    $corrupt = Read-RunState -Path $stateFile
    Check "an unreadable run file is reported invalid and claims nothing" `
        ((-not $corrupt.Valid) -and @($corrupt.Entries).Count -eq 0)

    $other = New-RunEntry -Name "frontend" -Id 5151 -StartTime $started.AddSeconds(3)
    Write-RunState -Path $stateFile -Entries @($entry, $other) -BrokerStartedHere $true
    $roundTrip = Read-RunState -Path $stateFile
    Check "a written run file reads back" `
        ($roundTrip.Valid -and @($roundTrip.Entries).Count -eq 2 -and $roundTrip.BrokerStartedHere)

    $table = New-Table @{ 4242 = $started; 5151 = $started.AddSeconds(3) }
    $kept = @(Get-OwnedRunEntries -Entries $roundTrip.Entries -Exclude @(4242) -StartTimeOf $table)
    Check "excluding this run's pids keeps another run's live entry" `
        ($kept.Count -eq 1 -and [int]$kept[0].id -eq 5151)
    $afterRecycle = @(Get-OwnedRunEntries -Entries $roundTrip.Entries `
        -StartTimeOf (New-Table @{ 4242 = $started; 5151 = $started.AddDays(1) }))
    Check "a recycled pid drops out of the run file" `
        ($afterRecycle.Count -eq 1 -and [int]$afterRecycle[0].id -eq 4242)

    Write-RunState -Path $stateFile -Entries @() -BrokerStartedHere $false
    Check "an empty state removes the run file" (-not (Test-Path -LiteralPath $stateFile))

    Write-RunState -Path $stateFile -Entries @() -BrokerStartedHere $true
    Check "a broker claim alone keeps the run file" (Test-Path -LiteralPath $stateFile)
} finally {
    Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
}

# -- launcher invariants that are cheap to hold in place ---------------------
$start = Get-Content -LiteralPath (Join-Path $scripts "start_powerguard.ps1") -Raw
Check "the launcher never passes a password on a command line" `
    (-not ($start -match '(?im)^[^#]*-(P|password)\s+\$'))
Check "the launcher does not kill by port or by process name" `
    (-not ($start -match '(?im)^[^#]*(Stop-Process\s+-Name|taskkill(\.exe)?\s+/IM|Get-NetTCPConnection)'))
Check "the launcher records start times, not bare pids" ($start -match 'New-RunEntry')

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Host "launcher checks: $passed passed, $($failures.Count) FAILED" -ForegroundColor Red
    exit 1
}
Write-Host "launcher checks: $passed passed" -ForegroundColor Green
