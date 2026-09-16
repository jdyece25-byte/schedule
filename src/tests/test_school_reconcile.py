"""Synthetic school reconciliation regressions; never use personal DB fixtures."""
from copy import deepcopy
import unittest

from src.school.reconcile import apply, belongs, event_hash, prepare


CONFIG = {"term": {"start": "2026-09-01", "end": "2026-12-31"}, "courses": [
    {"key": "logic", "name": "논리설계 및 실험", "aliases": ["논리설계 및 실험", "논리설계", "논설"]},
    {"key": "em", "name": "기초전자기학 및 연습", "aliases": ["기초전자기학 및 연습", "기초전자기학", "기전"]},
]}
SOURCE = {"id": "notice-a", "content_hash": "version-a", "term_start": "2026-09-01", "term_end": "2026-12-31"}


def candidate(**changes):
    return {"id": "candidate-a", "source_id": "notice-a", "source_hash": "version-a",
            "course": "logic", "kind": "deadline", "auto_eligible": True,
            "event": {"d": "2026-09-22", "n": "HW1 제출", "t": "deadline", "s": 1380}, **changes}


def existing(**changes):
    return {"id": "etl-existing-one", "d": "2026-09-22", "n": "논설 HW1 제출",
            "t": "deadline", "s": 1380, "series": "manual-series", "no": "사용자가 작성한 기존 메모", **changes}


