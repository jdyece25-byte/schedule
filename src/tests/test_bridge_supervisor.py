from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from src.bridge.supervisor import (
    Supervisor, SupervisorLock, atomic_json, ensure_running, probe_lock,
    read_maintenance, service_status,
)


MOCK_WORKER = '''
import json, os, pathlib, sys, time
config = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
root = pathlib.Path(config["data_dir"])
lock = (root / "worker.lock").open("a+b")
if (root / "worker.lock").stat().st_size == 0:
    lock.write(b"0")
    lock.flush()
lock.seek(0)
if os.name == "nt":
    import msvcrt
    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
else:
    import fcntl
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
starts = root / "starts.txt"
count = int(starts.read_text()) + 1 if starts.exists() else 1
starts.write_text(str(count))
if count <= config.get("crashes", 0):
    sys.exit(7)
while not (root / "stop.request").exists():
    time.sleep(0.01)
time.sleep(config.get("drain_seconds", 0))
(root / "finished.txt").write_text("finished")
'''


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="schedule-supervisor-test-")
        self.root = Path(self.folder.name)
        self.config_path = self.root / "config.json"
        self.worker_path = self.root / "worker.py"
        self.worker_path.write_text(MOCK_WORKER, encoding="utf-8")
        self.config = {"data_dir": str(self.root)}
        self.save_config()
        self.supervisors = []

    def save_config(self):
        atomic_json(self.config_path, self.config)

    def supervisor(self):
        value = Supervisor(self.config_path, worker_path=self.worker_path,
                           interval=0.01, restart_base=0.02, restart_max=0.08,
                           stable_seconds=1)
        self.supervisors.append(value)
        return value

    def tearDown(self):
        (self.root / "stop.request").write_text("stop", encoding="ascii")
        for value in self.supervisors:
            if value.child:
                try:
                    value.child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    value.child.kill()  # Only the isolated mock spawned by this test.
                    value.child.wait(timeout=3)
        self.folder.cleanup()

    def pump(self, supervisor, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            supervisor.step()
            if condition():
                return
            time.sleep(0.01)
        self.fail("Supervisor did not reach the expected state")

    def starts(self):
        path = self.root / "starts.txt"
        try:
            return int(path.read_text()) if path.exists() else 0
        except ValueError:  # Child may currently be writing its count.
            return 0

    def test_real_child_crashes_restart_with_bounded_backoff(self):
        self.config["crashes"] = 2
        self.save_config()
        supervisor = self.supervisor()
        self.pump(supervisor, lambda: self.starts() == 3)
        self.assertEqual(supervisor.starts, 3)
        self.assertEqual(supervisor.last_exit, 7)
        self.assertIsNone(supervisor.child.poll())
        self.assertEqual(json.loads((self.root / "supervisor-status.json").read_text())["state"], "running")

    def test_persistent_user_stop_survives_new_supervisor(self):
        (self.root / "service.disabled").write_text("user stopped")
        first = self.supervisor()
        first.step()
        second = self.supervisor()
        second.step()
        self.assertEqual(self.starts(), 0)
        self.assertEqual(service_status(self.root)["disabled"], True)
        self.assertTrue((self.root / "stop.request").exists())
        (self.root / "service.disabled").unlink()
        self.pump(second, lambda: self.starts() == 1)

    def test_maintenance_expires_and_resumes_without_manual_action(self):
        deadline = datetime.now(timezone.utc) + timedelta(seconds=0.2)
        atomic_json(self.root / "maintenance.json", {"resume_at": deadline.isoformat()})
        supervisor = self.supervisor()
        supervisor.step()
        self.assertEqual(self.starts(), 0)
        self.assertTrue((self.root / "stop.request").exists())
        self.pump(supervisor, lambda: self.starts() == 1)
        self.assertFalse((self.root / "maintenance.json").exists())
        self.assertFalse((self.root / "stop.request").exists())

    def test_pause_allows_real_child_to_finish_instead_of_killing_it(self):
        self.config["drain_seconds"] = 0.2
        self.save_config()
        supervisor = self.supervisor()
        self.pump(supervisor, lambda: self.starts() == 1)
        child = supervisor.child
        (self.root / "service.disabled").write_text("user stopped")
        supervisor.step()
        self.assertIsNone(child.poll())
        self.pump(supervisor, lambda: child.poll() is not None)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.root / "finished.txt").read_text(), "finished")
        self.assertEqual(self.starts(), 1)

    def test_stale_legacy_stop_signal_does_not_abandon_queue(self):
        (self.root / "stop.request").write_text("stale temporary pause")
        supervisor = self.supervisor()
        self.pump(supervisor, lambda: self.starts() == 1)
        self.assertFalse((self.root / "stop.request").exists())

    def test_invalid_and_far_future_maintenance_cannot_pause_forever(self):
        path = self.root / "maintenance.json"
        atomic_json(path, {"resume_at": "not a date"})
        self.assertTrue(service_status(self.root)["errors"])
        supervisor = self.supervisor()
        self.assertIsNone(supervisor.pause_reason())
        self.assertFalse(path.exists())
        atomic_json(path, {"resume_at": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()})
        deadline, error = read_maintenance(path)
        self.assertIsNone(error)
        self.assertLessEqual(deadline, time.time() + 3600)

    def test_existing_worker_os_lock_prevents_duplicate_child(self):
        supervisor = self.supervisor()
        with SupervisorLock(self.root / "worker.lock"):
            supervisor.step()
            self.assertIsNone(supervisor.child)
            self.assertTrue(service_status(self.root)["worker_running"])
        self.pump(supervisor, lambda: self.starts() == 1)

    def test_singleton_lock_is_released_and_status_probes_it(self):
        path = self.root / "supervisor.lock"
        self.assertEqual(probe_lock(path), ("missing", None))
        with SupervisorLock(path):
            self.assertEqual(probe_lock(path), ("held", None))
            self.assertTrue(service_status(self.root)["supervisor_running"])
            with self.assertRaises(RuntimeError):
                with SupervisorLock(path):
                    self.fail("A second supervisor acquired the singleton lock")
        self.assertEqual(probe_lock(path), ("free", None))

    def test_status_reports_read_errors_instead_of_claiming_health(self):
        (self.root / "supervisor-status.json").write_text("{bad json")
        result = service_status(self.root)
        self.assertFalse(result["supervisor_running"])
        self.assertFalse(result["worker_running"])
        self.assertTrue(result["errors"])

    def test_ensure_preserves_explicit_stop_and_resume_clears_it(self):
        supervisor = self.supervisor()
        for name in ("service.disabled", "supervisor.stop", "stop.request"):
            (self.root / name).write_text("stop")
        with patch("src.bridge.supervisor.subprocess.Popen") as spawn:
            result = ensure_running(supervisor)
            spawn.assert_not_called()
        self.assertTrue(result["disabled"])
        with SupervisorLock(self.root / "supervisor.lock"):
            result = ensure_running(supervisor, resume=True)
        self.assertFalse(result["disabled"])
        self.assertFalse(result["supervisor_stop_requested"])
        self.assertFalse((self.root / "stop.request").exists())

    def test_real_supervisor_cli_status_resume_and_graceful_shutdown(self):
        # Copy the independent runtime beside a mock worker: no GitHub/AI calls.
        source = Path(__file__).parents[1] / "bridge" / "supervisor.py"
        runtime = self.root / "supervisor.py"
        runtime.write_bytes(source.read_bytes())
        (self.root / "service.disabled").write_text("intentional user stop")
        command = [sys.executable, str(runtime), "--config", str(self.config_path)]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=flags)
        try:
            deadline = time.monotonic() + 5
            while not service_status(self.root)["supervisor_running"] and time.monotonic() < deadline:
                time.sleep(0.02)
            result = subprocess.run(command + ["--status"], capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertTrue(status["supervisor_running"])
            self.assertTrue(status["disabled"])
            self.assertFalse(status["worker_running"])
            result = subprocess.run(command + ["--ensure-running", "--resume"], capture_output=True,
                                    text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            deadline = time.monotonic() + 5
            while not service_status(self.root)["worker_running"] and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(service_status(self.root)["worker_running"])
        finally:
            (self.root / "supervisor.stop").write_text("test shutdown")
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()  # Only this test's isolated supervisor process.
                process.wait(timeout=3)
        self.assertFalse(service_status(self.root)["worker_running"])
        self.assertFalse(service_status(self.root)["supervisor_running"])
        self.assertTrue((self.root / "finished.txt").exists())


if __name__ == "__main__":
    unittest.main()
