import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from src.bridge.github import GitHubError
from src.notifications import scheduler as schedule
from src.notifications.sender import (Busy, STATE_PATH, StateStore, disable_expired,
                                      empty_state, load_school_notices, run, validate_subscription)


def at(day=14, hour=7, minute=32):
    return datetime(2026, 9, day, hour, minute, tzinfo=schedule.KST)


def event(**changes):
    return {"id": "one", "d": "2026-09-14", "t": "class", "n": "테스트 과목",
            "s": 600, "e": 675, "lid": "a", "no": "SECRET NOTES", **changes}


TRAVEL = {"locations": {"a": "첫 강의실", "b": "두 번째 강의실"}, "times": {"a-b": 40}}


def subscription(**changes):
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return {"version": 1, "device_id": "phone1", "enabled": True, "timezone": "Asia/Seoul",
            "created_at": schedule.stamp(at(13)), "updated_at": schedule.stamp(at(13)),
            "preferences": {kind: True for kind in schedule.KINDS},
            "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/test",
                             "expirationTime": None,
                             "keys": {"p256dh": encode(b"\x04" + b"\0" * 64), "auth": encode(b"a" * 16)}}, **changes}


def school_item(**changes):
    return {"id": "notice-one", "content_hash": "version-one", "course": "테스트 과목",
            "title": "PRIVATE ANNOUNCEMENT TITLE", "source_url": "https://private.invalid/token",
            "first_seen_at": schedule.stamp(at()), "updated_at": schedule.stamp(at(hour=6)),
            "state": "needs_review", "candidates": [{"evidence": "PRIVATE EVIDENCE"}], **changes}


class FakeGitHub:
    def __init__(self, events=None, now=None):
        self.now = now or at()
        self.revision = "revision1"
        self.files = {("owner/schedule", "DB/events.json"): events or [event()],
                      ("owner/schedule", "DB/travel.json"): deepcopy(TRAVEL),
                      ("owner/private", "subscriptions/phone1.json"): subscription()}
        self.versions = {}
        self.writes = []
        self.private = True

    def api(self, endpoint):
        if endpoint == "repos/owner/private":
            return {"private": self.private}
        if "/git/commits/" in endpoint:
            return {"committer": {"date": schedule.stamp(self.now)}}
        raise AssertionError(endpoint)

    def head(self, repo):
        return self.revision if repo == "owner/schedule" else "queue-head"

    def tree(self, repo, revision):
        return {path: "sha" for owner, path in self.files if owner == repo}

    def read_json(self, repo, path, ref="main"):
        return deepcopy(self.files.get((repo, path))), self.versions.get((repo, path))

    def put_json(self, repo, path, value, sha=None, **kwargs):
        key = (repo, path)
        if sha != self.versions.get(key):
            raise GitHubError(409, "conflict")
        self.files[key] = deepcopy(value)
        self.versions[key] = str(int(self.versions.get(key, "0")) + 1)
        self.writes.append(path)
        return self.versions[key]


class Transport:
    def __init__(self, status=201):
        self.status = status
        self.sent = []

    def send(self, subscription, notice, now):
        self.sent.append(deepcopy(notice))
        return self.status


