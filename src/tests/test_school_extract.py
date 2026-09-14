from copy import deepcopy
import unittest

from src.school.extract import extract_candidates
from src.school.sources import normalized_source

CONFIG = {"term": {"start": "2026-09-01", "end": "2026-12-31"}}
STAMP = "2026-09-14T00:00:00Z"


def source(content="", **kwargs):
    values = dict(kind="etl_announcement", course="logic", external_id="1:2", title="수업 안내",
                  content=content, updated_at=STAMP, extraction_status="parsed")
    values.update(kwargs)
    return normalized_source(**values)


class ExtractTests(unittest.TestCase):
    def test_effective_assignment_due_converts_kst_and_update_preserves_identity(self):
        item = source(kind="etl_assignment", title="Lab01 Report", due_at="2026-09-20T14:59:00Z")
        first = extract_candidates(item, CONFIG)[0]
        self.assertTrue(first["auto_eligible"])
        self.assertEqual(first["event"]["d"], "2026-09-20")
        self.assertEqual(first["event"]["s"], 1439)
        self.assertNotIn("e", first["event"])
        changed = source(kind="etl_assignment", title="Lab01 Report", due_at="2026-09-21T15:00:00Z")
        later = extract_candidates(changed, CONFIG)[0]
        self.assertEqual(first["id"], later["id"])
        self.assertEqual(later["event"]["d"], "2026-09-22")
        self.assertEqual(later["event"]["s"], 0)

    def test_teacher_recommendation_conflict_preserves_review(self):
        item = source(kind="etl_assignment", title="과제2", due_at="2026-09-27T14:59:00Z",
                      content="교수 공지: 밤 11시까지 제출 권장. eTL23:59와 구분")
        proposal = extract_candidates(item, CONFIG)[0]
        self.assertFalse(proposal["auto_eligible"])
        self.assertIn("별도 확인", proposal["reason"])

    def test_unpublished_or_cancelled_assignment_is_not_auto_added(self):
        item = source(kind="etl_assignment", due_at="2026-09-20T14:59:00Z", publication_state="unpublished")
        self.assertEqual(extract_candidates(item, CONFIG), [])
        item = source(kind="etl_assignment", title="취소된 과제", due_at="2026-09-20T14:59:00Z")
        self.assertFalse(extract_candidates(item, CONFIG)[0]["auto_eligible"])

    def test_local_and_quiz_due_values_require_review(self):
        for kind in ("local", "etl_quiz"):
            item = source(kind=kind, due_at="2026-10-01T00:00:00Z")
            self.assertFalse(extract_candidates(item, CONFIG)[0]["auto_eligible"])

    def test_explicit_exam_lab_deadline_and_cancel_dates_are_review_only(self):
        examples = [("10/20 18:30 중간고사", "exam", 1110),
                    ("9월 18일 오후 1시 30분 실습", "lab", 810),
                    ("2026-10-02 13:29 HW1 제출 마감", "deadline", 809),
                    ("9/24 수업 휴강", "cancellation", None)]
        for text, kind, minute in examples:
            with self.subTest(text=text):
                candidate = extract_candidates(source(content=text), CONFIG)[0]
                self.assertEqual(candidate["kind"], kind)
                self.assertEqual(candidate["event"].get("s"), minute)
                self.assertFalse(candidate["auto_eligible"])
                self.assertEqual(candidate["event"]["status"], "tentative")
                self.assertNotIn("no", candidate["event"])

    def test_no_guess_from_week_ranges_invalid_date_or_old_semester(self):
        for text in ("6주차 중간고사", "10/5~10/9 중간고사", "10/5~9일 중간고사", "2/30 10:00 시험",
                     "2025-10-20 18:30 중간고사", "기말고사 15주차", "9/20, 9/27 두 날짜 중 시험"):
            with self.subTest(text=text):
                self.assertEqual(extract_candidates(source(content=text), CONFIG), [])

    def test_open_upload_late_and_mixed_deadline_lines_are_not_deadlines(self):
        for text in ("9/10 21:00 과제 공개", "9/27 23:59 Lab01 지각 제출 잠금", "9/20 강의자료 업로드",
                     "9/20 23:59 제출 마감, 지각 제출 최종 잠금 9/27"):
            with self.subTest(text=text):
                self.assertEqual(extract_candidates(source(content=text), CONFIG), [])

    def test_summary_unsupported_and_uncertain_documents_are_never_auto_confirmed(self):
        for status in ("summary", "unsupported", "unreadable", "too_large", "no_text"):
            item = source(content="9/20 23:59 과제 마감", extraction_status=status)
            self.assertEqual(extract_candidates(item, CONFIG), [])
        item = source(content="10/7 중간고사 유력, 시간 미정")
        candidate = extract_candidates(item, CONFIG)[0]
        self.assertNotIn("s", candidate["event"])
        self.assertIn("추정", candidate["reason"])

    def test_duplicate_lines_are_one_candidate_and_input_is_not_mutated(self):
        item = source(content="10/20 18:30 중간고사\n10/20 18:30 중간고사")
        original = deepcopy(item)
        self.assertEqual(len(extract_candidates(item, CONFIG)), 1)
        self.assertEqual(item, original)


if __name__ == "__main__":
    unittest.main()
