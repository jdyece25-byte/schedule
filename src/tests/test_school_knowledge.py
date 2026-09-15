import copy
import json
import unittest

from src.bridge.school_knowledge import source_knowledge, build_school_context
from src.bridge.planner import build_prompt


class KnowledgeTests(unittest.TestCase):
    def test_schedule_rows_take_priority_over_incidental_text_and_secrets(self):
        source = {'content': '\n'.join(['수업 안내 ' + '설명' * 150] * 10 +
                                     ['12 2026-11-18 수 39동 B103호 특강 대체',
                                      '제출 https://example.test/?token=private github_pat_hidden']),
                  'extraction_status': 'parsed'}
        result = source_knowledge(source)
        self.assertIn('2026-11-18', result['excerpts'][0])
        self.assertLessEqual(sum(map(len, result['excerpts'])), 1800)
        self.assertNotIn('github_pat_hidden', json.dumps(result))
        self.assertNotIn('token=private', json.dumps(result))
        self.assertTrue(result['incomplete'])

    def test_read_notices_remain_knowledge_and_other_course_is_not_mixed_in(self):
        index = {'items': [{'id':'one','course_key':'leadership','course':'공도리','state':'ignored',
                            'knowledge':{'status':'parsed','excerpts':['2026-11-18 수요일 특강']}},
                           {'id':'two','course_key':'logic','course':'논설',
                            'knowledge':{'status':'parsed','excerpts':['2026-11-18 다른 실습']}}]}
        before = copy.deepcopy(index)
        context = build_school_context(index, {'text':'공도리 수요일 수업 확인'})
        self.assertEqual(len(context['sources']), 1)
        self.assertEqual(context['sources'][0]['review_state'], 'ignored')
        self.assertIn('수요일', context['sources'][0]['excerpts'][0])
        self.assertEqual(index, before)

    def test_missing_partial_and_old_index_evidence_are_explicit(self):
        self.assertFalse(build_school_context(None, {})['available'])
        index = {'collectors': {'etl': {'state':'partial','last_checked':'2026-09-15'}},
                 'items':[{'id':'one','candidates':[{'evidence':'9/16 수업 변경'}], 'knowledge':None}]}
        result = build_school_context(index, {})
        self.assertEqual(result['collectors']['etl']['state'], 'partial')
        self.assertTrue(result['sources'][0]['incomplete'])
        self.assertEqual(result['sources'][0]['excerpts'], ['9/16 수업 변경'])

    def test_private_context_is_in_model_prompt_as_evidence_not_change_authorization(self):
        context = {'available':True,'sources':[{'course':'공도리','review_state':'ignored','excerpts':['9/16 수업']} ]}
        prompt = build_prompt({'today':'2026-09-15','text':'내일 수업 알려줘'}, [], {}, '', school_context=context)
        self.assertIn('"school_context":', prompt)
        self.assertIn('ignored는 읽었다는 뜻', prompt)
        self.assertIn('게시일을 마감일로', prompt)
        self.assertIn('9/16 수업', prompt)
        self.assertIn('확정 날짜·시각·휴강 예외를 보존', prompt)
        self.assertIn('limited=true', prompt)

    def test_later_correction_precedes_older_schedule_with_same_priority(self):
        index = {'items': [
            {'id': 'old', 'course_key': 'writing', 'updated_at': '2026-09-01T04:00:00Z',
             'source_kind': 'etl_file', 'knowledge': {'status': 'parsed', 'excerpts': ['2026-12-08 종강']}},
            {'id': 'new', 'course_key': 'writing', 'updated_at': '2026-09-01T12:00:00Z',
             'source_kind': 'etl_file', 'knowledge': {'status': 'parsed', 'excerpts': ['2026-12-03 종강']}}]}
        result = build_school_context(index, {'text': '대글 종강일'}, limit=16)
        self.assertEqual(result['sources'][0]['id'], 'new')
        self.assertEqual(result['sources'][0]['excerpts'], ['2026-12-03 종강'])
        self.assertTrue(result['limited'])

    def test_line_count_long_line_and_summary_omissions_are_explicit(self):
        for source in (
            {'content': '\n'.join(f'과제 {n}' for n in range(30)), 'extraction_status': 'parsed'},
            {'content': '수업 ' + '긴 설명 ' * 120, 'extraction_status': 'parsed'},
            {'content': '수업 일부 요약', 'extraction_status': 'summary'},
        ):
            self.assertTrue(source_knowledge(source)['incomplete'])

    def test_partial_evidence_from_single_source_marks_context_budget_incomplete(self):
        index = {'collectors': {'etl': {'state': 'ok'}}, 'items': [
            {'id': 'one', 'knowledge': {'status': 'parsed', 'incomplete': False,
                                      'excerpts': ['첫 번째 수업', '두 번째 수업']}}]}
        result = build_school_context(index, {}, limit=8)
        self.assertEqual(len(result['sources']), 1)
        self.assertTrue(result['sources'][0]['incomplete'])
        self.assertTrue(result['limited'])
        self.assertTrue(result['incomplete'])

    def test_malformed_optional_shapes_do_not_break_normal_schedule_planning(self):
        for candidates in (None, 3, {}, 'unexpected'):
            index = {'collectors': [], 'items': [{'id': 'one', 'course_key': {}, 'candidates': candidates,
                                                  'knowledge': {'status': 'parsed', 'excerpts': ['9/16 수업']}}]}
            result = build_school_context(index, {})
            self.assertEqual(result['sources'][0]['excerpts'], ['9/16 수업'])
            self.assertTrue(result['incomplete'])

    def test_missing_course_name_does_not_match_every_specific_course_query(self):
        index = {'items': [{'id': 'unknown', 'knowledge': {'excerpts': ['9/16 관련 없는 수업']}},
                           {'id': 'correct', 'course_key': 'logic', 'knowledge': {'excerpts': ['9/16 논설 수업']}}]}
        result = build_school_context(index, {'text': '논설 수업'})
        self.assertEqual([source['id'] for source in result['sources']], ['correct'])

    def test_collector_coverage_stays_incomplete_even_if_each_excerpt_was_parsed(self):
        index = {'collectors': {'etl': {'state': 'partial', 'issue_count': 2, 'source_count': 10}}, 'items': [
            {'id': 'one', 'knowledge': {'status': 'parsed', 'incomplete': False, 'excerpts': ['9/16 수업']}}]}
        result = build_school_context(index, {})
        self.assertTrue(result['incomplete'])
        self.assertEqual(result['collectors']['etl']['issue_count'], 2)


if __name__ == '__main__':
    unittest.main()
