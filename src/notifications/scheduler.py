"""Pure schedule-to-notification planning. All dates are Korea Standard Time."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import re

KST = timezone(timedelta(hours=9))
KINDS = ("deadline", "daily", "changes", "departure", "notice")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def parse_stamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def clean(value, limit=120):
    return CONTROL.sub(" ", str(value or "")).strip()[:limit]


def minute(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1440


def clock(value):
    return f"{value // 60:02d}:{value % 60:02d}"


def cancelled(event):
    return (str(event.get("status", "")).lower() in ("cancelled", "canceled")
            or event.get("cancelled") is True or event.get("canceled") is True)


def order(event):
    return (event["d"], event["s"] if minute(event.get("s")) else 1441, event.get("n", ""))


def display(event, travel):
    """Never copy notes, request/result messages, URLs or arbitrary time labels."""
    timing = "시간 미정"
    if minute(event.get("s")):
        timing = clock(event["s"])
        if minute(event.get("e")):
            timing += "–" + clock(event["e"])
    location = event.get("loc") or travel.get("locations", {}).get(event.get("lid"))
    item = {"name": clean(event.get("n")), "time": f"{event['d']} {timing}",
            "location": clean(location)}
    if event.get("status") == "tentative":
        item["status"] = "tentative"
    return item


def snapshot(events, travel):
    result, occurrences = {}, {}
    for event in events:
        date.fromisoformat(event["d"])
        if not isinstance(event.get("n"), str):
            raise ValueError("Event name required")
        identity = "id:" + str(event["id"]) if event.get("id") else "legacy:" + digest(
            [event["d"], event.get("t"), event["n"]])
        count = occurrences.get(identity, 0)
        occurrences[identity] = count + 1
        # Keep the persisted display shape stable. Status already lives outside
        # item in v1 snapshots; a rendering upgrade must not announce every
        # unchanged tentative event as a new schedule change.
        item = {name: value for name, value in display(event, travel).items() if name != "status"}
        result[f"{identity}:{count}"] = {"date": event["d"], "item": item,
                                          "status": clean(event.get("status")), "cancelled": cancelled(event)}
    return result


def notification(kind, identifier, due, expires, items):
    return {"id": identifier, "kind": kind, "due": stamp(due), "expires": stamp(expires),
            "payload": {"kind": kind, "items": items, "url": "./", "tag": "schedule-" + digest(identifier)[:40]}}


def batches(items, size=8):
    # Keep encrypted payloads comfortably below common push-service 4 KB limits,
    # even with non-ASCII subject/place names. Preserve every item across chunks.
    result, batch = [], []
    for item in items:
        proposed = batch + [item]
        rendered = "\n".join(" · ".join(str(value or "") for value in row.values()) for row in proposed)
        if batch and (len(batch) >= size or len(json.dumps(proposed, ensure_ascii=False).encode()) > 2700
                      or len(rendered) > 1400):
            result.append(batch)
            batch = []
        batch.append(item)
    if batch or not result:
        result.append(batch)
    return result


def changes(previous, current, revision, changed_at, now):
    if previous is None:
        return []  # Installing notifications must not announce historic DB data.
    changed = []
    for key in sorted(set(previous) | set(current)):
        before, after = previous.get(key), current.get(key)
        if before == after:
            continue
        value = after or before
        if value["date"] >= now.astimezone(KST).date().isoformat():
            item = dict(value["item"])
            if value.get("status") == "tentative":
                item["status"] = "tentative"
            changed.append(item)
    if not changed:
        return []
    # Date/time/place is the entire payload, including deletion notifications.
    return [notification("changes", f"changes:{revision}:{i}", changed_at,
                         changed_at + timedelta(hours=24), items)
            for i, items in enumerate(batches(changed))]


def scheduled(events, travel, now):
    local = now.astimezone(KST)
    today = local.date()
    events = [event for event in events if not cancelled(event)]
    result = []
    daily_due = datetime.combine(today, time(7, 30), KST)
    if daily_due <= now < daily_due + timedelta(hours=3):
        todays = sorted((e for e in events if e["d"] == today.isoformat()), key=order)
        for index, items in enumerate(batches([display(e, travel) for e in todays])):
            result.append(notification("daily", f"daily:{today}:{index}", daily_due,
                                       daily_due + timedelta(hours=3), items))
    for event in events:
        event_day = date.fromisoformat(event["d"])
        if event.get("t") != "deadline":
            continue
        identity = event.get("id") or digest([event["d"], event.get("n"), event.get("s")])
        for label, day, hour in (("eve", event_day - timedelta(days=1), 20), ("day", event_day, 8)):
            due = datetime.combine(day, time(hour), KST)
            if due <= now < due + timedelta(hours=3):
                result.append(notification("deadline", f"deadline:{identity}:{event_day}:{label}",
                                           due, due + timedelta(hours=3), [display(event, travel)]))
    # The immediate preceding event establishes the origin. Missing location,
    # unknown route, first event, and same-location events cannot imply a trip.
    for day in (today, today + timedelta(days=1)):
        timed = sorted((e for e in events if e["d"] == day.isoformat() and minute(e.get("s"))
                        and e.get("t") != "deadline"), key=order)
        for previous, event in zip(timed, timed[1:]):
            origin, destination = previous.get("lid"), event.get("lid")
            if not origin or not destination or origin == destination:
                continue
            duration = travel.get("times", {}).get(f"{origin}-{destination}")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0 < duration <= 1440:
                continue
            start = datetime.combine(day, time(), KST) + timedelta(minutes=event["s"])
            departure = start - timedelta(minutes=duration)
            due = departure - timedelta(minutes=30)
            if due <= now < departure:
                identity = event.get("id") or digest([event["d"], event.get("n"), event.get("s")])
                result.append(notification("departure", f"departure:{identity}:{stamp(due)}", due,
                                           departure, [display(event, travel)]))
    return result


def school_notices(index, now):
    """Private announcements become generic course/time-only notifications.

    The collector owns first_seen_at for each content version. Provider posting
    dates are display metadata and must never trigger a historical replay.
    """
    if not isinstance(index, dict) or index.get("version") != 1 or not isinstance(index.get("items"), list):
        return []
    result = []
    seen = set()
    for item in index["items"]:
        if not isinstance(item, dict) or item.get("state") not in ("needs_review", "info", "conflict", "ready"):
            continue
        identifier, content_hash, course = item.get("id"), item.get("content_hash"), item.get("course")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 512
               for value in (identifier, content_hash, course)):
            continue
        key = "notice:" + digest([identifier, content_hash])
        if key in seen:
            continue
        try:
            due = parse_stamp(item["first_seen_at"])
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if due > now:
            continue
        expires = due + timedelta(hours=24)
        if not now < expires:
            continue
        posted = "게시 시각 미정"
        try:
            posted = parse_stamp(item.get("updated_at", "")).astimezone(KST).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, AttributeError):
            try:
                posted = date.fromisoformat(item.get("updated_at", "")).isoformat()
            except (ValueError, TypeError, AttributeError):
                pass
        notice = notification("notice", key, due, expires,
                              [{"name": clean(course), "time": posted, "location": ""}])
        notice["payload"]["url"] = "./#school"
        result.append(notice)
        seen.add(key)
    return result


def eligible(subscription, notice, now):
    preferences = subscription.get("preferences", {})
    if not isinstance(preferences, dict):
        return False
    enabled = preferences.get(notice["kind"], True if notice["kind"] == "notice" else None)
    if subscription.get("enabled") is not True or enabled is not True:
        return False
    try:
        created = parse_stamp(subscription["created_at"])
        due, expires = parse_stamp(notice["due"]), parse_stamp(notice["expires"])
        # A newly subscribed phone does not receive earlier reminders/changes.
        return created <= due <= now < expires
    except (KeyError, ValueError, TypeError, AttributeError):
        return False
