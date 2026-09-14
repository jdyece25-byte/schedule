import copy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import unittest

from bridge.github import GitHubError
from bridge.worker import Worker, WorkerLock, validate_request, stamp, utcnow, KST


def request(identifier="request-1", **overrides):
    value = {"version": 1, "id": identifier, "text": "연결 테스트", "agent": "codex",
             "created_at": stamp(), "today": utcnow().astimezone(KST).date().isoformat(),
             "timezone": "Asia/Seoul", "parent_id": None}
    value.update(overrides)
    return value


def plan(action="add", index=None):
    event = {"d": "2026-09-21", "t": "meeting", "n": "테스트 일정", "s": 600, "e": 660,
             "ti": "10:00–11:00", "lid": None, "loc": None, "no": "온라인"}
    return {"status": "ready", "message": "요청을 처리했습니다.", "questions": [],
            "operations": [] if action is None else [{"action": action, "index": index,
                                                     "event": None if action == "delete" else event}],
            "locations": [], "routes": []}


class FakeGitHub:
    def __init__(self):
        self.files, self.shas, self.counter = {}, {}, 0
        self.head_sha = "base"
        self.commits = []
        self.on_commit = None
        self.puts = []
        self.seed("owner/schedule", "events.json", [])
        self.seed("owner/schedule", "travel.json", {"locations": {}, "times": {}, "modes": {}})
        self.seed("owner/schedule", "SCHEDULE.md", "")

    def seed(self, repo, path, value):
        self.counter += 1
        self.files[repo, path] = copy.deepcopy(value)
        self.shas[repo, path] = str(self.counter)
        return str(self.counter)

    def read_json(self, repo, path, ref="main"):
        return copy.deepcopy(self.files.get((repo, path))), self.shas.get((repo, path))

    def read(self, repo, path, ref="main"):
        value, sha = self.read_json(repo, path, ref)
        return (value if isinstance(value, str) else json.dumps(value)), sha

    def put_json(self, repo, path, value, sha=None, branch="main", message=""):
        if self.shas.get((repo, path)) != sha:
            raise GitHubError(409, "Conflict")
        self.puts.append((path, copy.deepcopy(value)))
        return self.seed(repo, path, value)

    def head(self, repo, branch="main"):
        return self.head_sha

    def tree(self, repo, ref="main"):
        return {path: sha for (owner, path), sha in self.shas.items() if owner == repo}

    def commit_files(self, repo, branch, base, files, message):
        if self.on_commit:
            hook, self.on_commit = self.on_commit, None
            hook()
        if self.head_sha != base:
            raise GitHubError(422, "Not a fast forward")
        self.commits.append((base, copy.deepcopy(files), message))
        for path, content in files.items():
            self.seed(repo, path, json.loads(content))
        self.head_sha = "commit-" + str(len(self.commits))
        return self.head_sha


