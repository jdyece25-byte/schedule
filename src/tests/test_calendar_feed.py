"""Calendar interchange uses only public fields and never guesses missing times."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from src.build import FRONTEND_FILES, build
from src.calendar_feed import generate_calendar


STAMP = datetime(2026, 9, 14, 2, 3, 4, tzinfo=timezone.utc)


def unfold(content):
    return content.decode("utf-8").replace("\r\n ", "")


def properties(content, name):
    return [line.split(":", 1)[1] for line in unfold(content).split("\r\n")
            if line.startswith(name + ":")]


class CalendarFeedTests(unittest.TestCase):
    def feed(self, events, travel=None, **kwargs):
        return generate_calendar(events, travel or {"locations": {}}, generated_at=STAMP, **kwargs)

    def test_known_times_are_utc_and_midnight_end_rolls_to_next_local_day(self):
        content = self.feed([{"id": "online", "d": "2026-09-14", "n": "온라인 과외",
                              "s": 1320, "e": 1440}])
        self.assertEqual(properties(content, "DTSTART"), ["20260914T130000Z"])
        self.assertEqual(properties(content, "DTEND"), ["20260914T150000Z"])
        self.assertEqual(properties(content, "DTSTAMP"), ["20260914T020304Z"])

    def test_year_and_leap_day_boundaries_convert_without_24_hour_strings(self):
        content = self.feed([
            {"d": "2026-12-31", "n": "자정 마감", "s": 1440},
            {"d": "2028-02-29", "n": "아침", "s": 0, "e": 60},
        ])
        self.assertEqual(properties(content, "DTSTART"), ["20261231T150000Z", "20280228T150000Z"])
        self.assertEqual(properties(content, "DTEND"), ["20280228T160000Z"])
        self.assertNotIn("T240000", unfold(content))

    def test_missing_start_and_all_day_are_dates_missing_end_is_a_point(self):
        content = self.feed([
            {"d": "2026-09-18", "n": "시간 미정", "s": None, "e": None},
            {"d": "2026-09-19", "n": "종일", "s": 0, "e": 1440, "all_day": True},
            {"d": "2026-09-20", "n": "종료 미정", "s": 600},
            {"d": "2026-09-21", "n": "시작 미정", "e": 900},
        ])
        text = unfold(content)
        self.assertIn("DTSTART;VALUE=DATE:20260918\r\n", text)
        self.assertIn("DTSTART;VALUE=DATE:20260919\r\n", text)
        self.assertIn("DTSTART;VALUE=DATE:20260921\r\n", text)
        self.assertEqual(properties(content, "DTSTART"), ["20260920T010000Z"])
        self.assertNotIn("DTEND", text)
        self.assertNotIn("DURATION", text)

    def test_stable_ids_survive_reschedule_rename_location_and_metadata_changes(self):
        event = {"id": "request-1", "d": "2026-09-14", "n": "원래 일정", "s": 600, "e": 660}
        original = self.feed([event])
        changed = dict(event, d="2026-10-10", n="수정 일정", s=660, e=900,
                       loc="새 장소", no="비밀 메모", status="tentative")
        self.assertEqual(properties(original, "UID"), properties(self.feed([changed]), "UID"))

    def test_legacy_uids_survive_reorder_rebuild_time_edits_and_duplicate_rows_are_unique(self):
        first = {"d": "2026-09-14", "n": "기존 수업", "t": "class", "s": 600, "e": 660}
        other = {"d": "2026-09-15", "n": "다른 수업", "t": "class", "s": 600, "e": 660}
        original = properties(self.feed([first, other]), "UID")
        changed = dict(first, s=660, e=720, no="별도 메모", status="tentative")
        self.assertEqual(original, list(reversed(properties(self.feed([other, changed]), "UID"))))
        duplicates = properties(self.feed([first, deepcopy(first), other]), "UID")
        self.assertEqual(len(set(duplicates)), 3)
        self.assertEqual(duplicates, properties(self.feed([first, deepcopy(first), other]), "UID"))

    def test_location_resolution_status_and_expanded_occurrences(self):
        events = [
            {"id": "one", "d": "2026-09-14", "n": "첫 수업", "lid": "campus", "series": "weekly",
             "status": "tentative"},
            {"id": "two", "d": "2026-09-21", "n": "둘째 수업", "lid": "campus", "loc": "교실 102",
             "series": "weekly", "status": "cancelled"},
            {"id": "three", "d": "2026-09-28", "n": "셋째 수업", "lid": "unknown", "status": "confirmed"},
        ]
        content = self.feed(events, {"locations": {"campus": "서울대"}})
        self.assertEqual(properties(content, "LOCATION"), ["서울대", "교실 102"])
        self.assertEqual(properties(content, "STATUS"), ["TENTATIVE", "CANCELLED", "CONFIRMED"])
        self.assertEqual(unfold(content).count("BEGIN:VEVENT\r\n"), 3)
        self.assertNotIn("RRULE", unfold(content))

    def test_private_notes_and_unknown_metadata_are_not_exported_or_mutated(self):
        events = [{"d": "2026-09-14", "n": "공개 제목", "loc": "공개 장소", "no": "비밀상담",
                   "ti": "비밀시간표", "request": "private request", "token": "private token",
                   "metadata": {"phone": "private phone"}, "status": "secret-status"}]
        before = deepcopy(events)
        content = unfold(self.feed(events))
        self.assertEqual(events, before)
        for forbidden in ("비밀", "private", "secret-status", "DESCRIPTION", "COMMENT", "ATTENDEE"):
            self.assertNotIn(forbidden, content)
        allowed = {"BEGIN", "END", "VERSION", "PRODID", "CALSCALE", "UID", "DTSTAMP",
                   "DTSTART;VALUE=DATE", "SUMMARY", "LOCATION"}
        self.assertTrue(all(line.split(":", 1)[0] in allowed for line in content.split("\r\n") if line))

    def test_korean_emoji_text_folds_at_utf8_byte_limit_and_cannot_inject_fields(self):
        name = "회의,세미나;자료\\검토\r\n" + "한글📅" * 40 + "\rBEGIN:VEVENT"
        content = self.feed([{"d": "2026-09-14", "n": name, "loc": "서울;역,1번\\출구\n2층"}])
        self.assertTrue(content.endswith(b"\r\n"))
        self.assertNotIn(b"\n", content.replace(b"\r\n", b""))
        for physical_line in content.split(b"\r\n"):
            self.assertLessEqual(len(physical_line), 75)
            physical_line.decode("utf-8")
        self.assertIn(b"\r\n ", content)
        text = unfold(content)
        self.assertEqual(text.split("\r\n").count("BEGIN:VEVENT"), 1)
        self.assertIn("SUMMARY:회의\\,세미나\\;자료\\\\검토\\n", text)
        self.assertIn("LOCATION:서울\\;역\\,1번\\\\출구\\n2층", text)

    def test_generation_stamp_converts_to_utc_and_rejects_naive_time(self):
        stamp = datetime(2026, 9, 14, 11, 3, 4, tzinfo=timezone(timedelta(hours=9)))
        events = [{"d": "2026-09-14", "n": "일정"}]
        self.assertEqual(properties(generate_calendar(events, {}, generated_at=stamp), "DTSTAMP"),
                         ["20260914T020304Z"])
        with self.assertRaisesRegex(ValueError, "timezone"):
            generate_calendar([], {}, generated_at=datetime(2026, 9, 14))

    def test_invalid_ranges_dates_control_characters_and_duplicate_ids_fail(self):
        base = {"id": "one", "d": "2026-09-14", "n": "일정", "s": 600, "e": 660}
        for change in ({"d": "2026-02-30"}, {"d": "20260914"}, {"s": True}, {"s": -1},
                       {"e": 1441}, {"e": 600}, {"n": "bad\x00text"}, {"id": ""}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.feed([dict(base, **change)])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.feed([base, deepcopy(base)])


class CalendarBuildTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "project"
        (self.root / "src").mkdir(parents=True)
        (self.root / "DB").mkdir()
        self.output = Path(directory.name) / "site"
        for name in FRONTEND_FILES:
            (self.root / "src" / name).write_text("public frontend", encoding="utf-8")
        self.events = [{"id": "appointment-1", "d": "2026-09-14", "n": "공개 일정", "s": 600,
                        "loc": "공개 장소", "no": "feed must not expose this note"}]
        for name, data in (("events", self.events), ("travel", {"locations": {}}), ("plan", [])):
            (self.root / "DB" / (name + ".json")).write_text(json.dumps(data), encoding="utf-8")

    def build(self):
        with redirect_stdout(io.StringIO()):
            build(self.output, root=self.root, generated_at=STAMP)

    def test_artifact_has_explicit_frontend_db_aliases_and_generated_feed_only(self):
        (self.root / "src" / "backend-secret.json").write_text("private", encoding="utf-8")
        (self.root / "DB" / "SCHEDULE.md").write_text("semester notes", encoding="utf-8")
        (self.root / "DB" / "applied").mkdir()
        (self.root / "DB" / "applied" / "request.json").write_text("private", encoding="utf-8")
        before = {path.relative_to(self.root).as_posix(): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        self.build()
        files = {path.relative_to(self.output).as_posix() for path in self.output.rglob("*") if path.is_file()}
        expected = {"index.html", "bridge-client.js", "bridge-client.css", "push-client.js",
                    "push-client.css", "manifest.webmanifest", "sw.js", "push-config.json",
                    "icon-192.png", "icon-512.png", "badge-96.png", "events.ics",
                    "school-client.js", "school-client.css",
                    "DB/events.json", "DB/travel.json", "DB/plan.json",
                    "events.json", "travel.json", "plan.json"}
        self.assertEqual(files, expected)
        for name in ("events.json", "travel.json", "plan.json"):
            self.assertEqual((self.output / name).read_bytes(), (self.output / "DB" / name).read_bytes())
        feed = (self.output / "events.ics").read_bytes()
        self.assertEqual(properties(feed, "SUMMARY"), ["공개 일정"])
        self.assertNotIn(b"feed must not expose", feed)
        self.assertFalse((self.root / "events.ics").exists())
        self.assertEqual(before, {path.relative_to(self.root).as_posix(): path.read_bytes()
                                  for path in self.root.rglob("*") if path.is_file()})
        self.build()  # A previously generated feed is a permitted rebuild output.
        self.assertEqual((self.output / "events.ics").read_bytes(), feed)

    def test_missing_required_asset_fails_before_any_output_write(self):
        (self.root / "src" / "sw.js").unlink()
        with self.assertRaisesRegex(ValueError, "sw.js"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_invalid_calendar_fails_before_any_output_write(self):
        (self.root / "DB" / "events.json").write_text(
            json.dumps([dict(self.events[0], s=600, e=500)]), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "end must be after"):
            self.build()
        self.assertFalse(self.output.exists())

    def test_output_with_unrelated_file_is_rejected_without_changing_it(self):
        self.output.mkdir()
        path = self.output / "secret.json"
        path.write_text("keep this file", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unrelated files"):
            self.build()
        self.assertEqual(path.read_text(encoding="utf-8"), "keep this file")
        self.assertFalse((self.output / "index.html").exists())

    def test_source_and_database_cannot_be_output_directories(self):
        for destination in (self.root, self.root / "src", self.root / "DB" / "nested",
                            self.root / ".github" / "nested"):
            with self.subTest(destination=destination), self.assertRaisesRegex(ValueError, "separate"):
                build(destination, root=self.root, generated_at=STAMP)


if __name__ == "__main__":
    unittest.main()
