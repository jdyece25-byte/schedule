"""Conservative date extraction: effective API deadlines or explicit review items.

No LLM, recurrence expansion, inferred week dates, or automatic syllabus changes.
Review evidence is private; event fields contain only the proposed brief title/time.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re

from .sources import digest, safe_source_url

KST = timezone(timedelta(hours=9))
DATE = re.compile(r"(?<!\d)(?:(20\d{2})[./-])?(\d{1,2})[./-](\d{1,2})(?!\d)")
KOREAN_DATE = re.compile(r"(?:(20\d{2})\s*년\s*)?(\d{1,2})\s*월\s*(\d{1,2})\s*일")
TIME = re.compile(r"(?:(오전|오후|저녁|밤)\s*)?(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)")
KOREAN_TIME = re.compile(r"(오전|오후|저녁|밤)\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?(?!\s*반)")
UNCERTAIN = re.compile(r"추정|유력|미정|미공지|확인\s*필요|예상|변경될\s*수|may\s+change|tentative", re.I)
IGNORE_ONLY = re.compile(r"지각|늦은\s*제출|최종\s*잠금|잠금|공개|업로드|게시일|작성일|등록일|unlock|lock_at|late|upload", re.I)
DEADLINE = re.compile(r"마감|제출|due|deadline|assignment|prelab|lab\s*report|HW\s*\d+", re.I)
EXAM = re.compile(r"중간(?:고사|시험)|기말(?:고사|시험)|시험|고사|midterm|final\s*exam|examination", re.I)
LAB = re.compile(r"실습|실험|\bLab\s*\d+|MATLAB", re.I)
CANCEL = re.compile(r"휴강|취소|cancel(?:led|ed)?|대면\s*없음|녹화\s*(?:동영상|수업)", re.I)
RANGE = re.compile(r"\d(?:\([^)]*\))?\s*[~–]\s*(?:\d{1,2}[./월-]|\d{1,2}\s*일)")
TIMETABLE_ROW = re.compile(r"^\s*\d{1,2}\s+(20\d{2}[-./]\d{1,2}[-./]\d{1,2})\s*([월화수목금토일])(?:\s|\()")
ROOM = re.compile(r"(?<!\d)(\d{1,3})\s*동\s*([A-Za-z]?\d{2,4})\s*호")


def _in_term(day, config):
    return date.fromisoformat(config["term"]["start"]) <= day <= date.fromisoformat(config["term"]["end"])


def _kind(text):
    if CANCEL.search(text):
        return "cancellation"
    if DEADLINE.search(text):
        return "deadline"
    if EXAM.search(text):
        return "exam"
    if LAB.search(text):
        return "lab"
    if re.search(r"수업|보강|강의|class|lecture", text, re.I):
        return "class"
    return None


def _time(match):
    period, hour, minute = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if period:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if period in {"오후", "저녁", "밤"} else 0)
    if hour > 24 or minute > 59 or (hour == 24 and minute):
        return None
    return hour * 60 + minute


def _candidate(source, key, kind, title, day, start=None, end=None, *, evidence, auto=False, reason=""):
    identifier = digest([source["id"], key])
    event = {"id": "school-" + identifier[:24], "d": day.isoformat(), "n": title[:180],
             "t": {"deadline": "deadline", "exam": "exam", "lab": "lab"}.get(kind, "class")}
    if start is not None:
        event["s"] = start
    if end is not None and start is not None and end > start:
        event["e"] = end
    if not auto:
        event["status"] = "tentative"
    return {"id": identifier, "source_id": source["id"], "source_hash": source["content_hash"],
            "course": source["course"], "kind": kind, "title": title[:180], "event": event,
            "confidence": "high" if auto else "review", "auto_eligible": auto,
            "evidence": evidence[:500], "reason": reason,
            "source_url": safe_source_url(source.get("source_url", "")),
            "source_updated_at": source["updated_at"]}


def extract_candidates(source, config):
    """Return conservative proposals; caller owns alias/dedup/user-edit reconciliation."""
    if (source.get("course") == "학기 전체" or source.get("publication_state") == "unpublished"
            or source.get("extraction_status") in
            {"summary", "unsupported", "unreadable", "too_large", "no_text", "invalid_due_at"}):
        return []
    title = re.sub(r"[\x00-\x1f\x7f]", " ", str(source.get("title", ""))).strip()
    body = str(source.get("content", ""))
    if source.get("due_at"):
        try:
            moment = datetime.fromisoformat(source["due_at"].replace("Z", "+00:00"))
            if moment.utcoffset() is None:
                return []
            moment = moment.astimezone(KST)
            if not _in_term(moment.date(), config):
                return []
        except (ValueError, TypeError, AttributeError):
            return []
        auto = source.get("kind") == "etl_assignment" and source.get("extraction_status", "parsed") == "parsed"
        # A teacher's recommended deadline and the API deadline may differ.
        # Preserve this conflict for review; never silently discard accepted overrides.
        conflict = bool(re.search(r"권장|권고|추천|가급적|밤\s*11\s*시|23:00|recommended", body, re.I))
        if conflict:
            auto = False
        reason = ("본문의 제출 권장 시각과 시스템 마감은 별도 확인이 필요합니다." if conflict else
                  "로그인한 사용자에게 적용된 eTL 과제 due_at입니다." if auto else
                  "로컬/퀴즈 마감은 eTL 과제의 적용 마감과 대조해 주세요.")
        if CANCEL.search(title + " " + body) or UNCERTAIN.search(title + " " + body):
            auto = False
            reason = "원문이 취소·변경·미정을 표시합니다. 시스템 마감이 유효한지 확인해 주세요."
        return [_candidate(source, "due", "deadline", title, moment.date(), moment.hour * 60 + moment.minute,
                           evidence="due_at=" + source["due_at"], auto=auto, reason=reason)]
    result, seen = [], set()
    year = date.fromisoformat(config["term"]["start"]).year
    for line in body.splitlines():
        line = line.strip()
        context = title + " " + line
        if not line or len(line) > 2000 or RANGE.search(line):
            continue
        table = TIMETABLE_ROW.search(line)
        # A date/weekday/room row in a course timetable remains a class even
        # when its content is only 'TBA', a speaker name, or a pitching topic.
        kind = (_kind(line) or 'class') if table and ROOM.search(line) else _kind(context)
        if not kind:
            continue
        if IGNORE_ONLY.search(line):
            # Mixed due/open/late dates must be separated by a person, not a regex.
            continue
        dates = []
        for expression in (DATE, KOREAN_DATE):
            for match in expression.finditer(line):
                explicit_year, month, day = match.groups()
                try:
                    value = date(int(explicit_year or year), int(month), int(day))
                except ValueError:
                    continue
                if _in_term(value, config) and value not in dates:
                    dates.append(value)
        if len(dates) != 1:
            continue
        weekday_mismatch = bool(table and table[2] != '월화수목금토일'[dates[0].weekday()])
        moments = [value for value in (_time(match) for match in TIME.finditer(line)) if value is not None]
        if not moments:
            moments = [value for value in (_time(match) for match in KOREAN_TIME.finditer(line)) if value is not None]
        start = moments[0] if len(moments) <= 2 and moments else None
        if start is None and kind == 'deadline' and re.search(r'자정|밤\s*12\s*시', line):
            start = 1440
        end = moments[1] if len(moments) == 2 and kind != "deadline" else None
        # Unknown or conflicting duration/time labels stay editable in review.
        reason = "원문에 적힌 날짜입니다. 시간·장소와 기존 수업/마감 변경 여부를 검토해 주세요."
        if UNCERTAIN.search(context):
            reason = "원문이 추정·미정 또는 변경 가능성을 표시합니다. 확정 전에 검토해 주세요."
        if kind == "cancellation":
            reason = "휴강/수업 방식 변경 공지입니다. 해당 기존 일정만 선택하여 반영해 주세요."
        if table:
            reason = '강의계획표의 날짜·요일·강의실입니다. 기존 반복 수업과 다른 요일 또는 휴강을 확인해 주세요.'
        if weekday_mismatch:
            reason = '강의계획표의 날짜와 요일이 일치하지 않습니다. 원문을 다시 확인해 주세요.'
        key = [kind, dates[0].isoformat(), re.sub(r"\s+", " ", line)]
        candidate = _candidate(source, key, kind, title, dates[0], start, end,
                               evidence=line, reason=reason)
        if table:
            candidate['schedule_row'] = True
            candidate['weekday_conflict'] = weekday_mismatch
        room = ROOM.search(line)
        if room:
            candidate['event']['loc'] = f'서울대 {room[1]}동 {room[2].upper()}호'
        if candidate["id"] not in seen:
            result.append(candidate)
            seen.add(candidate["id"])
        if len(result) >= 100:
            break
    return result
