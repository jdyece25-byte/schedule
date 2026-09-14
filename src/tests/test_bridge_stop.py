"""Check stop acknowledgement against isolated fake CLI status snapshots."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


FAKE_STATUS = '''
from datetime import datetime, timezone
import json, pathlib, sys
root = pathlib.Path(sys.argv[sys.argv.index('--config') + 1]).parent
config = json.loads((root / 'scenario.json').read_text())
counter = root / 'calls.txt'
number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(number))
snapshot = {'state': 'maintenance', 'worker_pid': None,
            'updated_at': datetime.now(timezone.utc).isoformat()}
status = {'worker_running': False, 'supervisor_running': True, 'errors': [], 'snapshot': snapshot}
kind = config['kind']
if kind == 'starting_child' and number == 1:
    snapshot['worker_pid'] = 12345
if kind == 'old_snapshot' and number == 1:
    snapshot['updated_at'] = '2000-01-01T00:00:00Z'
if kind == 'not_acknowledged' and number == 1:
    snapshot['state'] = 'running'
if kind == 'worker_running' and number == 1:
    status['worker_running'] = True
if kind == 'absent_guard':
    status['supervisor_running'] = False
if kind == 'disabled':
    snapshot['state'] = 'stopped'
if kind == 'uninstalled':
    status['supervisor_running'] = False
    status['snapshot'] = None
print(json.dumps(status))
'''


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows service control")
class StopTests(unittest.TestCase):
    def run_stop(self, kind, *, permanent=False, uninstall=False, disabled=False):
        with tempfile.TemporaryDirectory(prefix="schedule-stop-test-") as temporary:
            root = Path(temporary)
            (root / "config.json").write_text("{}")
            (root / "scenario.json").write_text(json.dumps({"kind": kind}))
            (root / "fake-status.py").write_text(FAKE_STATUS)
            if disabled:
                (root / "service.disabled").write_text("existing intentional user stop")
            stop = (Path(__file__).parents[1] / "bridge" / "stop.ps1").read_text(encoding="utf-8")
            function_start = stop.index("function Write-Control")
            function_end = stop.index("if ($Uninstall -and", function_start)
            control_start = stop.index("$controlWrittenAt =")
            control_end = stop.index("if ($Uninstall) {\n    if ($task)", control_start)
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            script = f'''$ErrorActionPreference = 'Stop'
$dataDir = {quote(root)}
$configFile = Join-Path $dataDir 'config.json'
$supervisorSource = Join-Path $dataDir 'fake-status.py'
$python = {quote(sys.executable)}
$utf8 = [Text.UTF8Encoding]::new($false)
$permanentStop = {'$true' if permanent or uninstall else '$false'}
$Uninstall = {'$true' if uninstall else '$false'}
$MaintenanceOwner = 'isolated-test-owner'
$MaintenanceMinutes = 2
$WaitSeconds = 1
{stop[function_start:function_end]}
$failure = $null
try {{
{stop[control_start:control_end]}
}} catch {{ $failure = $_.Exception.Message }}
@{{failure=$failure}} | ConvertTo-Json -Compress
'''
            (root / "harness.ps1").write_text(script, encoding="utf-8-sig")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-File", str(root / "harness.ps1")],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            outcome = json.loads(result.stdout.strip())
            outcome["calls"] = int((root / "calls.txt").read_text())
            outcome["disabled"] = (root / "service.disabled").exists()
            outcome["supervisor_stop"] = (root / "supervisor.stop").exists()
            return outcome

    def test_free_worker_lock_does_not_hide_a_starting_child(self):
        result = self.run_stop("starting_child")
        self.assertIsNone(result["failure"])
        self.assertGreaterEqual(result["calls"], 2)

    def test_old_maintenance_snapshot_is_not_current_acknowledgement(self):
        result = self.run_stop("old_snapshot")
        self.assertIsNone(result["failure"])
        self.assertGreaterEqual(result["calls"], 2)

    def test_running_snapshot_is_not_a_pause_acknowledgement(self):
        result = self.run_stop("not_acknowledged")
        self.assertIsNone(result["failure"])
        self.assertGreaterEqual(result["calls"], 2)

    def test_live_worker_must_release_its_lock(self):
        result = self.run_stop("worker_running")
        self.assertIsNone(result["failure"])
        self.assertGreaterEqual(result["calls"], 2)

    def test_absent_guard_does_not_report_success(self):
        result = self.run_stop("absent_guard")
        self.assertIn("not acknowledged", result["failure"])
        self.assertFalse(result["disabled"])

    def test_temporary_pause_preserves_existing_permanent_stop(self):
        result = self.run_stop("disabled", disabled=True)
        self.assertIsNone(result["failure"])
        self.assertTrue(result["disabled"])

    def test_permanent_stop_is_preserved_even_if_guard_is_absent(self):
        result = self.run_stop("absent_guard", permanent=True)
        self.assertIn("not acknowledged", result["failure"])
        self.assertTrue(result["disabled"])

    def test_uninstall_accepts_both_process_locks_released(self):
        result = self.run_stop("uninstalled", uninstall=True)
        self.assertIsNone(result["failure"])
        self.assertTrue(result["disabled"])
        self.assertTrue(result["supervisor_stop"])


if __name__ == "__main__":
    unittest.main()
