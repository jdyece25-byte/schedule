import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from src.bridge.health import check_health


class FakeGitHub:
    def __init__(self, value):
        self.value = value

    def read_json(self, repo, path, ref):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value, "sha"


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 9, 14, 8, tzinfo=timezone.utc)
        self.config = {"data_dir": self.directory.name, "queue_repo": "owner/queue", "target_repo": "owner/schedule"}
        self.local = dict(worker_running=True, supervisor_running=True, disabled=False,
                          maintenance_until=None, supervisor_stop_requested=False, errors=[])
        self.heartbeat = {"version": 1, "target_repo": "owner/schedule", "updated_at": self.now.isoformat(), "agents": ["codex", "claude"]}

    def check(self, heartbeat=None):
        with patch("src.bridge.health.service_status", return_value={**self.local, "errors": list(self.local["errors"])}):
            return check_health(self.config, github=FakeGitHub(heartbeat or self.heartbeat), now=self.now)

    def test_remote_success_cannot_hide_missing_local_worker_or_guard(self):
        for field in ("worker_running", "supervisor_running"):
            self.local[field] = False
            self.assertFalse(self.check()["healthy"])
            self.local[field] = True

    def test_fresh_remote_and_both_local_locks_are_required(self):
        self.assertTrue(self.check()["healthy"])
        self.assertFalse(self.check({**self.heartbeat, "updated_at": (self.now-timedelta(minutes=3)).isoformat()})["healthy"])

    def test_intentional_stop_and_maintenance_are_reported_without_resuming(self):
        for field, value in (("disabled", True), ("maintenance_until", self.now.isoformat()), ("supervisor_stop_requested", True)):
            previous = self.local[field]
            self.local[field] = value
            self.assertFalse(self.check()["healthy"])
            self.assertEqual(self.check()[field], value)
            self.local[field] = previous

    def test_wrong_repo_corrupt_heartbeat_and_network_failure_are_unhealthy(self):
        for value in ({}, {**self.heartbeat, "target_repo": "someone/else"},
                      {**self.heartbeat, "updated_at": "2026-09-14T08:00:00"}, RuntimeError("network unavailable")):
            result = self.check(value if value else {"version": 9})
            self.assertFalse(result["healthy"])
            self.assertTrue(result["errors"])

    def test_local_only_does_not_contact_github(self):
        with patch("src.bridge.health.GitHub", side_effect=AssertionError("network forbidden")):
            result = check_health(self.config, remote=False)
        self.assertFalse(result["healthy"])
        self.assertIsNone(result["remote"])


if __name__ == "__main__":
    unittest.main()
