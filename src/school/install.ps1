[CmdletBinding()]
param([string]$LocalRoot)
$ErrorActionPreference = 'Stop'
if (-not $LocalRoot) { $LocalRoot = Join-Path (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))) '2-2' }
$LocalRoot = [IO.Path]::GetFullPath($LocalRoot)
if (-not (Test-Path -LiteralPath $LocalRoot -PathType Container)) { throw 'Specify -LocalRoot pointing to the 2-2 course folder.' }
$dataDir = Join-Path $env:LOCALAPPDATA 'ScheduleSchool'
$runtime = Join-Path $dataDir 'runtime'
$sourceRoot = Split-Path -Parent $PSScriptRoot
$repoRoot = Split-Path -Parent $sourceRoot
New-Item -ItemType Directory -Path $dataDir -Force | Out-Null
$python = Join-Path $dataDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    & python -m venv (Join-Path $dataDir '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Unable to prepare school collector Python.' }
}
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'PDF dependency installation failed; no existing service was changed.' }
foreach ($folder in @('school','bridge','notifications')) { New-Item -ItemType Directory -Path (Join-Path $runtime ('src\' + $folder)) -Force | Out-Null }
Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.py' -File | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $runtime ('src\school\' + $_.Name)) -Force
}
foreach ($name in @('github.py','planner.py','supervisor.py','school_knowledge.py')) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot ('bridge\' + $name)) -Destination (Join-Path $runtime ('src\bridge\' + $name)) -Force
}
Copy-Item -LiteralPath (Join-Path $sourceRoot 'validate_db.py') -Destination (Join-Path $runtime 'src\validate_db.py') -Force
Copy-Item -LiteralPath (Join-Path $sourceRoot 'notifications\pc.py') -Destination (Join-Path $runtime 'src\notifications\pc.py') -Force
Copy-Item -LiteralPath (Join-Path $repoRoot 'DB\school-sources.json') -Destination (Join-Path $dataDir 'sources.json') -Force
$configPath = Join-Path $dataDir 'config.json'
$utf8 = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText($configPath, (@{queue_repo='jdyece25-byte/schedule-requests';target_repo='jdyece25-byte/schedule';local_root=$LocalRoot} | ConvertTo-Json), $utf8)
$entry = Join-Path $runtime 'src\school\pc.py'
$pythonw = Join-Path (Split-Path -Parent $python) 'pythonw.exe'
$arguments = '-B "' + $entry + '" --config "' + $configPath + '" --ensure-running'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskName = 'ScheduleSchool-' + $identity.User.Value + '-Recovery'
$description = 'ScheduleSchool managed recovery'
$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing -and ($existing.Description -ne $description -or $existing.Actions.Count -ne 1 -or $existing.Actions[0].Execute -ne $pythonw -or $existing.Actions[0].Arguments -ne $arguments)) { throw 'An unrelated school recovery task was not replaced.' }
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $runtime
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) -RepetitionInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $taskName -InputObject (New-ScheduledTask -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description $description) -Force | Out-Null
$startup = Join-Path ([Environment]::GetFolderPath('Startup')) 'ScheduleSchool.vbs'
if ((Test-Path -LiteralPath $startup) -and -not ([IO.File]::ReadAllText($startup).Contains("' ScheduleSchool managed launcher"))) { throw 'An unrelated startup file was not replaced.' }
$command = '"' + $pythonw + '" ' + $arguments
$vbs = "' ScheduleSchool managed launcher`r`n" + 'Set shell = CreateObject("WScript.Shell")' + "`r`n" + 'shell.Run "' + $command.Replace('"','""') + '", 0, False' + "`r`n"
[IO.File]::WriteAllText($startup,$vbs,[Text.Encoding]::Unicode)
& $python -B $entry --config $configPath --ensure-running
if ($LASTEXITCODE -ne 0) { throw 'The school collector did not start. Existing services remain unchanged.' }
Write-Output 'Independent school collector installed; existing schedule and push workers were not stopped.'
