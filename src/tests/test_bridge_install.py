"""Run the real installer transaction with isolated files and fake service CLIs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows installer")
class InstallerTests(unittest.TestCase):
    def run_transaction(self, *, changed=False, busy=False, copy_failure=False):
        with tempfile.TemporaryDirectory(prefix="schedule-install-test-") as temporary:
            root = Path(temporary)
            source, data = root / "source", root / "data"
            runtime = data / "runtime"
            source.mkdir()
            runtime.mkdir(parents=True)
            real_source = Path(__file__).parents[1] / "bridge" / "install.ps1"
            installer = real_source.read_text(encoding="utf-8")
            (source / "install.ps1").write_text(installer, encoding="utf-8-sig")
            for name in ("worker.py", "planner.py", "github.py"):
                (runtime / name).write_text("# original runtime\n")
                (source / name).write_text("# changed runtime\n" if changed else "# original runtime\n")
            # No supervisor process, GitHub, AI, Startup folder or scheduled task
            # is touched; this CLI records calls and reports an adopted worker.
            (source / "supervisor.py").write_text('''
import json, pathlib, sys
config = json.loads(pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).read_text())
root = pathlib.Path(config['data_dir'])
with (root / 'guard-calls.txt').open('a') as file:
    file.write('ensure\\n')
print(json.dumps({'supervisor_running': True, 'worker_running': True, 'errors': []}))
''')
            (source / "stop.ps1").write_text('''
param($MaintenanceMinutes, $WaitSeconds, $MaintenanceOwner)
$data = Split-Path -Parent $PSScriptRoot
$data = Split-Path -Parent $data
[IO.File]::WriteAllText((Join-Path $data 'maintenance.json'), (@{owner=$MaintenanceOwner; resume_at=[DateTime]::UtcNow.AddMinutes(2).ToString('o')} | ConvertTo-Json))
[IO.File]::WriteAllText((Join-Path $data 'stop.request'), 'stop')
[IO.File]::WriteAllText((Join-Path $data 'pause-called.txt'), 'called')
if ($env:SCHEDULE_TEST_BUSY -eq '1') { throw 'mock active request still finishing' }
''', encoding="utf-8-sig")
            config = json.dumps({"data_dir": str(data), "queue_repo": "owner/queue", "agent": "codex"})
            (data / "config.json").write_text(config)
            start = installer.index("# Prepare and syntax-check every file")
            end = installer.index("$commandLine =", start)
            transaction = installer[start:end]
            if copy_failure:
                transaction = transaction.replace(
                    "function Copy-Atomic([string]$Source, [string]$Destination) {",
                    "function Copy-Atomic([string]$Source, [string]$Destination) {\n"
                    "    if ($Source.Contains('staged') -and (Split-Path -Leaf $Source) -eq 'worker.py') { throw 'mock disk write failure' }",
                )
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            harness = f'''$ErrorActionPreference = 'Stop'
$dataDir = {quote(data)}
$runtimeDir = {quote(runtime)}
$configFile = Join-Path $dataDir 'config.json'
$supervisorFile = Join-Path $runtimeDir 'supervisor.py'
$startupDir = Join-Path $dataDir 'mock-startup'
$python = {quote(sys.executable)}
$utf8 = [Text.UTF8Encoding]::new($false)
$existingConfig = [IO.File]::ReadAllText($configFile) | ConvertFrom-Json
$configJson = [IO.File]::ReadAllText($configFile)
$configChanged = $false
$failure = $null
try {{
{transaction}
}} catch {{ $failure = $_.Exception.Message }}
@{{failure=$failure}} | ConvertTo-Json -Compress
'''
            (source / "harness.ps1").write_text(harness, encoding="utf-8-sig")
            environment = dict(os.environ, SCHEDULE_TEST_BUSY="1" if busy else "0")
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-File", str(source / "harness.ps1")],
                capture_output=True, text=True, timeout=20, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            outcome = json.loads(result.stdout.strip())
            outcome.update({
                "paused": (data / "pause-called.txt").exists(),
                "maintenance": (data / "maintenance.json").exists(),
                "stop": (data / "stop.request").exists(),
                "guard": (runtime / "supervisor.py").exists(),
                "worker": (runtime / "worker.py").read_text(),
                "planner": (runtime / "planner.py").read_text(),
                "github": (runtime / "github.py").read_text(),
                "config": (data / "config.json").read_text(),
                "guard_calls": (data / "guard-calls.txt").read_text().count("ensure")
                    if (data / "guard-calls.txt").exists() else 0,
            })
            return outcome

    def test_guard_only_install_adopts_worker_without_pause(self):
        result = self.run_transaction()
        self.assertIsNone(result["failure"])
        self.assertFalse(result["paused"])
        self.assertEqual(result["worker"], "# original runtime\n")
        self.assertGreaterEqual(result["guard_calls"], 2)

    def test_busy_worker_abort_keeps_original_core_and_resumes(self):
        result = self.run_transaction(changed=True, busy=True)
        self.assertIn("active request", result["failure"])
        self.assertTrue(result["paused"])
        self.assertFalse(result["maintenance"])
        self.assertFalse(result["stop"])
        self.assertTrue(result["guard"])
        self.assertEqual(result["worker"], "# original runtime\n")
        self.assertGreaterEqual(result["guard_calls"], 2)

    def test_partial_copy_failure_restores_core_and_resumes(self):
        result = self.run_transaction(changed=True, copy_failure=True)
        self.assertIn("disk write failure", result["failure"])
        self.assertFalse(result["maintenance"])
        self.assertFalse(result["stop"])
        self.assertEqual(result["worker"], "# original runtime\n")
        self.assertEqual(result["planner"], "# original runtime\n")
        self.assertEqual(result["github"], "# original runtime\n")
        self.assertGreaterEqual(result["guard_calls"], 2)

    def test_core_upgrade_resumes_after_success(self):
        result = self.run_transaction(changed=True)
        self.assertIsNone(result["failure"])
        self.assertTrue(result["paused"])
        self.assertFalse(result["maintenance"])
        self.assertFalse(result["stop"])
        self.assertEqual(result["worker"], "# changed runtime\n")
        self.assertEqual(result["planner"], "# changed runtime\n")
        self.assertEqual(result["github"], "# changed runtime\n")


if __name__ == "__main__":
    unittest.main()