class ReconcileTests(unittest.TestCase):
    def apply(self, events, values, approved=False, source=None):
        return apply(events, values, source or SOURCE, approved=approved, config=CONFIG)

    def managed(self):
        original = candidate()
        result, _ = self.apply([], [prepare(original, [], CONFIG)])
        return result[0]

    def test_aliases_keep_same_numbered_assignments_in_different_courses_separate(self):
        other = existing(id="em-one", n="기전 HW1 제출")
        self.assertFalse(belongs(other, "logic", CONFIG))
        prepared = prepare(candidate(), [other], CONFIG)
        self.assertEqual(prepared["action"], "add")
        result, _ = self.apply([other], [prepared])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], other)

    def test_same_assignment_aliases_link_existing_etl_id_without_any_mutation(self):
        original = existing()
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertEqual(prepared["action"], "link")
        result, ids = self.apply([original], [prepared])
        self.assertEqual(result, [original])
        self.assertEqual(ids, [original["id"]])

    def test_existing_manual_time_conflict_requires_review_and_preserves_all_fields(self):
        original = existing(s=1320)
        before = deepcopy(original)
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertEqual(prepared["action"], "update")
        self.assertFalse(prepared["auto_eligible"])
        with self.assertRaises(ValueError):
            self.apply([original], [prepared])
        result, ids = self.apply([original], [prepared], approved=True)
        self.assertEqual(ids, [original["id"]])
        self.assertEqual(result[0]["s"], 1380)
        self.assertEqual(result[0]["no"], before["no"])
        self.assertEqual(result[0]["series"], before["series"])
        self.assertEqual(result[0]["n"], before["n"])
        self.assertEqual(original, before)

    def test_cross_date_assignment_change_does_not_auto_duplicate_manual_etl_event(self):
        original = existing(d="2026-09-21")
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertFalse(prepared["auto_eligible"])
        self.assertEqual(prepared.get("target_id"), original["id"])
        self.assertEqual(prepared["action"], "update")
        with self.assertRaises(ValueError):
            self.apply([original], [prepared])

    def test_recurring_class_on_different_day_is_a_new_occurrence(self):
        for kind in ("class", "lab"):
            original = existing(d="2026-09-21", n="논리설계 및 실험", t=kind, s=570, e=645)
            value = candidate(kind=kind, auto_eligible=False, event={**original, "d": "2026-09-24"})
            prepared = prepare(value, [original], CONFIG)
            self.assertEqual(prepared["action"], "add")
            result, _ = self.apply([original], [prepared], approved=True)
            self.assertEqual(len(result), 2)

    def test_new_api_title_gets_course_prefix_but_existing_alias_is_not_prefixed_twice(self):
        original = candidate()
        before = deepcopy(original)
        prepared = prepare(original, [], CONFIG)
        self.assertEqual(prepared["event"]["n"], "논리설계 및 실험 · HW1 제출")
        self.assertEqual(original, before)
        result, _ = self.apply([], [prepared])
        self.assertEqual(result[0]["n"], prepared["event"]["n"])
        aliased = candidate(event={**candidate()["event"], "n": "논설 HW1 제출"})
        self.assertEqual(prepare(aliased, [], CONFIG)["event"]["n"], aliased["event"]["n"])

    def test_previous_term_assignment_is_not_a_current_term_duplicate(self):
        original = existing(d="2025-09-22")
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertEqual(prepared["action"], "add")
        result, _ = self.apply([original], [prepared])
        self.assertEqual(len(result), 2)

    def test_cross_date_duplicate_check_is_repeated_at_apply_for_concurrent_db_add(self):
        prepared = prepare(candidate(), [], CONFIG)
        with self.assertRaises(ValueError):
            self.apply([existing(d="2026-09-21")], [prepared], approved=True)

    def test_short_api_title_at_same_course_deadline_slot_requires_review_without_linking(self):
        original = existing(n="논설 HW1 온라인 퀴즈 마감")
        before = deepcopy(original)
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertEqual(prepared["action"], "add")
        self.assertFalse(prepared["auto_eligible"])
        self.assertNotIn("target_id", prepared)
        with self.assertRaises(ValueError):
            self.apply([original], [prepared])
        self.assertEqual(original, before)

    def test_deadline_exam_type_difference_does_not_bypass_same_course_slot_review(self):
        original = existing(n="논설 온라인 퀴즈", t="exam")
        prepared = prepare(candidate(), [original], CONFIG)
        self.assertFalse(prepared["auto_eligible"])
        self.assertNotIn("target_id", prepared)

    def test_different_known_time_or_course_does_not_block_distinct_title_auto_add(self):
        for original in (existing(n="논설 HW2 제출", s=1320), existing(n="기전 HW2 제출")):
            prepared = prepare(candidate(), [original], CONFIG)
            self.assertEqual(prepared["action"], "add")
            self.assertTrue(prepared["auto_eligible"])
            result, _ = self.apply([original], [prepared])
            self.assertEqual(len(result), 2)

    def test_concurrent_same_course_slot_add_is_rechecked_before_auto_apply(self):
        prepared = prepare(candidate(), [], CONFIG)
        original = existing(n="논설 HW2 제출")
        with self.assertRaises(ValueError):
            self.apply([original], [prepared])
        # The owner may explicitly confirm two distinct assignments with the
        # same deadline. This never changes the established event's ID/data.
        result, _ = self.apply([original], [prepared], approved=True)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], original)

    def test_repreparing_discards_stale_target_fields(self):
        prepared = prepare(candidate(target_id="unrelated", target_hash="oldhash"), [], CONFIG)
        self.assertEqual(prepared["action"], "add")
        self.assertNotIn("target_id", prepared)
        self.assertNotIn("target_hash", prepared)

    def test_same_day_duplicate_add_is_rejected_even_after_explicit_approval(self):
        with self.assertRaises(ValueError):
            self.apply([existing()], [{**candidate(), "action": "add"}], approved=True)

    def test_add_cannot_replace_an_existing_target_via_stale_or_tampered_metadata(self):
        original = existing(n="기전 HW2 제출")
        value = {**candidate(), "action": "add", "target_id": original["id"], "target_hash": event_hash(original)}
        with self.assertRaises(ValueError):
            self.apply([original], [value], approved=True)

    def test_owned_candidate_collision_cannot_target_another_course(self):
        original = self.managed()
        value = candidate(course="em", source_id="notice-em", event={**candidate()["event"], "n": "기전 HW1 제출"})
        prepared = prepare(value, [original], CONFIG)
        self.assertEqual(prepared["action"], "add")
        self.assertNotIn("target_id", prepared)

    def test_source_course_and_explicit_target_course_guards_reject_cross_course_apply(self):
        prepared = prepare(candidate(), [], CONFIG)
        with self.assertRaises(ValueError):
            self.apply([], [prepared], approved=True, source={**SOURCE, "course_key": "em"})
        other = existing(n="기전 HW1 제출")
        for action in ("link", "update"):
            with self.assertRaises(ValueError):
                self.apply([other], [{**prepared, "action": action, "target_id": other["id"],
                                       "target_hash": event_hash(other)}], approved=True)

    def test_explicit_course_provenance_is_authoritative_over_a_mentioned_alias(self):
        original = existing(n="논설 HW1 참고 기전 자료", school={"course": "em"})
        self.assertFalse(belongs(original, "logic", CONFIG))
        self.assertTrue(belongs(original, "em", CONFIG))

    def test_managed_update_keeps_id_and_user_edit_detection_disables_auto_apply(self):
        original = self.managed()
        value = candidate(event={**candidate()["event"], "s": 1320})
        prepared = prepare(value, [original], CONFIG)
        self.assertEqual(prepared["action"], "update")
        self.assertTrue(prepared["auto_eligible"])
        result, ids = self.apply([original], [prepared])
        self.assertEqual(ids, [original["id"]])
        self.assertEqual(result[0]["s"], 1320)
        edited = {**original, "no": "사람이 이후 수정함"}
        prepared = prepare(value, [edited], CONFIG)
        self.assertFalse(prepared["auto_eligible"])
        with self.assertRaises(ValueError):
            self.apply([edited], [prepared])

    def test_forging_auto_flag_cannot_bypass_manual_event_protection(self):
        original = existing(s=1320)
        prepared = prepare(candidate(), [original], CONFIG)
        with self.assertRaises(ValueError):
            self.apply([original], [{**prepared, "auto_eligible": True}])

    def test_approved_edited_deadline_cannot_be_replaced_by_later_api_due_date(self):
        proposal = prepare(candidate(), [], CONFIG)
        proposal['event']['s'] = 1320
        result, _ = self.apply([], [proposal], approved=True)
        confirmed = result[0]
        self.assertTrue(confirmed['school']['user_confirmed'])
        self.assertEqual(confirmed['school']['managed_hash'], event_hash(confirmed))
        newer = {**SOURCE, 'content_hash': 'version-b'}
        changed = candidate(source_hash='version-b', event={**candidate()['event'], 'd': '2026-09-23'})
        prepared = prepare(changed, result, CONFIG)
        self.assertEqual(prepared['action'], 'update')
        self.assertFalse(prepared['auto_eligible'])
        before = deepcopy(result)
        # Apply repeats the protection even if a stale caller retains its old
        # automatic flag or prepares the candidate before user confirmation.
        with self.assertRaises(ValueError):
            self.apply(result, [{**prepared, 'auto_eligible': True}], source=newer)
        self.assertEqual(result, before)
        revised, _ = self.apply(result, [prepared], approved=True, source=newer)
        self.assertEqual(revised[0]['id'], confirmed['id'])
        self.assertEqual(revised[0]['d'], '2026-09-23')
        self.assertTrue(revised[0]['school']['user_confirmed'])

    def test_confirmed_status_also_blocks_automatic_managed_update(self):
        original = self.managed()
        original['status'] = 'confirmed'
        # Model a legacy confirmed event whose managed hash already includes
        # its status; hash mismatch must not be the only safety check.
        original['school']['managed_hash'] = event_hash(original)
        proposal = prepare(candidate(event={**candidate()['event'], 's': 1320}), [original], CONFIG)
        self.assertFalse(proposal['auto_eligible'])
        with self.assertRaises(ValueError):
            self.apply([original], [{**proposal, 'auto_eligible': True}])

    def test_explicit_link_of_owned_event_records_confirmation_without_changing_event_fields(self):
        original = self.managed()
        proposal = prepare(candidate(), [original], CONFIG)
        self.assertEqual(proposal['action'], 'link')
        linked, _ = self.apply([original], [proposal], approved=True)
        self.assertTrue(linked[0]['school']['user_confirmed'])
        self.assertEqual(event_hash(original), event_hash(linked[0]))
        changed = prepare(candidate(event={**candidate()['event'], 's': 1320}), linked, CONFIG)
        self.assertFalse(changed['auto_eligible'])

    def test_managed_location_or_confirmation_change_is_not_discarded_as_link(self):
        original = self.managed()
        for fields in ({"loc": "새 강의실"}, {"status": "confirmed"}):
            prepared = prepare(candidate(event={**candidate()["event"], **fields}), [original], CONFIG)
            self.assertEqual(prepared["action"], "update")
            result, _ = self.apply([original], [prepared])
            for name, value in fields.items():
                self.assertEqual(result[0][name], value)

    def test_changed_db_target_hash_rejects_approved_update_and_delete(self):
        original = existing(s=1320)
        prepared = prepare(candidate(), [original], CONFIG)
        changed = {**original, "no": "동시 사용자 변경"}
        for action in ("update", "delete"):
            with self.assertRaises(ValueError):
                self.apply([changed], [{**prepared, "action": action}], approved=True)

    def test_link_rechecks_target_hash_and_existence_before_claiming_success(self):
        original = existing()
        prepared = prepare(candidate(), [original], CONFIG)
        for events in ([], [{**original, "s": 1320}]):
            with self.assertRaises(ValueError):
                self.apply(events, [prepared])

    def test_cancellation_targets_only_course_occurrence_and_always_requires_approval(self):
        original = existing(n="논설 수업", t="class", s=570, e=645)
        other = existing(id="em-class", n="기전 수업", t="class", s=840, e=915)
        value = candidate(kind="cancellation", event={"d": original["d"], "n": "휴강", "t": "class"})
        prepared = prepare(value, [original, other], CONFIG)
        self.assertEqual(prepared["action"], "delete")
        self.assertEqual(prepared["target_id"], original["id"])
        self.assertEqual(prepared["event"]["n"], original["n"])
        self.assertEqual(prepared["event"]["s"], original["s"])
        self.assertEqual(prepared["event"]["id"], original["id"])
        self.assertFalse(prepared["auto_eligible"])
        with self.assertRaises(ValueError):
            self.apply([original, other], [prepared])
        result, ids = self.apply([original, other], [prepared], approved=True)
        self.assertEqual(result, [other])
        self.assertEqual(ids, [original["id"]])

    def test_delete_approval_displays_actual_target_details_but_preserves_notice_evidence(self):
        original = existing(n="논설 실험", t="lab", s=1080, e=1200, loc="실험실 308호", lid="snu301", status="tentative")
        value = candidate(kind="cancellation", title="수업 취소 공지", evidence="원문에 적힌 휴강 설명",
                          event={"d": original["d"], "n": "학교 공지 제목", "t": "class"})
        prepared = prepare(value, [original], CONFIG)
        self.assertEqual(prepared["event"], {key: original[key] for key in ("id", "d", "n", "t", "s", "e", "loc", "lid", "status")})
        self.assertEqual(prepared["title"], value["title"])
        self.assertEqual(prepared["evidence"], value["evidence"])
        self.assertNotIn("no", prepared["event"])

    def test_multiple_possible_cancellations_do_not_silently_pick_one(self):
        originals = [existing(id="am", n="논설 수업", t="class", s=570, e=645),
                     existing(id="pm", n="논설 실험", t="lab", s=1080, e=1200)]
        prepared = prepare(candidate(kind="cancellation"), originals, CONFIG)
        self.assertFalse(prepared["auto_eligible"])
        self.assertNotIn("target_id", prepared)
        with self.assertRaises(ValueError):
            self.apply(originals, [prepared], approved=True)

    def test_cancellation_cannot_be_recast_as_add_or_another_day_delete(self):
        original = existing(n="논설 수업", t="class", s=570, e=645)
        prepared = prepare(candidate(kind="cancellation"), [original], CONFIG)
        for fields in ({"action": "add", "target_id": None, "target_hash": None},
                       {"event": {**prepared["event"], "d": "2026-09-23"}}):
            with self.assertRaises(ValueError):
                self.apply([original], [{**prepared, **fields}], approved=True)

    def test_duplicate_batch_rejection_leaves_input_database_untouched(self):
        events = [existing(id="other", n="기전 HW3 제출")]
        before = deepcopy(events)
        first = prepare(candidate(), events, CONFIG)
        second = prepare(candidate(id="candidate-b"), events, CONFIG)
        with self.assertRaises(ValueError):
            self.apply(events, [first, second], approved=True)
        self.assertEqual(events, before)

    def test_candidate_from_another_source_version_is_rejected(self):
        value = prepare(candidate(), [], CONFIG)
        for fields in ({"source_hash": "old-version"}, {"source_id": "different-notice"}):
            with self.assertRaises(ValueError):
                self.apply([], [{**value, **fields}], approved=True)

    def test_event_status_and_term_validation_reject_invalid_approval(self):
        for fields in ({"d": "2027-01-01"}, {"status": "invented-status"}):
            value = prepare(candidate(event={**candidate()["event"], **fields}), [], CONFIG)
            with self.assertRaises(ValueError):
                self.apply([], [value], approved=True)

    def test_event_write_does_not_copy_source_body_evidence_or_candidate_tokens(self):
        value = candidate(evidence="PRIVATE EVIDENCE", token="PRIVATE TOKEN", event={**candidate()["event"],
                            "no": "PRIVATE SOURCE BODY", "source_url": "https://private.invalid/", "token": "PRIVATE TOKEN"})
        result, _ = self.apply([], [prepare(value, [], CONFIG)])
        self.assertNotIn("PRIVATE", str(result))
        self.assertNotIn("private.invalid", str(result))

    def test_recognizable_credentials_cannot_be_written_in_public_display_fields(self):
        markers = ("github_pat_SYNTHETIC123", "ghp_SYNTHETIC123", "Bearer SYNTHETIC_TOKEN",
                   "-----BEGIN PRIVATE KEY-----", "-----BEGIN EC PRIVATE KEY-----")
        for key in ("n", "loc", "ti"):
            for marker in markers:
                with self.subTest(field=key, marker=marker):
                    value = candidate(event={**candidate()["event"], key: marker})
                    with self.assertRaises(ValueError):
                        self.apply([], [prepare(value, [], CONFIG)], approved=True)

    def test_timetable_add_rechecks_same_course_date_after_concurrent_class_creation(self):
        value = candidate(kind='class', schedule_row=True, auto_eligible=False,
                          event={'d': '2026-09-22', 'n': '강의계획표', 't': 'class'})
        prepared = prepare(value, [], CONFIG)
        manual = existing(n='논설 특별수업', t='class', s=570, e=645)
        with self.assertRaisesRegex(ValueError, '같은 과목·날짜'):
            self.apply([manual], [prepared], approved=True)
        other = {**manual, 'n': '기전 특별수업'}
        result, _ = self.apply([other], [prepared], approved=True)
        self.assertEqual(len(result), 2)

    def test_weekday_conflict_cannot_be_auto_applied_even_with_retained_or_forged_auto_flag(self):
        value = candidate(weekday_conflict=True)
        prepared = prepare(value, [], CONFIG)
        self.assertFalse(prepared['auto_eligible'])
        with self.assertRaisesRegex(ValueError, '날짜와 요일'):
            self.apply([], [{**prepared, 'auto_eligible': True}])
        # Explicit review may resolve an incorrect printed weekday/date.
        result, _ = self.apply([], [prepared], approved=True)
        self.assertEqual(len(result), 1)


if __name__ == "__main__":
    unittest.main()
