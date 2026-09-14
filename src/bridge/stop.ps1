[CmdletBinding()]
param(
    [switch]$Uninstall,
    [switch]$Permanent,
    [ValidateRange(1,60)][int]$MaintenanceMinutes = 2,
    [ValidateRange(1,120)][int]$WaitSeconds = 30,
    [ValidatePattern('^[A-Za-z0-9-]{1,100}$')][string]$MaintenanceOwner = ([Guid]::NewGuid().ToString('N'))
)
$ErrorActionPreference = 'Stop'
$permanentStop = $Permanent -or $Uninstall
if ($permanentStop -and $PSBoundParameters.ContainsKey('MaintenanceMinutes')) { throw 'Permanent stop and temporary maintenance cannot be combined.' }
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required on Windows.' }
$dataDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ScheduleBridge'))
$runtimeDir = Join-Path $dataDir 'runtime'
$configFile = Join-Path $dataDir 'config.json'
if (-not (Test-Path -LiteralPath $configFile)) { Write-Output 'ScheduleBridge is not installed.'; return }
$python = (Get-Command python.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
$supervisorSource = Join-Path $PSScriptRoot 'supervisor.py'
$startupFile = Join-Path ([Environment]::GetFolderPath('Startup')) 'ScheduleBridge.vbs'
$taskName = 'ScheduleBridge-' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value + '-Recovery'
$utf8 = [Text.UTF8Encoding]::new($false)
function Write-Control([string]$Name, [string]$Content) {
    $target = Join-Path $dataDir $Name
    $temporary = $target + '.' + $PID + '.tmp'
    [IO.File]::WriteAllText($temporary, $Content, $utf8)
    if (Test-Path -LiteralPath $target) { [IO.File]::Replace($temporary, $target, [NullString]::Value) }
    else { [IO.File]::Move($temporary, $target) }
}
if ($Uninstall -and (Test-Path -LiteralPath $startupFile)) {
    $launcher = [IO.File]::ReadAllText($startupFile)
    if (-not $launcher.Contains("' ScheduleBridge managed launcher") -or -not $launcher.Contains($runtimeDir) -or -not $launcher.Contains($configFile)) {
        throw 'The Startup launcher is not owned by this installation; nothing was removed.'
    }
}
if ($Uninstall) {
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task -and ($task.Description -ne 'ScheduleBridge managed recovery' -or $task.Actions.Count -ne 1 -or -not $task.Actions[0].Arguments.Contains((Join-Path $runtimeDir 'supervisor.py')) -or -not $task.Actions[0].Arguments.Contains($configFile))) {
        throw 'An unrelated recovery task was not removed; service state was not changed.'
    }
}
$controlWrittenAt = [DateTime]::UtcNow
if (-not $permanentStop) {
    Write-Control 'maintenance.json' (@{owner=$MaintenanceOwner; resume_at=[DateTime]::UtcNow.AddMinutes($MaintenanceMinutes).ToString('o')} | ConvertTo-Json -Compress)
} else {
    # Explicit user stop persists; normal DB edits never call this.
    Write-Control 'service.disabled' 'Explicit user stop'
}
Write-Control 'stop.request' 'Finish current request, then stop'
if ($Uninstall) { Write-Control 'supervisor.stop' 'Explicit uninstall' }
$deadline = [DateTime]::UtcNow.AddSeconds($WaitSeconds)
$pauseAcknowledged = $false
do {
    $rawStatus = & $python -B $supervisorSource --config $configFile --status
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect service locks; no process was killed.' }
    $status = $rawStatus | ConvertFrom-Json
    if ($status.errors.Count) { throw ('Cannot inspect service: ' + ($status.errors -join '; ')) }
    if ($Uninstall) {
        $pauseAcknowledged = -not $status.worker_running -and -not $status.supervisor_running
    } else {
        # A child can exist before worker.py acquires worker.lock. Require the
        # guard to acknowledge this pause and report that no owned child remains.
        # An old maintenance snapshot must not count as a new acknowledgement.
        $snapshot = $status.snapshot
        $acknowledgedAt = [DateTimeOffset]::MinValue
        $fresh = $snapshot -and
            [DateTimeOffset]::TryParse([string]$snapshot.updated_at, [ref]$acknowledgedAt) -and
            $acknowledgedAt.UtcDateTime -ge $controlWrittenAt
        $pauseAcknowledged = -not $status.worker_running -and $status.supervisor_running -and
            $fresh -and $snapshot.state -in @('maintenance', 'stopped') -and
            $null -eq $snapshot.worker_pid
    }
    if ($pauseAcknowledged) { break }
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $deadline)
if (-not $pauseAcknowledged) {
    throw 'The worker is still finishing or the supervisor has not acknowledged this pause. No process was killed. Retry after checking service health.'
}
if ($Uninstall) {
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    if (Test-Path -LiteralPath $startupFile) { Remove-Item -LiteralPath $startupFile }
}
Write-Output $(if ($Uninstall) { 'ScheduleBridge stopped; automatic startup/recovery removed. Data and history kept.' }
              elseif (-not $permanentStop) { "Worker paused temporarily; automatic resume within $MaintenanceMinutes minute(s)." }
              else { 'Request processing stopped by explicit user choice. Use supervisor.py --ensure-running --resume to resume.' })
