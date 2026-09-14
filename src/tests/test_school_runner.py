"""Private inbox/public DB CAS and crash recovery, using immutable fake revisions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

from src.bridge.github import GitHubError
from src.school.reconcile import digest, prepare
from src.school.runner import Importer, INDEX, LEASE, QUEUE, TARGET, run_once
from src.school.sources import normalized_source


CONFIG = {"term": {"start": "2026-09-01", "end": "2026-12-31"}, "courses": [
    {"key": "logic", "name": "논리설계 및 실험", "aliases": ["논설", "논리설계"]}]}
STAMP = "2026-09-14T00:00:00Z"
KEEP = {"id": "personal-existing", "d": "2026-09-15", "t": "meeting", "n": "기존 약속",
        "s": 600, "e": 660, "no": "사용자의 메모", "series": "keep-this-series"}


def source(*, kind="etl_assignment", content="과제 안내", **changes):
    fields = {"kind": kind, "course": "logic", "external_id": "course-1:notice-2",
              "title": "HW1 제출", "content": content, "updated_at": STAMP,
              "extraction_status": "parsed"}
    if kind == "etl_assignment":
        fields["due_at"] = "2026-09-22T14:59:00Z"
    fields.update(changes)
    return normalized_source(**fields)


def result(*sources, status="ok"):
    return {"status": status, "sources": list(sources), "issues": []}


class MemoryGitHub:
    """Immutable commit trees, SHA-based file writes, and injected network races."""
    def __init__(self, events=None):
        self.snapshots, self.heads, self.commits, self.reads = {}, {}, [], []
        self.counter = 0
        self.before_commit = None
        self.after_commit = None
        self.lose_response_for = None
        self._advance(QUEUE, {})
        self._advance(TARGET, {"DB/events.json": json.dumps(events if events is not None else [KEEP])})

    @staticmethod
    def blob(text):
        return hashlib.sha1(text.encode("utf-8")).hexdigest()

    def _advance(self, repo, files):
        self.counter += 1
        revision = f"{self.counter:040x}"
        self.snapshots[revision] = deepcopy(files)
        self.heads[repo] = revision
        return revision

    def seed(self, repo, path, value):
        files = deepcopy(self.snapshots[self.heads[repo]])
        files[path] = json.dumps(value, ensure_ascii=False)
        return self._advance(repo, files)

    def api(self, endpoint, method="GET", data=None):
        if endpoint == "repos/" + QUEUE:
            return {"private": True}
        raise AssertionError("Unexpected API route: " + endpoint)

    def head(self, repo, branch="main"):
        return self.heads[repo]

    def read_json(self, repo, path, ref="main"):
        self.reads.append((repo, path, ref))
        revision = self.heads[repo] if ref == "main" else ref
        text = self.snapshots[revision].get(path)
        return (json.loads(text), self.blob(text)) if text is not None else (None, None)

    def tree(self, repo, ref="main"):
        revision = self.heads[repo] if ref == "main" else ref
        return {path: self.blob(text) for path, text in self.snapshots[revision].items()}

    def put_json(self, repo, path, value, sha=None, branch="main", message=""):
        _, current = self.read_json(repo, path)
        if current != sha:
            raise GitHubError(409, "synthetic file CAS conflict")
        text = json.dumps(value, ensure_ascii=False)
        self.commit_files(repo, branch, self.head(repo), {path: text}, message)
        return self.blob(text)

    def commit_files(self, repo, branch, base, files, message):
        if self.before_commit:
            self.before_commit(repo, base, files)
        if base != self.heads[repo]:
            raise GitHubError(409, "synthetic ref CAS conflict")
        updated = deepcopy(self.snapshots[base])
        updated.update(deepcopy(files))
        revision = self._advance(repo, updated)
        self.commits.append({"repo": repo, "base": base, "revision": revision, "files": deepcopy(files)})
        if self.after_commit:
            self.after_commit(repo, revision, files)
        if repo == self.lose_response_for:
            self.lose_response_for = None
            raise GitHubError(0, "synthetic response lost after successful commit")
        return revision

    def public_commits(self):
        return [commit for commit in self.commits if commit["repo"] == TARGET]


class SchoolRunnerTests(unittest.TestCase):
    def setUp(self):
        self.github = MemoryGitHub()

    def importer(self, **kwargs):
        importer = Importer(self.github, CONFIG, **kwargs)
        importer.acquire()
        self.addCleanup(importer.release)
        return importer

    def make_review_item(self, *, kind="deadline", target=None):
        notice = source(kind="local", content="10/1 HW1 마감\n10/2 HW2 마감")
        candidates = []
        for number in (1, 2):
            value = {"id": "candidate-" + str(number), "source_id": notice["id"],
                     "source_hash": notice["content_hash"], "course": "logic", "kind": kind,
                     "auto_eligible": False, "event": {"d": f"2026-10-0{number}",
                      "n": f"논설 HW{number} 제출", "t": "deadline", "s": 1380}}
            if target:
                value.update(kind="cancellation", event={"d": target["d"], "n": "논설 휴강", "t": "class"})
            events, _ = self.github.read_json(TARGET, "DB/events.json")
            candidates.append(prepare(value, events, CONFIG))
            if target:
                break
        item = {"id": notice["id"], "content_hash": notice["content_hash"], "course": "논리설계 및 실험",
                "course_key": "logic", "title": "일정 공지", "source_kind": "local", "source_url": "",
                "updated_at": STAMP, "first_seen_at": STAMP, "candidates": candidates,
                "event_ids": [], "state": "needs_review"}
        self.github.seed(QUEUE, INDEX, {"version": 1, "updated_at": STAMP, "collectors": {}, "items": [item]})
        return item

    def decision(self, item, candidates=None, *, identifier="decision-1", **changes):
        value = {"version": 1, "id": identifier, "source_id": item["id"], "source_hash": item["content_hash"],
                 "action": "approve", "candidates": deepcopy(candidates if candidates is not None else item["candidates"])}
        value.update(changes)
        self.github.seed(QUEUE, "school/decisions/" + identifier + ".json", value)
        return value

    def test_first_local_baseline_never_changes_db_or_stores_raw_content(self):
        importer = self.importer()
        original = source(kind="local", content="9/22 23:59 HW1 제출 마감")
        importer.consume({"local": result(original)})
        self.assertEqual(self.github.read_json(TARGET, "DB/events.json")[0], [KEEP])
        self.assertEqual(self.github.public_commits(), [])
        index = self.github.read_json(QUEUE, INDEX)[0]
        self.assertTrue(index["collectors"]["local"]["initialized"])
        self.assertEqual(index["items"][0]["state"], "baseline")
        self.assertEqual(index["items"][0]["candidates"], [])
        self.assertNotIn("school/sources/" + original["id"] + ".json", self.github.tree(QUEUE))

    def test_changed_local_notice_requires_review_and_unchanged_scan_is_idempotent(self):
        importer = self.importer()
        original = source(kind="local", content="9/22 23:59 HW1 제출 마감")
        importer.consume({"local": result(original)})
        changed = source(kind="local", content="9/23 23:59 HW1 제출 마감")
        importer.consume({"local": result(changed)})
        importer.consume({"local": result(changed)})
        item = self.github.read_json(QUEUE, INDEX)[0]["items"][0]
        self.assertEqual(item["state"], "needs_review")
        self.assertEqual(len(item["candidates"]), 1)
        self.assertEqual(item["candidates"][0]["event"]["d"], "2026-09-23")
        self.assertEqual(self.github.public_commits(), [])
        self.assertIn("school/sources/" + original["id"] + ".json", self.github.tree(QUEUE))

    def test_cloud_inbox_marks_ready_without_public_writes_then_pc_applies_once(self):
        cloud = self.importer(inbox_only=True)
        cloud.consume({"etl": result(source())})
        self.assertEqual(cloud.index["items"][0]["state"], "ready")
        cloud.ready()
        self.assertFalse(cloud.decisions())
        self.assertEqual(self.github.public_commits(), [])
        cloud.release()
        pc = self.importer()
        pc.ready()
        pc.ready()
        self.assertEqual(pc.index["items"][0]["state"], "applied")
        events = self.github.read_json(TARGET, "DB/events.json")[0]
        self.assertEqual(events[0], KEEP)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(self.github.public_commits()), 1)
        public_text = "\n".join(self.github.public_commits()[0]["files"].values())
        self.assertNotIn("과제 안내", public_text)

    def test_stale_source_hash_approval_is_rejected_without_db_change(self):
        item = self.make_review_item()
        self.decision(item, source_hash="stale-version")
        importer = self.importer()
        importer.decisions()
        outcome = self.github.read_json(QUEUE, "school/decision-results/decision-1.json")[0]
        self.assertEqual(outcome["state"], "conflict")
        self.assertEqual(self.github.public_commits(), [])
        self.assertEqual(importer.index["items"][0]["state"], "needs_review")

    def test_partial_approval_keeps_remaining_candidates_for_next_decision(self):
        item = self.make_review_item()
        self.decision(item, [item["candidates"][0]])
        importer = self.importer()
        importer.decisions()
        current = importer.index["items"][0]
        self.assertEqual(current["state"], "needs_review")
        self.assertEqual([c["id"] for c in current["candidates"]], ["candidate-2"])
        self.assertEqual(len(current["event_ids"]), 1)
        self.decision(current, identifier="decision-2")
        importer.decisions()
        self.assertEqual(current["state"], "applied")
        self.assertEqual(current["candidates"], [])
        self.assertEqual(len(current["event_ids"]), 2)
        self.assertEqual(len(self.github.read_json(TARGET, "DB/events.json")[0]), 3)

    def test_editing_delete_target_event_is_rejected(self):
        target = {"id": "class-occurrence", "d": "2026-10-01", "n": "논설 수업", "t": "class", "s": 570, "e": 645}
        self.github.seed(TARGET, "DB/events.json", [KEEP, target])
        item = self.make_review_item(kind="cancellation", target=target)
        self.assertEqual(item["candidates"][0]["action"], "delete")
        changed = deepcopy(item["candidates"])
        changed[0]["event"]["d"] = "2026-10-02"
        self.decision(item, changed)
        self.importer().decisions()
        self.assertEqual(self.github.read_json(QUEUE, "school/decision-results/decision-1.json")[0]["state"], "conflict")
        self.assertEqual(self.github.read_json(TARGET, "DB/events.json")[0], [KEEP, target])

    def test_receipt_rejects_changed_payload_for_same_operation(self):
        item = self.make_review_item()
        importer = self.importer()
        candidates = [item["candidates"][0]]
        identifiers = importer.publish(item, candidates, "operation-one", approved=True)
        self.assertEqual(importer.publish(item, candidates, "operation-one", approved=True), identifiers)
        altered = deepcopy(candidates)
        altered[0]["event"]["s"] = 1200
        with self.assertRaisesRegex(ValueError, "이전에 처리한 요청"):
            importer.publish(item, altered, "operation-one", approved=True)
        self.assertEqual(len(self.github.public_commits()), 1)

    def test_public_commit_response_loss_recovers_receipt_without_duplicate_event(self):
        importer = self.importer()
        self.github.lose_response_for = TARGET
        importer.consume({"etl": result(source())})
        self.assertEqual(importer.index["items"][0]["state"], "applied")
        self.assertEqual(len(self.github.public_commits()), 1)
        self.assertEqual(len(self.github.read_json(TARGET, "DB/events.json")[0]), 2)
        self.assertEqual(sum(path.startswith("DB/school-applied/") for path in self.github.tree(TARGET)), 1)

    def test_index_sha_is_read_at_own_commit_and_concurrent_writer_is_preserved(self):
        importer = self.importer()
        external = {"version": 1, "collectors": {}, "items": [], "external_change": "preserve"}
        original_commit = []
        def after_commit(repo, revision, files):
            if repo == QUEUE and INDEX in files:
                self.github.after_commit = None
                original_commit.append(revision)
                self.github.seed(QUEUE, INDEX, external)
        self.github.after_commit = after_commit
        importer.save_index()
        _, own_sha = self.github.read_json(QUEUE, INDEX, original_commit[0])
        self.assertEqual(importer.index_sha, own_sha)
        self.assertNotEqual(importer.index_sha, self.github.read_json(QUEUE, INDEX)[1])
        importer.index["later_change"] = "do not overwrite external state"
        with self.assertRaisesRegex(RuntimeError, "edited concurrently"):
            importer.save_index()
        self.assertEqual(self.github.read_json(QUEUE, INDEX)[0], external)

    def test_unrelated_queue_commit_cas_retry_preserves_other_file(self):
        importer = self.importer()
        def before_commit(repo, base, files):
            if repo == QUEUE and INDEX in files:
                self.github.before_commit = None
                self.github.seed(QUEUE, "worker-heartbeat.json", {"state": "online"})
        self.github.before_commit = before_commit
        importer.save_index()
        self.assertEqual(self.github.read_json(QUEUE, "worker-heartbeat.json")[0], {"state": "online"})
        self.assertEqual(self.github.read_json(QUEUE, INDEX)[0], importer.index)

    def test_invalid_index_after_acquiring_lease_still_releases_lease(self):
        self.github.seed(QUEUE, INDEX, {"version": 999, "items": []})
        with self.assertRaisesRegex(ValueError, "Unsupported school index"):
            run_once(CONFIG, collect=False, github=self.github)
        lease = self.github.read_json(QUEUE, LEASE)[0]
        self.assertIsNone(lease["owner"])
        self.assertLessEqual(datetime.fromisoformat(lease["until"].replace("Z", "+00:00")), datetime.now(timezone.utc))
        self.assertEqual(self.github.public_commits(), [])

    def test_no_etl_auth_still_processes_pc_decisions(self):
        item = self.make_review_item()
        self.decision(item, [item["candidates"][0]])
        with patch("src.school.runner.collect_etl", return_value=result(status="auth_required")):
            report = run_once(CONFIG, github=self.github, token="")
        self.assertEqual(report["collectors"]["etl"], "auth_required")
        self.assertEqual(self.github.read_json(QUEUE, "school/decision-results/decision-1.json")[0]["state"], "completed")
        self.assertEqual(len(self.github.public_commits()), 1)


if __name__ == "__main__":
    unittest.main()