class ScheduleTests(unittest.TestCase):
    def test_daily_uses_korea_day_even_when_utc_previous_day(self):
        notices = schedule.scheduled([event()], TRAVEL, at().astimezone(timezone.utc))
        self.assertEqual([n["kind"] for n in notices], ["daily"])
        self.assertEqual(notices[0]["payload"]["items"][0]["time"], "2026-09-14 10:00–11:15")

    def test_daily_catchup_window_and_empty_day(self):
        self.assertFalse(schedule.scheduled([], TRAVEL, at(hour=7, minute=29)))
        self.assertEqual(schedule.scheduled([], TRAVEL, at())[0]["payload"]["items"], [])
        self.assertFalse(schedule.scheduled([], TRAVEL, at(hour=10, minute=30)))

    def test_deadline_evening_and_morning_but_not_exam(self):
        events = [event(t="deadline", d="2026-09-15", s=1440, e=None), event(id="exam", t="exam", d="2026-09-15")]
        for now, label in ((at(hour=20), "eve"), (at(day=15, hour=8), "day")):
            notices = [n for n in schedule.scheduled(events, TRAVEL, now) if n["kind"] == "deadline"]
            self.assertEqual(len(notices), 1)
            self.assertTrue(notices[0]["id"].endswith(label))
            self.assertEqual(notices[0]["payload"]["items"][0]["time"], "2026-09-15 24:00")

    def test_deadline_year_boundary(self):
        now = datetime(2026, 12, 31, 20, 2, tzinfo=schedule.KST)
        notices = schedule.scheduled([event(t="deadline", d="2027-01-01")], TRAVEL, now)
        self.assertEqual(len(notices), 1)

    def test_departure_is_thirty_before_departure_using_directional_route(self):
        events = [event(s=480, e=540), event(id="next", lid="b", s=660, e=720)]
        notices = schedule.scheduled(events, TRAVEL, at(hour=9, minute=52))
        self.assertEqual([n["kind"] for n in notices], ["daily", "departure"])
        departure = notices[1]
        self.assertEqual(schedule.parse_stamp(departure["due"]), at(hour=9, minute=50))
        self.assertFalse(any(n["kind"] == "departure" for n in schedule.scheduled(events, TRAVEL, at(hour=10, minute=20))))

    def test_departure_never_invents_first_unknown_same_or_reverse_route(self):
        next_event = event(id="next", lid="b", s=660)
        for events, travel in (([next_event], TRAVEL),
                               ([event(lid=None), next_event], TRAVEL),
                               ([event(lid="b"), next_event], TRAVEL),
                               ([event(), next_event], {"times": {"b-a": 40}})):
            self.assertFalse(any(n["kind"] == "departure" for n in schedule.scheduled(events, travel, at(hour=9, minute=52))))

    def test_next_day_departure_warning_can_be_due_before_midnight(self):
        events = [event(d="2026-09-15", s=5, e=10),
                  event(id="next", d="2026-09-15", s=50, e=60, lid="b")]
        notices = schedule.scheduled(events, TRAVEL, at(hour=23, minute=42))
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["kind"], "departure")
        self.assertEqual(schedule.parse_stamp(notices[0]["due"]), at(hour=23, minute=40))

    def test_null_times_sort_safely_and_cancelled_events_skip_clock_alerts(self):
        events = [event(id="unknown", s=None, e=None), event(),
                  event(id="cancel", t="deadline", status="cancelled")]
        notices = schedule.scheduled(events, TRAVEL, at(hour=8))
        self.assertEqual([notice["kind"] for notice in notices], ["daily"])
        items = notices[0]["payload"]["items"]
        self.assertEqual(len(items), 2)
        self.assertIn("시간 미정", items[-1]["time"])

    def test_cancellation_and_tentative_status_changes_are_detected(self):
        before = schedule.snapshot([event()], TRAVEL)
        for update in ({"status": "cancelled"}, {"cancelled": True}, {"status": "tentative"}):
            after = schedule.snapshot([event(**update)], TRAVEL)
            notices = schedule.changes(before, after, "newrevision", at(), at())
            self.assertEqual(len(notices), 1)
            expected = {"name", "time", "location"}
            if update.get("status") == "tentative":
                expected.add("status")
            self.assertEqual(set(notices[0]["payload"]["items"][0]), expected)

    def test_tentative_rendering_preserves_existing_snapshot_without_change_replay(self):
        previous = {"id:one:0": {
            "date": "2026-09-14", "item": {"name": "테스트 과목", "time": "2026-09-14 10:00–11:15", "location": "첫 강의실"},
            "status": "tentative", "cancelled": False,
        }}
        current = schedule.snapshot([event(status="tentative")], TRAVEL)
        self.assertEqual(current, previous)
        self.assertFalse(schedule.changes(previous, current, "render-upgrade", at(), at()))
        changed = schedule.snapshot([event(status="tentative", s=660)], TRAVEL)
        item = schedule.changes(previous, changed, "time-update", at(), at())[0]["payload"]["items"][0]
        self.assertEqual(item["status"], "tentative")
        self.assertIn("11:00", item["time"])

    def test_only_exact_tentative_status_is_allowed_into_payloads(self):
        self.assertEqual(schedule.display(event(status="tentative"), TRAVEL)["status"], "tentative")
        for status in ("confirmed", "private secret", "TENTATIVE", None):
            self.assertNotIn("status", schedule.display(event(status=status), TRAVEL))

    def test_tentative_deadlines_keep_evening_and_morning_reminders(self):
        values = [event(t="deadline", status="tentative", d="2026-09-15")]
        for moment, due in ((at(hour=20, minute=2), at(hour=20, minute=0)),
                            (at(day=15, hour=8, minute=2), at(day=15, hour=8, minute=0))):
            notices = [n for n in schedule.scheduled(values, TRAVEL, moment) if n["kind"] == "deadline"]
            self.assertEqual(len(notices), 1)
            self.assertEqual(schedule.parse_stamp(notices[0]["due"]), due)
            self.assertEqual(notices[0]["payload"]["items"][0]["status"], "tentative")
            self.assertTrue(schedule.eligible(subscription(), notices[0], moment))

    def test_lab_and_exam_departures_use_known_routes_and_preserve_tentative_marker(self):
        for kind in ("lab", "exam"):
            values = [event(s=480, e=540), event(id="next", t=kind, status="tentative", lid="b", s=660, e=720)]
            notices = [n for n in schedule.scheduled(values, TRAVEL, at(hour=9, minute=52)) if n["kind"] == "departure"]
            self.assertEqual(len(notices), 1)
            self.assertEqual(notices[0]["payload"]["items"][0]["status"], "tentative")
            self.assertFalse(any(n["kind"] == "departure" for n in schedule.scheduled(values, {}, at(hour=9, minute=52))))

    def test_display_excludes_notes_arbitrary_time_labels_and_control_characters(self):
        value = schedule.display(event(n="과목\n이름", no="secret", ti="secret token", s=None, e=None), TRAVEL)
        self.assertEqual(set(value), {"name", "time", "location"})
        self.assertEqual(value["name"], "과목 이름")
        self.assertNotIn("secret", str(value))
        self.assertIn("시간 미정", value["time"])

    def test_initial_snapshot_and_note_only_edits_do_not_send_changes(self):
        before = schedule.snapshot([event()], TRAVEL)
        after = schedule.snapshot([event(no="changed private note")], TRAVEL)
        self.assertFalse(schedule.changes(None, after, "one", at(), at()))
        self.assertFalse(schedule.changes(before, after, "two", at(), at()))

    def test_added_updated_deleted_changes_and_old_events_filtered(self):
        old = schedule.snapshot([event(), event(id="delete"), event(id="old", d="2026-01-01")], TRAVEL)
        new = schedule.snapshot([event(s=660), event(id="added", n="추가 과목")], TRAVEL)
        notices = schedule.changes(old, new, "newrevision", at(), at())
        self.assertEqual(len(notices[0]["payload"]["items"]), 3)
        self.assertNotIn("2026-01-01", str(notices))

    def test_new_subscription_and_preferences_gate_historical_reminders(self):
        notice = schedule.scheduled([event()], TRAVEL, at())[0]
        self.assertTrue(schedule.eligible(subscription(), notice, at()))
        self.assertFalse(schedule.eligible(subscription(created_at=schedule.stamp(at(minute=31))), notice, at()))
        self.assertFalse(schedule.eligible(subscription(enabled=False), notice, at()))
        self.assertFalse(schedule.eligible(subscription(preferences={"daily": False}), notice, at()))

    def test_long_daily_summaries_split_without_disclosing_non_display_fields(self):
        import json
        for name, location in (("가" * 120, "나" * 120), ("A" * 120, "B" * 120)):
            values = [event(id=str(i), n=name, loc=location) for i in range(20)]
            notices = schedule.scheduled(values, TRAVEL, at())
            self.assertEqual(sum(len(n["payload"]["items"]) for n in notices), 20)
            self.assertTrue(all(len(json.dumps(n["payload"], ensure_ascii=False).encode()) < 3500 for n in notices))
            self.assertTrue(all(len("\n".join(" · ".join(item.values()) for item in n["payload"]["items"])) <= 1400
                                for n in notices))


