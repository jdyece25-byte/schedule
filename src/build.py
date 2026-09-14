"""Assemble only the public frontend and runtime DB; no build dependencies."""
import argparse
import json
from pathlib import Path
import shutil


def build(destination):
    root = Path(__file__).resolve().parent.parent
    output = Path(destination).resolve()
    # Reject source paths before copying; this command never deletes directories.
    if output == root or any(output == root / part or (root / part) in output.parents
                             for part in ("src", "DB", ".github")):
        raise ValueError("Build output must be separate from the source and DB folders")
    sources = {name: root / "src" / name for name in
               ("index.html", "bridge-client.js", "bridge-client.css")}
    for name, kind in (("events.json", list), ("travel.json", dict), ("plan.json", list)):
        path = root / "DB" / name
        if not isinstance(json.loads(path.read_text(encoding="utf-8")), kind):
            raise ValueError(f"Unexpected DB shape: {name}")
        sources["DB/" + name] = path
        # Older open pages can finish loading after the source layout migration.
        sources[name] = path
    # Require a dedicated output so unrelated files cannot enter the artifact.
    existing = {path.relative_to(output).as_posix() for path in output.rglob("*") if path.is_file()} if output.exists() else set()
    if existing - sources.keys():
        raise ValueError("Build output contains unrelated files; choose an empty directory")
    for name, source in sources.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    print(f"Published files: {len(sources)} -> {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="Empty output directory, e.g. .git/site-preview")
    build(parser.parse_args().output)
