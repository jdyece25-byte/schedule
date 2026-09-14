[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$dataDir = Join-Path $env:LOCALAPPDATA 'SchedulePush'
$config = Join-Path $dataDir 'config.json'
$privateKey = Join-Path $dataDir 'vapid.dpapi'
if (-not (Test-Path -LiteralPath $config) -or -not (Test-Path -LiteralPath $privateKey)) {
    throw 'Run src/notifications/provision.py first. The schedule worker has not been changed.'
}
$python = Join-Path $dataDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & python -m venv (Join-Path $dataDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated push environment.' }
}
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Cannot install push dependencies. ScheduleBridge remains running.' }
$runtime = Join-Path $dataDir 'runtime'
$notificationDir = Join-Path $runtime 'src\notifications'
$bridgeDir = Join-Path $runtime 'src\bridge'
New-Item -ItemType Directory -Path $notificationDir,$bridgeDir -Force | Out-Null
Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File | Where-Object { $_.Name -ne 'provision.py' } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $notificationDir $_.Name) -Force
}
foreach ($name in @('github.py','supervisor.py')) {
    Copy-Item -LiteralPath (Join-Path (Split-Path -Parent $PSScriptRoot) ('bridge\' + $name)) -Destination (Join-Path $bridgeDir $name) -Force
}
$entry = Join-Path $notificationDir 'pc.py'
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
$arguments = '-B "' + $entry + '" --config "' + $config + '" --ensure-running'
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'SchedulePush.vbs'
$description = 'SchedulePush managed recovery'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'SchedulePush-' + $identity.User.Value + '-Recovery'
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($task -and ($task.Description -ne $description -or $task.Actions.Count -ne 1 -or
    $task.Actions[0].Execute -ne $pythonw -or $task.Actions[0].Arguments -ne $arguments)) {
    throw 'An unrelated task uses the push recovery name; it was not replaced.'
}
if ((Test-Path -LiteralPath $startup) -and -not ([IO.File]::ReadAllText($startup).Contains("' SchedulePush managed launcher"))) {
    throw 'An unrelated Startup entry exists; it was not replaced.'
}
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $runtime
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -InputObject (New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description) -Force | Out-Null
$command = '"' + $pythonw + '" ' + $arguments
$vbs = "' SchedulePush managed launcher`r`n" + 'Set shell = CreateObject("WScript.Shell")' + "`r`n" + 'shell.Run "' + $command.Replace('"','""') + '", 0, False' + "`r`n"
[IO.File]::WriteAllText($startup, $vbs, [Text.Encoding]::Unicode)
& $python -B $entry --config $config --ensure-running
if ($LASTEXITCODE -ne 0) { throw 'Push helper did not start; the existing schedule worker was not changed.' }
Write-Output 'PC push helper installed independently. ScheduleBridge was not stopped or restarted.'
