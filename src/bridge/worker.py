"""Serial, restart-safe PC worker for the private schedule request queue."""
from __future__ import annotations

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
import uuid

try:
    from .github import GitHub, GitHubError
    from .planner import PLAN_SCHEMA, apply_plan, build_prompt
    from .school_knowledge import build_school_context
except ImportError:
    from github import GitHub, GitHubError
    from planner import PLAN_SCHEMA, apply_plan, build_prompt
    from school_knowledge import build_school_context

KST = timezone(timedelta(hours=9))
TERMINAL = {"completed", "needs_input", "failed"}
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
EVENTS_PATH = "DB/events.json"
TRAVEL_PATH = "DB/travel.json"
NOTES_PATH = "DB/SCHEDULE.md"
APPLIED_PATH = "DB/applied"


def utcnow():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def parse_stamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("시간대가 없는 요청입니다.")
    return parsed


def validate_request(request, request_id):
    if not isinstance(request, dict) or request.get("version") != 1:
        raise ValueError("지원하지 않는 요청 형식입니다.")
    if not ID_RE.fullmatch(request_id) or request.get("id") != request_id:
        raise ValueError("요청 ID가 올바르지 않습니다.")
    text = request.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 6000:
        raise ValueError("요청은 1~6,000자여야 합니다.")
    if request.get("agent") not in ("codex", "claude"):
        raise ValueError("처리 에이전트를 확인해 주세요.")
    if request.get("timezone") != "Asia/Seoul":
        raise ValueError("일정은 한국 시간으로 요청해 주세요.")
    submitted = parse_stamp(request.get("created_at", ""))
    if request.get("today") != submitted.astimezone(KST).date().isoformat():
        raise ValueError("요청 날짜와 한국 시간 기준일이 일치하지 않습니다.")
    if submitted > utcnow() + timedelta(minutes=10):
        raise ValueError("휴대폰의 날짜·시간 설정을 확인해 주세요.")
    parent = request.get("parent_id")
    if parent is not None and (not isinstance(parent, str) or not ID_RE.fullmatch(parent) or parent == request_id):
        raise ValueError("추가 답변의 원래 요청을 확인할 수 없습니다.")


