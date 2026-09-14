"""Generate a minimal RFC 5545 calendar without exporting notes or metadata."""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
import json
from uuid import NAMESPACE_URL, uuid5


KST = timezone(timedelta(hours=9), name="Asia/Seoul")
UID_NAMESPACE = uuid5(NAMESPACE_URL, "https://jdyece25-byte.github.io/schedule/")


def escape_text(value):
    """Escape TEXT property values, including newlines that could inject fields."""
    if not isinstance(value, str):
        raise ValueError("Calendar text must be a string")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(character) < 32 and character not in "\n\t") or ord(character) == 127
           for character in value):
        raise ValueError("Calendar text contains an unsupported control character")
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(";", "\\;").replace(",", "\\,")


def fold_line(line):
    """Fold at 75 UTF-8 octets, counting continuation spaces and preserving codepoints."""
    chunks, current, size = [], [], 0
    for character in line:
        width = len(character.encode("utf-8"))
        if size + width > 75:
            chunks.append("".join(current))
            current, size = [" "], 1
        current.append(character)
        size += width
    chunks.append("".join(current))
    return "\r\n".join(chunks)


def _minutes(value, field):
    if value is not None and (type(value) is not int or not 0 <= value <= 1440):
        raise ValueError(f"{field} must be integer minutes from 0 through 1440")
    return value


def _utc(day, minutes):
    # 1440 represents midnight at the end of the stored local calendar day.
    local = datetime.combine(day, time(), tzinfo=KST) + timedelta(minutes=minutes)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _identity(event, occurrences):
    identifier = event.get("id")
    if identifier is not None:
        if not isinstance(identifier, str) or not identifier.strip():
            raise ValueError("Event id must be nonempty text")
        identity = "id:" + identifier
    else:
        # Legacy rows lack a persistent ID. Exclude time, location, status and
        # notes so ordinary edits/rebuilds do not create a second calendar item.
        # A renamed/rescheduled legacy row has no recoverable identity; durable
        # identity across those changes requires an ID in the source database.
        legacy = json.dumps([event.get(key) for key in ("d", "n", "t", "series")],
                            ensure_ascii=False, separators=(",", ":"))
        occurrences[legacy] += 1
        identity = f"legacy:{legacy}:{occurrences[legacy]}"
    return str(uuid5(UID_NAMESPACE, identity)) + "@schedule.jdyece25-byte.github.io"


def generate_calendar(events, travel, *, generated_at=None):
    """Return UTF-8 CRLF bytes; repeated schedules are already expanded in DB."""
    if not isinstance(events, list) or not isinstance(travel, dict):
        raise ValueError("Expected an events array and travel object")
    locations = travel.get("locations", {})
    if not isinstance(locations, dict):
        raise ValueError("Travel locations must be an object")
    stamp = generated_at if generated_at is not None else datetime.now(timezone.utc)
    if not isinstance(stamp, datetime) or stamp.utcoffset() is None:
        raise ValueError("Calendar generation time must include a timezone")
    dtstamp = stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Schedule App//Calendar Feed//KO",
             "CALSCALE:GREGORIAN"]
    occurrences, identifiers = Counter(), set()
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Each calendar event must be an object")
        day = date.fromisoformat(event["d"])
        if day.isoformat() != event["d"]:
            raise ValueError("Event date must use YYYY-MM-DD")
        name = event.get("n")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Event name must be nonempty text")
        start, end = (_minutes(event.get(field), field) for field in ("s", "e"))
        if start is not None and end is not None and end <= start:
            raise ValueError("Event end must be after its start")
        identifier = _identity(event, occurrences)
        if identifier in identifiers:
            raise ValueError("Duplicate calendar event ID")
        identifiers.add(identifier)
        lines.extend(["BEGIN:VEVENT", "UID:" + identifier, "DTSTAMP:" + dtstamp])
        if start is None or event.get("all_day") is True or event.get("allDay") is True:
            # A DATE with no DTEND is one day in RFC 5545; no time is invented.
            lines.append("DTSTART;VALUE=DATE:" + day.strftime("%Y%m%d"))
        else:
            lines.append("DTSTART:" + _utc(day, start))
            if end is not None:
                lines.append("DTEND:" + _utc(day, end))
            # DATE-TIME without DTEND denotes a point, never a guessed duration.
        lines.append("SUMMARY:" + escape_text(name))
        location = event.get("loc") or locations.get(event.get("lid"))
        if location:
            lines.append("LOCATION:" + escape_text(location))
        status = event.get("status")
        status = status.upper() if isinstance(status, str) else ""
        if status == "CANCELED":
            status = "CANCELLED"
        if not status and event.get("tentative") is True:
            status = "TENTATIVE"
        if status in {"CONFIRMED", "TENTATIVE", "CANCELLED"}:
            lines.append("STATUS:" + status)
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(fold_line(line) for line in lines) + "\r\n").encode("utf-8")
