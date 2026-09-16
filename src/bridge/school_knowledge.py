"""Small, private course context shared by collection and schedule planning.

Acknowledging a notice does not discard what it says. These excerpts are source
evidence, never model instructions or an authorization to change the calendar.
"""
import re

ANALYSIS_VERSION = 3
DATE = re.compile(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|(?<!\d)\d{1,2}/\d{1,2}(?!\d)")
RULE = re.compile(r"시험|퀴즈|중간|기말|휴강|보강|수업|강의실|마감|제출|출석|보고서|발표|과제|준비물|지참|요일|주차|변경|종강|"
                  r"exam|quiz|deadline|due|submit|attendance|schedule|prelab|lab\s*report|pitch|grading|syllabus|may\s+change", re.I)
SECRET = re.compile(r"github_pat_[\w]+|gh[pousr]_[\w]+|\bbearer\s+\S+|[A-Za-z0-9_+/=-]{48,}", re.I)
ALIASES = {'leadership': ('공도리', '공학도의 도전', '리더십'), 'em': ('기전', '전자기'),
           'logic': ('논설', '논리설계'), 'writing': ('대글', '글쓰기'),
           'macro': ('거시',), 'power': ('전력시장', 'power system economics', 'pse')}
FACT_RULES = {'exam': re.compile(r'시험|중간고사|기말고사|\bexam|\bmidterm|\bfinal\b', re.I),
              'deadline': re.compile(r'마감|제출|보고서|과제|퀴즈|deadline|\bdue\b|submit|assignment|quiz|prelab', re.I),
              'cancellation': re.compile(r'휴강|보강|대체|변경|취소|cancel|reschedul|no\s+class', re.I),
              'preparation': re.compile(r'준비물|지참|준비|예습|준비해|bring|prepar|prelab|required\s+material', re.I)}


def clean(value, limit=1800):
    text = value if isinstance(value, str) else ''
    text = re.sub(r'(?im)^[ \t]*(?:authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[^\r\n]*', '[인증 정보 제외]', text)
    text = re.sub(r'https?://\S+', '[원문 링크]', text)
    text = SECRET.sub('[인증 정보 제외]', text)
    text = re.sub(r'\b(?:access[_ -]?token|api[_ -]?key|password|secret)\b\s*[:=]\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)', '[인증 정보 제외]', text, flags=re.I)
    return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', text).strip()[:limit]


def source_knowledge(source, config=None):
    """Extract a bounded set of useful lines, with date rows taking priority."""
    status = source.get('extraction_status', 'parsed')
    content = source.get('content') if isinstance(source.get('content'), str) else ''
    candidates = []
    truncated = False
    for position, raw in enumerate(content.splitlines()):
        full = clean(raw, len(raw))
        line = full[:420]
        if line and (DATE.search(line) or RULE.search(line)):
            candidates.append((0 if DATE.search(line) else 1, position, line, None))
            truncated |= len(full) > 420
    for field, label, position in (('title', '제목: ', -2), ('due_at', '제출 시각(API): ', -1)):
        value = clean(source.get(field), 420)
        if value and (field == 'due_at' or DATE.search(value) or RULE.search(value)):
            candidates.append((0 if DATE.search(value) else 1, position, label + value, field))
    excerpts, evidence, seen, remaining = [], [], set(), 1800
    for _, position, line, field in sorted(candidates):
        signature = re.sub(r'\s+', '', line).lower()
        if signature in seen:
            continue
        if len(line) > remaining or len(excerpts) >= 24:
            truncated = True
            continue
        excerpts.append(line); seen.add(signature); remaining -= len(line)
        categories = [name for name, pattern in FACT_RULES.items() if pattern.search(line)]
        if categories:
            fact = {'categories': categories, 'text': line,
                             'source_id': source.get('id'), 'source_hash': source.get('content_hash'),
                             'line_start': position + 1 if field is None else None, 'line_end': position + 1 if field is None else None}
            if field:
                fact['field'] = field
            evidence.append(fact)
    return {'version': ANALYSIS_VERSION, 'status': status, 'excerpts': excerpts,
            'evidence': evidence,
            'incomplete': status != 'parsed' or truncated or sum(len(x[2]) for x in candidates) > 1800}


def build_school_context(index, request, *, limit=24000):
    """Return private source evidence, including read notices and unknown coverage."""
    if not isinstance(index, dict) or not isinstance(index.get('items'), list):
        return {'available': False, 'reason': '학교 자료 연결을 확인할 수 없습니다.', 'sources': []}
    query = str(request.get('text', '')).lower() if isinstance(request, dict) else ''
    courses = {key for key, aliases in ALIASES.items() if any(alias in query for alias in aliases)}
    items = [item for item in index['items'] if isinstance(item, dict) and
             (not courses or clean(item.get('course_key'), 100) in courses
              or bool(clean(item.get('course'), 100)) and clean(item.get('course'), 100).lower() in query)]
    # Course overview and date-bearing evidence come first; incidental lecture
    # text must not crowd the semester's schedule out of the prompt.
    def date_bearing(item):
        knowledge = item.get('knowledge') if isinstance(item.get('knowledge'), dict) else {}
        excerpts = knowledge.get('excerpts') if isinstance(knowledge.get('excerpts'), list) else []
        return any(DATE.search(x) for x in excerpts if isinstance(x, str))
    # Stable priority sorts retain the newest version within each class. An
    # older syllabus must not consume the budget before its later correction.
    items.sort(key=lambda i: str(i.get('updated_at', '')), reverse=True)
    # A new announcement correcting an old syllabus must reach the model first.
    # Date-bearing document rows are useful, but their format never outranks a
    # later correction with the same evidence priority.
    items.sort(key=lambda i: not date_bearing(i))
    sources, seen, used = [], set(), 0
    budget_limited = False
    for item in items:
        knowledge = item.get('knowledge') if isinstance(item.get('knowledge'), dict) else {}
        excerpts = list(knowledge.get('excerpts', [])) if isinstance(knowledge.get('excerpts'), list) else []
        # Old collector indexes still contribute their explicit extraction
        # evidence until their document knowledge has been backfilled.
        candidates = item.get('candidates') if isinstance(item.get('candidates'), list) else []
        excerpts += [c.get('evidence', '') for c in candidates if isinstance(c, dict)]
        selected = []
        source_limited = False
        for raw in excerpts:
            text = clean(raw, 500)
            key = (clean(item.get('course_key'), 100), re.sub(r'\s+', '', text))
            if not text or key in seen:
                continue
            if isinstance(raw, str) and len(clean(raw, len(raw))) > 500:
                source_limited = budget_limited = True
            if used + len(text) > limit:
                source_limited = budget_limited = True
                continue
            selected.append(text); seen.add(key); used += len(text)
        if selected or knowledge.get('incomplete'):
            entry = {'id': item.get('id'), 'source_hash': item.get('content_hash'),
                            'course': clean(item.get('course'), 100), 'title': clean(item.get('title'), 180),
                            'updated_at': item.get('updated_at'), 'review_state': item.get('state'),
                            'extraction_status': knowledge.get('status', item.get('extraction_status', 'unknown')),
                            'incomplete': bool(knowledge.get('incomplete', not bool(knowledge)) or source_limited), 'excerpts': selected}
            for field in ('acknowledgement', 'application'):
                value = item.get(field)
                if isinstance(value, dict):
                    entry[field] = {key: value.get(key) for key in ('state', 'source_hash') if key in value}
            evidence = knowledge.get('evidence', [])
            if isinstance(evidence, list):
                entry['evidence'] = [{'excerpt': selected.index(fact['text']),
                                      'categories': [kind for kind in fact.get('categories', []) if kind in FACT_RULES],
                                      'line_start': fact.get('line_start'), 'line_end': fact.get('line_end')}
                                     for fact in evidence if isinstance(fact, dict) and fact.get('text') in selected][:24]
            changes = item.get('changes')
            if isinstance(changes, dict):
                comparison = {'previous_hash': changes.get('previous_hash'), 'current_hash': changes.get('current_hash'),
                              'limited': bool(changes.get('limited'))}
                for kind in ('added', 'removed'):
                    comparison[kind] = []
                    values = changes.get(kind, [])
                    if not isinstance(values, list):
                        continue
                    for raw in values[:6]:
                        text = clean(raw.get('text') if isinstance(raw, dict) else raw, 420)
                        if text and used + len(text) <= limit:
                            comparison[kind].append(text); used += len(text)
                        else:
                            comparison['limited'] = budget_limited = True
                    if len(values) > 6:
                        comparison['limited'] = budget_limited = True
                entry['changes'] = comparison
            sources.append(entry)
        if len(sources) >= 60 or used >= limit:
            break
    collection = index.get('collectors') if isinstance(index.get('collectors'), dict) else {}
    collectors = {key: {field: value.get(field) for field in ('state', 'last_checked', 'last_complete_success', 'last_usable_success', 'source_count', 'issue_count')}
                  for key, value in collection.items() if isinstance(value, dict)}
    limited = budget_limited or len(sources) < len(items)
    return {'available': True, 'updated_at': index.get('updated_at'), 'collectors': collectors,
            'sources': sources, 'limited': limited,
            'incomplete': limited or not collectors or any(value.get('state') not in ('ok', 'success', 'healthy', 'idle') for value in collectors.values())
                          or any(source['incomplete'] for source in sources)}