class Runner:
    def __init__(self, callback=None, output=None):
        self.calls = 0
        self.callback = callback
        self.output = output if output is not None else plan()

    def run(self, request, events, travel, notes, history, tick):
        self.calls += 1
        if self.callback:
            self.callback(self.calls)
        return copy.deepcopy(self.output)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = {"data_dir": self.directory.name, "queue_repo": "owner/inbox",
                       "target_repo": "owner/schedule"}
        self.github = FakeGitHub()
        self.runner = Runner()
        self.worker = Worker(self.config, self.github, self.runner)
        self.req = request()
        self.req_sha = self.github.seed("owner/inbox", "requests/request-1.json", self.req)
        self.worker.last_heartbeat = float("inf")

    def start(self):
        self.assertTrue(self.worker.claim(self.req["id"], self.req_sha))

    def test_atomic_change_contains_events_and_recovery_marker_not_prompt(self):
        self.start()
        self.worker.process(self.req, self.req_sha)
        self.assertEqual(len(self.github.commits), 1)
        files = self.github.commits[0][1]
        self.assertIn("events.json", files)
        self.assertIn(".bridge/applied/request-1.json", files)
        self.assertNotIn(self.req["text"], self.github.commits[0][2])
        self.assertEqual(self.worker.result("request-1")[0]["state"], "completed")

    def test_restart_after_public_commit_does_not_invoke_model_or_duplicate(self):
        self.start()
        finish = self.worker.finish
        self.worker.finish = lambda *a, **k: (_ for _ in ()).throw(GitHubError(0, "lost response"))
        with self.assertRaises(GitHubError):
            self.worker.process(self.req, self.req_sha)
        restarted = Worker(self.config, self.github, Runner())
        restarted.last_heartbeat = float("inf")
        self.assertTrue(restarted.claim("request-1", self.req_sha))
        restarted.process(self.req, self.req_sha)
        self.assertEqual(restarted.runner.calls, 0)
        self.assertEqual(len(self.github.commits), 1)
        self.assertEqual(len(self.github.files["owner/schedule", "events.json"]), 1)

    def test_lost_patch_response_recovers_success(self):
        self.start()
        original = self.github.commit_files
        def lost(*args):
            original(*args)
            raise GitHubError(0, "lost PATCH response")
        self.github.commit_files = lost
        self.worker.process(self.req, self.req_sha)
        self.assertEqual(self.worker.result("request-1")[0]["state"], "completed")
        self.assertEqual(len(self.github.commits), 1)

    def test_changed_branch_replans_against_new_snapshot(self):
        self.start()
        self.github.on_commit = lambda: setattr(self.github, "head_sha", "someone-else")
        self.worker.process(self.req, self.req_sha)
        self.assertEqual(self.runner.calls, 2)
        self.assertEqual(self.github.commits[0][0], "someone-else")

    def test_no_change_replans_after_concurrent_deletion(self):
        existing = plan()["operations"][0]["event"]
        self.github.seed("owner/schedule", "events.json", [existing])
        snapshots = []

        def propose(request, events, travel, notes, history, tick):
            snapshots.append(copy.deepcopy(events))
            if len(snapshots) == 1:
                # The event existed when planning began, then another client
                # deleted it before the already-present decision was saved.
                self.github.seed("owner/schedule", "events.json", [])
                self.github.head_sha = "concurrent-deletion"
                return plan(None)
            return plan()

        self.runner.run = propose
        self.start()
        self.worker.process(self.req, self.req_sha)
        self.assertEqual(snapshots, [[existing], []])
        self.assertEqual(len(self.github.commits), 1)
        self.assertEqual(self.github.commits[0][0], "concurrent-deletion")
        self.assertEqual(len(self.github.files["owner/schedule", "events.json"]), 1)
        self.assertEqual(self.worker.result("request-1")[0]["state"], "completed")

    def test_lost_lease_prevents_commit(self):
        self.start()
        def take_over(calls):
            value, _ = self.worker.result("request-1")
            value["owner"] = "another-worker"
            self.github.seed("owner/inbox", "results/request-1.json", value)
        self.runner.callback = take_over
        with self.assertRaisesRegex(RuntimeError, "인계"):
            self.worker.process(self.req, self.req_sha)
        self.assertFalse(self.github.commits)

    def test_other_pc_cannot_claim_live_lease(self):
        self.start()
        with tempfile.TemporaryDirectory() as other:
            peer = Worker({**self.config, "data_dir": other}, self.github, Runner())
            self.assertFalse(peer.claim("request-1", self.req_sha))
            value, _ = self.worker.result("request-1")
            value["lease_until"] = stamp(utcnow() - timedelta(seconds=1))
            self.github.seed("owner/inbox", "results/request-1.json", value)
            self.assertTrue(peer.claim("request-1", self.req_sha))

    def test_restart_keeps_no_change_decision_even_when_schedule_changed(self):
        self.runner.output = plan(None)
        self.start()
        self.worker.finish = lambda *a, **k: (_ for _ in ()).throw(GitHubError(0, "interrupted"))
        with self.assertRaises(GitHubError):
            self.worker.process(self.req, self.req_sha)
        restarted = Worker(self.config, self.github, Runner())
        self.assertTrue(restarted.claim("request-1", self.req_sha))
        restarted.process(self.req, self.req_sha)
        self.assertEqual(restarted.runner.calls, 0)
        self.assertFalse(self.github.commits)

    def test_poll_recovers_saved_no_change_completion_after_transient_finish_failure(self):
        self.runner.output = plan(None)
        real_put = self.github.put_json
        completion_attempts = []

        def transient_put(repo, path, value, *args, **kwargs):
            if path == "results/request-1.json" and value.get("state") == "completed":
                completion_attempts.append(copy.deepcopy(value))
                if len(completion_attempts) == 1:
                    persisted, _ = self.worker.result("request-1")
                    self.assertEqual(persisted["state"], "processing")
                    self.assertEqual(persisted["completion"]["message"], self.runner.output["message"])
                    raise GitHubError(0, "transient result write failure")
            return real_put(repo, path, value, *args, **kwargs)

        self.github.put_json = transient_put
        with self.assertLogs(level="ERROR"):
            self.worker.poll()
        result, _ = self.worker.result("request-1")
        self.assertEqual(len(completion_attempts), 2)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["message"], self.runner.output["message"])
        self.assertEqual(result["warnings"], [])
        self.assertIsNone(result["commit_sha"])
        self.assertEqual(self.runner.calls, 1)
        self.assertFalse(self.github.commits)
        self.assertFalse(any(value.get("state") == "failed" for _, value in self.github.puts))

    def test_clarification_replays_original_request_and_question(self):
        question = {**plan(None), "status": "needs_input", "questions": ["몇 시인가요?"]}
        self.runner.output = question
        self.start()
        self.worker.process(self.req, self.req_sha)
        followup = request("reply", text="오후 2시입니다.", parent_id="request-1")
        history = self.worker.history(followup)
        self.assertEqual(history[0]["request"]["text"], self.req["text"])
        self.assertEqual(history[0]["result"]["questions"], ["몇 시인가요?"])
        self.assertFalse(self.github.commits)

    def test_clarification_rejects_parent_changed_after_question_was_issued(self):
        self.runner.output = {**plan(None), "status": "needs_input", "questions": ["몇 시인가요?"]}
        self.start()
        self.worker.process(self.req, self.req_sha)
        changed_sha = self.github.seed(
            "owner/inbox", "requests/request-1.json", {**self.req, "text": "다른 일정으로 원래 요청을 변경"},
        )
        parent_result, _ = self.worker.result("request-1")
        self.assertNotEqual(changed_sha, parent_result["request_sha"])
        followup = request("reply", text="오후 2시입니다.", parent_id="request-1")
        with self.assertRaisesRegex(ValueError, "원래 확인 요청의 내용이 바뀌"):
            self.worker.history(followup)
        self.assertEqual(self.runner.calls, 1)
        self.assertFalse(self.github.commits)

    def test_invalid_parent_and_changed_request_rejected(self):
        with self.assertRaises(ValueError):
            self.worker.history(request("reply", parent_id="missing"))
        self.github.seed("owner/schedule", ".bridge/applied/request-1.json", {"request_sha": "old"})
        self.start()
        with self.assertRaisesRegex(ValueError, "내용이 바뀌"):
            self.worker.process(self.req, self.req_sha)

    def test_request_validation_kst_and_path(self):
        validate_request(self.req, "request-1")
        for overrides in ({"today": "2000-01-01"}, {"id": "../x"}, {"text": ""},
                          {"agent": "shell"}, {"parent_id": "request-1"}):
            with self.assertRaises((ValueError, TypeError)):
                validate_request({**self.req, **overrides}, "request-1")

    def test_os_lock_releases_without_deleting_lock_file(self):
        path = Path(self.directory.name) / "lock"
        with WorkerLock(path):
            with self.assertRaises(RuntimeError):
                with WorkerLock(path):
                    pass
        with WorkerLock(path):
            pass


if __name__ == "__main__":
    unittest.main()
