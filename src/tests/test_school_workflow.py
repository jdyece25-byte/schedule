"""Cloud document downloads retain progress across bounded, private runs."""
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import textwrap
import unittest
from unittest.mock import patch

from src.school.documents import DocumentCache


WORKFLOW = Path(__file__).resolve().parents[1] / 'school' / 'collect.yml'


class SchoolWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding='utf-8')

    def section(self, name):
        return self.workflow.split('      - name: ' + name + '\n', 1)[1].split('      - name:', 1)[0]

    def fingerprint(self, root):
        section = self.section('Fingerprint completed document cache')
        code = textwrap.dedent(section.split("python - <<'PY'\n", 1)[1].split('          PY\n', 1)[0])
        output = root.parent / 'synthetic-output.txt'
        output.write_text('', encoding='utf-8')
        environment = {**os.environ, 'SCHEDULE_SCHOOL_CACHE_DIR': str(root), 'RUNNER_OS': 'Linux', 'GITHUB_OUTPUT': str(output)}
        result = subprocess.run([sys.executable, '-B', '-c', code], env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        return output.read_text(encoding='utf-8').strip()

    def test_cache_is_private_collection_only_and_saves_after_failure(self):
        self.assertIn('if: github.event.repository.private == true', self.workflow)
        self.assertIn('SCHEDULE_SCHOOL_CACHE_DIR: ${{ runner.temp }}/schedule-school-documents', self.workflow)
        for name in ('Restore private school document cache', 'Fingerprint completed document cache', 'Save completed private school documents'):
            self.assertIn("github.event_name != 'push'", self.section(name))
        save = self.section('Save completed private school documents')
        self.assertIn('if: always()', save)
        self.assertIn('cache-matched-key', save)
        self.assertIn('restore-keys: school-documents-${{ runner.os }}-v1-', self.section('Restore private school document cache'))

    def test_next_run_reads_remaining_document_and_unchanged_run_reuses_cache_key(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'cache'
            calls = []
            def transport(url, headers, limit):
                calls.append(url)
                return 200, b'9/22 18:00 deadline', {}
            def read(cache, number):
                return cache.read(['synthetic', number], '2026-09-01', 'notice.txt',
                                  f'https://myetl.snu.ac.kr/files/{number}/download?verifier=secret',
                                  'https://myetl.snu.ac.kr', 'synthetic-token')
            with patch('src.school.documents.MAX_DOWNLOADS', 1):
                first = DocumentCache(root, transport=transport)
                self.assertEqual(read(first, 1)['status'], 'parsed')
                self.assertEqual(read(first, 2)['status'], 'download_budget')
                first_key = self.fingerprint(root)
                # A failed later collection still saved the completed records;
                # restoring them gives the next process its entire new budget.
                second = DocumentCache(root, transport=transport)
                self.assertEqual(read(second, 1)['status'], 'parsed')
                self.assertEqual(read(second, 2)['status'], 'parsed')
                second_key = self.fingerprint(root)
                self.assertNotEqual(first_key, second_key)
                third = DocumentCache(root, transport=transport)
                read(third, 1); read(third, 2)
                self.assertEqual(second_key, self.fingerprint(root))
            self.assertEqual(len(calls), 2)
            self.assertRegex(second_key, r'^key=school-documents-Linux-v1-[a-f0-9]{64}$')
            records = ''.join(path.read_text(encoding='utf-8') for path in root.glob('*.json'))
            self.assertNotIn('synthetic-token', records)
            self.assertNotIn('verifier', records)

    def test_failed_setup_without_documents_does_not_publish_empty_cache_key(self):
        with TemporaryDirectory() as temporary:
            self.assertEqual(self.fingerprint(Path(temporary) / 'absent-cache'), '')


if __name__ == '__main__':
    unittest.main()
