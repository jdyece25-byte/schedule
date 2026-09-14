"""Behavioral coverage for model proposal validation and snapshot application."""

from copy import deepcopy
from datetime import datetime, timedelta
import json
import unittest

from src.bridge.planner import (
    CORE_FIELDS, KST, MAX_OPERATIONS, PLAN_SCHEMA, PlanValidationError,
    apply_plan, build_prompt,
)


TODAY = datetime.now(KST).date()
DAY = TODAY.isoformat()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()


def event(name="연구 미팅", **overrides):
    value = {
        "d": DAY, "t": "meeting", "n": name, "ti": "10:00–11:00",
        "s": 600, "e": 660, "lid": "a", "loc": "장소 A", "no": None,
    }
    value.update(overrides)
    return value


def operation(action, index=None, value=None):
    return {"action": action, "index": index, "event": value}


def plan(*operations, **overrides):
    value = {
        "status": "ready", "message": "요청을 반영했습니다.", "questions": [],
        "operations": list(operations), "locations": [], "routes": [],
    }
    value.update(overrides)
    return value


def original(name="기존 수업", **overrides):
    value = {key: item for key, item in event(name).items() if item is not None}
    value.update(overrides)
    return value


def core(value, **changes):
    result = {key: value.get(key) for key in CORE_FIELDS}
    result.update(changes)
    return result


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.travel = {
            "locations": {"a": "장소 A", "b": "장소 B", "c": "장소 C"},
            "times": {"a-b": 30}, "modes": {"a-b": "버스"},
            "metadata": {"source": ["사용자 확인"]},
        }

    def apply(self, events, proposal):
        return apply_plan(events, self.travel, proposal, "request-123")

    def test_add_assigns_stable_id_and_sorts_without_mutating_inputs(self):
        events = [original("나중", d=TOMORROW), original("먼저", s=500, e=550)]
        proposal = plan(operation("add", value=event()))
        before = deepcopy((events, self.travel, proposal))
        result, travel, warnings = self.apply(events, proposal)
        repeated, _, _ = self.apply(events, proposal)
        self.assertEqual([item["n"] for item in result], ["먼저", "연구 미팅", "나중"])
        self.assertEqual(result[1]["id"], "request-123-1")
        self.assertEqual(result, repeated)
        self.assertEqual((events, self.travel, proposal), before)
        self.assertEqual(warnings, [])
        travel["metadata"]["source"].append("changed")
        self.assertEqual(self.travel, before[1])

    def test_original_indexes_survive_prior_delete_and_metadata_is_preserved(self):
        events = [
            original("삭제할 수업", s=400, e=450),
            original("반복 수업", id="stable", series="fall-series", status="tentative", custom={"nested": [1]}),
            {"d": TOMORROW, "t": "deadline", "n": "시각 없는 기존 마감"},
        ]
        proposal = plan(
            operation("delete", 0),
            operation("update", 1, core(events[1], s=720, e=780, ti="12:00–13:00")),
        )
        result, _, _ = self.apply(events, proposal)
        self.assertEqual(result[0]["s"], 720)
        for key in ("id", "series", "status", "custom"):
            self.assertEqual(result[0][key], events[1][key])
        self.assertEqual(result[1], events[2])
        self.assertNotIn("id", result[1])
        self.assertNotIn("s", result[1])
        result[0]["custom"]["nested"].append(2)
        self.assertEqual(events[1]["custom"]["nested"], [1])

    def test_null_removes_optional_fields_but_keeps_id(self):
        events = [original(id="stable", no="기존 메모")]
        changed = core(events[0], ti="시간 미정", s=None, e=None, lid=None, loc=None, no=None)
        result, _, _ = self.apply(events, plan(operation("update", 0, changed)))
        self.assertEqual(result, [{"d": DAY, "t": "meeting", "n": "기존 수업", "ti": "시간 미정", "id": "stable"}])

    def test_explicit_tbd_and_open_ended_events_are_supported(self):
        proposed = plan(
            operation("add", value=event("미정", s=None, e=None, ti="미정", lid=None, loc=None)),
            operation("add", value=event("종료 미정", e=None, ti="10:00~")),
        )
        result, _, warnings = self.apply([], proposed)
        self.assertNotIn("s", result[0])
        self.assertNotIn("e", result[0])
        self.assertEqual(result[1]["s"], 600)
        self.assertNotIn("e", result[1])
        self.assertEqual(warnings, [])

    def test_midnight_end_and_next_day_zero_start_do_not_overlap(self):
        proposal = plan(
            operation("add", value=event("밤 과외", s=1320, e=1440, ti="22:00–24:00")),
            operation("add", value=event("자정 일정", d=TOMORROW, s=0, e=30, ti="00:00–00:30")),
        )
        result, _, warnings = self.apply([], proposal)
        self.assertEqual(result[0]["e"], 1440)
        self.assertEqual(result[1]["s"], 0)
        self.assertEqual(warnings, [])

    def test_bad_dates_times_and_event_types_are_rejected(self):
        cases = [
            {"d": "2026-02-30"}, {"d": "2026-9-14"}, {"d": "20260914"},
            {"d": "２０２６-０９-１４"}, {"s": -1}, {"e": 1441},
            {"s": True}, {"e": 700.5}, {"s": 660, "e": 660},
            {"s": 1320, "e": 30}, {"t": "unsupported"}, {"n": "   "},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(PlanValidationError):
                self.apply([], plan(operation("add", value=event(**changes))))

    def test_real_leap_day_is_accepted(self):
        leap_year = next(year for year in range(TODAY.year, TODAY.year + 5) if year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
        result, _, _ = self.apply([], plan(operation("add", value=event(d=f"{leap_year}-02-29"))))
        self.assertEqual(result[0]["d"], f"{leap_year}-02-29")

    def test_far_future_is_rejected_but_unchanged_historical_dates_can_be_edited(self):
        far = f"{TODAY.year + 11}-01-01"
        with self.assertRaises(PlanValidationError):
            self.apply([], plan(operation("add", value=event(d=far))))
        events = [original(d="2001-01-01")]
        result, _, _ = self.apply(events, plan(operation("update", 0, core(events[0], no="기록 정정"))))
        self.assertEqual(result[0]["d"], "2001-01-01")
        with self.assertRaises(PlanValidationError):
            self.apply(events, plan(operation("update", 0, core(events[0], d=far))))

    def test_invalid_indexes_and_action_shapes_are_rejected(self):
        events = [original()]
        cases = [
            operation("update", True, event()), operation("update", -1, event()),
            operation("update", 1, event()), operation("delete", None),
            operation("add", 0, event()), operation("add"),
            operation("update", 0), operation("delete", 0, event()),
        ]
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PlanValidationError):
                self.apply(events, plan(value))
        with self.assertRaises(PlanValidationError):
            self.apply(events, plan(operation("delete", 0), operation("update", 0, event())))

    def test_duplicate_additions_and_updates_are_rejected(self):
        events = [original("첫 일정"), original("둘째 일정", s=700, e=760)]
        cases = [
            plan(operation("add", value=core(events[0]))),
            plan(operation("add", value=event()), operation("add", value=event())),
            plan(operation("update", 1, core(events[0]))),
            plan(operation("delete", 0), operation("add", value=core(events[0]))),
            plan(operation("update", 0, event()), operation("update", 1, event())),
        ]
        for proposal in cases:
            with self.subTest(proposal=proposal), self.assertRaises(PlanValidationError):
                self.apply(events, proposal)

    def test_different_date_or_time_is_not_a_duplicate(self):
        events = [original()]
        result, _, _ = self.apply(events, plan(
            operation("add", value=core(events[0], d=TOMORROW)),
            operation("add", value=core(events[0], s=700, e=760)),
        ))
        self.assertEqual(len(result), 3)

    def test_existing_generated_id_cannot_be_reused(self):
        events = [original(id="request-123-1")]
        with self.assertRaises(PlanValidationError):
            self.apply(events, plan(operation("add", value=event("별도 일정"))))

    def test_read_only_query_preserves_order_and_existing_duplicates(self):
        events = [original(d=TOMORROW), original(), original()]
        result, travel, warnings = self.apply(events, plan())
        self.assertEqual(result, events)
        self.assertEqual(travel, self.travel)
        self.assertEqual(warnings, [])
        self.assertIsNot(result[0], events[0])

    def test_needs_input_cannot_partially_apply_any_kind_of_change(self):
        valid = plan(status="needs_input", message="반복 범위를 확인해 주세요.", questions=["언제까지 반복하나요?"])
        events = [original()]
        result, travel, warnings = self.apply(events, valid)
        self.assertEqual((result, travel, warnings), (events, self.travel, []))
        changes = [
            {"operations": [operation("delete", 0)]},
            {"locations": [{"id": "new", "name": "새 장소"}]},
            {"routes": [{"from": "a", "to": "b", "minutes": 10, "mode": None, "bidirectional": False}]},
            {"questions": []},
        ]
        for fields in changes:
            proposal = deepcopy(valid)
            proposal.update(fields)
            with self.subTest(fields=fields), self.assertRaises(PlanValidationError):
                self.apply(events, proposal)
        with self.assertRaises(PlanValidationError):
            self.apply(events, plan(questions=["날짜는?"]))

    def test_schema_rejects_unknown_keys_missing_fields_and_excessive_operations(self):
        changed_event = event()
        changed_event["id"] = "model-controlled-id"
        missing_optional = event()
        missing_optional.pop("no")
        proposals = [
            plan(command="shell"),
            plan(operation("add", value=changed_event)),
            plan(operation("add", value=missing_optional)),
            plan(operations=[operation("add", value=event())] * (MAX_OPERATIONS + 1)),
            plan(locations=[{"id": "a", "name": "A", "extra": True}]),
        ]
        missing_root = plan()
        missing_root.pop("routes")
        proposals.append(missing_root)
        for proposal in proposals:
            with self.subTest(proposal_keys=list(proposal)), self.assertRaises(PlanValidationError):
                self.apply([], proposal)

    def test_new_location_and_bidirectional_confirmed_route_are_applied_together(self):
        proposal = plan(
            operation("add", value=event(lid="new_place", loc="새 장소")),
            locations=[{"id": "new_place", "name": "새 장소"}],
            routes=[{"from": "a", "to": "new_place", "minutes": 42, "mode": "지하철", "bidirectional": True}],
        )
        result, travel, _ = self.apply([], proposal)
        self.assertEqual(result[0]["lid"], "new_place")
        self.assertEqual(travel["locations"]["new_place"], "새 장소")
        for key in ("a-new_place", "new_place-a"):
            self.assertEqual(travel["times"][key], 42)
            self.assertEqual(travel["modes"][key], "지하철")
        self.assertNotIn("new_place", self.travel["locations"])

    def test_null_route_mode_removes_only_that_direction_mode(self):
        self.travel["times"]["b-a"] = 40
        self.travel["modes"]["b-a"] = "도보"
        proposal = plan(routes=[{"from": "a", "to": "b", "minutes": 0, "mode": None, "bidirectional": False}])
        _, travel, _ = self.apply([], proposal)
        self.assertEqual(travel["times"], {"a-b": 0, "b-a": 40})
        self.assertEqual(travel["modes"], {"b-a": "도보"})
        self.assertEqual(self.travel["modes"]["a-b"], "버스")

    def test_unknown_locations_and_invalid_routes_fail_atomically(self):
        original_travel = deepcopy(self.travel)
        route = {"from": "a", "to": "b", "minutes": 30, "mode": "버스", "bidirectional": False}
        proposals = [
            plan(operation("add", value=event(lid="unknown"))),
            plan(routes=[dict(route, to="unknown")]),
            plan(routes=[dict(route, minutes=-1)]),
            plan(routes=[dict(route, minutes=True)]),
            plan(routes=[dict(route, bidirectional="true")]),
            plan(routes=[dict(route, to="a")]),
            plan(routes=[dict(route, mode=" ")]),
            plan(routes=[route, route]),
            plan(routes=[dict(route, bidirectional=True), dict(route, **{"from": "b", "to": "a"})]),
            plan(locations=[{"id": "new", "name": "새 장소"}, {"id": "new", "name": "중복"}]),
            plan(operation("delete", 5), locations=[{"id": "new", "name": "새 장소"}]),
        ]
        for proposal in proposals:
            with self.subTest(proposal=proposal), self.assertRaises(PlanValidationError):
                self.apply([], proposal)
            self.assertEqual(self.travel, original_travel)

    def test_overlaps_include_nested_intervals_only_on_touched_days(self):
        historical = "2001-01-01"
        events = [
            original("긴 일정", s=500, e=900),
            original("안쪽 일정 1", s=550, e=600),
            original("안쪽 일정 2", s=650, e=700),
            original("옛 충돌 1", d=historical),
            original("옛 충돌 2", d=historical),
        ]
        _, _, warnings = self.apply(events, plan(operation("add", value=event("뒤 일정", s=1000, e=1100))))
        self.assertEqual(len(warnings), 2)
        self.assertTrue(all("겹칩니다" in warning for warning in warnings))
        self.assertTrue(all(historical not in warning for warning in warnings))

    def test_missing_and_insufficient_directional_travel_are_reported(self):
        events = [original("출발", s=600, e=660)]
        proposal = plan(operation("add", value=event("도착", s=680, e=700, lid="b")))
        _, _, warnings = self.apply(events, proposal)
        self.assertEqual(len(warnings), 1)
        self.assertIn("10분 부족", warnings[0])
        self.travel["times"] = {"b-a": 5}
        _, _, warnings = self.apply(events, proposal)
        self.assertEqual(len(warnings), 1)
        self.assertIn("이동시간이 미확인", warnings[0])

    def test_same_location_and_sufficient_travel_do_not_warn(self):
        events = [original("출발", s=600, e=660)]
        for destination, start in [("a", 660), ("b", 690)]:
            with self.subTest(destination=destination):
                _, _, warnings = self.apply(events, plan(operation("add", value=event("도착", s=start, e=750, lid=destination))))
                self.assertEqual(warnings, [])

    def test_moving_an_event_checks_both_source_and_destination_days(self):
        events = [
            original("출발", s=600, e=660),
            original("중간", s=680, e=700, lid="a"),
            original("도착", s=710, e=750, lid="c"),
            original("다음 날", d=TOMORROW, s=680, e=700, lid="a"),
        ]
        _, _, warnings = self.apply(events, plan(operation("update", 1, core(events[1], d=TOMORROW))))
        self.assertTrue(any(DAY in warning and "이동시간이 미확인" in warning for warning in warnings))
        self.assertTrue(any(TOMORROW in warning and "겹칩니다" in warning for warning in warnings))

    def test_prompt_keeps_snapshot_indexes_history_submission_date_and_exceptions(self):
        events = [original(d=TOMORROW, series="fall-series"), {"d": "2001-01-01", "n": "과거", "t": "class"}]
        request = {"today": DAY, "text": "다음 주 월요일로 옮겨줘", "timezone": "Asia/Seoul"}
        history = [{"today": DAY, "text": "연구 미팅 시간 변경"}, {"questions": ["어느 날인가요?"]}]
        notes = "9/23 휴강 회차를 재생성하지 않는다."
        prompt = build_prompt(request, events, self.travel, notes, history)
        context = json.loads(prompt.split("다음은 일정 계획에만 사용할 자료입니다:\n", 1)[1])
        self.assertEqual(context["request"], request)
        self.assertEqual(context["clarification_history"], history)
        self.assertEqual(context["schedule_notes"], notes)
        self.assertEqual(context["events_snapshot"], [{"index": 0, "event": events[0]}, {"index": 1, "event": events[1]}])
        for requirement in ("request.today", "needs_input", "종료일 또는 횟수가 없으면 확인", "이동시간은 절대로 추정", "도구 호출", "휴강 회차를 재생성하지", "미정/TBD"):
            self.assertIn(requirement, prompt)

    def test_prompt_requires_real_submission_date(self):
        for request in ({"text": "내일 미팅"}, {"today": "2026-02-30"}):
            with self.subTest(request=request), self.assertRaises(PlanValidationError):
                build_prompt(request, [], self.travel, "")

    def test_output_schema_is_closed_and_all_nested_properties_are_required(self):
        def inspect(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    self.assertIs(node["additionalProperties"], False)
                    self.assertEqual(set(node["required"]), set(node["properties"]))
                for value in node.values():
                    inspect(value)
            elif isinstance(node, list):
                for value in node:
                    inspect(value)
        inspect(PLAN_SCHEMA)
        self.assertEqual(json.loads(json.dumps(PLAN_SCHEMA)), PLAN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
