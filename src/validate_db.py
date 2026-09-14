"""Read-only structural checks for DB edits, without historical schedule assertions."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys

if __package__:
    from .bridge.planner import CORE_FIELDS, _EVENT_SCHEMA, _calendar_date, _validate_schema
else:
    from bridge.planner import CORE_FIELDS, _EVENT_SCHEMA, _calendar_date, _validate_schema


class DatabaseValidationError(ValueError):
    """A database file cannot safely be consumed by the website and worker."""


def _fail(path, message):
    raise DatabaseValidationError(f"{path}: {message}")


def _text(value, path, *, nonempty=False, limit=None):
    if not isinstance(value, str):
        _fail(path, "expected text")
    if nonempty and not value.strip():
        _fail(path, "must not be blank")
    if limit is not None and len(value) > limit:
        _fail(path, f"text exceeds {limit} characters")


def _date(value, path):
    try:
        _calendar_date(value, path)
    except ValueError as error:
        raise DatabaseValidationError(str(error)) from error


def _read_json(path):
    def reject_constant(value):
        raise ValueError(f"{value} is not a JSON number")

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON number exceeds the finite numeric range")
        return number

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"),
                          parse_constant=reject_constant, parse_float=finite_float,
                          object_pairs_hook=unique_object)
    except (OSError, UnicodeError, ValueError) as error:
        raise DatabaseValidationError(f"{path.name}: cannot read valid JSON ({error})") from error


def validate_events(events):
    if not isinstance(events, list):
        _fail("DB/events.json", "expected an array of events")
    identifiers = set()
    for index, event in enumerate(events):
        path = f"DB/events.json[{index}]"
        if not isinstance(event, dict):
            _fail(path, "expected an event object")
        # The planner's proposal schema requires all core keys, while stored
        # legacy records legitimately omit optional fields and retain metadata.
        core = {key: event.get(key) for key in CORE_FIELDS}
        try:
            _validate_schema(core, _EVENT_SCHEMA, path)
        except ValueError as error:
            raise DatabaseValidationError(str(error)) from error
        _date(event["d"], path + ".d")
        _text(event["n"], path + ".n", nonempty=True)
        start, end = event.get("s"), event.get("e")
        # Like the planner, permit s=1440 for a deadline due at the day's end.
        # A duration still needs an end strictly later than its start.
        if start is not None and end is not None and end <= start:
            _fail(path + ".e", "end must be after start; split overnight events by date")
        for name in ("id", "series", "status"):
            if name in event and event[name] is not None:
                _text(event[name], path + "." + name, nonempty=True)
        identifier = event.get("id")
        if identifier is not None:
            if identifier in identifiers:
                _fail(path + ".id", f"duplicate event ID {identifier!r}")
            identifiers.add(identifier)
        # Unknown locations, overlap and duplicate appointment contents need
        # human review; they do not make the stored database structurally invalid.


def validate_travel(travel):
    if not isinstance(travel, dict):
        _fail("DB/travel.json", "expected an object")
    for name in ("locations", "times", "modes"):
        values = travel.get(name, {})
        if not isinstance(values, dict):
            _fail("DB/travel.json." + name, "expected an object mapping")
        for key, value in values.items():
            path = f"DB/travel.json.{name}[{key!r}]"
            _text(key, path + " key", nonempty=True)
            if name == "locations":
                _text(value, path, nonempty=True, limit=500)
            elif name == "times":
                if type(value) is not int or not 0 <= value <= 1440:
                    _fail(path, "travel time must be integer minutes in 0..1440")
            else:
                _text(value, path, nonempty=True, limit=100)


def _display_quantity(value, path):
    if isinstance(value, str):
        return
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        _fail(path, "expected display text or a finite nonnegative number")


def validate_plan(plan):
    if not isinstance(plan, list):
        _fail("DB/plan.json", "expected an array of study-plan days")
    for index, day in enumerate(plan):
        path = f"DB/plan.json[{index}]"
        if not isinstance(day, dict):
            _fail(path, "expected a study-plan day object")
        _date(day.get("d"), path + ".d")
        for name in ("date", "exam", "alert", "note"):
            if name in day and day[name] is not None:
                _text(day[name], path + "." + name)
        for name in ("rest", "free", "grid"):
            if name in day and day[name] is not None and type(day[name]) is not bool:
                _fail(path + "." + name, "expected a boolean")
        if day.get("total") is not None:
            _display_quantity(day["total"], path + ".total")
        blocks = day.get("blocks")
        if blocks is None:
            continue
        if not isinstance(blocks, list):
            _fail(path + ".blocks", "expected an array")
        for block_index, block in enumerate(blocks):
            block_path = f"{path}.blocks[{block_index}]"
            if not isinstance(block, dict):
                _fail(block_path, "expected a study block object")
            for name in ("c", "s", "d"):
                if name in block and block[name] is not None:
                    _text(block[name], block_path + "." + name)
            if block.get("h") is not None:
                _display_quantity(block["h"], block_path + ".h")


def validate_database(root=None):
    """Validate runtime DB files without changing data or accessing the network."""
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    events = _read_json(root / "DB" / "events.json")
    travel = _read_json(root / "DB" / "travel.json")
    plan = _read_json(root / "DB" / "plan.json")
    validate_events(events)
    validate_travel(travel)
    validate_plan(plan)
    return {"events": len(events), "locations": len(travel.get("locations", {})),
            "plan_days": len(plan)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, help="Repository root containing DB/ (default: this repository)")
    args = parser.parse_args(argv)
    try:
        counts = validate_database(args.root)
    except DatabaseValidationError as error:
        print(f"DB validation failed: {error}", file=sys.stderr)
        return 1
    print("DB validation passed: " + ", ".join(f"{name}={value}" for name, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
