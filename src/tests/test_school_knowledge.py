import copy
import json
import unittest

from src.bridge.school_knowledge import source_knowledge, build_school_context, clean
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

    def test_new_announcement_correction_outranks_old_syllabus_and_preserves_diff(self):
        index = {'items': [
            {'id': 'old', 'course_key': 'writing', 'updated_at': '2026-09-01',
             'source_kind': 'etl_file', 'knowledge': {'excerpts': ['12/8 기말시험']}},
            {'id': 'new', 'course_key': 'writing', 'updated_at': '2026-09-16',
             'source_kind': 'etl_announcement', 'content_hash': 'new-hash',
             'acknowledgement': {'state': 'read', 'source_hash': 'new-hash'},
             'application': {'state': 'not_applied'},
             'knowledge': {'excerpts': ['12/5 기말시험']},
             'changes': {'previous_hash': 'old-hash', 'current_hash': 'new-hash',
                         'added': ['12/5 기말시험'], 'removed': ['12/8 기말시험']}}]}
        result = build_school_context(index, {'text': '대글 시험일 변경'}, limit=40)
        current = result['sources'][0]
        self.assertEqual(current['id'], 'new')
        self.assertEqual(current['changes']['removed'], ['12/8 기말시험'])
        self.assertEqual(current['acknowledgement']['state'], 'read')
        self.assertEqual(current['application']['state'], 'not_applied')

    def test_fact_categories_retain_exact_source_line_without_inventing_dates(self):
        document = {'id': 'one', 'content_hash': 'revision-one', 'content':
                    '일반 안내\n시험은 추후 안내\n9/20 과제 제출\n9/24 휴강\n준비물: 노트북 지참'}
        knowledge = source_knowledge(document)
        facts = {item['text']: item for item in knowledge['evidence']}
        self.assertEqual(facts['시험은 추후 안내']['line_start'], 2)
        self.assertIn('exam', facts['시험은 추후 안내']['categories'])
        self.assertIn('deadline', facts['9/20 과제 제출']['categories'])
        self.assertIn('cancellation', facts['9/24 휴강']['categories'])
        self.assertIn('preparation', facts['준비물: 노트북 지참']['categories'])
        self.assertEqual(facts['시험은 추후 안내']['source_hash'], 'revision-one')
        self.assertNotIn('date', facts['시험은 추후 안내'])

    def test_empty_quiz_description_still_retains_title_and_api_due_evidence(self):
        knowledge = source_knowledge({'id': 'quiz-one', 'content_hash': 'one', 'kind': 'etl_quiz',
                                      'title': 'Quiz 2', 'content': '', 'due_at': '2026-09-30T14:59:00Z'})
        self.assertTrue(any('Quiz 2' in line for line in knowledge['excerpts']))
        self.assertTrue(any('2026-09-30T14:59:00Z' in line for line in knowledge['excerpts']))
        self.assertEqual({fact.get('field') for fact in knowledge['evidence']}, {'title', 'due_at'})
        self.assertTrue(all(fact['line_start'] is None for fact in knowledge['evidence']))

    def test_auth_headers_and_short_credentials_are_not_model_evidence(self):
        text = 'Authorization: Basic c2VjcmV0\nCookie: sid=privatecookie\n제출 api_key=secretvalue'
        cleaned = clean(text)
        for secret in ('c2VjcmV0', 'privatecookie', 'secretvalue'):
            self.assertNotIn(secret, cleaned)

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
