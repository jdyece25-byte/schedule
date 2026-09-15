"""Pure reconciliation: preserve existing IDs, user edits and uncertain dates."""
from copy import deepcopy
import hashlib
import json
import re

from src.validate_db import validate_events

EDITABLE = ('d', 't', 'n', 's', 'e', 'loc', 'lid', 'status', 'ti')
CREDENTIAL = re.compile(r'github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+|\bbearer\s+\S+|'
                        r'-----BEGIN [^-\r\n]*PRIVATE KEY-----', re.IGNORECASE)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def event_hash(event):
    return digest({key: value for key, value in event.items() if key != 'school'})


def normalized(value):
    return re.sub(r'[^a-z0-9가-힣]', '', str(value).lower())


def definition(course, config):
    if not config:
        return None
    return next((c for c in config['courses'] if course in (c['key'], c['name'])), None)


def course_key(course, config):
    value = definition(course, config)
    return value['key'] if value else course


def school(event):
    value = event.get('school', {})
    return value if isinstance(value, dict) else {}


def belongs(event, course, config):
    owned_course = school(event).get('course')
    if owned_course:
        # Explicit provenance is authoritative even if an event title mentions
        # another subject. Do not claim another course's similarly named HW1.
        return course_key(owned_course, config) == course_key(course, config)
    value = definition(course, config)
    return bool(value and any(normalized(alias) and normalized(alias) in normalized(event.get('n', ''))
                              for alias in [value['name'], *value.get('aliases', [])]))


def owned(event, candidate, config):
    value = school(event)
    return (value.get('candidate_id') == candidate.get('id')
            and value.get('source_id') == candidate.get('source_id')
            and belongs(event, candidate.get('course'), config))


def semantic(value):
    text = str(value).lower()
    result = re.findall(r'(?:hw|homework|과제)\s*#?\s*0*(\d+)', text)
    result = ['hw' + number for number in result]
    result += ['lab' + number for number in re.findall(r'(?:lab|실습)\s*#?\s*0*(\d+)', text)]
    result += [word for word in ('초고', '수정고', '중간1', '중간2', '중간', '기말', '퀴즈') if word in normalized(text)]
    if re.search(r'pre[ -]?lab|프리랩|예비', text):
        result.append('prelab')
    elif re.search(r'보고서|report|리포트', text):
        result.append('report')
    return tuple(sorted(set(result)))


def same_item(left, right):
    a, b = normalized(left.get('n')), normalized(right.get('n'))
    if a == b:
        return True
    one, two = semantic(a), semantic(b)
    if one and two:
        return one == two
    return len(min(a, b, key=len)) >= 5 and (a in b or b in a)


def matching_item(event, expected, course, config, term=None):
    if event.get('t') != expected.get('t') or not same_item(event, expected):
        return False
    if not (belongs(event, course, config) if config or school(event).get('course')
            else normalized(event.get('n')) == normalized(expected.get('n'))):
        return False
    if event.get('d') == expected.get('d'):
        return True
    # Assignments and exams may move to another date. A recurring class or lab
    # with the same title on a different day remains a separate occurrence.
    if expected.get('t') not in ('deadline', 'exam'):
        return False
    limits = term or (config or {}).get('term', {})
    return (not limits or limits['start'] <= event.get('d', '') <= limits['end'])


def same_course_slot(event, expected, course, config):
    """An unmatched title at an existing deadline slot is ambiguous, not new.

    API titles often omit a user's descriptive suffix, and a quiz may be
    represented as either an exam or a deadline. Distinct items can share a
    deadline, so this guard requires review rather than merging their IDs.
    """
    return (event.get('t') in ('deadline', 'exam')
            and expected.get('t') in ('deadline', 'exam')
            and event.get('d') == expected.get('d')
            and event.get('s') == expected.get('s')
            and belongs(event, course, config))


def patch_event(target, event):
    updated = deepcopy(target)
    for key, value in event.items():
        if key not in EDITABLE:
            continue
        if value is None or (key in ('loc', 'lid', 'status', 'ti') and value == ''):
            updated.pop(key, None)
        else:
            updated[key] = value
    return updated


