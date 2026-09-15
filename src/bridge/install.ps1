[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$QueueRepo = 'jdyece25-byte/schedule-requests',
    [ValidatePattern('^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
    [string]$TargetRepo = 'jdyece25-byte/schedule',
    [ValidateSet('codex', 'claude')]
    [string]$Agent = 'codex'
)

$ErrorActionPreference = 'Stop'
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required on Windows.' }
$dataDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ScheduleBridge'))
$runtimeDir = Join-Path $dataDir 'runtime'
$configFile = Join-Path $dataDir 'config.json'
$workerFile = Join-Path $runtimeDir 'worker.py'
$supervisorFile = Join-Path $runtimeDir 'supervisor.py'
$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { throw 'The current user Startup folder could not be resolved.' }
$startupFile = Join-Path $startupDir 'ScheduleBridge.vbs'
$utf8 = [Text.UTF8Encoding]::new($false)
$stageDir = $null
$installLock = $null
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
try {
    $installLock = [IO.File]::Open((Join-Path $dataDir 'install.lock'), [IO.FileMode]::OpenOrCreate,
                                 [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch { throw 'Another ScheduleBridge installation is already running; the worker was left running.' }
try {
$existingConfig = $null
if (Test-Path -LiteralPath $configFile -PathType Leaf) {
    $existingConfig = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json
    if (-not $PSBoundParameters.ContainsKey('QueueRepo')) { $QueueRepo = $existingConfig.queue_repo }
    if (-not $PSBoundParameters.ContainsKey('TargetRepo')) { $TargetRepo = $existingConfig.target_repo }
    if (-not $PSBoundParameters.ContainsKey('Agent')) { $Agent = $existingConfig.agent }
    if (-not [string]::Equals([IO.Path]::GetFullPath($existingConfig.data_dir), $dataDir, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The installed configuration belongs to another data directory; nothing was changed.'
    }
}

function Find-Executable([string]$Name) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($command) { return $command.Source }
    return $null
}

function Find-CodexCommand {
    $shim = Get-Command codex -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($shim -and [IO.Path]::GetExtension($shim.Source) -eq '.exe') { return ,@($shim.Source) }
    $node = Find-Executable 'node.exe'
    if (-not $shim -or -not $node) { return ,@() }
    $entry = Join-Path (Split-Path -Parent $shim.Source) 'node_modules/@openai/codex/bin/codex.js'
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { return ,@() }
    $entry = [IO.Path]::GetFullPath($entry)
    # Match the installed official wrapper's optional-package and legacy-vendor resolution.
    $resolver = @'
const fs = require('fs');
const path = require('path');
const req = require('module').createRequire(process.argv[1]);
const arch = process.arch;
const triple = arch === 'arm64' ? 'aarch64-pc-windows-msvc' : 'x86_64-pc-windows-msvc';
if (!['x64', 'arm64'].includes(arch)) process.exit(1);
let vendor;
try { vendor = path.join(path.dirname(req.resolve('@openai/codex-win32-' + arch + '/package.json')), 'vendor'); }
catch { vendor = path.join(path.dirname(process.argv[1]), '..', 'vendor'); }
const binary = path.join(vendor, triple, 'bin', 'codex.exe');
if (!fs.existsSync(binary)) process.exit(1);
process.stdout.write(Buffer.from(fs.realpathSync(binary), 'utf8').toString('base64'));
'@
    $encodedPath = & $node -e $resolver $entry
    if ($LASTEXITCODE -eq 0 -and $encodedPath) {
        $resolved = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encodedPath))
        if (Test-Path -LiteralPath $resolved -PathType Leaf) { return ,@($resolved) }
    }
    # A direct node invocation still avoids shell interpretation if packaging changes.
    return ,@($node, [IO.Path]::GetFullPath($entry))
}

$sourceWorker = Join-Path $PSScriptRoot 'worker.py'
if (-not (Test-Path -LiteralPath $sourceWorker -PathType Leaf)) { throw 'worker.py is missing beside install.ps1.' }
$python = Find-Executable 'python.exe'
if (-not $python) { throw 'Python 3 is required. Install Python, then run this installer again.' }
$pythonResult = & $python -c 'import sys; print(sys.version_info >= (3, 10))'
if ($LASTEXITCODE -ne 0 -or $pythonResult -ne 'True') {
    throw 'Python 3.10 or later is required.'
}
$pythonw = Find-Executable 'pythonw.exe'
if (-not $pythonw) { $pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe' }
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw 'pythonw.exe is required for the hidden worker.' }
if (-not (Find-Executable 'gh.exe')) { throw 'GitHub CLI (gh) is required. Sign in with gh auth login first.' }
$gh = Find-Executable 'gh.exe'
& $gh auth status --hostname github.com *> $null
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not signed in. Run gh auth login before installing.' }
$codexCommand = Find-CodexCommand
$claudeExecutable = Find-Executable 'claude.exe'
$claudeCommand = @()
if ($claudeExecutable) { $claudeCommand = @($claudeExecutable) }
if ($Agent -eq 'codex' -and -not $codexCommand.Count) { throw 'Codex CLI was not found. Install it and sign in first.' }
if ($Agent -eq 'claude' -and -not $claudeCommand.Count) { throw 'Claude Code was not found. Install it and sign in first.' }
if ($Agent -eq 'codex') {
    $helpArguments = @()
    if ($codexCommand.Count -gt 1) { $helpArguments += $codexCommand[1..($codexCommand.Count - 1)] }
    $helpArguments += @('exec', '--help')
    $helpText = (& $codexCommand[0] @helpArguments | Out-String)
    foreach ($flag in @('--ignore-user-config', '--output-schema', '--output-last-message', '--ephemeral')) {
        if (-not $helpText.Contains($flag)) { throw "The resolved Codex CLI does not support $flag. Update that CLI first." }
    }
} else {
    $helpText = (& $claudeExecutable --help | Out-String)
    foreach ($flag in @('--safe-mode', '--tools', '--json-schema', '--no-session-persistence')) {
        if (-not $helpText.Contains($flag)) { throw "The resolved Claude Code does not support $flag. Update that CLI first." }
    }
}
if (Test-Path -LiteralPath $startupFile -PathType Leaf) {
    $existingLauncher = [IO.File]::ReadAllText($startupFile)
    if (-not $existingLauncher.Contains("' ScheduleBridge managed launcher") -or
        (-not $existingLauncher.Contains($workerFile) -and -not $existingLauncher.Contains($supervisorFile)) -or
        -not $existingLauncher.Contains($configFile)) {
        throw 'An unrelated ScheduleBridge.vbs already exists in Startup; it was not replaced.'
    }
}

$defaults = [ordered]@{
    queue_repo = $QueueRepo
    target_repo = $TargetRepo
    queue_branch = 'main'
    target_branch = 'main'
    agent = $Agent
    poll_seconds = 20
    heartbeat_seconds = 60
    agent_timeout_seconds = 600
    codex_command = @($codexCommand)
    claude_command = @($claudeCommand)
    data_dir = $dataDir
}
$config = [ordered]@{}
if ($existingConfig) {
    foreach ($property in $existingConfig.PSObject.Properties) { $config[$property.Name] = $property.Value }
}
foreach ($key in $defaults.Keys) { if (-not $config.Contains($key)) { $config[$key] = $defaults[$key] } }
foreach ($pair in @(@('QueueRepo', 'queue_repo', $QueueRepo), @('TargetRepo', 'target_repo', $TargetRepo), @('Agent', 'agent', $Agent))) {
    if ($PSBoundParameters.ContainsKey($pair[0])) { $config[$pair[1]] = $pair[2] }
}
$configJson = $config | ConvertTo-Json -Depth 20
$configChanged = -not $existingConfig -or
    (($existingConfig | ConvertTo-Json -Depth 20 -Compress) -cne ($config | ConvertTo-Json -Depth 20 -Compress))

# Prepare and syntax-check every file before asking a running worker to pause.
$installId = [Guid]::NewGuid().ToString('N')
$stageDir = Join-Path $dataDir ('.install-' + $installId)
$staged = Join-Path $stageDir 'staged'
$backup = Join-Path $stageDir 'backup'
New-Item -ItemType Directory -Path $staged, $backup, $runtimeDir, $startupDir -Force | Out-Null
$runtimeNames = @((Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File).Name) + @('install.ps1', 'stop.ps1')
foreach ($name in $runtimeNames) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $staged $name) }
foreach ($name in @('worker.py', 'planner.py', 'github.py', 'supervisor.py', 'school_knowledge.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $staged $name))) { throw "Required runtime file is missing: $name" }
}
[IO.File]::WriteAllText((Join-Path $staged 'config.json'), $configJson, $utf8)
$syntaxCheck = 'import pathlib,sys; files=list(pathlib.Path(sys.argv[2]).glob("*.py")); [compile(p.read_bytes(), str(p), "exec") for p in files]'
$encodedCheck = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($syntaxCheck))
& $python -B -c 'import base64,sys; exec(base64.b64decode(sys.argv[1]))' $encodedCheck $staged
if ($LASTEXITCODE -ne 0) { throw 'Runtime validation failed; the running worker was not paused.' }
foreach ($name in @('install.ps1', 'stop.ps1')) {
    $parseErrors = $null
    [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $staged $name), [ref]$null, [ref]$parseErrors)
    if ($parseErrors.Count) { throw "PowerShell validation failed: $name" }
}

function Different-File([string]$Source, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Destination -PathType Leaf)) { return $true }
    return (Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
}
function Copy-Atomic([string]$Source, [string]$Destination) {
    $temporary = $Destination + '.' + $installId + '.tmp'
    try {
        [IO.File]::Copy($Source, $temporary, $true)
        if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($temporary, $Destination, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Destination) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary } }
}
function Ensure-Guard {
    if (-not (Test-Path -LiteralPath $supervisorFile) -or -not (Test-Path -LiteralPath $configFile)) { return }
    $result = & $python -B $supervisorFile --config $configFile --ensure-running
    if ($LASTEXITCODE -ne 0) { throw ('Cannot start recovery supervisor: ' + ($result -join ' ')) }
    $state = $result | ConvertFrom-Json
    if (-not $state.supervisor_running) { throw 'The recovery supervisor did not start. Inspect supervisor.log.' }
}
$coreChanged = $configChanged
foreach ($name in @('worker.py', 'planner.py', 'github.py', 'school_knowledge.py')) {
    if (Different-File (Join-Path $staged $name) (Join-Path $runtimeDir $name)) { $coreChanged = $true }
}
$targets = @{}
foreach ($name in $runtimeNames) { $targets[$name] = Join-Path $runtimeDir $name }
$targets['config.json'] = $configFile
foreach ($name in $targets.Keys) {
    if (Test-Path -LiteralPath $targets[$name] -PathType Leaf) {
        Copy-Item -LiteralPath $targets[$name] -Destination (Join-Path $backup $name)
    }
}
$modified = [Collections.Generic.List[string]]::new()
$maintenanceClaimed = $false
try {
    # Explicit installation restores the recovery launcher, while preserving a
    # user's service.disabled choice. An existing worker keeps its process/lease.
    $supervisorStop = Join-Path $dataDir 'supervisor.stop'
    if (Test-Path -LiteralPath $supervisorStop) { Remove-Item -LiteralPath $supervisorStop }
    if (Different-File (Join-Path $staged 'supervisor.py') $supervisorFile) {
        # This independent safeguard remains installed even if a core upgrade
        # must abort while an existing request is finishing.
        Copy-Atomic (Join-Path $staged 'supervisor.py') $supervisorFile
    }
    if ($existingConfig) {
        Ensure-Guard
        if ($coreChanged) {
            $maintenanceClaimed = $true
            & (Join-Path $staged 'stop.ps1') -MaintenanceMinutes 2 -WaitSeconds 30 -MaintenanceOwner $installId
            # A still-finishing request aborts before any core/config replacement.
        }
    }
    foreach ($name in $runtimeNames) {
        if ($name -eq 'supervisor.py') { continue }
        if (Different-File (Join-Path $staged $name) $targets[$name]) {
            $modified.Add($name)
            Copy-Atomic (Join-Path $staged $name) $targets[$name]
        }
    }
    if ($configChanged) {
        $modified.Add('config.json')
        Copy-Atomic (Join-Path $staged 'config.json') $configFile
    }
} catch {
    # Restore complete original files if a multi-file installation failed.
    foreach ($name in $modified) {
        $original = Join-Path $backup $name
        if (Test-Path -LiteralPath $original) { Copy-Atomic $original $targets[$name] }
        elseif (Test-Path -LiteralPath $targets[$name]) { Remove-Item -LiteralPath $targets[$name] }
    }
    throw
} finally {
    try {
        if ($maintenanceClaimed) {
            $maintenanceFile = Join-Path $dataDir 'maintenance.json'
            if (Test-Path -LiteralPath $maintenanceFile) {
                $pause = [IO.File]::ReadAllText($maintenanceFile) | ConvertFrom-Json
                if ($pause.owner -eq $installId) {
                    Remove-Item -LiteralPath $maintenanceFile
                    $stopFile = Join-Path $dataDir 'stop.request'
                    if (-not (Test-Path -LiteralPath (Join-Path $dataDir 'service.disabled')) -and
                        -not (Test-Path -LiteralPath (Join-Path $dataDir 'supervisor.stop')) -and
                        (Test-Path -LiteralPath $stopFile)) { Remove-Item -LiteralPath $stopFile }
                }
            }
        }
    } finally { Ensure-Guard }
}

