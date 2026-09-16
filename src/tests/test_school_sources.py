from copy import deepcopy
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from src.school.sources import (CollectionError, collect_etl, collect_local, normalized_source,
                                safe_source_url, validated_origin, _pages)

CONFIG = {"term": {"start": "2026-09-01", "end": "2026-12-31"},
          "courses": [{"key": "writing", "name": "대학 글쓰기 1", "folder": "대글1",
                       "aliases": ["College Writing 1"], "canvas_id": None}],
          "etl": {"base_url": "https://myetl.snu.ac.kr"}}
STAMP = "2026-09-14T10:00:00+09:00"


class LocalSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "대글1" / "eTL"
        self.folder.mkdir(parents=True)

    def test_local_notice_summary_and_unsupported_files_are_explicit_without_mutation(self):
        (self.folder / "공지.txt").write_text("9/20 23:59 과제 제출 마감", encoding="utf-8")
        (self.folder / "구형.hwp").write_bytes(b"old binary")
        (self.folder / "자료.zip").write_bytes(b"not extracted")
        (self.folder.parent / "내 과제.pdf").write_bytes(b"student work, not a notice")
        (self.root / "2026-2 일정 정리 (eTL 추출).md").write_text("9/20 과제 마감", encoding="utf-8")
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = collect_local(self.root, CONFIG)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["sources"]), 4)
        self.assertEqual(sum(s["extraction_status"] == "unreadable" for s in result["sources"]), 2)
        summary = next(s for s in result["sources"] if s["course"] == "학기 전체")
        self.assertEqual(summary["extraction_status"], "summary")
        for source in result["sources"]:
            self.assertFalse(Path(source["local_path"]).is_absolute())
            self.assertEqual(source["source_url"], "")
        self.assertEqual(before, {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_normalized_json_notice_keeps_due_but_ignores_student_and_grade_fields(self):
        payload = {"title": "과제 제출", "content": "9/20 23:59 마감", "due_at": "2026-09-20T14:59:00Z",
                   "grades": ["private grade"], "students": ["private student"]}
        (self.folder / "공지.json").write_text(json.dumps(payload), encoding="utf-8")
        source = collect_local(self.root, CONFIG)["sources"][0]
        self.assertEqual(source["title"], "과제 제출")
        self.assertEqual(source["due_at"], payload["due_at"])
        self.assertNotIn("private", source["content"])

    def test_office_package_text_is_read_without_extracting_archive_paths(self):
        xml = '<a:p xmlns:a="urn:test"><a:r><a:t>10/20 중간고사</a:t></a:r></a:p>'
        with zipfile.ZipFile(self.folder / "실습OT.pptx", "w") as package:
            package.writestr("ppt/slides/slide1.xml", xml)
        with zipfile.ZipFile(self.folder / "위험.docx", "w") as package:
            package.writestr("../escape.txt", "should never be written")
            package.writestr("word/document.xml", xml)
        result = collect_local(self.root, CONFIG)
        self.assertIn("10/20 중간고사", next(s for s in result["sources"] if s["title"] == "실습OT.pptx")["content"])
        unsafe = next(s for s in result["sources"] if s["title"] == "위험.docx")
        self.assertEqual(unsafe["extraction_status"], "unreadable")
        self.assertFalse((self.root / "escape.txt").exists())

    def test_limits_do_not_claim_every_file_was_parsed(self):
        (self.folder / "공지.txt").write_text("한" * 100, encoding="utf-8")
        with patch("src.school.sources.MAX_TEXT", 10):
            result = collect_local(self.root, CONFIG)
        self.assertEqual(result["sources"][0]["extraction_status"], "truncated")
        self.assertEqual(len(result["sources"][0]["content"]), 10)
        self.assertEqual(result["status"], "partial")
        with patch("src.school.sources.MAX_FILE_BYTES", 10):
            source = collect_local(self.root, CONFIG)["sources"][0]
        self.assertEqual(source["extraction_status"], "too_large")
        self.assertEqual(source["content"], "")
        with patch("src.school.sources.MAX_TOTAL_TEXT_BYTES", 12):
            source = collect_local(self.root, CONFIG)["sources"][0]
        self.assertEqual(len(source["content"].encode("utf-8")), 12)
        self.assertTrue(source["needs_review"])

    def test_source_identity_stays_fixed_and_content_hash_ignores_timestamp_only_change(self):
        first = normalized_source("local", "writing", "대글1/eTL/공지.txt", "공지", "내용", STAMP)
        later = normalized_source("local", "writing", "대글1/eTL/공지.txt", "공지", "내용", "2026-10-01T00:00:00Z")
        changed = normalized_source("local", "writing", "대글1/eTL/공지.txt", "공지", "수정", STAMP)
        self.assertEqual(first["id"], changed["id"])
        self.assertEqual(first["content_hash"], later["content_hash"])
        self.assertNotEqual(first["content_hash"], changed["content_hash"])


class ApiSourceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict('os.environ', {'SCHEDULE_SCHOOL_CACHE_DIR': temporary.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.calls = []
        self.catalog = [{"id": 123, "name": "대학 글쓰기 1 (043)", "term": {"name": "2026년 2학기"}}]

    def fetch(self, url, headers):
        self.calls.append((url, headers))
        path = urlsplit(url).path
        if path == "/api/v1/courses":
            return self.catalog, {}
        if re.fullmatch(r'/api/v1/courses/\d+', path):
            return {'syllabus_body': ''}, {}
        if path.endswith(('/files', '/pages', '/modules')):
            return [], {}
        if path.endswith("/assignments"):
            return [{"id": 456, "name": "과제 마감", "updated_at": STAMP, "description": "<p>과제 안내</p>",
                     "due_at": "2026-09-20T14:59:00Z", "lock_at": "2026-09-27T14:59:00Z",
                     "unlock_at": "2026-09-10T00:00:00Z", "html_url": "https://evil.test/?token=secret",
                     "submission": {"score": "do not collect"}}], {}
        if path == "/api/v1/announcements":
            return [{"id": 789, "title": "휴강 안내", "posted_at": STAMP,
                     "message": "<p>9/24 휴강</p><script>secret script</script>"}], {}
        if path.endswith("/quizzes"):
            return [{"id": 555, "title": "온라인 퀴즈", "due_at": None, "assignment_id": 456}], {}
        raise AssertionError("Unexpected endpoint")

    def test_no_auth_does_not_call_network_or_report_success(self):
        with patch("src.school.sources._fetch") as fetch:
            result = collect_etl(CONFIG, token="")
            fetch.assert_not_called()
        self.assertEqual(result["status"], "auth_required")

    def test_get_only_allowed_course_resources_and_effective_due_not_lock_or_unlock(self):
        result = collect_etl(CONFIG, token="synthetic-etl-token", fetch=self.fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["sources"]), 3)
        assignment = next(s for s in result["sources"] if s["kind"] == "etl_assignment")
        self.assertEqual(assignment["due_at"], "2026-09-20T14:59:00Z")
        self.assertEqual(assignment["source_url"], "https://myetl.snu.ac.kr/courses/123/assignments/456")
        self.assertNotIn("secret", json.dumps(result))
        self.assertNotIn("do not collect", json.dumps(result))
        self.assertNotIn("synthetic-etl-token", json.dumps(result))
        self.assertTrue(any(parse_qs(urlsplit(url).query).get("override_assignment_dates") == ["true"]
                            for url, _ in self.calls))

    def test_old_semester_and_ambiguous_course_names_are_not_selected(self):
        self.catalog[0]["term"]["name"] = "2026년 1학기"
        result = collect_etl(CONFIG, token="test", fetch=self.fetch)
        self.assertEqual(result["sources"], [])
        self.assertEqual(len(self.calls), 1)
        self.catalog = [dict(self.catalog[0], term={"name": "2026-2"})] * 2
        self.assertEqual(collect_etl(CONFIG, token="test", fetch=self.fetch)["sources"], [])

    def test_host_path_and_query_restrictions_prevent_credential_forwarding(self):
        for link in ("https://evil.test/api/v1/courses?page=2", "https://myetl.snu.ac.kr/api/v1/users/1/grades?page=2",
                     "https://myetl.snu.ac.kr/api/v1/courses?include[]=students&page=2"):
            calls = []
            def fetch(url, headers):
                calls.append(url)
                return self.catalog, {"Link": '<' + link + '>; rel="next"'}
            result = collect_etl(CONFIG, token="never-forward", fetch=fetch)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["status"], "error")
            self.assertNotIn("never-forward", json.dumps(result))

    def test_same_origin_filtered_pagination_and_auth_errors(self):
        original = self.fetch
        def fetch(url, headers):
            if urlsplit(url).path == "/api/v1/courses" and "page=2" not in url:
                self.calls.append((url, headers))
                return [], {"Link": '<' + url + '&page=2>; rel="next"'}
            return original(url, headers)
        self.assertEqual(collect_etl(CONFIG, token="test", fetch=fetch)["status"], "ok")
        def denied(url, headers):
            raise CollectionError("auth_required")
        self.assertEqual(collect_etl(CONFIG, token="test", fetch=denied)["status"], "auth_required")

    def test_canvas_opaque_cursor_and_json_links_can_omit_original_filters(self):
        origin = CONFIG["etl"]["base_url"]
        for suffix in (".json?page=opaque_2-a", "?cursor=YXNkOmFiYw%3D%3D",
                       "?bookmark=next-page", "?opaqueContinuation_2-a"):
            with self.subTest(suffix=suffix):
                calls = []
                next_url = origin + "/api/v1/courses" + suffix
                def fetch(url, headers):
                    calls.append(url)
                    if len(calls) == 1:
                        return [{"id": 1}], {"Link": '<' + next_url + '>; rel="next"'}
                    return [{"id": 2}], {}
                result = _pages(origin, "/api/v1/courses", {"per_page": 100, "include[]": "term"},
                                "synthetic", fetch)
                self.assertEqual(result, [{"id": 1}, {"id": 2}])
                self.assertEqual(calls[1], next_url)

    def test_pagination_rejects_context_changes_metadata_expansion_and_unbounded_cursors(self):
        origin = CONFIG["etl"]["base_url"]
        params = {"per_page": 100, "context_codes[]": "course_123", "start_date": "2026-09-01"}
        unsafe = ["?context_codes[]=course_999&page=2", "?start_date=2025-09-01&page=2",
                  "?include[]=students&page=2", "?include[]=term&page=2",
                  "?access_token=synthetic&page=2", "?as_user_id=123&page=2",
                  "?page=a&page=b", "?page=a%0Ab", "?cursor=" + "x" * 2049,
                  "?page=2#fragment", ".json/../users?page=2", "?grades", "?access_token"]
        for suffix in unsafe:
            with self.subTest(suffix=suffix[:80]):
                calls = []
                def fetch(url, headers):
                    calls.append(url)
                    return [], {"Link": '<' + origin + '/api/v1/announcements' + suffix + '>; rel="next"'}
                with self.assertRaises(CollectionError) as caught:
                    _pages(origin, "/api/v1/announcements", params, "synthetic", fetch)
                self.assertEqual(caught.exception.code, "unsafe_pagination")
                self.assertEqual(len(calls), 1)

    def test_original_include_values_cannot_expand_or_change_on_next_page(self):
        origin = CONFIG["etl"]["base_url"]
        for query in ("include[]=students", "include[]=term&include[]=students", "per_page=500"):
            calls = []
            def fetch(url, headers):
                calls.append(url)
                return [], {"Link": '<' + origin + '/api/v1/courses?' + query + '&page=opaque>; rel="next"'}
            with self.subTest(query=query), self.assertRaises(CollectionError):
                _pages(origin, "/api/v1/courses", {"per_page": 100, "include[]": "term"}, "synthetic", fetch)
            self.assertEqual(len(calls), 1)

    def test_korean_academic_year_labels_and_safe_catalog_name_wrappers(self):
        for term_name in ("2026학년도 2학기", "2026학년도 제2학기", "2026년 제 2 학기"):
            for name in ("[2026-2] 대학 글쓰기 1 (043)",
                         "(2026학년도 제2학기) 대학 글쓰기 1 [043]",
                         "대학 글쓰기 1 (430.447-001)"):
                with self.subTest(term=term_name, name=name):
                    self.catalog = [{"id": 123, "name": name, "term": {"name": term_name}}]
                    result = collect_etl(CONFIG, token="synthetic", fetch=self.fetch)
                    self.assertEqual(result["status"], "ok")
                    self.assertEqual(result["courses"], [{"key": "writing", "canvas_id": "123"}])

    def test_original_name_is_used_when_canvas_name_is_a_nickname(self):
        self.catalog = [{"id": 123, "name": "나의 글쓰기", "original_name": "[2026-2] 대학 글쓰기 1 (043)",
                         "term": {"name": "Default Term", "start_at": None, "end_at": None}}]
        self.assertEqual(collect_etl(CONFIG, token="synthetic", fetch=self.fetch)["status"], "ok")

    def test_actual_snu_bare_semester_prefix_matches_all_six_configured_subjects(self):
        subjects = [
            ("macro", "거시경제이론", "거시경제이론", "001"),
            ("leadership", "공학도의 도전과 리더십 2", "공학도의 도전과 리더십 2", "001"),
            ("em", "기초전자기학 및 연습", "기초전자기학 및 연습", "002"),
            ("logic", "논리설계 및 실험", "논리설계 및 실험", "002"),
            ("writing", "대학 글쓰기 1", "대학 글쓰기 1", "043"),
            ("power", "전력시장이론", "Power System Economics", "001"),
        ]
        config = deepcopy(CONFIG)
        config["courses"] = [{"key": key, "name": name, "aliases": [alias], "canvas_id": None}
                             for key, name, alias, section in subjects]
        self.catalog = []
        for index, (_, _, alias, section) in enumerate(subjects, 123):
            name = f"2026-2 {alias} ({section})"
            self.catalog.append({"id": index, "name": name, "course_code": name,
                                 "original_name": None, "term": {"name": "2026년 2학기"}})
        result = collect_etl(config, token="synthetic", fetch=self.fetch)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["courses"], [{"key": subject[0], "canvas_id": str(index)}
                                            for index, subject in enumerate(subjects, 123)])
        self.assertEqual(len(result["sources"]), 18)

    def test_bare_prefix_retains_semester_and_ambiguous_section_guards(self):
        current = {"id": 123, "name": "2026-2 대학 글쓰기 1 (043)", "term": {"name": "2026년 2학기"}}
        old = {"id": 100, "name": "2025-2 대학 글쓰기 1 (043)", "term": {"name": "2025년 2학기"}}
        self.catalog = [old, current]
        result = collect_etl(CONFIG, token="synthetic", fetch=self.fetch)
        self.assertEqual(result["courses"], [{"key": "writing", "canvas_id": "123"}])
        self.catalog = [dict(old, term={"name": "2026년 2학기"})]
        self.assertEqual(collect_etl(CONFIG, token="synthetic", fetch=self.fetch)["courses"], [])
        self.catalog = [current, dict(current, id=124, name="2026-2 대학 글쓰기 1 (044)")]
        self.assertEqual(collect_etl(CONFIG, token="synthetic", fetch=self.fetch)["courses"], [])
        self.catalog = [dict(current, name="2026-2대학 글쓰기 1 (043)")]
        self.assertEqual(collect_etl(CONFIG, token="synthetic", fetch=self.fetch)["courses"], [])

    def test_term_evidence_is_required_and_conflicting_title_semester_is_rejected(self):
        cases = [
            {"name": "대학 글쓰기 1", "term": {"name": "Default Term", "start_at": None, "end_at": None}},
            {"name": "[2026-1] 대학 글쓰기 1", "term": {"name": "2026학년도 제2학기"}},
            {"name": "대학 글쓰기 1", "original_name": "[2025-2] 대학 글쓰기 1", "term": {"name": "2026-2"}},
            {"name": "대학 글쓰기 1", "course_code": "2025-2-writing", "term": {"name": "2026-2"}},
            {"name": "대학 글쓰기 1 (심화)", "term": {"name": "2026-2"}},
            {"name": "[기초] 대학 글쓰기 1", "term": {"name": "2026-2"}},
        ]
        for item in cases:
            with self.subTest(item=item):
                self.calls.clear()
                self.catalog = [dict(item, id=123)]
                result = collect_etl(CONFIG, token="synthetic", fetch=self.fetch)
                self.assertEqual(result["sources"], [])
                self.assertEqual(result["courses"], [])
                self.assertEqual(len(self.calls), 1)

    def test_malformed_notice_does_not_hide_other_valid_notices(self):
        original = self.fetch
        def fetch(url, headers):
            payload, response_headers = original(url, headers)
            if urlsplit(url).path.endswith("/assignments"):
                payload.insert(0, dict(payload[0], id=999, due_at="missing timezone"))
            return payload, response_headers
        result = collect_etl(CONFIG, token="test", fetch=fetch)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["sources"]), 3)
        self.assertTrue(any(issue["code"] == "invalid_notice_fields" for issue in result["issues"]))

    def test_api_text_limits_are_reported_and_preserve_utf8(self):
        with patch("src.school.sources.MAX_TOTAL_TEXT_BYTES", 3):
            result = collect_etl(CONFIG, token="test", fetch=self.fetch)
        self.assertEqual(result["status"], "partial")
        self.assertLessEqual(sum(len(s["content"].encode("utf-8")) for s in result["sources"]), 3)
        self.assertTrue(any(s["needs_review"] for s in result["sources"]))

    def test_api_coverage_records_all_three_lists_and_preserves_document_coverage(self):
        result = collect_etl(CONFIG, token='synthetic-private-token', fetch=self.fetch)
        coverage = result['coverage']
        self.assertFalse(coverage['api_incomplete'])
        self.assertIn('files', coverage['writing'])
        self.assertEqual(set(coverage['api']['writing']), {'assignments', 'announcements', 'quizzes'})
        for resource in coverage['api']['writing'].values():
            self.assertEqual(resource, {'status': 'ok', 'count': 1, 'collected': 1,
                                        'pages': 1, 'listing_complete': True, 'incomplete': False})
        metadata = json.dumps(coverage['api'])
        for sensitive in ('https:', 'Bearer', 'synthetic-private-token', '과제 안내', 'secret'):
            self.assertNotIn(sensitive, metadata)

    def test_api_coverage_counts_all_pages_only_after_terminal_page(self):
        def fetch(url, headers):
            if urlsplit(url).path.endswith('/quizzes'):
                if 'page=2' in url:
                    return [{'id': 556, 'title': 'Second quiz'}], {}
                return [{'id': 555, 'title': 'First quiz'}], {'Link': '<' + url + '&page=2>; rel="next"'}
            return self.fetch(url, headers)
        result = collect_etl(CONFIG, token='test', fetch=fetch)
        quiz = result['coverage']['api']['writing']['quizzes']
        self.assertEqual(quiz['count'], 2)
        self.assertEqual(quiz['collected'], 2)
        self.assertEqual(quiz['pages'], 2)
        self.assertTrue(quiz['listing_complete'])
        self.assertFalse(quiz['incomplete'])

    def test_failed_later_api_page_retains_lower_bound_count_without_claiming_empty_or_complete(self):
        def fetch(url, headers):
            if urlsplit(url).path.endswith('/quizzes'):
                if 'page=2' in url:
                    raise CollectionError('http_503')
                return [{'id': 555, 'title': 'First quiz'}], {'Link': '<' + url + '&page=2>; rel="next"'}
            return self.fetch(url, headers)
        result = collect_etl(CONFIG, token='test', fetch=fetch)
        quiz = result['coverage']['api']['writing']['quizzes']
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(quiz['status'], 'partial')
        self.assertEqual(quiz['code'], 'http_503')
        self.assertEqual(quiz['count'], 1)
        self.assertEqual(quiz['pages'], 1)
        self.assertEqual(quiz['collected'], 0)
        self.assertFalse(quiz['listing_complete'])
        self.assertTrue(quiz['incomplete'])
        self.assertTrue(result['coverage']['api_incomplete'])
        self.assertEqual(len(result['sources']), 2, 'other successful resources remain collected')

    def test_complete_list_with_invalid_item_reports_missing_extraction_separately(self):
        def fetch(url, headers):
            payload, response_headers = self.fetch(url, headers)
            if urlsplit(url).path.endswith('/assignments'):
                payload.append({'id': 999, 'name': '', 'description': 'invalid title'})
            return payload, response_headers
        result = collect_etl(CONFIG, token='test', fetch=fetch)
        assignment = result['coverage']['api']['writing']['assignments']
        self.assertEqual(assignment['status'], 'partial')
        self.assertEqual(assignment['count'], 2)
        self.assertEqual(assignment['collected'], 1)
        self.assertTrue(assignment['listing_complete'])
        self.assertTrue(assignment['incomplete'])
        self.assertEqual(assignment['code'], 'invalid_notice_fields')

    def test_empty_successful_list_is_distinct_from_unavailable_course_and_authentication(self):
        def fetch(url, headers):
            return ([], {}) if urlsplit(url).path.endswith('/quizzes') else self.fetch(url, headers)
        result = collect_etl(CONFIG, token='test', fetch=fetch)
        empty = result['coverage']['api']['writing']['quizzes']
        self.assertEqual(empty['status'], 'ok')
        self.assertEqual(empty['count'], 0)
        self.assertTrue(empty['listing_complete'])
        self.assertFalse(empty['incomplete'])
        self.catalog = []
        for result in (collect_etl(CONFIG, token='test', fetch=self.fetch), collect_etl(CONFIG, token='')):
            self.assertTrue(result['coverage']['api_incomplete'])
            for resource in result['coverage']['api']['writing'].values():
                self.assertEqual(resource['status'], 'unavailable')
                self.assertFalse(resource['listing_complete'])
                self.assertTrue(resource['incomplete'])

    def test_api_text_truncation_does_not_claim_complete_extraction_despite_complete_listing(self):
        with patch('src.school.sources.MAX_TOTAL_TEXT_BYTES', 3):
            result = collect_etl(CONFIG, token='test', fetch=self.fetch)
        assignment = result['coverage']['api']['writing']['assignments']
        self.assertTrue(assignment['listing_complete'])
        self.assertEqual(assignment['count'], assignment['collected'])
        self.assertEqual(assignment['status'], 'partial')
        self.assertTrue(assignment['incomplete'])
        self.assertTrue(result['coverage']['api_incomplete'])

    def test_signed_urls_are_never_preserved_and_university_origin_validation_is_strict(self):
        self.assertEqual(safe_source_url("https://myetl.snu.ac.kr/courses/1/assignments/2?token=secret#anchor"),
                         "https://myetl.snu.ac.kr/courses/1/assignments/2")
        self.assertEqual(safe_source_url("https://myetl.snu.ac.kr/files/2/download?verifier=secret"), "")
        for url in ("http://myetl.snu.ac.kr", "https://myetl.snu.ac.kr.evil.test", "https://me:secret@myetl.snu.ac.kr",
                    "https://myetl.snu.ac.kr:444", "https://myetl.snu.ac.kr/api"):
            with self.subTest(url=url), self.assertRaises(CollectionError):
                validated_origin(url)


if __name__ == "__main__":
    unittest.main()
