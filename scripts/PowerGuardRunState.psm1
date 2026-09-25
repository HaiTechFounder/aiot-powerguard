# What a launcher run owns, recorded so that it - and only it - can be stopped.
#
# Shared by start_powerguard.ps1 and stop_powerguard.ps1, and exercised by
# test_launcher.ps1. Windows PowerShell 5.1 compatible: no `&&`, no ternary,
# no null-coalescing.
#
# Identity is (pid, process start time). A pid alone proves nothing: Windows
# recycles them, and killing a recycled pid would kill somebody else's
# program. An entry whose start time cannot be read or does not match is
# never treated as ours.

# Two seconds absorbs the rounding of a start time through JSON; a recycled
# pid lands on a process that started seconds or hours later.
$script:StartTimeToleranceSeconds = 2

function ConvertTo-RunTimestamp {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [datetime]) {
        # PowerShell 7's ConvertFrom-Json turns ISO strings into DateTime on
        # its own; 5.1 leaves them as strings. Both end up local time here.
        if ($Value.Kind -eq [System.DateTimeKind]::Utc) { return $Value.ToLocalTime() }
        return $Value
    }
    $parsed = [datetime]::MinValue
    $ok = [datetime]::TryParse(
        [string]$Value,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [System.Globalization.DateTimeStyles]::RoundtripKind,
        [ref]$parsed
    )
    if (-not $ok) { return $null }
    if ($parsed.Kind -eq [System.DateTimeKind]::Utc) { return $parsed.ToLocalTime() }
    return $parsed
}

function New-RunEntry {
    param([string]$Name, [int]$Id, [datetime]$StartTime)
    return [pscustomobject]@{
        name      = $Name
        id        = $Id
        startTime = $StartTime.ToString("o")
    }
}

function Get-ProcessStartTime {
    # $null when the process is gone or its start time cannot be read (a
    # process owned by another user, for example). Neither is ours to stop.
    param([int]$Id)
    $process = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $process) { return $null }
    try { return $process.StartTime } catch { return $null }
}

function Test-RunEntryOwned {
    # True only when the pid is alive AND started when the entry says it did.
    param(
        $Entry,
        # Injected by the tests; production reads the live process table.
        [scriptblock]$StartTimeOf = { param($Id) Get-ProcessStartTime -Id $Id }
    )
    if (-not $Entry) { return $false }
    $recorded = ConvertTo-RunTimestamp $Entry.startTime
    if ($null -eq $recorded) { return $false }
    $id = 0
    if (-not [int]::TryParse([string]$Entry.id, [ref]$id) -or $id -le 0) { return $false }
    $actual = & $StartTimeOf $id
    if ($null -eq $actual) { return $false }
    $delta = [math]::Abs(($actual - $recorded).TotalSeconds)
    return $delta -le $script:StartTimeToleranceSeconds
}

function Read-RunState {
    # Never throws. A missing file is an empty state; an unreadable one is an
    # empty state with Valid = $false, so the caller can say so.
    param([string]$Path)
    $result = [pscustomobject]@{ Entries = @(); BrokerStartedHere = $false; Valid = $true }
    if (-not (Test-Path -LiteralPath $Path)) { return $result }
    try {
        $state = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
    } catch {
        $result.Valid = $false
        return $result
    }
    if (-not $state) {
        $result.Valid = $false
        return $result
    }
    $entries = @()
    foreach ($entry in @($state.pids)) {
        if ($entry) { $entries += $entry }
    }
    $result.Entries = $entries
    $result.BrokerStartedHere = [bool]$state.brokerStartedHere
    return $result
}

function Write-RunState {
    # An empty state is no state: the file is removed rather than left behind
    # claiming ownership of nothing.
    param(
        [string]$Path,
        [object[]]$Entries,
        [bool]$BrokerStartedHere
    )
    $kept = @($Entries | Where-Object { $_ })
    if ($kept.Count -eq 0 -and -not $BrokerStartedHere) {
        Remove-Item -LiteralPath $Path -ErrorAction SilentlyContinue
        return
    }
    $state = [pscustomobject]@{
        updatedAt         = (Get-Date).ToString("o")
        brokerStartedHere = $BrokerStartedHere
        pids              = $kept
    }
    $state | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Get-OwnedRunEntries {
    # The entries of a state that still name a live process this project
    # started, minus any pid in -Exclude.
    param(
        [object[]]$Entries,
        [int[]]$Exclude = @(),
        [scriptblock]$StartTimeOf = { param($Id) Get-ProcessStartTime -Id $Id }
    )
    $owned = @()
    foreach ($entry in @($Entries)) {
        if (-not $entry) { continue }
        if ($Exclude -contains [int]$entry.id) { continue }
        if (Test-RunEntryOwned -Entry $entry -StartTimeOf $StartTimeOf) { $owned += $entry }
    }
    return ,$owned
}

Export-ModuleMember -Function ConvertTo-RunTimestamp, New-RunEntry, Get-ProcessStartTime,
    Test-RunEntryOwned, Read-RunState, Write-RunState, Get-OwnedRunEntries
