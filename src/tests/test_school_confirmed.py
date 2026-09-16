"""Confirmed choices and source revisions survive autonomous collection.

All data and GitHub revisions are synthetic; no personal schedules are fixtures.
"""
from copy import deepcopy
import unittest
from unittest.mock import patch

from src.school.reconcile import event_hash
from src.school.runner import Importer, INDEX, QUEUE, TARGET, run_once
from src.tests.test_school_runner import CONFIG, KEEP, STAMP, MemoryGitHub, result, source


class ConfirmedSchoolChoicesTests(unittest.TestCase):
    def setUp(self):
        self.github = MemoryGitHub()

    def importer(self, **kwargs):
        importer = Importer(self.github, CONFIG, **kwargs)
        importer.acquire()
        self.addCleanup(importer.release)
        return importer

    def reviewed_source(self, *, cloud=False):
        importer = self.importer(inbox_only=cloud)
        importer.index['collectors']['etl'] = {'initialized': True}
        document = source(extraction_version=4, raw_content_hash='original-document-bytes')
        importer.consume({'etl': result(document)})
        return importer, document, importer.index['items'][0]

    def decision(self, item):
        choices = deepcopy(item['candidates'])
        choices[0]['event']['s'] = 1320
        value = {'version': 1, 'id': 'owner-choice', 'source_id': item['id'],
                 'source_hash': item['content_hash'], 'action': 'approve', 'candidates': choices}
        self.github.seed(QUEUE, 'school/decisions/owner-choice.json', value)
        return value

    def test_legacy_completed_approval_protects_later_api_revision_without_public_backfill(self):
        importer, document, item = self.reviewed_source()
        events = self.github.read_json(TARGET, 'DB/events.json')[0]
        events[-1]['s'] = 1320
        events[-1]['school'].pop('user_confirmed', None)
        events[-1]['school']['managed_hash'] = event_hash(events[-1])
        self.github.seed(TARGET, 'DB/events.json', events)
        item['review'] = {'state': 'completed', 'action': 'approve',
                          'source_hash': document['content_hash'], 'updated_at': STAMP}
        importer.save_index()
        before = deepcopy(events)
        commits = len(self.github.public_commits())
        for day in ('23', '24'):
            changed = source(due_at=f'2026-09-{day}T14:59:00Z', extraction_version=4,
                             raw_content_hash='teacher-revision-' + day)
            importer.consume({'etl': result(changed)})
            importer.ready()
            self.assertEqual(self.github.read_json(TARGET, 'DB/events.json')[0], before)
            current = importer.index['items'][0]
            self.assertEqual(current['state'], 'needs_review')
            self.assertTrue(current['user_confirmed'])
            self.assertTrue(all(not c['auto_eligible'] for c in current['candidates']))
        self.assertEqual(len(self.github.public_commits()), commits)

    def test_later_source_versions_do_not_recreate_an_event_removed_by_owner(self):
        importer, _, item = self.reviewed_source()
        self.assertTrue(item['event_ids'])
        self.github.seed(TARGET, 'DB/events.json', [KEEP])
        commits = len(self.github.public_commits())
        for day in ('23', '24'):
            changed = source(due_at=f'2026-09-{day}T14:59:00Z', extraction_version=4,
                             raw_content_hash='teacher-revision-' + day)
            importer.consume({'etl': result(changed)})
            importer.ready()
            self.assertEqual(self.github.read_json(TARGET, 'DB/events.json')[0], [KEEP])
            current = importer.index['items'][0]
            self.assertEqual(current['state'], 'needs_review')
            self.assertTrue(all(not c['auto_eligible'] for c in current['candidates']))
        self.assertEqual(len(self.github.public_commits()), commits)

    def test_collected_new_source_rejects_queued_old_approval_before_any_db_write(self):
        importer, _, item = self.reviewed_source(cloud=True)
        self.decision(item)
        importer.release()
        changed = source(due_at='2026-09-23T14:59:00Z', extraction_version=4,
                         raw_content_hash='new-document-bytes')
        with patch('src.school.runner.collect_etl', return_value=result(changed)):
            run_once(CONFIG, github=self.github)
        outcome = self.github.read_json(QUEUE, 'school/decision-results/owner-choice.json')[0]
        self.assertEqual(outcome['state'], 'conflict')
        self.assertEqual(self.github.read_json(TARGET, 'DB/events.json')[0], [KEEP])
        self.assertEqual(self.github.public_commits(), [])
        current = self.github.read_json(QUEUE, INDEX)[0]['items'][0]
        self.assertEqual(current['content_hash'], changed['content_hash'])
        self.assertEqual(current['state'], 'needs_review')

    def test_unregistered_approval_survives_parser_only_upgrade_of_same_document(self):
        importer, document, item = self.reviewed_source(cloud=True)
        self.decision(item)
        self.assertNotIn('review', item)
        importer.release()
        upgraded = {**document, 'content_hash': 'new-parser-hash', 'extraction_version': 5,
                    'extraction_upgrade': True}
        with patch('src.school.runner.collect_etl', return_value=result(upgraded)):
            run_once(CONFIG, github=self.github)
        outcome = self.github.read_json(QUEUE, 'school/decision-results/owner-choice.json')[0]
        self.assertEqual(outcome['state'], 'completed')
        events = self.github.read_json(TARGET, 'DB/events.json')[0]
        self.assertEqual(events[0], KEEP)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]['s'], 1320)
        self.assertTrue(events[-1]['school']['user_confirmed'])
        self.assertEqual(len(self.github.public_commits()), 1)

    def test_same_source_uses_edited_approval_before_automatic_ready_candidate(self):
        importer, document, item = self.reviewed_source(cloud=True)
        self.decision(item)
        importer.release()
        with patch('src.school.runner.collect_etl', return_value=result(document)):
            run_once(CONFIG, github=self.github)
        outcome = self.github.read_json(QUEUE, 'school/decision-results/owner-choice.json')[0]
        self.assertEqual(outcome['state'], 'completed')
        events = self.github.read_json(TARGET, 'DB/events.json')[0]
        self.assertEqual(events[-1]['s'], 1320)
        self.assertEqual(len(self.github.public_commits()), 1)


if __name__ == '__main__':
    unittest.main()
