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
$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { throw 'The current user Startup folder could not be resolved.' }
$startupFile = Join-Path $startupDir 'ScheduleBridge.vbs'
$utf8 = [Text.UTF8Encoding]::new($false)

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
        -not $existingLauncher.Contains($workerFile) -or -not $existingLauncher.Contains($configFile)) {
        throw 'An unrelated ScheduleBridge.vbs already exists in Startup; it was not replaced.'
    }
}

# Stop only the worker with this installation's exact command line before replacing runtime files.
& (Join-Path $PSScriptRoot 'stop.ps1')
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File | ForEach-Object {
    $destination = Join-Path $runtimeDir $_.Name
    if (-not [string]::Equals($_.FullName, $destination, [StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
    }
}
foreach ($name in @('install.ps1', 'stop.ps1')) {
    $source = Join-Path $PSScriptRoot $name
    $destination = Join-Path $runtimeDir $name
    if (-not [string]::Equals($source, $destination, [StringComparison]::OrdinalIgnoreCase)) {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}
$config = [ordered]@{
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
[IO.File]::WriteAllText($configFile, ($config | ConvertTo-Json -Depth 5), $utf8)
$commandLine = '"' + $pythonw + '" "' + $workerFile + '" --config "' + $configFile + '"'
$vbs = "' ScheduleBridge managed launcher`r`n" +
    'Set shell = CreateObject("WScript.Shell")' + "`r`n" +
    'shell.CurrentDirectory = "' + $runtimeDir.Replace('"', '""') + '"' + "`r`n" +
    'shell.Run "' + $commandLine.Replace('"', '""') + '", 0, False' + "`r`n"
[IO.File]::WriteAllText($startupFile, $vbs, [Text.Encoding]::Unicode)
$stopFile = Join-Path $dataDir 'stop.request'
if (Test-Path -LiteralPath $stopFile -PathType Leaf) { Remove-Item -LiteralPath $stopFile }
$arguments = '"' + $workerFile + '" --config "' + $configFile + '"'
$process = Start-Process -FilePath $pythonw -ArgumentList $arguments -WorkingDirectory $runtimeDir -WindowStyle Hidden -PassThru
Start-Sleep -Seconds 2
$process.Refresh()
if ($process.HasExited) {
    throw "The worker exited during startup. Check logs in $dataDir. The Startup launcher is installed."
}
Write-Output "ScheduleBridge installed for the current user and started (PID $($process.Id))."
Write-Output "Configuration: $configFile"
Write-Output "Stop: powershell -NoProfile -File `"$runtimeDir\stop.ps1`""
Write-Output "Remove automatic startup: powershell -NoProfile -File `"$runtimeDir\stop.ps1`" -Uninstall"
