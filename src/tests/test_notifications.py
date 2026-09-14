import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from src.bridge.github import GitHubError
from src.notifications import scheduler as schedule
from src.notifications.sender import (Busy, STATE_PATH, StateStore, disable_expired,
                                      empty_state, run, validate_subscription)


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
            self.assertEqual(set(notices[0]["payload"]["items"][0]), {"name", "time", "location"})

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


if __name__ == "__main__":
    unittest.main()
