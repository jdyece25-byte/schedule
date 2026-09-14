"""Read-only service/heartbeat check for agents editing the schedule DB."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path

try:
    from .github import GitHub
    from .supervisor import service_status
except ImportError:
    from github import GitHub
    from supervisor import service_status


def check_health(config, *, github=None, remote=True, now=None):
    result = service_status(config["data_dir"])
    result["remote"] = None
    result["healthy"] = bool(result["worker_running"] and result["supervisor_running"] and
                             not result["disabled"] and not result["maintenance_until"] and
                             not result["supervisor_stop_requested"] and not result["errors"])
    if not remote:
        return result
    try:
        github = github or GitHub()
        value, _ = github.read_json(config["queue_repo"], "worker.json", config.get("queue_branch", "main"))
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("target_repo") != config["target_repo"]:
            raise ValueError("Missing or mismatched remote worker heartbeat")
        updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        if updated.tzinfo is None:
            raise ValueError("Heartbeat must include timezone")
        age = ((now or datetime.now(timezone.utc)) - updated).total_seconds()
        result["remote"] = {"updated_at": value["updated_at"], "age_seconds": round(age, 1),
                            "fresh": -60 <= age <= 120, "agents": value.get("agents", [])}
        result["healthy"] = result["healthy"] and result["remote"]["fresh"]
    except Exception as error:
        result["errors"].append("Remote heartbeat: " + str(error))
        result["healthy"] = False
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    default = str(Path(os.environ.get("LOCALAPPDATA", ".")) / "ScheduleBridge/config.json")
    parser.add_argument("--config", default=default)
    parser.add_argument("--local", action="store_true", help="Skip remote heartbeat; checks local OS locks only")
    args = parser.parse_args()
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        result = check_health(config, remote=not args.local)
    except Exception as error:
        result = {"healthy": False, "errors": [str(error)]}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["healthy"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
