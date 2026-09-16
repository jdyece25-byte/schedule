import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.school.archive import ArchiveError, SourceArchive, default_root


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'knowledge'
        self.archive = SourceArchive(self.root)

    def source(self, **changes):
        source = {'id': 'notice-one', 'content_hash': 'revision-one', 'kind': 'etl_announcement',
                  'course': 'logic', 'title': '수업 안내', 'updated_at': '2026-09-16T01:00:00Z',
                  'extraction_status': 'parsed', 'content': '10/20 중간 시험\n보고서는 9/20 제출\n준비물: 노트북',
                  'source_url': 'https://myetl.snu.ac.kr/courses/123/discussion_topics/5'}
        source.update(changes)
        return source

    def test_versions_are_immutable_and_recoverable_after_restart(self):
        first = self.archive.record(self.source())
        first_file = next(self.root.glob('versions/*/*.json'))
        first_bytes = first_file.read_bytes()
        second = self.archive.record(self.source(content_hash='revision-two', content='10/21 중간 시험\n9/23 휴강'))
        self.assertEqual(first['state'], 'new')
        self.assertEqual(second['state'], 'changed')
        self.assertEqual(first_file.read_bytes(), first_bytes)
        restarted = SourceArchive(self.root)
        old = restarted.read('notice-one', 'revision-one')['version']['source']
        self.assertIn('10/20', old['content'])
        self.assertIn('10/21', restarted.read('notice-one')['version']['source']['content'])
        self.assertEqual(restarted.summary()['version_count'], 2)
        self.assertFalse(restarted.summary()['calendar_modified'])

    def test_evidence_is_categorized_with_exact_source_lines(self):
        result = self.archive.record(self.source())
        evidence = result['evidence']
        self.assertEqual([(x['category'], x['line_start']) for x in evidence],
                         [('exam', 1), ('deadline', 2), ('preparation', 3)])
        self.assertTrue(all(x['source_hash'] == 'revision-one' for x in evidence))
        revised = self.archive.record(self.source(content_hash='revision-two', content='9/23 휴강\n시험은 추후 공지'))
        self.assertEqual(revised['evidence'][0]['category'], 'cancellation')
        self.assertNotIn('date', revised['evidence'][0])

    def test_changed_text_and_due_metadata_are_compared(self):
        self.archive.record(self.source(due_at='2026-09-20T14:59:00Z'))
        record = self.archive.record(self.source(content_hash='revision-two', content='10/21 중간 시험',
                                                 due_at='2026-09-21T14:59:00Z'))
        changes = record['changes']
        self.assertEqual(changes['previous_hash'], 'revision-one')
        self.assertEqual(changes['current_hash'], 'revision-two')
        self.assertEqual(changes['added'][0]['text'], '10/21 중간 시험')
        self.assertEqual(changes['removed'][0]['line'], 1)
        self.assertIn('due_at', [field['field'] for field in changes['metadata']])

    def test_failed_empty_read_keeps_current_and_records_separate_failure(self):
        self.archive.record(self.source())
        failure = self.archive.record(self.source(content_hash='failed-two', content='', extraction_status='unavailable'))
        self.assertTrue(failure['current_preserved'])
        self.assertEqual(failure['state'], 'read_failed')
        record = self.archive.read('notice-one')
        self.assertEqual(record['version']['source']['content_hash'], 'revision-one')
        self.assertEqual(record['entry']['read_failure']['attempt_hash'], 'failed-two')
        self.assertEqual(self.archive.summary()['read_failure_count'], 1)
        self.assertEqual(self.archive.read('notice-one', 'failed-two')['version']['usable'], False)
        self.archive.record(self.source(content_hash='revision-three', content='10/22 중간 시험'))
        self.assertEqual(self.archive.summary()['read_failure_count'], 0)

    def test_empty_partial_first_read_remains_queryable_as_missing(self):
        self.archive.record(self.source(content='', extraction_status='partial_document'))
        result = self.archive.read('notice-one')
        self.assertIsNone(result['entry']['current_hash'])
        self.assertFalse(result['version']['usable'])
        self.assertIsNotNone(self.archive.query()[0]['read_failure'])

    def test_no_text_rescan_cannot_erase_previously_parsed_attachment(self):
        self.archive.record(self.source(kind='etl_file'))
        result = self.archive.record(self.source(kind='etl_file', content_hash='scan-two', content='', extraction_status='no_text'))
        self.assertTrue(result['current_preserved'])
        self.assertEqual(self.archive.read('notice-one')['version']['source']['content_hash'], 'revision-one')

    def test_nonempty_partial_update_keeps_previous_full_version_available(self):
        self.archive.record(self.source())
        self.archive.record(self.source(content_hash='partial-two', content='10/21 중간 시험', extraction_status='partial_document'))
        latest = self.archive.read('notice-one')['version']
        self.assertEqual(latest['source']['content_hash'], 'partial-two')
        self.assertTrue(latest['evidence'][0]['incomplete'])
        previous = self.archive.read('notice-one', 'revision-one')['version']
        self.assertIn('준비물', previous['source']['content'])
        self.assertFalse(previous['evidence'][0]['incomplete'])

    def test_unchanged_keeps_last_diff_and_does_not_create_duplicate_version(self):
        self.archive.record(self.source())
        revision = self.source(content_hash='revision-two', content='9/23 휴강')
        changed = self.archive.record(revision)
        unchanged = self.archive.record(dict(revision, updated_at='2026-09-17T01:00:00Z'))
        self.assertEqual(unchanged['state'], 'unchanged')
        self.assertEqual(unchanged['changes'], changed['changes'])
        self.assertEqual(self.archive.summary()['version_count'], 2)

    def test_tokens_headers_and_signed_queries_never_persist(self):
        source = self.source(
            content='준비물 https://example.test/file.pdf?token=verysecret&x=3\nAuthorization: Bearer hiddenkey\napi_key=keysecret\ngithub_pat_sensitivevalue\nAuthorization: Basic basicsecret\nCookie: session=cookiesecret; other=anothersecret',
            source_url='https://username:password@myetl.snu.ac.kr/courses/1?access_token=topsecret#secret',
            headers={'Authorization': 'Bearer headersecret'}, token='privatevalue',
            local_path='C:\\Users\\private\\document.pdf')
        self.archive.record(source)
        persisted = '\n'.join(path.read_text(encoding='utf-8') for path in self.root.rglob('*.json'))
        for secret in ('verysecret', 'hiddenkey', 'keysecret', 'github_pat_sensitivevalue', 'topsecret',
                       'headersecret', 'privatevalue', 'username:password', 'C:\\\\Users',
                       'basicsecret', 'cookiesecret', 'anothersecret'):
            self.assertNotIn(secret, persisted)
        self.assertIn('https://example.test/file.pdf', persisted)

    def test_quiz_title_and_structured_due_at_remain_evidence_with_empty_body(self):
        self.archive.record(self.source(kind='etl_quiz', title='Review 3', content='', due_at='2026-09-30T14:59:00Z'))
        result = self.archive.query(category='exam')
        self.assertEqual(result[0]['evidence'][0]['field'], 'title')
        deadline = self.archive.query(category='deadline')[0]['evidence'][0]
        self.assertEqual(deadline['field'], 'due_at')
        self.assertEqual(deadline['text'], '2026-09-30T14:59:00Z')

    def test_nested_coverage_retains_missing_file_and_category_counters(self):
        coverage = {'logic': {'files': {'status': 'ok', 'count': 12},
                              'attachments': {'referenced': 6, 'files_found': 4},
                              'pages': {'status': 'unavailable', 'code': 'forbidden'},
                              'Authorization': 'Bearer private'}}
        result = self.archive.record_collection('etl', {'status': 'partial', 'sources': [], 'coverage': coverage})
        self.assertEqual(result['coverage']['logic']['attachments']['files_found'], 4)
        self.assertEqual(result['coverage']['logic']['pages']['status'], 'unavailable')
        self.assertNotIn('Authorization', result['coverage']['logic'])

    def test_partial_collection_tracks_missing_without_removing_sources(self):
        one, two = self.source(), self.source(id='notice-two', content_hash='other-one')
        self.archive.record(one)
        self.archive.record(two)
        self.archive.record_collection('etl', {'status': 'ok', 'sources': [one, two], 'issues': []})
        success = self.archive.summary()['collectors']['etl']['last_success_at']
        with patch('src.school.archive._stamp', return_value='2026-09-18T01:00:00Z'):
            status = self.archive.record_collection('etl', {'status': 'partial', 'sources': [one],
                                                          'issues': [{'code': 'locked', 'source_id': 'notice-two'}]})
        self.assertEqual(status['last_success_at'], success)
        self.assertEqual(status['last_attempt_at'], '2026-09-18T01:00:00Z')
        self.assertEqual(status['last_usable_at'], '2026-09-18T01:00:00Z')
        self.assertEqual(status['missing_source_ids'], ['notice-two'])
        self.assertFalse(status['missing_is_deletion'])
        self.assertEqual(self.archive.summary()['source_count'], 2)
        self.assertIsNotNone(self.archive.read('notice-two'))

    def test_query_filters_preserved_evidence_by_course_and_category(self):
        self.archive.record(self.source())
        self.archive.record(self.source(id='another-course', course='macro', content_hash='another-hash'))
        result = SourceArchive(self.root).query('중간 시험', course='logic', category='exam')
        self.assertEqual([x['id'] for x in result], ['notice-one'])
        self.assertEqual([x['category'] for x in result[0]['evidence']], ['exam'])
        with self.assertRaises(ArchiveError):
            self.archive.query(category='unknown')

    def test_concurrent_archives_do_not_lose_index_entries(self):
        def save(number):
            return SourceArchive(self.root).record(self.source(id='notice-' + str(number), content_hash='hash-' + str(number)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save, range(12)))
        self.assertEqual(len(results), 12)
        self.assertEqual(self.archive.summary()['source_count'], 12)
        self.assertEqual(len(list(self.root.glob('versions/*/*.json'))), 12)

    def test_explicit_root_and_cli_work_without_importing_project(self):
        self.archive.record(self.source())
        script = Path(__file__).resolve().parents[1] / 'school' / 'archive.py'
        result = subprocess.run([sys.executable, '-B', str(script), '--root', str(self.root), '--course', 'logic', '--json'],
                                cwd=self.temporary.name, capture_output=True, encoding='utf-8', check=True)
        self.assertEqual(json.loads(result.stdout)[0]['id'], 'notice-one')
        with patch.dict('os.environ', {'SCHEDULE_SCHOOL_ARCHIVE_DIR': str(self.root)}):
            self.assertEqual(default_root(), self.root)

    def test_read_only_missing_archive_does_not_create_files(self):
        self.assertEqual(self.archive.summary()['source_count'], 0)
        self.assertFalse(self.root.exists())
        self.assertIsNone(self.archive.read('../unsafe'))

    def test_archive_refuses_git_checkout_even_with_explicit_root(self):
        checkout = Path(self.temporary.name) / 'public-checkout'
        checkout.mkdir()
        (checkout / '.git').write_text('gitdir: somewhere', encoding='utf-8')
        for path in (checkout / 'knowledge', checkout / '.git' / 'knowledge'):
            with self.assertRaisesRegex(ArchiveError, 'archive_git_checkout_forbidden'):
                SourceArchive(path)
        self.assertFalse((checkout / 'knowledge').exists())


if __name__ == '__main__':
    unittest.main()
