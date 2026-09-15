"""Small, private course context shared by collection and schedule planning.

Acknowledging a notice does not discard what it says. These excerpts are source
evidence, never model instructions or an authorization to change the calendar.
"""
import re

ANALYSIS_VERSION = 2
DATE = re.compile(r"20\d{2}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}\s*월\s*\d{1,2}\s*일|(?<!\d)\d{1,2}/\d{1,2}(?!\d)")
RULE = re.compile(r"시험|중간|기말|휴강|보강|수업|강의실|마감|제출|출석|보고서|발표|과제|준비물|지참|요일|주차|변경|종강|"
                  r"exam|deadline|due|submit|attendance|schedule|prelab|lab\s*report|pitch|grading|syllabus|may\s+change", re.I)
SECRET = re.compile(r"github_pat_[\w]+|gh[pousr]_[\w]+|\bbearer\s+\S+|[A-Za-z0-9_+/=-]{48,}", re.I)
ALIASES = {'leadership': ('공도리', '공학도의 도전', '리더십'), 'em': ('기전', '전자기'),
           'logic': ('논설', '논리설계'), 'writing': ('대글', '글쓰기'),
           'macro': ('거시',), 'power': ('전력시장', 'power system economics', 'pse')}


def clean(value, limit=1800):
    text = value if isinstance(value, str) else ''
    text = re.sub(r'https?://\S+', '[원문 링크]', text)
    text = SECRET.sub('[인증 정보 제외]', text)
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
            candidates.append((0 if DATE.search(line) else 1, position, line))
            truncated |= len(full) > 420
    excerpts, seen, remaining = [], set(), 1800
    for _, _, line in sorted(candidates):
        signature = re.sub(r'\s+', '', line).lower()
        if signature in seen:
            continue
        if len(line) > remaining or len(excerpts) >= 24:
            truncated = True
            continue
        excerpts.append(line); seen.add(signature); remaining -= len(line)
    return {'version': ANALYSIS_VERSION, 'status': status, 'excerpts': excerpts,
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
    items.sort(key=lambda i: (not date_bearing(i),
                             i.get('source_kind') not in ('etl_syllabus', 'etl_file', 'etl_external_file')))
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
            sources.append({'id': item.get('id'), 'source_hash': item.get('content_hash'),
                            'course': clean(item.get('course'), 100), 'title': clean(item.get('title'), 180),
                            'updated_at': item.get('updated_at'), 'review_state': item.get('state'),
                            'extraction_status': knowledge.get('status', item.get('extraction_status', 'unknown')),
                            'incomplete': bool(knowledge.get('incomplete', not bool(knowledge)) or source_limited), 'excerpts': selected})
        if len(sources) >= 60 or used >= limit:
            break
    collection = index.get('collectors') if isinstance(index.get('collectors'), dict) else {}
    collectors = {key: {field: value.get(field) for field in ('state', 'last_checked', 'source_count', 'issue_count')}
                  for key, value in collection.items() if isinstance(value, dict)}
    limited = budget_limited or len(sources) < len(items)
    return {'available': True, 'updated_at': index.get('updated_at'), 'collectors': collectors,
            'sources': sources, 'limited': limited,
            'incomplete': limited or not collectors or any(value.get('state') not in ('ok', 'success', 'healthy', 'idle') for value in collectors.values())
                          or any(source['incomplete'] for source in sources)}
