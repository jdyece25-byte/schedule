import copy
import unittest

from src.bridge.planner import compact_context, daily_briefing, apply_plan


class EfficiencyTests(unittest.TestCase):
    def test_roundtrip_preserves_all_events_and_original_indexes(self):
        repeated = 'Long repeated notes with cancellation and travel exceptions.'
        events = [{'id': 'one', 'd': '2026-09-19', 'n': 'A', 'no': repeated,
                   'status': 'tentative', 'school': {'user_confirmed': True}},
                  {'id': 'two', 'd': '2026-09-20', 'n': 'B', 'no': repeated, 's': None}]
        original = {'events_snapshot': [{'index': i, 'event': e} for i, e in enumerate(events)],
                    'school_context': {'sources': []}}
        before = copy.deepcopy(original)
        packed = compact_context(original)
        def decode(value):
            if isinstance(value, dict):
                if set(value) == {'$text'}:
                    return packed['text_dictionary'][value['$text']]
                if set(value) == {'$columns', '$rows'}:
                    return [dict(zip(value['$columns'], [decode(child) for child in row])) for row in value['$rows']]
                return {key: decode(child) for key, child in value.items()}
            if isinstance(value, list):
                return [decode(child) for child in value]
            return value
        table = decode(packed['context'])['events_snapshot']
        restored = [{'index': index, 'event': dict(zip([table['columns'][i] for i in present], values))}
                    for index, present, values in table['rows']]
        self.assertEqual(restored, original['events_snapshot'])
        self.assertEqual(original, before)

    def test_briefing_is_read_only_and_preserves_uncertainty(self):
        events = [{'d': '2026-09-19', 'n': 'lesson', 's': 1320, 'e': 1440,
                   'status': 'tentative', 'no': 'bring materials', 'lid': 'a'}]
        travel = {'locations': {'a': 'Online'}}
        request = {'today': '2026-09-18', 'text': '내일 일정 브리핑해줘'}
        result = daily_briefing(request, events, travel)
        self.assertIn('22:00–24:00', result['message'])
        self.assertIn('확인 필요', result['message'])
        self.assertIn('bring materials', result['message'])
        self.assertIn('Online', result['message'])
        updated, routes, warnings = apply_plan(events, travel, result, 'test')
        self.assertEqual(updated, events)
        self.assertEqual(routes, travel)
        self.assertEqual(warnings, [])

    def test_mutations_ambiguous_dates_and_followups_never_bypass_model(self):
        for text in ['오늘 일정 삭제해줘', '내일 일정 알려줘 그리고 과제 추가해줘',
                     '9월 31일 일정', '이번 학기 일정', '오늘 학교 공지 확인해줘']:
            self.assertIsNone(daily_briefing({'today': '2026-09-19', 'text': text}, [], {}))
        self.assertIsNone(daily_briefing({'today': '2026-09-19', 'text': '오늘 일정'}, [], {}, [{}]))

    def test_week_and_month_boundaries(self):
        for text, start, end in [('다음 주 일정', '2026-09-21', '2026-09-27'),
                                 ('이번 달 일정 알려줘', '2026-09-01', '2026-09-30')]:
            result = daily_briefing({'today': '2026-09-19', 'text': text}, [], {})
            self.assertIn(start + ' ~ ' + end, result['message'])

    def test_local_query_never_launches_agent(self):
        from src.bridge.worker import AgentRunner
        runner = AgentRunner({})
        result = runner.run({'today': '2026-09-19', 'text': '오늘 일정'}, [], {}, '', [], lambda: None)
        self.assertEqual(result['operations'], [])

    def test_provenance_aliases_preserve_equalities(self):
        first, second = 'a' * 64, 'b' * 64
        packed = compact_context({'events_snapshot': [], 'school_context': {
            'source_hash': first, 'changes': {'previous_hash': second, 'current_hash': first}}})['context']
        source = packed['school_context']
        self.assertEqual(source['source_hash'], source['changes']['current_hash'])
        self.assertNotEqual(source['source_hash'], source['changes']['previous_hash'])
