"""DB edits remain independent of fixed historical schedule expectations."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from src.validate_db import DatabaseValidationError, validate_database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "DB").mkdir()
        self.events = [
            {"d": "2026-09-14", "t": "class", "n": "기존 수업", "s": 1020, "e": 1095},
            {"id": "appointment-1", "d": "2026-09-18", "t": "deadline", "n": "보고서"},
        ]
        self.travel = {"locations": {"campus": "학교"}, "times": {}, "modes": {}}
        self.plan = []

    def write(self):
        for name, value in (("events", self.events), ("travel", self.travel), ("plan", self.plan)):
            (self.root / "DB" / (name + ".json")).write_text(
                json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def validate(self):
        self.write()
        return validate_database(self.root)

    def test_legitimate_removals_and_date_changes_do_not_restore_historical_data(self):
        self.assertEqual(self.validate()["events"], 2)
        self.events.pop(0)
        self.events[0]["d"] = "2030-02-28"
        self.events[0]["status"] = "confirmed"
        self.assertEqual(self.validate()["events"], 1)
        self.events.clear()
        self.assertEqual(self.validate()["events"], 0)

    def test_sparse_tentative_unknown_time_and_midnight_end_are_valid(self):
        self.events += [
            {"id": "midnight", "d": "2028-02-29", "t": "tutor", "n": "온라인 과외",
             "s": 1320, "e": 1440, "ti": "22:00–24:00", "series": "online",
             "status": "tentative", "custom_metadata": {"keep": True}},
            {"d": "2026-09-18", "t": "meeting", "n": "종료 미정", "s": 600},
            {"d": "2026-09-19", "t": "meeting", "n": "시간 미정", "s": None, "e": None},
            {"d": "2026-09-20", "t": "deadline", "n": "자정 마감", "s": 1440, "ti": "24:00까지"},
        ]
        before = deepcopy(self.events)
        self.assertEqual(self.validate()["events"], 6)
        self.assertEqual(self.events, before)

    def test_existing_duplicates_overlaps_unknown_locations_and_missing_ids_are_valid(self):
        item = {"d": "2026-09-14", "t": "tutor", "n": "기존 과외", "s": 1020,
                "e": 1140, "lid": "legacy-location"}
        self.events += [deepcopy(item), deepcopy(item)]
        self.travel["times"]["legacy-location-unknown"] = 30
        self.assertEqual(self.validate()["events"], 4)

    def test_invalid_dates_times_types_and_duplicate_ids_fail(self):
        cases = [
            ({"d": "2026-02-30"}, ".d"),
            ({"d": "2026-9-14"}, ".d"),
            ({"s": 1020, "e": 1019}, "end must be after start"),
            ({"s": 1020, "e": 1020}, "end must be after start"),
            ({"s": -1}, ".s"),
            ({"s": 1441, "e": None}, ".s"),
            ({"s": True}, ".s"),
            ({"e": 1441}, ".e"),
            ({"t": "unsupported"}, ".t"),
            ({"n": " "}, ".n"),
            ({"status": []}, ".status"),
            ({"id": 15}, ".id"),
            ({"id": "appointment-1"}, "duplicate event ID"),
        ]
        original = deepcopy(self.events)
        for changes, expected in cases:
            with self.subTest(changes=changes):
                self.events = deepcopy(original)
                self.events[0].update(changes)
                with self.assertRaisesRegex(DatabaseValidationError, expected):
                    self.validate()

    def test_invalid_json_missing_files_and_top_level_shapes_fail(self):
        self.write()
        path = self.root / "DB" / "events.json"
        for content in ('[{', '[{"n":"one","n":"two"}]', '[NaN]', '[1e999]', '{}', '[null]'):
            with self.subTest(content=content):
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(DatabaseValidationError):
                    validate_database(self.root)
        path.unlink()
        with self.assertRaisesRegex(DatabaseValidationError, "events.json"):
            validate_database(self.root)

    def test_travel_mapping_shapes_and_minute_ranges_are_checked(self):
        for value in ([], {"locations": []}, {"locations": {"campus": 5}},
                      {"times": {"a-b": -1}}, {"times": {"a-b": True}},
                      {"times": {"a-b": 1441}}, {"modes": {"a-b": []}}):
            with self.subTest(value=value):
                self.travel = value
                with self.assertRaisesRegex(DatabaseValidationError, "travel.json"):
                    self.validate()
        self.travel = {"times": {"unknown-other": 0}, "metadata": {"preserved": True}}
        self.validate()

    def test_sparse_study_days_rest_days_and_display_quantities_are_valid(self):
        self.plan = [
            {"d": "2026-09-18", "rest": True, "note": "휴식"},
            {"d": "2026-09-19", "total": 2.5, "blocks": [
                {"s": "14:00–16:30", "d": "공부", "h": "2.5시간", "c": "#34aaff"}]},
            {"d": "2026-09-20"},
        ]
        self.assertEqual(self.validate()["plan_days"], 3)

    def test_malformed_study_days_and_blocks_fail(self):
        for value in ({}, [None], [{"d": "2026-02-30"}], [{"d": "2026-09-18", "blocks": {}}],
                      [{"d": "2026-09-18", "blocks": [None]}],
                      [{"d": "2026-09-18", "rest": "yes"}],
                      [{"d": "2026-09-18", "blocks": [{"s": 42}]}],
                      [{"d": "2026-09-18", "total": -1}],
                      [{"d": "2026-09-18", "total": 10 ** 500}]):
            with self.subTest(value=value):
                self.plan = value
                with self.assertRaisesRegex(DatabaseValidationError, "plan.json"):
                    self.validate()

    def test_cli_accepts_external_root_and_never_changes_database(self):
        self.write()
        files = sorted((self.root / "DB").iterdir())
        before = {path.name: path.read_bytes() for path in files}
        script = Path(__file__).resolve().parents[1] / "validate_db.py"
        command = [sys.executable, "-B", str(script), "--root", str(self.root)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("DB validation passed", result.stdout)
        self.assertEqual({path.name: path.read_bytes() for path in files}, before)
        (self.root / "DB" / "events.json").write_text("[", encoding="utf-8")
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("events.json", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
