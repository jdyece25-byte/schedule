from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import zipfile
import zlib

from src.school.documents import (CollectionError, DocumentCache, collect_documents,
                                  download, hwp_text, _hwp_paragraph)
from src.school.sources import _read_document, digest, normalized_source

ORIGIN = 'https://myetl.snu.ac.kr'
STAMP = '2026-09-01T03:00:00Z'


class DownloadTests(unittest.TestCase):
    def test_school_storage_redirect_gets_no_authorization(self):
        calls = []
        def transport(url, headers, limit):
            calls.append((url, headers))
            if len(calls) == 1:
                return 302, b'', {'Location': 'https://kr.object.gov-ncloudstorage.com/snu-canvas-contents/account_1/files/4?signature=synthetic'}
            return 200, b'%PDF-test', {}
        self.assertEqual(download(ORIGIN + '/files/4/download?verifier=synthetic', ORIGIN, 'fake-token', transport=transport), b'%PDF-test')
        self.assertEqual(calls[0][1]['Authorization'], 'Bearer fake-token')
        self.assertNotIn('Authorization', calls[1][1])

    def test_dropbox_suffix_is_bounded_and_every_hop_is_tokenless(self):
        calls = []
        def transport(url, headers, limit):
            calls.append((url, headers))
            if len(calls) == 1:
                return 302, b'', {'Location': 'https://a1b2.dl.dropboxusercontent.com/cd/document.pdf?signature=synthetic'}
            return 200, b'%PDF-test', {}
        download('https://www.dropbox.com/scl/fi/id/document.pdf?rlkey=synthetic&dl=0', ORIGIN, 'never-send', mode='dropbox', transport=transport)
        self.assertIn('dl=1', calls[0][0])
        self.assertTrue(all('Authorization' not in headers for _, headers in calls))

    def test_unsafe_redirects_and_non_pdf_external_responses_are_rejected(self):
        unsafe = ['https://evil.example/document.pdf', 'http://www.dropbox.com/document.pdf',
                  'https://a.dl.dropboxusercontent.com.evil.example/document.pdf',
                  'https://name:secret@dl.dropboxusercontent.com/document.pdf',
                  'https://dl.dropboxusercontent.com:444/document.pdf']
        for target in unsafe:
            calls = []
            def transport(url, headers, limit):
                calls.append(url); return 302, b'', {'Location': target}
            with self.subTest(target=target), self.assertRaises(CollectionError):
                download('https://www.dropbox.com/a.pdf', ORIGIN, 'synthetic', mode='dropbox', transport=transport)
            self.assertEqual(len(calls), 1)
        with self.assertRaises(CollectionError):
            download('https://www.dropbox.com/a.pdf', ORIGIN, '', mode='dropbox', transport=lambda *args: (200, b'<html>login</html>', {}))
        with self.assertRaises(CollectionError):
            download('https://sugang.snu.ac.kr/sugang/cc/cc103.action?a=b', ORIGIN, '', mode='academic',
                     transport=lambda *args: (302, b'', {'Location': 'https://sugang.snu.ac.kr/'}))

    def test_wrong_school_bucket_and_redirect_loop_are_rejected(self):
        for target in ('https://kr.object.gov-ncloudstorage.com/other-account/4', ORIGIN + '/files/4/download'):
            with self.subTest(target=target), self.assertRaises(CollectionError):
                download(ORIGIN + '/files/4/download', ORIGIN, 'fake', transport=lambda *args: (302, b'', {'Location': target}))


class DocumentReaderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_pdf_prefers_correct_korean_text_and_preserves_page_boundary(self):
        import pymupdf
        path = self.root / 'sample.pdf'
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((50, 50), '주차 9/16 수요일 특별 강연', fontname='korea')
            document.new_page()
            document.save(path)
        text = _read_document(path)
        self.assertIn('주차', text)
        self.assertIn('[PAGE 1]', text)
        self.assertIn('[UNREADABLE PDF PAGES: 2]', text)
        self.assertNotIn('[PAGE 2]', text)

    def test_archives_never_extract_paths_and_report_unsupported_members(self):
        safe = self.root / 'safe.zip'
        with zipfile.ZipFile(safe, 'w') as archive:
            archive.writestr('lecture/notice.txt', '9/16 18:00 lecture')
            archive.writestr('image.png', b'not OCR')
        text = _read_document(safe)
        self.assertIn('9/16 18:00 lecture', text)
        self.assertIn('UNREADABLE ARCHIVE MEMBERS: image.png', text)
        self.assertFalse((self.root / 'lecture').exists())
        unsafe = self.root / 'unsafe.zip'
        with zipfile.ZipFile(unsafe, 'w') as archive:
            archive.writestr('../escape.txt', 'never write')
        with self.assertRaises(ValueError): _read_document(unsafe)
        self.assertFalse((self.root.parent / 'escape.txt').exists())

    def test_legacy_hwp_text_controls_compression_and_protected_files(self):
        visible = '9/16 수요일'.encode('utf-16le')
        tab = struct.pack('<8H', 9, 0, 0, 0, 0, 0, 0, 9)
        paragraph = visible + tab + '18:00 강연'.encode('utf-16le')
        record = struct.pack('<I', 67 | len(paragraph) << 20) + paragraph
        compressed = zlib.compress(record)[2:-4]
        header = bytearray(256); header[:17] = b'HWP Document File'; header[35] = 5
        struct.pack_into('<I', header, 36, 1)
        class Ole:
            def __init__(self, path): pass
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def listdir(self): return [['BodyText', 'Section0']]
            def openstream(self, name): return io.BytesIO(header if name == 'FileHeader' else compressed)
        with patch.dict('sys.modules', {'olefile': types.SimpleNamespace(OleFileIO=Ole)}):
            self.assertIn('9/16 수요일\t18:00 강연', hwp_text(self.root / 'sample.hwp'))
            struct.pack_into('<I', header, 36, 3)
            with self.assertRaises(ValueError): hwp_text(self.root / 'protected.hwp')
        with self.assertRaises(ValueError): _hwp_paragraph(b'\x09\x00')

    def test_cache_skips_repeated_bytes_and_reuses_raw_file_for_parser_upgrade(self):
        calls = []
        def transport(url, headers, limit):
            calls.append(url); return 200, b'9/16 18:00 deadline', {}
        args = (['canvas', 123, 456], [STAMP, 19], 'notice.txt', ORIGIN + '/files/456/download?verifier=first', ORIGIN, 'fake')
        first = DocumentCache(self.root, transport=transport).read(*args)
        cache = DocumentCache(self.root, transport=transport)
        second = cache.read(*args[:3], ORIGIN + '/files/456/download?verifier=changed', *args[4:])
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(cache.hits, 1)
        with patch('src.school.documents.EXTRACTOR_VERSION', 999):
            upgraded = DocumentCache(self.root, transport=transport).read(*args)
        self.assertTrue(upgraded['extraction_upgrade'])
        self.assertEqual(upgraded['file_hash'], first['file_hash'])
        self.assertEqual(len(calls), 1)
        self.assertNotIn('verifier', ''.join(p.read_text() for p in self.root.glob('*.json')))
        self.assertNotIn('fake', ''.join(p.read_text() for p in self.root.glob('*.json')))

    def test_limits_and_unsupported_types_have_explicit_status_without_download(self):
        cache = DocumentCache(self.root, transport=lambda *args: self.fail('No download expected'))
        args = (['canvas', 123, 456], STAMP, 'image.png', ORIGIN + '/files/456/download', ORIGIN, 'fake')
        self.assertEqual(cache.read(*args)['status'], 'unsupported')
        self.assertEqual(cache.read(['large'], STAMP, 'large.pdf', *args[3:], size=21 * 1024 * 1024)['status'], 'too_large')
        with patch('src.school.documents.MAX_DOWNLOADS', 0):
            self.assertEqual(cache.read(['other'], STAMP, 'pending.txt', *args[3:])['status'], 'download_budget')


class CourseCoverageTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.cache = DocumentCache(temporary.name, transport=self.transport)

    def transport(self, url, headers, limit):
        if 'sugang.snu.ac.kr' in url:
            self.assertNotIn('Authorization', headers)
            return 302, b'', {'Location': 'https://sugang.snu.ac.kr/'}
        return 200, '9/16 18:00 과제 제출 마감'.encode(), {}

    def fetch(self, url, headers):
        self.calls.append(url); path = urlsplit(url).path
        if path == '/api/v1/courses/123':
            return {'syllabus_body': '<p>수업계획서 한글 ENG 편집</p><iframe src="https://sugang.snu.ac.kr/sugang/cc/cc103.action?openSchyy=2026"></iframe>'}, {}
        if path == '/api/v1/courses/123/pages': return [{'page_id': 10, 'title': '수업 계획'}], {}
        if path == '/api/v1/courses/123/pages/10':
            return {'page_id': 10, 'url': 'course-plan', 'title': '수업 계획', 'body': '<p>9/16 18:00 과제 제출 마감</p><a href="/courses/123/files/4/download?verifier=private">안내</a>', 'updated_at': STAMP}, {}
        if path == '/api/v1/courses/123/modules':
            return [{'id': 5, 'name': '1주차', 'items_count': 2,
                     'items': [{'id': 6, 'type': 'File', 'content_id': 4},
                               {'id': 7, 'type': 'ExternalTool', 'title': '외부 영상'}]}], {}
        if path == '/api/v1/courses/123/files':
            return [{'id': 4, 'display_name': 'notice.txt', 'size': 100, 'updated_at': STAMP,
                     'url': ORIGIN + '/files/4/download?verifier=private'},
                    {'id': 8, 'display_name': 'locked.pdf', 'locked_for_user': True}], {}
        raise AssertionError(path)

    def test_pages_modules_attachments_and_syllabus_wrappers_have_honest_coverage(self):
        result = collect_documents(ORIGIN, 'course', '123', 'fake-token', self.fetch, self.cache)
        self.assertEqual({s['kind'] for s in result['sources']}, {'etl_syllabus', 'etl_page', 'etl_module', 'etl_file'})
        self.assertEqual(sum(s['external_id'] == '4' for s in result['sources']), 1)
        syllabus = next(s for s in result['sources'] if s['kind'] == 'etl_syllabus')
        self.assertEqual(syllabus['content'], '')
        self.assertEqual(syllabus['extraction_status'], 'download_failed')
        self.assertTrue(syllabus['needs_review'])
        self.assertTrue(any(s['extraction_status'] == 'external_tool' for s in result['sources']))
        self.assertTrue(any(s['extraction_status'] == 'restricted' for s in result['sources']))
        self.assertTrue(all(s['historical_import'] for s in result['sources']))
        self.assertNotIn('verifier', json.dumps(result))
        self.assertNotIn('fake-token', json.dumps(result))
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(result['coverage']['modules']['count'], 1)
        self.assertEqual(result['coverage']['files']['count'], 2)

    def test_pipeline_flags_and_signed_links_do_not_change_content_identity(self):
        first = normalized_source('etl_file', 'course', '123:4', 'notice.txt', '9/16 마감', STAMP,
                                  source_url=ORIGIN + '/courses/123/files/4?verifier=a', historical_import=True, extraction_version=1)
        second = normalized_source('etl_file', 'course', '123:4', 'notice.txt', '9/16 마감', '2026-09-02T00:00:00Z',
                                   source_url=ORIGIN + '/courses/123/files/4?verifier=b', historical_import=False, extraction_version=2, extraction_upgrade=True)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['content_hash'], second['content_hash'])
        self.assertEqual(first['source_url'], ORIGIN + '/courses/123/files/4')

    def test_explicit_prior_semester_file_is_not_presented_as_current_verified_text(self):
        original = self.fetch
        def fetch(url, headers):
            value, response_headers = original(url, headers)
            if urlsplit(url).path.endswith('/files'):
                value = deepcopy(value)
                value[0]['display_name'] = '계획서(2026년 1학기).txt'
            return value, response_headers
        result = collect_documents(ORIGIN, 'course', '123', 'fake', fetch, self.cache,
                                   term={'start': '2026-09-01', 'end': '2026-12-31'})
        source = next(s for s in result['sources'] if s['external_id'] == '4')
        self.assertEqual(source['extraction_status'], 'prior_term')
        self.assertTrue(source['term_conflict'])
        self.assertTrue(source['needs_review'])


if __name__ == '__main__':
    unittest.main()