class SenderTests(unittest.TestCase):
    def execute(self, github, transport, changes_only=False):
        return run(github, transport, "owner/private", "owner/schedule", changes_only, now=lambda: github.now)

    def test_first_run_only_sends_due_daily_then_deduplicates_without_write_churn(self):
        github, transport = FakeGitHub(), Transport()
        result = self.execute(github, transport)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(transport.sent[0]["kind"], "daily")
        writes = len(github.writes)
        self.assertEqual(self.execute(github, transport)["status"], "idle")
        self.assertEqual(len(github.writes), writes)
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(all(path.startswith("notification-state/") for path in github.writes))

    def test_changes_only_skips_daily_and_announces_subsequent_db_edits(self):
        github, transport = FakeGitHub(), Transport()
        self.execute(github, transport, True)
        self.assertFalse(transport.sent)
        github.files[("owner/schedule", "DB/events.json")][0]["s"] = 660
        github.revision = "revision2"
        self.assertEqual(self.execute(github, transport, True)["sent"], 1)
        self.assertEqual(transport.sent[0]["kind"], "changes")
        self.assertEqual(self.execute(github, transport, True)["sent"], 0)

    def test_retries_429_500_and_network_failure_without_false_delivery(self):
        for code in (0, 429, 500, 403):
            with self.subTest(code=code):
                github, transport = FakeGitHub(), Transport(code)
                self.assertEqual(self.execute(github, transport)["retry"], 1)
                self.assertEqual(github.files[("owner/private", STATE_PATH)]["delivered"], {})
                transport.status = 201
                self.assertEqual(self.execute(github, transport)["sent"], 1)
                self.assertEqual(self.execute(github, transport)["sent"], 0)

    def test_change_outbox_survives_failure_until_retry(self):
        github, transport = FakeGitHub(), Transport()
        self.execute(github, transport, True)
        github.files[("owner/schedule", "DB/events.json")][0]["s"] = 660
        github.revision = "revision2"
        transport.status = 503
        self.assertEqual(self.execute(github, transport, True)["retry"], 1)
        self.assertEqual(len(github.files[("owner/private", STATE_PATH)]["outbox"]), 1)
        transport.status = 201
        self.assertEqual(self.execute(github, transport, True)["sent"], 1)

    def test_expired_endpoints_disabled_without_deleting_subscription(self):
        for code in (404, 410):
            github, transport = FakeGitHub(), Transport(code)
            self.assertEqual(self.execute(github, transport)["expired"], 1)
            value = github.files[("owner/private", "subscriptions/phone1.json")]
            self.assertFalse(value["enabled"])
            self.assertIn("subscription", value)

    def test_concurrent_subscription_refresh_is_not_disabled(self):
        github = FakeGitHub()
        stale = subscription()
        github.files[("owner/private", "subscriptions/phone1.json")]["subscription"]["endpoint"] += "-refreshed"
        disable_expired(github, "owner/private", stale, at())
        self.assertTrue(github.files[("owner/private", "subscriptions/phone1.json")]["enabled"])

    def test_live_lease_blocks_parallel_sender_and_expired_lease_recovers(self):
        github = FakeGitHub()
        first = StateStore(github, "owner/private", lambda: github.now)
        second = StateStore(github, "owner/private", lambda: github.now)
        first.acquire()
        with self.assertRaises(Busy):
            second.acquire()
        github.now += timedelta(minutes=4)
        second.acquire()
        with self.assertRaises(Busy):
            first.save()

    def test_disabled_and_missing_preferences_never_send(self):
        for value in (subscription(enabled=False), subscription(preferences={})):
            github, transport = FakeGitHub(), Transport()
            github.files[("owner/private", "subscriptions/phone1.json")] = value
            self.execute(github, transport)
            self.assertFalse(transport.sent)

    def test_public_queue_is_rejected_before_any_state_writes_or_sends(self):
        github, transport = FakeGitHub(), Transport()
        github.private = False
        with self.assertRaises(ValueError):
            self.execute(github, transport)
        self.assertFalse(github.writes)
        self.assertFalse(transport.sent)

    def test_explicit_test_request_bypasses_type_preferences_and_deduplicates(self):
        github, transport = FakeGitHub(), Transport()
        github.files[("owner/private", "subscriptions/phone1.json")].update(
            preferences={}, test_requested_at=schedule.stamp(at()))
        self.assertEqual(self.execute(github, transport, True)["sent"], 1)
        self.assertEqual(transport.sent[0]["payload"]["items"][0]["name"], event()["n"])
        self.assertEqual(self.execute(github, transport, True)["sent"], 0)

    def test_old_test_request_is_not_replayed(self):
        github, transport = FakeGitHub(), Transport()
        github.files[("owner/private", "subscriptions/phone1.json")].update(
            test_requested_at=schedule.stamp(at() - timedelta(minutes=16)))
        self.execute(github, transport, True)
        self.assertFalse(transport.sent)

    def test_explicit_test_handles_null_time_and_ignores_cancelled_events(self):
        github = FakeGitHub(events=[event(id="unknown", s=None),
                                    event(id="cancelled", s=400, status="cancelled"), event()])
        transport = Transport()
        github.files[("owner/private", "subscriptions/phone1.json")].update(
            test_requested_at=schedule.stamp(at()))
        self.assertEqual(self.execute(github, transport, True)["sent"], 1)
        self.assertEqual(transport.sent[0]["payload"]["items"][0]["time"], "2026-09-14 10:00–11:15")

    def test_opt_out_while_state_is_saved_is_honored_before_delivery(self):
        github, transport = FakeGitHub(), Transport()
        original = github.put_json

        def opt_out(*args, **kwargs):
            result = original(*args, **kwargs)
            github.files[("owner/private", "subscriptions/phone1.json")]["enabled"] = False
            return result

        github.put_json = opt_out
        self.assertEqual(self.execute(github, transport)["sent"], 0)
        self.assertFalse(transport.sent)

    def test_stale_prelease_public_read_refreshes_before_snapshot_diff(self):
        github, transport = FakeGitHub(), Transport()
        self.execute(github, transport, True)
        # A remote revision arrives while the sender is acquiring its lease.
        github.files[("owner/schedule", "DB/events.json")][0]["s"] = 630
        github.revision = "revision2"
        original = github.put_json

        def advance(*args, **kwargs):
            result = original(*args, **kwargs)
            if "Acquire" in kwargs.get("message", ""):
                github.revision = "revision3"
                github.files[("owner/schedule", "DB/events.json")][0]["s"] = 660
            return result

        github.put_json = advance
        self.execute(github, transport, True)
        self.assertEqual(transport.sent[0]["payload"]["items"][0]["time"], "2026-09-14 11:00–11:15")
        self.assertEqual(github.files[("owner/private", STATE_PATH)]["revision"], "revision3")