def prepare(candidate, events, config):
    value = deepcopy(candidate)
    value.pop('target_id', None)
    value.pop('target_hash', None)
    value['action'] = 'add'
    value['auto_eligible'] = value.get('auto_eligible') is True
    if value.get('weekday_conflict'):
        value.update(auto_eligible=False, reason='원문의 날짜와 요일이 일치하지 않습니다. 정확한 날짜를 확인해 주세요.')
    expected = value['event']
    course = definition(value['course'], config)
    if not course:
        value.update(auto_eligible=False, reason='등록되지 않은 과목입니다. 원문 과목을 확인해 주세요.')
        return value
    if not belongs(expected, value['course'], config):
        expected['n'] = course['name'] + ' · ' + expected['n']
    owned_matches = [e for e in events if owned(e, value, config)]
    matches = owned_matches or [e for e in events if matching_item(e, expected, value['course'], config)]
    if not matches and value.get('schedule_row') and value.get('kind') == 'class':
        # Explicit timetable rows can refine a unique same-day class without
        # duplicating it under the attachment's generic title (e.g. Intro.pdf).
        matches = [e for e in events if belongs(e, value['course'], config)
                   and e.get('d') == expected.get('d') and e.get('t') == 'class']
        value['auto_eligible'] = False
    if value.get('kind') == 'cancellation':
        matches = [e for e in events if belongs(e, value['course'], config)
                   and e.get('d') == expected.get('d') and e.get('t') in ('class', 'lab', 'exam')]
        value.update(action='delete', auto_eligible=False)
    if len(matches) > 1:
        value.update(auto_eligible=False, reason='기존 일정 후보가 여러 개입니다. 날짜·제목을 확인해 주세요.')
        return value
    if not matches:
        if value['action'] == 'delete':
            value.update(reason='휴강 대상 회차를 특정하지 못했습니다. 일정을 직접 확인해 주세요.')
        elif any(same_course_slot(e, expected, value['course'], config) for e in events):
            value.update(auto_eligible=False,
                         reason='같은 과목·날짜·시각의 마감 또는 시험이 이미 있습니다. 별도 일정인지 확인해 주세요.')
        return value
    target = matches[0]
    value.update(target_id=target.get('id'), target_hash=event_hash(target),
                 action='delete' if value['action'] == 'delete' else 'update')
    if not target.get('id'):
        value.update(auto_eligible=False, reason='기존 ID 없는 일정과 겹칩니다. 중복을 만들지 않고 확인을 기다립니다.')
        return value
    if value['action'] == 'delete':
        # The approval card must show the actual row that will disappear,
        # while candidate title/evidence continue to describe the notice.
        value['event'] = {key: deepcopy(target[key]) for key in (*EDITABLE, 'id') if key in target}
        return value
    proposed = patch_event(target, expected)
    fields_equal = all(target.get(k) == proposed.get(k) for k in EDITABLE)
    same_except_name = all(target.get(k) == proposed.get(k) for k in EDITABLE if k != 'n')
    if fields_equal or (same_except_name and not owned_matches):
        value.update(action='link', auto_eligible=False, reason='같은 일정이 이미 등록되어 있습니다.')
    elif not owned_matches or school(target).get('managed_hash') != event_hash(target):
        value.update(auto_eligible=False, reason='기존 일정 또는 사용자가 수정한 내용과 다릅니다. 기존 내용을 보존하고 확인을 기다립니다.')
    if not owned_matches:
        # Source aliases must not rename a user's established calendar title.
        expected['n'] = target['n']
    return value


