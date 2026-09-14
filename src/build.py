"""Assemble only the public frontend and runtime DB; no build dependencies."""
import argparse
import json
from pathlib import Path
import shutil

if __package__:
    from .calendar_feed import generate_calendar
else:
    from calendar_feed import generate_calendar


FRONTEND_FILES = (
    "index.html", "bridge-client.js", "bridge-client.css",
    "push-client.js", "push-client.css", "manifest.webmanifest", "sw.js",
    "push-config.json", "icon-192.png", "icon-512.png", "badge-96.png",
    "school-client.js", "school-client.css",
)


def build(destination, *, root=None, generated_at=None):
    root = Path(root).resolve() if root is not None else Path(__file__).resolve().parent.parent
    output = Path(destination).resolve()
    # Reject source paths before copying; this command never deletes directories.
    if output == root or any(output == root / part or (root / part) in output.parents
                             for part in ("src", "DB", ".github")):
        raise ValueError("Build output must be separate from the source and DB folders")
    sources = {name: root / "src" / name for name in FRONTEND_FILES}
    database = {}
    for name, kind in (("events.json", list), ("travel.json", dict), ("plan.json", list)):
        path = root / "DB" / name
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, kind):
            raise ValueError(f"Unexpected DB shape: {name}")
        database[name] = value
        sources["DB/" + name] = path
        # Older open pages can finish loading after the source layout migration.
        sources[name] = path
    calendar = generate_calendar(database["events.json"], database["travel.json"], generated_at=generated_at)
    # Preflight every source before writing an output, never copy backend,
    # request history, private configuration, or arbitrary files from src/DB.
    for source in sources.values():
        if not source.is_file() or source.is_symlink():
            raise ValueError(f"Missing or unsafe website source: {source.relative_to(root)}")
    allowed = set(sources) | {"events.ics"}
    # Require a dedicated output so unrelated files cannot enter the artifact.
    entries = list(output.rglob("*")) if output.exists() else []
    if any(path.is_symlink() for path in entries):
        raise ValueError("Build output must not contain symbolic links")
    existing = {path.relative_to(output).as_posix() for path in entries if path.is_file()}
    if existing - allowed:
        raise ValueError("Build output contains unrelated files; choose an empty directory")
    for name, source in sources.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    (output / "events.ics").write_bytes(calendar)
    print(f"Published files: {len(allowed)} -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Empty output directory, e.g. .git/site-preview")
    build(parser.parse_args().output)
