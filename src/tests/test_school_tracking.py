"""Read/archive state must not imply calendar application or complete coverage."""
from copy import deepcopy
from unittest.mock import Mock, patch
import unittest

from src.school.runner import INDEX, QUEUE, run_once, sync_item_status
from src.tests import test_school_runner as fixtures
from src.tests.test_school_runner import CONFIG, source, result


class TrackingTests(unittest.TestCase):
    setUp = fixtures.SchoolRunnerTests.setUp
    importer = fixtures.SchoolRunnerTests.importer
    decision = fixtures.SchoolRunnerTests.decision

    def import_source(self, importer, document):
        importer.consume({'etl': result(document)})
        return importer.index['items'][0]
    def test_read_then_later_approval_keeps_candidate_and_separate_application(self):
        importer = self.importer()
        item = self.import_source(importer, source())
        self.decision(item, action='ignore')
        importer.decisions()
        item = importer.index['items'][0]
        self.assertEqual(item['acknowledgement']['state'], 'read')
        self.assertEqual(item['application']['state'], 'not_applied')
        self.assertTrue(item['candidates'])
        self.decision(item, identifier='later-approval')
        importer.decisions()
        self.assertEqual(item['application']['state'], 'applied')
        self.assertEqual(item['acknowledgement']['state'], 'read')
        self.assertTrue(item['user_confirmed'])

    def test_partial_success_never_advances_full_success_or_discards_missing_source(self):
        importer = self.importer()
        importer.consume({'etl': result(source())})
        complete = importer.index['collectors']['etl']['last_complete_success']
        items = deepcopy(importer.index['items'])
        with patch('src.school.runner.stamp', return_value='2026-09-17T01:00:00Z'):
            importer.consume({'etl': result(status='partial')})
        status = importer.index['collectors']['etl']
        self.assertEqual(status['last_complete_success'], complete)
        self.assertEqual(status['last_usable_success'], '2026-09-17T01:00:00Z')
        self.assertEqual(importer.index['items'], items)
        importer.consume({'etl': result(status='auth_required')})
        self.assertEqual(importer.index['collectors']['etl']['last_complete_success'], complete)

    def test_legacy_partial_timestamp_does_not_become_complete_success(self):
        importer = self.importer()
        importer.index['collectors']['etl'] = {'state': 'partial', 'last_success': '2026-09-15'}
        importer.consume({'etl': result(status='auth_required')})
        status = importer.index['collectors']['etl']
        self.assertIsNone(status['last_complete_success'])
        self.assertEqual(status['last_usable_success'], '2026-09-15')

    def test_archive_disk_failure_remains_visible_without_losing_read_decision(self):
        importer = self.importer()
        item = self.import_source(importer, source())
        importer.release()
        self.decision(item, action='ignore')
        archive = Mock()
        archive.record.side_effect = OSError('synthetic unavailable disk')
        with patch('src.school.runner.collect_etl', return_value=result(source())):
            report = run_once(CONFIG, github=self.github, archive=archive)
        self.assertEqual(report['status'], 'retry_pending')
        index, _ = self.github.read_json(QUEUE, INDEX)
        self.assertEqual(index['local_archive']['state'], 'error')
        self.assertEqual(index['items'][0]['acknowledgement']['state'], 'read')
        self.assertEqual(index['items'][0]['application']['state'], 'not_applied')

    def test_acknowledgement_is_bound_to_exact_source_version(self):
        item = {'content_hash': 'new', 'state': 'ignored', 'candidates': [{}],
                'acknowledgement': {'state': 'read', 'source_hash': 'old'}}
        sync_item_status(item)
        self.assertEqual(item['acknowledgement'], {'state': 'unread', 'source_hash': 'new'})
        self.assertEqual(item['application']['state'], 'not_applied')

    def test_lease_retry_keeps_full_archived_comparison_after_archive_already_advanced(self):
        importer = self.importer()
        original = source()
        self.import_source(importer, original)
        revised = source(content='수정된 과제 안내')
        changes = {'previous_hash': original['content_hash'], 'current_hash': revised['content_hash'],
                   'added': [{'line': 20, 'text': '키워드 발췌 밖의 변경 문구'}], 'removed': [], 'limited': False}
        # The first pass saved the archive but failed to reserve the queue.
        # Its retry sees an unchanged archive and an older remote inbox.
        importer.consume({'etl': result(revised)}, {revised['id']: {'state': 'unchanged', 'changes': changes}})
        self.assertEqual(importer.index['items'][0]['changes'], changes)


if __name__ == '__main__':
    unittest.main()