def apply(events, candidates, source, *, approved=False, config=None):
    result = deepcopy(events)
    applied_ids = []
    for candidate in candidates:
        if (candidate.get('source_id') != source.get('id')
                or candidate.get('source_hash') != source.get('content_hash')):
            raise ValueError('일정 후보의 원문 또는 버전이 변경되었습니다.')
        if config and not definition(candidate.get('course'), config):
            raise ValueError('등록되지 않은 과목의 일정입니다.')
        source_course = source.get('course_key') or source.get('course')
        if source_course and course_key(source_course, config) != course_key(candidate.get('course'), config):
            raise ValueError('원문 과목과 일정 후보 과목이 일치하지 않습니다.')
        action = candidate.get('action', 'add')
        if action not in ('add', 'update', 'delete', 'link'):
            raise ValueError('지원하지 않는 일정 작업입니다.')
        if candidate.get('kind') == 'cancellation' and action != 'delete':
            raise ValueError('휴강 후보는 확인된 기존 회차 삭제로만 적용할 수 있습니다.')
        if action == 'add' and (candidate.get('target_id') is not None or candidate.get('target_hash') is not None):
            raise ValueError('새 일정 추가에는 기존 수정·삭제 대상이 올 수 없습니다.')
        if action != 'link' and not approved and candidate.get('auto_eligible') is not True:
            raise ValueError('확인되지 않은 변경은 자동 적용할 수 없습니다.')
        if action != 'link' and not approved and candidate.get('weekday_conflict'):
            raise ValueError('날짜와 요일이 다른 일정은 사용자 확인 후 반영해야 합니다.')
        event = {key: candidate['event'][key] for key in EDITABLE if key in candidate['event']}
        if any(isinstance(event.get(key), str) and CREDENTIAL.search(event[key]) for key in ('n', 'loc', 'ti')):
            raise ValueError('인증 정보로 보이는 문자열은 공개 일정에 저장할 수 없습니다.')
        if not source['term_start'] <= event.get('d', '') <= source['term_end']:
            raise ValueError('학기 범위를 벗어난 날짜입니다.')
        if event.get('status') not in (None, '', 'tentative', 'confirmed'):
            raise ValueError('지원하지 않는 일정 확인 상태입니다.')
        target = next((e for e in result if e.get('id') == candidate.get('target_id')), None) if candidate.get('target_id') else None
        if action in ('update', 'delete', 'link'):
            if not target or event_hash(target) != candidate.get('target_hash'):
                raise ValueError('기존 일정이 달라졌습니다. 최신 내용으로 다시 확인해 주세요.')
            if (config or school(target).get('course')) and not belongs(target, candidate.get('course'), config):
                raise ValueError('다른 과목의 기존 일정을 변경할 수 없습니다.')
            if action == 'link':
                applied_ids.append(target['id'])
                continue
            if action == 'delete':
                if not approved or candidate.get('kind') != 'cancellation' or event.get('d') != target.get('d'):
                    raise ValueError('회차 삭제는 사용자 확인이 필요합니다.')
                result.remove(target)
                applied_ids.append(target['id'])
                continue
            if not approved and (not owned(target, candidate, config)
                                 or school(target).get('managed_hash') != event_hash(target)):
                raise ValueError('기존 일정 또는 사용자가 수정한 일정은 확인 후 변경해야 합니다.')
            updated = patch_event(target, event)
        elif action == 'add':
            identifier = 'school-' + digest([source['id'], candidate['id']])[:28]
            existing = next((e for e in result if e.get('id') == identifier), None)
            if existing:
                raise ValueError('이미 등록된 공지 일정입니다. 기존 회차를 확인해 주세요.')
            updated = {key: value for key, value in event.items() if value is not None}
            updated['id'] = identifier
        else:
            raise ValueError('지원하지 않는 일정 작업입니다.')
        term = {'start': source['term_start'], 'end': source['term_end']}
        if any(e is not target and matching_item(e, updated, candidate['course'], config, term) for e in result):
            raise ValueError('기존 일정과 중복될 수 있습니다. 먼저 기존 회차를 확인해 주세요.')
        if action == 'add' and candidate.get('schedule_row') and updated.get('t') == 'class' and any(
                e.get('d') == updated.get('d') and e.get('t') == 'class' and belongs(e, candidate['course'], config)
                for e in result):
            raise ValueError('같은 과목·날짜의 기존 수업이 있습니다. 새로 추가하지 말고 해당 회차를 확인해 주세요.')
        if not approved and any(e is not target and same_course_slot(e, updated, candidate['course'], config)
                                for e in result):
            raise ValueError('같은 과목·날짜·시각의 기존 일정과 겹칩니다. 별도 일정인지 확인해 주세요.')
        updated['school'] = {'source_id': source['id'], 'candidate_id': candidate['id'],
                             'course': candidate['course'], 'source_hash': source['content_hash'],
                             'managed_hash': event_hash(updated)}
        if action == 'update':
            result[result.index(target)] = updated
        else:
            result.append(updated)
        applied_ids.append(updated['id'])
    validate_events(result)
    return result, [value for value in applied_ids if value]
