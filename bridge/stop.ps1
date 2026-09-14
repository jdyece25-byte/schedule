[CmdletBinding()]
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
if (-not $env:LOCALAPPDATA) { throw 'LOCALAPPDATA is required on Windows.' }
$dataDir = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'ScheduleBridge'))
$runtimeDir = Join-Path $dataDir 'runtime'
$workerFile = Join-Path $runtimeDir 'worker.py'
$configFile = Join-Path $dataDir 'config.json'
$pidFile = Join-Path $dataDir 'worker.pid'
$stopFile = Join-Path $dataDir 'stop.request'
$startupDir = [Environment]::GetFolderPath('Startup')
if (-not $startupDir) { throw 'The current user Startup folder could not be resolved.' }
$startupFile = [IO.Path]::GetFullPath((Join-Path $startupDir 'ScheduleBridge.vbs'))

if (-not ('ScheduleBridge.CommandLine' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace ScheduleBridge {
    public static class CommandLine {
        [DllImport("shell32.dll", SetLastError = true)]
        static extern IntPtr CommandLineToArgvW([MarshalAs(UnmanagedType.LPWStr)] string line, out int count);
        [DllImport("kernel32.dll")]
        static extern IntPtr LocalFree(IntPtr pointer);
        public static string[] Split(string line) {
            int count;
            IntPtr pointer = CommandLineToArgvW(line, out count);
            if (pointer == IntPtr.Zero) throw new InvalidOperationException("Cannot inspect process arguments.");
            try {
                string[] result = new string[count];
                for (int index = 0; index < count; index++)
                    result[index] = Marshal.PtrToStringUni(Marshal.ReadIntPtr(pointer, index * IntPtr.Size));
                return result;
            } finally { LocalFree(pointer); }
        }
    }
}
'@
}

function Get-VerifiedWorker([int]$WorkerId) {
    $record = Get-CimInstance Win32_Process -Filter "ProcessId = $WorkerId" -ErrorAction Stop
    if (-not $record) { return $null }
    if (-not $record.CommandLine) { throw "Cannot inspect process $WorkerId; it was not stopped." }
    $arguments = [ScheduleBridge.CommandLine]::Split($record.CommandLine)
    $valid = $arguments.Count -eq 4 -and
        $record.Name -match '^pythonw?\.exe$' -and
        [string]::Equals($arguments[1], $workerFile, [StringComparison]::OrdinalIgnoreCase) -and
        $arguments[2] -eq '--config' -and
        [string]::Equals($arguments[3], $configFile, [StringComparison]::OrdinalIgnoreCase)
    if (-not $valid) { throw "Process $WorkerId does not match this ScheduleBridge installation; it was not stopped." }
    return $record
}

if (Test-Path -LiteralPath $pidFile -PathType Leaf) {
    $workerId = 0
    $pidText = [IO.File]::ReadAllText($pidFile).Trim()
    if (-not [int]::TryParse($pidText, [ref]$workerId) -or $workerId -le 0) {
        throw 'The ScheduleBridge PID file is invalid; no process was stopped.'
    }
    $original = Get-VerifiedWorker $workerId
    if ($original) {
        [IO.File]::WriteAllText($stopFile, 'stop', [Text.UTF8Encoding]::new($false))
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        do {
            Start-Sleep -Milliseconds 250
            $current = Get-VerifiedWorker $workerId
        } while ($current -and [DateTime]::UtcNow -lt $deadline)
        if ($current) {
            if ($current.CreationDate -ne $original.CreationDate) {
                throw 'The worker PID was reused; the replacement process was not stopped.'
            }
            $taskkill = Join-Path $env:SystemRoot 'System32/taskkill.exe'
            & $taskkill /PID ([string]$workerId) /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and (Get-VerifiedWorker $workerId)) {
                throw 'The verified worker process tree could not be stopped.'
            }
            Wait-Process -Id $workerId -Timeout 10 -ErrorAction SilentlyContinue
            if (Get-VerifiedWorker $workerId) { throw 'The worker did not exit; runtime files were not changed.' }
        }
    }
    if (Test-Path -LiteralPath $pidFile) { Remove-Item -LiteralPath $pidFile }
}

if ($Uninstall -and (Test-Path -LiteralPath $startupFile -PathType Leaf)) {
    $expected = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath('Startup')) 'ScheduleBridge.vbs'))
    if (-not [string]::Equals($startupFile, $expected, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'The Startup launcher path did not match this installation.'
    }
    $contents = [IO.File]::ReadAllText($startupFile)
    if (-not $contents.Contains("' ScheduleBridge managed launcher") -or
        -not $contents.Contains($workerFile) -or -not $contents.Contains($configFile)) {
        throw 'The Startup launcher is not owned by this installation; it was not removed.'
    }
    Remove-Item -LiteralPath $startupFile
}
Write-Output $(if ($Uninstall) { 'ScheduleBridge stopped and its Startup launcher removed. Configuration and history were kept.' } else { 'ScheduleBridge stopped.' })