$commandLine = '"' + $pythonw + '" "' + $supervisorFile + '" --config "' + $configFile + '" --ensure-running'
$vbs = "' ScheduleBridge managed launcher`r`n" +
    'Set shell = CreateObject("WScript.Shell")' + "`r`n" +
    'shell.CurrentDirectory = "' + $runtimeDir.Replace('"', '""') + '"' + "`r`n" +
    'shell.Run "' + $commandLine.Replace('"', '""') + '", 0, False' + "`r`n"
[IO.File]::WriteAllText($startupFile, $vbs, [Text.Encoding]::Unicode)
$arguments = '"' + $supervisorFile + '" --config "' + $configFile + '" --ensure-running'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'ScheduleBridge-' + $identity.User.Value + '-Recovery'
try {
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask -and ($existingTask.Actions.Count -ne 1 -or
        -not [string]::Equals($existingTask.Actions[0].Execute, $pythonw, [StringComparison]::OrdinalIgnoreCase) -or
        $existingTask.Actions[0].Arguments -cne $arguments -or
        $existingTask.Description -ne 'ScheduleBridge managed recovery')) {
        throw 'An unrelated recovery task already uses this name and was not replaced.'
    }
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $runtimeDir
    $trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
    $principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'ScheduleBridge managed recovery'
    Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null
} catch {
    Write-Warning ('The running supervisor and Startup launcher were kept; periodic recovery could not be registered: ' + $_.Exception.Message)
}
Write-Output $(if ($coreChanged) { 'ScheduleBridge runtime installed; request processing resumed unless explicitly disabled.' }
              else { 'ScheduleBridge recovery installed without stopping or restarting the existing worker.' })
Write-Output "Configuration: $configFile"
Write-Output "Pause for 2 minutes: powershell -NoProfile -File `"$runtimeDir\stop.ps1`""
Write-Output "Stop until explicitly resumed: powershell -NoProfile -File `"$runtimeDir\stop.ps1`" -Permanent"
Write-Output "Remove automatic startup: powershell -NoProfile -File `"$runtimeDir\stop.ps1`" -Uninstall"
} finally {
    try {
        if ($stageDir -and (Test-Path -LiteralPath $stageDir)) {
            $resolvedStage = [IO.Path]::GetFullPath($stageDir)
            $allowedPrefix = $dataDir.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            if (-not $resolvedStage.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase) -or
                (Split-Path -Leaf $resolvedStage) -notmatch '^\.install-[a-f0-9]{32}$') {
                throw 'The staging directory was outside this installation; it was not removed.'
            }
            Remove-Item -LiteralPath $resolvedStage -Recurse -Force
        }
    } finally { if ($installLock) { $installLock.Dispose() } }
}