class WorkerLock:
    """OS releases this lock even after a crash; no stale .lock reservation."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            if self.path.stat().st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("일정 처리 프로그램이 이미 실행 중입니다.") from None
        else:
            import fcntl
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.file.close()
                raise RuntimeError("일정 처리 프로그램이 이미 실행 중입니다.") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


class AgentRunner:
    def __init__(self, config):
        self.config = config

    def command(self, agent, folder):
        if agent == "codex":
            executable = self.config.get("codex_command")
            if not executable:
                raise RuntimeError("PC에 Codex CLI가 설정되지 않았습니다.")
            return executable + [
                "exec", "--ignore-user-config", "--sandbox", "read-only", "--skip-git-repo-check",
                "--ephemeral", "--cd", str(folder), "--color", "never", "--json",
                "--disable", "shell_tool", "--disable", "unified_exec", "--disable", "apps",
                "--disable", "plugins", "--disable", "hooks", "--disable", "multi_agent",
                "--disable", "browser_use", "--disable", "computer_use", "--disable", "image_generation",
                "-c", 'web_search="disabled"', "-c", "agents.enabled=false",
                "-c", "tools.view_image=false", "-c", "project_doc_max_bytes=0",
                "-c", 'approval_policy="never"',
                "--output-schema", str(folder / "schema.json"),
                "--output-last-message", str(folder / "answer.json"), "-",
            ]
        executable = self.config.get("claude_command")
        if not executable:
            raise RuntimeError("PC에 Claude Code가 설정되지 않았습니다.")
        return executable + [
            "-p", "--safe-mode", "--no-chrome", "--disable-slash-commands", "--permission-mode", "dontAsk",
            "--tools", "", "--output-format", "json", "--no-session-persistence",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--setting-sources", "",
            "--json-schema", json.dumps(PLAN_SCHEMA, ensure_ascii=False),
        ]

    def run(self, request, events, travel, notes, history, tick, school_context=None):
        root = Path(self.config["data_dir"]) / "jobs"
        folder = root / (request["id"] + "-" + uuid.uuid4().hex[:8])
        folder.mkdir(parents=True)
        (folder / "schema.json").write_text(json.dumps(PLAN_SCHEMA), encoding="utf-8")
        prompt = build_prompt(request, events, travel, notes, history, school_context=school_context)
        (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
        command = self.command(request["agent"], folder)
        # Do not pass GitHub or provider API tokens to the model process.
        allowed = {"systemroot", "windir", "path", "pathext", "temp", "tmp", "userprofile",
                   "appdata", "localappdata", "home", "homedrive", "homepath", "comspec",
                   "programfiles", "programfiles(x86)", "programdata", "codex_home"}
        environment = {k: v for k, v in os.environ.items() if k.lower() in allowed}
        environment["PYTHONUTF8"] = "1"
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        started = time.monotonic()
        process = None
        with (folder / "agent.log").open("wb") as log, (folder / "stdout.jsonl").open("wb") as output, \
                (folder / "prompt.txt").open("rb") as prompt_input:
            try:
                process = subprocess.Popen(command, cwd=folder, env=environment, stdin=prompt_input,
                                           stdout=output, stderr=log, creationflags=flags)
                while process.poll() is None:
                    tick()
                    if time.monotonic() - started > self.config.get("agent_timeout_seconds", 600):
                        raise RuntimeError("에이전트 응답 시간이 초과되었습니다. 요청 기록에서 다시 보내 주세요.")
                    time.sleep(0.5)
                if process.returncode:
                    raise RuntimeError("에이전트를 실행하지 못했습니다. PC의 로그인·사용 한도와 worker.log를 확인해 주세요.")
            finally:
                if process is not None and process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, creationflags=flags, timeout=15)
                    else:
                        process.kill()
                    process.wait(timeout=15)
        if request["agent"] == "codex":
            return json.loads((folder / "answer.json").read_text(encoding="utf-8-sig"))
        result = json.loads((folder / "stdout.jsonl").read_text(encoding="utf-8-sig"))
        if result.get("is_error"):
            raise RuntimeError("Claude Code가 요청을 완료하지 못했습니다. PC의 사용 한도를 확인해 주세요.")
        if "structured_output" in result:
            return result["structured_output"]
        return json.loads(result["result"])


class Worker:
    def __init__(self, config, github=None, runner=None):
        self.config = config
        self.github = github or GitHub()
        self.runner = runner or AgentRunner(config)
        self.queue = config["queue_repo"]
        self.target = config["target_repo"]
        self.branch = config.get("target_branch", "main")
        self.queue_branch = config.get("queue_branch", "main")
        identity_path = Path(config["data_dir"]) / "worker.id"
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with identity_path.open("x", encoding="ascii") as identity_file:
                identity_file.write(uuid.uuid4().hex)
        except FileExistsError:
            pass
        self.identity = identity_path.read_text(encoding="ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{32}", self.identity):
            raise RuntimeError("PC 처리 프로그램 ID가 손상되었습니다.")
        self.terminal_cache = {}
        self.last_heartbeat = 0
        self.last_lease = 0
        self.active = None

    def verify(self):
        for repo in (self.queue, self.target):
            if not REPO_RE.fullmatch(repo):
                raise ValueError("저장소 설정은 owner/name 형식이어야 합니다.")
        user = self.github.api("user")["login"]
        queue = self.github.api(f"repos/{self.queue}")
        if not queue.get("private") or queue["owner"]["login"].lower() != user.lower():
            raise RuntimeError("요청 저장소는 현재 GitHub 계정 소유의 비공개 저장소여야 합니다.")
        target = self.github.api(f"repos/{self.target}")
        if not target.get("permissions", {}).get("push"):
            raise RuntimeError("일정 저장소의 쓰기 권한이 없습니다.")

    def heartbeat(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_heartbeat < self.config.get("heartbeat_seconds", 60):
            return
        value, sha = self.github.read_json(self.queue, "worker.json", self.queue_branch)
        self.github.put_json(self.queue, "worker.json", {
            "version": 1, "target_repo": self.target, "agent": self.config.get("agent", "codex"),
            "agents": [agent for agent in ("codex", "claude") if self.config.get(agent + "_command")],
            "updated_at": stamp(), "poll_seconds": self.config.get("poll_seconds", 20),
        }, sha, self.queue_branch, "Update worker availability")
        self.last_heartbeat = now

    def result(self, request_id):
        return self.github.read_json(self.queue, f"results/{request_id}.json", self.queue_branch)

    def save_result(self, request_id, value, sha):
        value.update({"version": 1, "id": request_id, "updated_at": stamp()})
        return self.github.put_json(self.queue, f"results/{request_id}.json", value, sha, self.queue_branch)

    def claim(self, request_id, request_sha):
        value, sha = self.result(request_id)
        if value and value.get("state") in TERMINAL:
            return False
        if value and value.get("state") == "processing":
            try:
                if parse_stamp(value["lease_until"]) > utcnow() and value.get("owner") != self.identity:
                    return False
            except (KeyError, ValueError, TypeError):
                return False  # Malformed reservations require manual repair, never steal blindly.
        processing = {"state": "processing", "message": "PC에서 일정 변경 내용을 확인하고 있습니다.",
                      "questions": [], "warnings": [], "commit_sha": None, "owner": self.identity,
                      "request_sha": request_sha, "lease_until": stamp(utcnow() + timedelta(minutes=15))}
        if value and value.get("request_sha") == request_sha and value.get("completion"):
            processing["completion"] = value["completion"]
        try:
            new_sha = self.save_result(request_id, processing, sha)
        except GitHubError as error:
            if error.status in (409, 422):
                return False
            raise
        self.active = (request_id, processing, new_sha)
        self.last_lease = time.monotonic()
        return True

    def assert_owner(self):
        request_id, _, _ = self.active
        value, sha = self.result(request_id)
        if not value or value.get("owner") != self.identity or value.get("state") != "processing":
            raise RuntimeError("다른 처리 프로그램이 요청을 인계받았습니다.")
        if parse_stamp(value["lease_until"]) <= utcnow():
            raise RuntimeError("요청 처리 예약이 만료됐습니다. 잠시 후 자동으로 재확인합니다.")
        self.active = (request_id, value, sha)

    def tick(self):
        self.heartbeat()
        if self.active and time.monotonic() - self.last_lease >= 60:
            self.assert_owner()
            request_id, value, sha = self.active
            value["lease_until"] = stamp(utcnow() + timedelta(minutes=15))
            self.active = (request_id, value, self.save_result(request_id, value, sha))
            self.last_lease = time.monotonic()

    def finish(self, state, message, questions=None, warnings=None, commit_sha=None):
        self.assert_owner()
        request_id, value, sha = self.active
        value.update({"state": state, "message": message, "questions": questions or [],
                      "warnings": warnings or [], "commit_sha": commit_sha})
        value.pop("lease_until", None)
        new_sha = self.save_result(request_id, value, sha)
        self.terminal_cache[request_id] = new_sha
        self.active = None

    def history(self, request):
        history, seen = [], {request["id"]}
        parent = request.get("parent_id")
        while parent:
            if parent in seen or len(history) >= 8:
                raise ValueError("확인 질문이 너무 길게 이어졌습니다. 새 요청으로 정리해 주세요.")
            seen.add(parent)
            item, request_sha = self.github.read_json(self.queue, f"requests/{parent}.json", self.queue_branch)
            result, _ = self.result(parent)
            if not item or not result or result.get("state") != "needs_input":
                raise ValueError("답변할 확인 질문을 찾지 못했습니다.")
            if result.get("request_sha") != request_sha:
                raise ValueError("원래 확인 요청의 내용이 바뀌었습니다. 새 요청으로 보내 주세요.")
            validate_request(item, parent)
            history.append({"request": item, "result": result})
            parent = item.get("parent_id")
        return list(reversed(history))

    def recovered_commit(self, request_id, request_sha, base):
        # Keep pre-migration receipts readable: a restarted job must never repeat
        # an edit merely because the repository layout changed after its commit.
        for folder in (APPLIED_PATH, ".bridge/applied"):
            marker, _ = self.github.read_json(self.target, f"{folder}/{request_id}.json", base)
            if marker is not None:
                if not isinstance(marker, dict) or marker.get("request_sha") != request_sha:
                    raise ValueError("이미 처리된 요청의 내용이 바뀌었습니다. 새 요청으로 보내 주세요.")
                return base
        return None

    def process(self, request, request_sha):
        request_id = request["id"]
        validate_request(request, request_id)
        history = self.history(request)
        if self.active[1].get("completion"):
            completion = self.active[1]["completion"]
            self.finish("completed", completion["message"], warnings=completion["warnings"])
            return
        for attempt in range(3):
            self.tick()
            base = self.github.head(self.target, self.branch)
            recovered = self.recovered_commit(request_id, request_sha, base)
            if recovered:
                self.finish("completed", "이 요청은 일정에 반영되어 있습니다. 저장 완료 상태를 복구했습니다.", commit_sha=recovered)
                return
            events, _ = self.github.read_json(self.target, EVENTS_PATH, base)
            travel, _ = self.github.read_json(self.target, TRAVEL_PATH, base)
            notes, _ = self.github.read(self.target, NOTES_PATH, base)
            if not isinstance(events, list) or not isinstance(travel, dict):
                raise ValueError("현재 일정 파일을 읽을 수 없습니다.")
            try:
                school_index, _ = self.github.read_json(self.queue, 'school/index.json')
                school_context = build_school_context(school_index, request)
            except (GitHubError, ValueError, TypeError):
                # Ordinary schedule requests remain available during an eTL
                # outage, while the model must explicitly admit missing evidence.
                school_context = {'available': False, 'reason': '학교 자료 조회 실패', 'sources': []}
            plan = self.runner.run(request, events, travel, notes or "", history, self.tick,
                                   school_context=school_context)
            updated_events, updated_travel, warnings = apply_plan(events, travel, plan, request_id)
            if plan["status"] == "needs_input":
                self.finish("needs_input", plan["message"], plan["questions"])
                return
            self.assert_owner()
            if events == updated_events and travel == updated_travel:
                if self.github.head(self.target, self.branch) != base:
                    continue
                # Persist the no-change decision before completing. Restart must not turn
                # an already-finished no-op into a new edit against later schedule data.
                request_id, value, result_sha = self.active
                value["completion"] = {"message": plan["message"], "warnings": warnings}
                self.active = (request_id, value, self.save_result(request_id, value, result_sha))
                self.finish("completed", plan["message"], warnings=warnings)
                return
            files = {APPLIED_PATH + "/" + request_id + ".json": json.dumps({
                "version": 1, "id": request_id, "request_sha": request_sha, "applied_at": stamp(),
            }) + "\n"}
            if events != updated_events:
                files[EVENTS_PATH] = json.dumps(updated_events, ensure_ascii=False, indent=2) + "\n"
            if travel != updated_travel:
                files[TRAVEL_PATH] = json.dumps(updated_travel, ensure_ascii=False, indent=2) + "\n"
            try:
                commit_sha = self.github.commit_files(self.target, self.branch, base, files,
                                                     f"Apply schedule request {request_id}")
            except GitHubError as error:
                if error.status in (0, 409, 422):
                    # A successful PATCH may have lost its response; marker recovery precedes retries.
                    latest = self.github.head(self.target, self.branch)
                    recovered = self.recovered_commit(request_id, request_sha, latest)
                    if recovered:
                        self.finish("completed", plan["message"], warnings=warnings, commit_sha=recovered)
                        return
                    if latest != base:
                        logging.info("Branch changed; re-plan request %s", request_id)
                        continue
                raise
            self.finish("completed", plan["message"], warnings=warnings, commit_sha=commit_sha)
            return
        raise RuntimeError("일정이 연속해서 변경되어 저장하지 않았습니다. 최신 일정을 확인한 뒤 다시 보내 주세요.")

    def poll(self):
        self.heartbeat()
        tree = self.github.tree(self.queue, self.queue_branch)
        paths = sorted(path for path in tree if path.startswith("requests/") and path.endswith(".json"))
        for path in paths:
            request_id = path[len("requests/"):-len(".json")]
            if not ID_RE.fullmatch(request_id):
                continue
            result_sha = tree.get(f"results/{request_id}.json")
            if result_sha and self.terminal_cache.get(request_id) == result_sha:
                continue
            existing, existing_sha = self.result(request_id) if result_sha else (None, None)
            if existing and existing.get("state") in TERMINAL:
                self.terminal_cache[request_id] = existing_sha
                continue
            if not self.claim(request_id, tree[path]):
                # Respect oldest in-progress request across workers; don't start a second writer.
                return
            try:
                request, request_sha = self.github.read_json(self.queue, path, self.queue_branch)
                if request_sha != tree[path]:
                    raise ValueError("접수 후 요청 내용이 바뀌었습니다. 새 요청으로 보내 주세요.")
                validate_request(request, request_id)
                logging.info("Processing request %s via %s", request_id, request["agent"])
                self.process(request, request_sha)
            except Exception as error:
                logging.exception("Request %s failed", request_id)
                try:
                    # Never report failure if the atomic schedule write has actually succeeded.
                    base = self.github.head(self.target, self.branch)
                    recovered = self.recovered_commit(request_id, tree[path], base)
                    if recovered:
                        self.finish("completed", "일정에 반영되었습니다. 처리 결과를 복구했습니다.", commit_sha=recovered)
                    else:
                        value, _ = self.result(request_id)
                        completion = (value or {}).get("completion")
                        if completion and value.get("request_sha") == tree[path]:
                            self.finish("completed", completion["message"], warnings=completion["warnings"])
                        else:
                            self.finish("failed", str(error)[:1200])
                except Exception:
                    logging.exception("Could not persist result; lease recovery will retry")
            finally:
                self.active = None
            # Re-fetch the queue after each job; no stale iteration after a long model run.
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true", help="Handle at most one pending request")
    parser.add_argument("--check", action="store_true", help="Check authentication/configuration only")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    root = Path(config["data_dir"])
    root.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(root / "worker.log", maxBytes=2_000_000,
                                                      backupCount=3, encoding="utf-8")])
    if args.check:
        worker = Worker(config)
        worker.verify()
        print("GitHub 인증·비공개 요청 저장소·일정 저장 권한 확인 완료")
        return
    with WorkerLock(root / "worker.lock"):
        (root / "worker.pid").write_text(str(os.getpid()), encoding="ascii")
        worker = Worker(config)
        worker.verify()
        logging.info("Worker started for %s", config["target_repo"])
        worker.heartbeat(force=True)
        while True:
            if (root / "stop.request").exists():
                logging.info("Worker stopped on request")
                return
            try:
                worker.poll()
            except Exception:
                logging.exception("Polling failed; retrying after interval")
                if args.once:
                    raise
            if args.once:
                return
            time.sleep(max(10, config.get("poll_seconds", 20)))


if __name__ == "__main__":
    main()