class EndpointTests(unittest.TestCase):
    def test_supported_browser_services_and_valid_key_lengths(self):
        for hostname in ("fcm.googleapis.com", "web.push.apple.com", "other.push.apple.com", "updates.push.services.mozilla.com"):
            value = subscription()
            value["subscription"]["endpoint"] = "https://" + hostname + "/opaque"
            self.assertTrue(validate_subscription(value, "phone1"))

    def test_ssrf_credentials_custom_ports_and_host_suffix_attacks_rejected(self):
        for endpoint in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://localhost/x",
                         "https://fcm.googleapis.com.evil.test/x", "https://evilpush.apple.com/x",
                         "https://user:password@fcm.googleapis.com/x", "https://fcm.googleapis.com:444/x",
                         "https://fcm.googleapis.com/x#fragment", "https://169.254.169.254/x"):
            value = subscription()
            value["subscription"]["endpoint"] = endpoint
            self.assertFalse(validate_subscription(value, "phone1"), endpoint)

    def test_device_key_or_timezone_mismatch_rejected(self):
        value = subscription()
        self.assertFalse(validate_subscription(value, "different-device"))
        self.assertFalse(validate_subscription(value, "../phone1"))
        value["subscription"]["keys"]["auth"] = "bad"
        self.assertFalse(validate_subscription(value, "phone1"))


