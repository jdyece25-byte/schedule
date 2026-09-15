"""Private inbox/public DB CAS and crash recovery, using immutable fake revisions."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import unittest
from unittest.mock import patch

from src.bridge.github import GitHubError
from src.school.reconcile import digest, prepare
from src.school.runner import CollectorBusy, Importer, INDEX, LEASE, QUEUE, TARGET, run_once
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
        cloud.index['collectors']['etl'] = {'initialized': True}
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

    def test_first_etl_connection_keeps_existing_schedule_and_reviews_imported_deadlines(self):
        importer = self.importer()
        initial = source()
        importer.consume({'etl': result(initial)})
        item = importer.index['items'][0]
        self.assertEqual(item['state'], 'needs_review')
        self.assertTrue(item['initial_review'])
        self.assertFalse(item['notify'])
        self.assertFalse(item['candidates'][0]['auto_eligible'])
        importer.ready()
        changed = deepcopy(initial)
        changed['content_hash'] = 'new-initial-source-version'
        importer.consume({'etl': result(changed)})
        self.assertFalse(importer.index['items'][0]['candidates'][0]['auto_eligible'])
        self.assertTrue(importer.index['items'][0]['notify'])
        self.assertEqual(self.github.public_commits(), [])
        self.assertEqual(self.github.read_json(TARGET, 'DB/events.json')[0], [KEEP])

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
        importer.index['collectors']['etl'] = {'initialized': True}
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

    def test_cloud_decision_only_resolves_ignore_without_collecting_or_public_write(self):
        item = self.make_review_item()
        self.decision(item, action='ignore')
        with patch('src.school.runner.collect_etl', side_effect=AssertionError('must not collect')):
            report = run_once(CONFIG, collect=False, github=self.github, inbox_only=True)
        outcome = self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0]
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(outcome['state'], 'completed')
        self.assertEqual(outcome['source_id'], item['id'])
        self.assertEqual(outcome['source_hash'], item['content_hash'])
        self.assertEqual(outcome['action'], 'ignore')
        self.assertEqual(self.github.read_json(QUEUE, INDEX)[0]['items'][0]['review']['state'], 'completed')
        self.assertEqual(self.github.public_commits(), [])

    def test_cloud_acknowledges_approval_but_keeps_it_pending_for_pc(self):
        item = self.make_review_item()
        self.decision(item)
        cloud = self.importer(inbox_only=True)
        self.assertTrue(cloud.decisions())
        current = self.github.read_json(QUEUE, INDEX)[0]['items'][0]
        self.assertEqual(current['review']['state'], 'queued')
        self.assertEqual(current['state'], 'needs_review')
        self.assertIsNone(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0])
        self.assertFalse(cloud.decisions())
        self.assertEqual(self.github.public_commits(), [])
        cloud.release()
        self.importer().decisions()
        self.assertEqual(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0]['state'], 'completed')

    def test_transient_approval_failure_does_not_block_later_ignore_or_create_conflict(self):
        item = self.make_review_item()
        self.decision(item, identifier='a-approval')
        other = deepcopy(item); other['id'] = 'other-source'
        index = self.github.read_json(QUEUE, INDEX)[0]; index['items'].append(other)
        self.github.seed(QUEUE, INDEX, index)
        self.decision(other, identifier='b-ignore', action='ignore')
        importer = self.importer()
        with patch.object(importer, 'publish', side_effect=GitHubError(503, 'provider response must stay private')):
            importer.decisions()
        self.assertTrue(importer.retry_pending)
        self.assertIsNone(self.github.read_json(QUEUE, 'school/decision-results/a-approval.json')[0])
        self.assertEqual(self.github.read_json(QUEUE, 'school/decision-results/b-ignore.json')[0]['state'], 'completed')
        self.assertEqual(importer.index['items'][0]['state'], 'needs_review')

    def test_terminal_outcome_is_durable_before_a_later_decision_read_fails(self):
        item = self.make_review_item()
        self.decision(item, identifier='a-ignore', action='ignore')
        self.decision(item, identifier='b-ignore', action='ignore')
        importer = self.importer()
        real_read = self.github.read_json
        def read(repo, path, ref='main'):
            if path == 'school/decisions/b-ignore.json':
                raise GitHubError(401, 'auth failed')
            return real_read(repo, path, ref)
        with patch.object(self.github, 'read_json', side_effect=read):
            with self.assertRaises(GitHubError):
                importer.decisions()
        self.assertEqual(real_read(QUEUE, 'school/decision-results/a-ignore.json')[0]['state'], 'completed')

    def test_lease_reservation_cas_race_is_busy_not_workflow_failure(self):
        other = {'owner': 'other-owner', 'until': '2099-01-01T00:00:00Z'}
        real_put = self.github.put_json
        def put(repo, path, value, sha=None, **kwargs):
            if path == LEASE:
                self.github.seed(QUEUE, LEASE, other)
                raise GitHubError(409, 'race')
            return real_put(repo, path, value, sha, **kwargs)
        with patch.object(self.github, 'put_json', side_effect=put):
            with self.assertRaises(CollectorBusy):
                run_once(CONFIG, collect=False, github=self.github)
        self.assertEqual(self.github.read_json(QUEUE, LEASE)[0], other)

    def test_lost_lease_reservation_response_recovers_ownership_and_releases(self):
        self.github.lose_response_for = QUEUE
        report = run_once(CONFIG, collect=False, github=self.github)
        self.assertEqual(report['status'], 'ok')
        self.assertIsNone(self.github.read_json(QUEUE, LEASE)[0]['owner'])

    def test_collection_reads_do_not_hold_the_decision_lease(self):
        def collect(*args, **kwargs):
            self.assertIsNone(self.github.read_json(QUEUE, LEASE)[0])
            return result()
        with patch('src.school.runner.collect_etl', side_effect=collect):
            run_once(CONFIG, github=self.github)

    def test_partial_approval_review_receipt_preserves_remaining_count(self):
        item = self.make_review_item()
        self.decision(item, [item['candidates'][0]])
        importer = self.importer(); importer.decisions()
        outcome = self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0]
        self.assertEqual(outcome['remaining_count'], 1)
        self.assertEqual(importer.index['items'][0]['review']['remaining_count'], 1)

    def test_analysis_upgrade_enriches_acknowledged_sources_without_reopening_or_db_writes(self):
        for state in ('ignored', 'applied', 'baseline'):
            with self.subTest(state=state):
                api = MemoryGitHub()
                document = source()
                old = {'id': document['id'], 'content_hash': document['content_hash'], 'state': state,
                       'candidates': [], 'event_ids': ['manual-existing'], 'first_seen_at': STAMP,
                       'review': {'state': 'completed', 'source_hash': document['content_hash']}}
                api.seed(QUEUE, INDEX, {'version': 1, 'collectors': {'etl': {'initialized': True}}, 'items': [old]})
                importer = Importer(api, CONFIG); importer.acquire()
                try:
                    importer.consume({'etl': result(document)})
                    current = importer.index['items'][0]
                    self.assertEqual(current['state'], state)
                    self.assertEqual(current['event_ids'], old['event_ids'])
                    self.assertEqual(current['review'], old['review'])
                    self.assertEqual(current['candidates'], [])
                    self.assertEqual(current['analysis_version'], 2)
                    self.assertIn('excerpts', current['knowledge'])
                    self.assertTrue(current['notify'])  # Same-version migration preserves prior eligibility.
                    from src.notifications.scheduler import school_notices
                    self.assertEqual(school_notices(importer.index, datetime.now(timezone.utc)), [])
                    self.assertEqual(api.public_commits(), [])
                finally:
                    importer.release()

    def test_new_historical_files_require_review_and_do_not_replay_notifications(self):
        importer = self.importer()
        importer.index['collectors']['etl'] = {'initialized': True, 'last_checked': '2026-09-15T00:00:00Z'}
        document = source(kind='etl_file', content='9/22 23:59 HW1 제출 마감', updated_at=STAMP)
        importer.consume({'etl': result(document)})
        current = importer.index['items'][0]
        self.assertTrue(current['initial_review'])
        self.assertFalse(current['notify'])
        self.assertTrue(all(not c['auto_eligible'] for c in current['candidates']))
        self.assertEqual(self.github.public_commits(), [])

    def test_parser_changed_hash_keeps_ignored_choice_but_a_real_new_version_can_notify(self):
        importer = self.importer()
        importer.index['collectors']['etl'] = {'initialized': True}
        original = source(extraction_version=4, raw_content_hash='original-bytes')
        importer.consume({'etl': result(original)})
        importer.index['items'][0]['state'] = 'ignored'
        upgraded = {**original, 'content_hash': 'parser-hash', 'extraction_upgrade': True, 'extraction_version': 5}
        importer.consume({'etl': result(upgraded)})
        self.assertEqual(importer.index['items'][0]['state'], 'ignored')
        self.assertTrue(importer.index['items'][0]['notify'])
        self.assertEqual(importer.index['items'][0]['notice_hash'], original['content_hash'])
        actual = {**upgraded, 'content_hash': 'real-new-version', 'raw_content_hash': 'new-bytes', 'content': 'New teacher announcement'}
        importer.consume({'etl': result(actual)})
        self.assertTrue(importer.index['items'][0]['notify'])
        self.assertEqual(importer.index['items'][0]['notice_hash'], actual['content_hash'])

    def test_same_hash_active_analysis_migration_preserves_a_real_undelivered_notification(self):
        document = source()
        first_seen = datetime.now(timezone.utc).isoformat()
        old = {'id': document['id'], 'content_hash': document['content_hash'], 'state': 'needs_review',
               'candidates': [], 'event_ids': [], 'first_seen_at': first_seen, 'notify': True}
        self.github.seed(QUEUE, INDEX, {'version': 1, 'collectors': {'etl': {'initialized': True}}, 'items': [old]})
        importer = self.importer(); importer.consume({'etl': result(document)})
        self.assertTrue(importer.index['items'][0]['notify'])
        self.assertEqual(importer.index['items'][0]['first_seen_at'], first_seen)
        from src.notifications.scheduler import school_notices
        self.assertEqual(len(school_notices(importer.index, datetime.now(timezone.utc))), 1)

    def test_queued_approval_keeps_its_source_contract_during_parser_upgrade_until_pc_applies(self):
        item = self.make_review_item()
        self.decision(item)
        cloud = self.importer(inbox_only=True); cloud.decisions()
        original = deepcopy(cloud.index['items'][0])
        enhanced = {**source(kind='local'), 'id': item['id'], 'content_hash': 'upgraded-parser-hash',
                    'extraction_version': 5, 'raw_content_hash': 'legacy-original-bytes'}
        cloud.consume({'local': result(enhanced)})
        self.assertEqual(cloud.index['items'][0], original)
        self.assertIsNone(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0])
        cloud.release()
        pc = self.importer(); pc.decisions()
        self.assertEqual(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0]['state'], 'completed')
        self.assertEqual(len(self.github.public_commits()), 1)
        pc.consume({'local': result(enhanced)})
        self.assertEqual(pc.index['items'][0]['state'], 'applied')
        self.assertEqual(pc.index['items'][0]['extraction_version'], 5)

    def test_pending_explicit_approval_blocks_automatic_ready_fallback_after_transient_failure(self):
        item = self.make_review_item()
        item['state'] = 'ready'
        for candidate in item['candidates']:
            candidate['auto_eligible'] = True
        self.github.seed(QUEUE, INDEX, {'version': 1, 'collectors': {}, 'items': [item]})
        self.decision(item, [item['candidates'][0]])
        importer = self.importer()
        with patch.object(importer, 'publish', side_effect=GitHubError(503, 'temporary')) as publish:
            importer.decisions()
            importer.ready()
        self.assertEqual(publish.call_count, 1, 'ready must not apply the unedited automatic candidate')
        self.assertTrue(importer.retry_pending)
        self.assertEqual(self.github.read_json(QUEUE, INDEX)[0]['items'][0]['review']['state'], 'queued')
        self.assertIsNone(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0])
        self.assertEqual(self.github.public_commits(), [])

    def test_new_raw_bytes_are_not_silenced_even_when_parser_version_changes_together(self):
        importer = self.importer(inbox_only=True)
        original = source(kind='local', extraction_version=4, raw_content_hash='old-bytes')
        importer.consume({'local': result(original)})
        self.assertEqual(importer.index['items'][0]['state'], 'baseline')
        changed = {**original, 'content_hash': 'new-hash', 'raw_content_hash': 'new-bytes', 'extraction_version': 5, 'extraction_upgrade': True}
        importer.consume({'local': result(changed)})
        self.assertNotEqual(importer.index['items'][0]['state'], 'baseline')
        self.assertTrue(importer.index['items'][0]['notify'])

    def test_changed_parser_hash_preserves_undelivered_notice_and_original_delivery_key(self):
        from src.notifications.scheduler import school_notices
        document = source(kind='local', extraction_version=4, raw_content_hash='same-original-bytes')
        first_seen = datetime.now(timezone.utc).isoformat()
        old = {'id': document['id'], 'content_hash': document['content_hash'], 'state': 'needs_review',
               'course': '논리설계 및 실험', 'candidates': [], 'event_ids': [], 'first_seen_at': first_seen,
               'notify': True, 'extraction_version': 4, 'raw_content_hash': 'same-original-bytes', 'analysis_version': 2}
        self.github.seed(QUEUE, INDEX, {'version': 1, 'collectors': {'local': {'initialized': True}}, 'items': [old]})
        before = school_notices({'version': 1, 'items': [old]}, datetime.now(timezone.utc))[0]
        upgraded = {**document, 'content_hash': 'parser-v5-hash', 'extraction_version': 5}
        importer = self.importer(inbox_only=True); importer.consume({'local': result(upgraded)})
        after = school_notices(importer.index, datetime.now(timezone.utc))[0]
        self.assertEqual(before['id'], after['id'])
        self.assertEqual(before['due'], after['due'])
        self.assertTrue(importer.index['items'][0]['notify'])
        # Successive parser migrations keep the original key rather than the
        # immediately preceding parser hash. Real bytes changes get a new key.
        importer.consume({'local': result({**upgraded, 'content_hash': 'parser-v6-hash', 'extraction_version': 6})})
        self.assertEqual(school_notices(importer.index, datetime.now(timezone.utc))[0]['id'], before['id'])
        importer.consume({'local': result({**upgraded, 'content_hash': 'teacher-new-content', 'raw_content_hash': 'new-bytes'})})
        self.assertNotEqual(school_notices(importer.index, datetime.now(timezone.utc))[0]['id'], before['id'])

    def test_empty_failed_read_preserves_verified_data_and_clears_diagnostic_on_same_hash_recovery(self):
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        good = source(kind='etl_file', content='9/22 보고서 제출 마감', extraction_version=5, raw_content_hash='verified-bytes')
        importer.consume({'etl': result(good)})
        current = importer.index['items'][0]
        current.update(state='ignored', review={'state': 'completed', 'source_hash': good['content_hash'], 'action': 'ignore'})
        verified = deepcopy(current)
        raw_path = 'school/sources/' + good['id'] + '.json'
        for status in ('download_budget', 'download_failed', 'no_text', 'truncated'):
            failed = {**good, 'content_hash': 'empty-' + status, 'content': '', 'extraction_status': status, 'raw_content_hash': ''}
            importer.consume({'etl': result(failed, status='partial')})
            for key in ('content_hash', 'knowledge', 'candidates', 'state', 'review', 'first_seen_at', 'notify', 'notice_hash'):
                self.assertEqual(current[key], verified[key], key)
            self.assertFalse(current['read_status']['stale'])
            self.assertEqual(current['read_status']['state'], status)
            self.assertEqual(self.github.read_json(QUEUE, raw_path)[0], good)
            self.assertEqual(importer.index['collectors']['etl']['state'], 'partial')
        importer.consume({'etl': result(good)})
        recovered = importer.index['items'][0]
        self.assertNotIn('read_status', recovered)
        self.assertNotIn('diagnostic', recovered)
        self.assertEqual(recovered['knowledge'], verified['knowledge'])
        self.assertEqual(recovered['state'], 'ignored')
        self.assertEqual(self.github.public_commits(), [])

    def test_changed_metadata_failed_read_marks_old_knowledge_stale_until_normal_recovery(self):
        from src.bridge.school_knowledge import build_school_context
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        good = source(kind='etl_file', content='9/22 보고서 제출 마감', extraction_version=5, raw_content_hash='verified-bytes')
        importer.consume({'etl': result(good)})
        current = importer.index['items'][0]
        current.update(state='ignored', review={'state': 'completed', 'source_hash': good['content_hash'], 'action': 'ignore'})
        verified = deepcopy(current)
        failed = {**good, 'content_hash': 'unreadable-file-hash', 'content': '', 'extraction_status': 'download_failed',
                  'updated_at': '2026-09-16T00:00:00Z', 'title': '수정된 보고서 안내'}
        importer.consume({'etl': result(failed, status='partial')})
        current = importer.index['items'][0]
        self.assertEqual(current['state'], 'needs_review'); self.assertEqual(current['candidates'], [])
        self.assertNotEqual(current['content_hash'], verified['content_hash']); self.assertNotIn('review', current)
        self.assertTrue(current['read_status']['stale']); self.assertTrue(current['knowledge']['incomplete'])
        self.assertIn('날짜·시각 변경 근거로 사용하지 마세요', current['knowledge']['excerpts'][0])
        self.assertEqual(current['knowledge']['excerpts'][1:], verified['knowledge']['excerpts'])
        context = build_school_context(importer.index, {'text': '논설 일정 알려줘'})
        self.assertTrue(context['incomplete'])
        self.assertIn('날짜·시각 변경 근거로 사용하지 마세요', str(context['sources']))
        observed_hash = current['content_hash']; first_seen = current['first_seen_at']
        current['state'] = 'ignored'
        importer.consume({'etl': result({**failed, 'extraction_status': 'download_budget'}, status='partial')})
        self.assertEqual(current['content_hash'], observed_hash); self.assertEqual(current['first_seen_at'], first_seen)
        self.assertEqual(current['state'], 'ignored')
        self.assertEqual(len(current['knowledge']['excerpts']), len(verified['knowledge']['excerpts']) + 1)
        recovered = source(kind='etl_file', content='9/23 보고서 제출 마감', extraction_version=5,
                           raw_content_hash='new-verified-bytes', title=failed['title'], updated_at=failed['updated_at'])
        importer.consume({'etl': result(recovered)})
        current = importer.index['items'][0]
        self.assertNotIn('knowledge_stale', current); self.assertNotIn('read_previous', current)
        self.assertEqual(current['knowledge']['status'], 'parsed')
        self.assertNotIn('이전 원문 참고', str(current['knowledge']))
        self.assertEqual(current['content_hash'], recovered['content_hash'])
        self.assertEqual(self.github.public_commits(), [])

    def test_metadata_only_failure_recovers_original_acknowledgement_if_verified_content_is_unchanged(self):
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        good = source(kind='etl_file', content='9/22 보고서 제출 마감')
        importer.consume({'etl': result(good)})
        importer.index['items'][0].update(state='ignored', reason='사용자 확인')
        verified = deepcopy(importer.index['items'][0])
        failed = {**good, 'content_hash': 'bad', 'content': '', 'updated_at': '2026-09-16T00:00:00Z', 'extraction_status': 'download_failed'}
        importer.consume({'etl': result(failed, status='partial')})
        importer.consume({'etl': result({**good, 'updated_at': failed['updated_at']})})
        current = importer.index['items'][0]
        self.assertEqual(current['state'], 'ignored'); self.assertEqual(current['knowledge'], verified['knowledge'])
        self.assertEqual(current['content_hash'], verified['content_hash'])
        self.assertNotIn('read_status', current); self.assertNotIn('knowledge_stale', current)

    def test_changed_unreadable_source_invalidates_old_approval_before_collection_pass_can_publish(self):
        item = self.make_review_item()
        item['knowledge'] = {'version': 2, 'status': 'parsed', 'incomplete': False, 'excerpts': ['10/1 보고서 제출']}
        self.github.seed(QUEUE, INDEX, {'version': 1, 'collectors': {}, 'items': [item]})
        self.decision(item)
        failed = {**source(kind='local'), 'id': item['id'], 'title': '변경된 학교 원문', 'content_hash': 'failed-new',
                  'content': '', 'updated_at': '2026-09-16T00:00:00Z', 'extraction_status': 'download_failed'}
        with patch('src.school.runner.collect_etl', return_value=result(failed, status='partial')):
            run_once(CONFIG, github=self.github)
        self.assertEqual(self.github.public_commits(), [])
        self.assertEqual(self.github.read_json(QUEUE, 'school/decision-results/decision-1.json')[0]['state'], 'conflict')
        self.assertTrue(self.github.read_json(QUEUE, INDEX)[0]['items'][0]['knowledge']['incomplete'])

    def test_prior_term_empty_source_never_silently_keeps_old_knowledge_as_current(self):
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        good = source(kind='etl_syllabus', content='9/22 시험 안내')
        importer.consume({'etl': result(good)})
        importer.consume({'etl': result({**good, 'content': '', 'content_hash': 'old-term', 'extraction_status': 'prior_term', 'term_conflict': True}, status='partial')})
        current = importer.index['items'][0]
        self.assertTrue(current['read_status']['stale'])
        self.assertEqual(current['knowledge']['status'], 'prior_term')
        self.assertEqual(current['candidates'], [])

    def test_historical_references_keep_knowledge_in_history_but_announcements_stay_actionable(self):
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        documents = [source(kind=kind, content='출석 정책을 설명하는 강의 참고 자료', historical_import=True) for kind in ('etl_file', 'etl_page', 'etl_module', 'etl_syllabus', 'etl_announcement')]
        importer.consume({'etl': result(*documents, status='partial')})
        for item in importer.index['items']:
            self.assertTrue(item['knowledge']['excerpts'])
            self.assertEqual(item['state'], 'info' if item['source_kind'] == 'etl_announcement' else 'baseline')
        self.assertEqual(importer.index['collectors']['etl']['state'], 'partial')

    def test_existing_historical_reference_info_migrates_on_same_hash_but_true_new_version_reappears(self):
        importer = self.importer(inbox_only=True)
        importer.index['collectors']['etl'] = {'initialized': True}
        old = source(kind='etl_file', content='출석 정책 설명', historical_import=True, extraction_version=5, raw_content_hash='old-bytes')
        importer.consume({'etl': result(old)})
        importer.index['items'][0]['state'] = 'info'  # Earlier deployment left reference imports actionable.
        importer.consume({'etl': result(old)})
        self.assertEqual(importer.index['items'][0]['state'], 'baseline')
        changed = source(kind='etl_file', content='출석 정책이 변경되었습니다', historical_import=True, extraction_version=5, raw_content_hash='new-bytes')
        importer.consume({'etl': result(changed)})
        self.assertEqual(importer.index['items'][0]['state'], 'info')
        self.assertTrue(importer.index['items'][0]['notify'])
        importer.consume({'etl': result(changed)})
        self.assertEqual(importer.index['items'][0]['state'], 'info')
        self.assertEqual(importer.index['items'][0]['content_hash'], changed['content_hash'])
        current = importer.index['items'][0]
        current.update(review={'state': 'queued', 'source_hash': changed['content_hash']}, initial_review=True)
        importer.consume({'etl': result(changed)})
        self.assertEqual(current['state'], 'info')


if __name__ == "__main__":
    unittest.main()
