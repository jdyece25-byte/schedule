from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
import zipfile

from src.school.sources import (CollectionError, collect_etl, collect_local, normalized_source,
                                safe_source_url, validated_origin)

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
        self.assertEqual(sum(s["extraction_status"] == "unsupported" for s in result["sources"]), 2)
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
        self.calls = []
        self.catalog = [{"id": 123, "name": "대학 글쓰기 1 (043)", "term": {"name": "2026년 2학기"}}]

    def fetch(self, url, headers):
        self.calls.append((url, headers))
        path = urlsplit(url).path
        if path == "/api/v1/courses":
            return self.catalog, {}
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