class SchoolNoticeTests(unittest.TestCase):
    def test_initial_import_is_visible_for_review_without_replaying_phone_alerts(self):
        item = school_item(notify=False)
        self.assertEqual(self.notices([item]), [])
        item.update(notify=True, content_hash='changed-after-first-connection')
        self.assertEqual(len(self.notices([item])), 1)

    def notices(self, items=None, now=None, pending=()):
        return schedule.school_notices({"version": 1, "items": [school_item()] if items is None else items}, now or at(), pending)

    def execute(self, github, transport):
        return run(github, transport, "owner/private", "owner/schedule", True, now=lambda: github.now)

    def setup_sender(self, items=None):
        github, transport = FakeGitHub(), Transport()
        github.files[("owner/private", "school/index.json")] = {"version": 1, "items": [school_item()] if items is None else items}
        return github, transport

    def test_only_reviewable_states_emit_generic_course_time_payload(self):
        for state in ("needs_review", "info", "conflict", "ready"):
            notice = self.notices([school_item(state=state)])[0]
            self.assertEqual(notice["kind"], "notice")
            self.assertEqual(notice["payload"]["items"], [{"name": "테스트 과목", "time": "2026-09-14 06:32", "location": ""}])
            self.assertEqual(notice["payload"]["url"], "./#school")
            self.assertNotIn("PRIVATE", str(notice))
            self.assertNotIn("private.invalid", str(notice))
        for state in ("baseline", "ignored", "applied", "unknown"):
            self.assertEqual(self.notices([school_item(state=state)]), [])

    def test_first_seen_controls_due_time_and_older_posting_does_not_replay(self):
        item = school_item(updated_at="2020-01-01T00:00:00Z")
        notice = self.notices([item])[0]
        self.assertEqual(schedule.parse_stamp(notice["due"]), at())
        self.assertEqual(notice["payload"]["items"][0]["time"], "2020-01-01 09:00")
        self.assertFalse(self.notices([school_item(first_seen_at=schedule.stamp(at() - timedelta(days=1)))]))
        self.assertFalse(self.notices([school_item(first_seen_at=schedule.stamp(at() + timedelta(minutes=1)))]))
        self.assertFalse(self.notices([school_item(first_seen_at="9999-12-31T23:59:59Z")]))

    def test_old_device_absent_notice_preference_defaults_on_but_explicit_off_is_honored(self):
        notice = self.notices()[0]
        self.assertTrue(schedule.eligible(subscription(preferences={"daily": False}), notice, at()))
        for value in (False, None, "true", 1):
            self.assertFalse(schedule.eligible(subscription(preferences={"notice": value}), notice, at()))
        self.assertFalse(schedule.eligible(subscription(enabled=False), notice, at()))
        self.assertFalse(schedule.eligible(subscription(created_at=schedule.stamp(at() + timedelta(seconds=1))), notice, at() + timedelta(seconds=2)))

    def test_same_content_hash_deduplicates_and_new_content_version_notifies(self):
        github, transport = self.setup_sender()
        self.assertEqual(self.execute(github, transport)["sent"], 1)
        self.assertEqual(self.execute(github, transport)["sent"], 0)
        self.assertEqual(transport.sent[0]["kind"], "notice")
        github.files[("owner/private", "school/index.json")]["items"][0]["content_hash"] = "version-two"
        self.assertEqual(self.execute(github, transport)["sent"], 1)
        self.assertNotEqual(transport.sent[0]["id"], transport.sent[1]["id"])

    def test_duplicate_registry_entries_and_delimiter_collision_do_not_duplicate_or_merge(self):
        values = [school_item(), school_item(), school_item(id="a:b", content_hash="c"), school_item(id="a", content_hash="b:c")]
        notices = self.notices(values)
        self.assertEqual(len(notices), 3)
        self.assertEqual(len({notice["id"] for notice in notices}), 3)

    def test_invalid_or_missing_index_does_not_block_regular_daily_alert(self):
        for value in (None, {}, {"version": 2, "items": []}, {"version": 1, "items": "invalid"},
                      {"version": 1, "items": [None, school_item(first_seen_at="bad"), school_item(course={})]}):
            github, transport = FakeGitHub(), Transport()
            github.files[("owner/private", "school/index.json")] = value
            result = run(github, transport, "owner/private", "owner/schedule", now=lambda: github.now)
            self.assertEqual(result["sent"], 1)
            self.assertEqual([notice["kind"] for notice in transport.sent], ["daily"])

    def test_index_network_failure_is_isolated_from_regular_schedule(self):
        github, transport = FakeGitHub(), Transport()
        read = github.read_json

        def failed_index(repo, path, ref="main"):
            if path == "school/index.json":
                raise GitHubError(503, "network unavailable")
            return read(repo, path, ref)

        github.read_json = failed_index
        result = run(github, transport, "owner/private", "owner/schedule", now=lambda: github.now)
        self.assertEqual(result["sent"], 1)

    def test_notice_retry_reuses_durable_delivery_ledger(self):
        github, transport = self.setup_sender()
        transport.status = 503
        self.assertEqual(self.execute(github, transport)["retry"], 1)
        self.assertFalse(github.files[("owner/private", STATE_PATH)]["delivered"])
        transport.status = 201
        self.assertEqual(self.execute(github, transport)["sent"], 1)
        self.assertEqual(self.execute(github, transport)["sent"], 0)

    def test_review_completed_while_waiting_for_send_lease_suppresses_notice(self):
        github, transport = self.setup_sender()
        put = github.put_json

        def apply_notice(*args, **kwargs):
            result = put(*args, **kwargs)
            github.files[("owner/private", "school/index.json")]["items"][0]["state"] = "applied"
            return result

        github.put_json = apply_notice
        self.assertEqual(self.execute(github, transport)["sent"], 0)
        self.assertFalse(transport.sent)

    def test_posted_date_without_time_is_preserved_and_invalid_time_not_guessed(self):
        for updated_at, expected in (("2026-09-13", "2026-09-13"), ("PRIVATE UNKNOWN TEXT", "게시 시각 미정")):
            self.assertEqual(self.notices([school_item(updated_at=updated_at)])[0]["payload"]["items"][0]["time"], expected)

    def decision(self, **changes):
        return {"version": 1, "id": "decision-one", "source_id": "notice-one", "source_hash": "version-one",
                "action": "ignore", "candidates": [], "created_at": schedule.stamp(at()), **changes}

    def test_durable_pending_suppresses_only_matching_source_version_and_valid_acknowledgements(self):
        for action in ("approve", "ignore"):
            self.assertFalse(self.notices(pending=[self.decision(action=action)]))
            self.assertEqual(len(self.notices([school_item(content_hash="version-two")], pending=[self.decision(action=action)])), 1)
            self.assertEqual(len(self.notices([school_item(id="other-notice")], pending=[self.decision(action=action)])), 1)
        for invalid in (None, {}, self.decision(version=2), self.decision(id="../bad"), self.decision(action="edit"), self.decision(source_hash="old")):
            self.assertEqual(len(self.notices(pending=[invalid])), 1)

    def test_projected_queued_and_fully_completed_reviews_suppress_but_conflicts_partial_and_new_versions_resume(self):
        review = {"source_hash": "version-one", "action": "approve", "remaining_count": 0}
        for state in ("queued", "processing", "completed"):
            self.assertFalse(self.notices([school_item(review={**review, "state": state})]))
        for changes in ({"state": "conflict"}, {"state": "failed"}, {"state": "completed", "remaining_count": 2},
                        {"state": "queued", "source_hash": "old-version"}, {"state": "queued", "action": "invalid"}):
            self.assertEqual(len(self.notices([school_item(review={**review, **changes})])), 1)

    def test_sender_hydrates_pending_decision_before_index_projection_without_touching_regular_reminders(self):
        github, transport = self.setup_sender()
        github.files[("owner/private", "school/decisions/decision-one.json")] = self.decision()
        self.assertFalse(load_school_notices(github, "owner/private", github.now))
        result = run(github, transport, "owner/private", "owner/schedule", now=lambda: github.now)
        self.assertEqual(result["sent"], 1)
        self.assertEqual([notice["kind"] for notice in transport.sent], ["daily"])
        github.files[("owner/private", "school/index.json")]["items"][0]["content_hash"] = "version-two"
        self.assertEqual(self.execute(github, transport)["sent"], 1)
        self.assertEqual(transport.sent[-1]["kind"], "notice")

    def test_decision_accepted_while_waiting_for_send_lease_is_suppressed(self):
        github, transport = self.setup_sender()
        put = github.put_json

        def acknowledge(*args, **kwargs):
            result = put(*args, **kwargs)
            github.files[("owner/private", "school/decisions/decision-one.json")] = self.decision(action="approve")
            return result

        github.put_json = acknowledge
        self.assertEqual(self.execute(github, transport)["sent"], 0)
        self.assertFalse(transport.sent)

    def test_terminal_result_is_not_treated_as_pending_and_historical_decision_body_is_not_downloaded(self):
        github, _ = self.setup_sender([school_item(state="conflict", review={"state": "conflict", "source_hash": "version-one", "action": "approve"})])
        github.files[("owner/private", "school/decisions/decision-one.json")] = self.decision()
        github.files[("owner/private", "school/decision-results/decision-one.json")] = {"version": 1, "state": "conflict"}
        read = github.read_json
        reads = []

        def track(repo, path, ref="main"):
            reads.append((path, ref))
            if path.startswith("school/decisions/"):
                raise AssertionError("historical decision must not be downloaded")
            return read(repo, path, ref)

        github.read_json = track
        self.assertEqual(len(load_school_notices(github, "owner/private", github.now)), 1)
        self.assertTrue(all(ref == "queue-head" for _, ref in reads))

    def test_pending_decision_read_failure_isolated_from_daily_and_deadline_reminders(self):
        for error in (GitHubError(503, "network unavailable"), RuntimeError("truncated queue")):
            github, transport = self.setup_sender()
            github.now = at(hour=8, minute=2)
            github.files[("owner/schedule", "DB/events.json")] = [event(t="deadline")]
            github.files[("owner/private", "school/decisions/decision-one.json")] = self.decision()
            read = github.read_json

            def fail(repo, path, ref="main"):
                if path.startswith("school/decisions/"):
                    raise error
                return read(repo, path, ref)

            github.read_json = fail
            result = run(github, transport, "owner/private", "owner/schedule", now=lambda: github.now)
            self.assertEqual(result["sent"], 2)
            self.assertEqual({notice["kind"] for notice in transport.sent}, {"daily", "deadline"})

    def test_notice_acknowledgement_does_not_remove_actual_departure_or_deadline_schedule(self):
        github, transport = self.setup_sender()
        github.now = at(hour=9, minute=52)
        github.files[("owner/schedule", "DB/events.json")] = [event(s=480, e=540), event(id="next", lid="b", s=660, e=720)]
        github.files[("owner/private", "school/decisions/decision-one.json")] = self.decision()
        run(github, transport, "owner/private", "owner/schedule", now=lambda: github.now)
        self.assertEqual({notice["kind"] for notice in transport.sent}, {"daily", "departure"})

    def test_parser_hash_migration_preserves_undelivered_notice_without_redelivery_to_notified_device(self):
        github, transport = self.setup_sender()
        self.assertEqual(self.execute(github, transport)['sent'], 1)
        item = github.files[("owner/private", "school/index.json")]['items'][0]
        item.update(content_hash='new-parser-hash', notice_hash='version-one')
        self.assertEqual(self.execute(github, transport)['sent'], 0)
        fresh, fresh_transport = self.setup_sender([item])
        self.assertEqual(self.execute(fresh, fresh_transport)['sent'], 1)
        self.assertEqual(fresh_transport.sent[0]['id'], transport.sent[0]['id'])
        item.update(content_hash='actual-new-content', notice_hash='actual-new-content')
        self.assertEqual(self.execute(github, transport)['sent'], 1)

    def test_notice_delivery_alias_does_not_change_pending_source_version_validation(self):
        item = school_item(content_hash='parser-current', notice_hash='version-one')
        self.assertEqual(len(self.notices([item], pending=[self.decision(source_hash='version-one')])), 1)
        self.assertEqual(self.notices([item], pending=[self.decision(source_hash='parser-current')]), [])
        expected = self.notices()[0]['id']
        for invalid in (None, {}, '', ' ', 'x' * 513):
            self.assertEqual(self.notices([school_item(notice_hash=invalid)])[0]['id'], expected)


if __name__ == "__main__":
    unittest.main()
