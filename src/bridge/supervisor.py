"""Keep the independently installed queue worker running across child failures.

`service.disabled` is a persistent, explicit user stop. `maintenance.json` uses
an ISO UTC `resume_at` deadline and cannot leave the service paused indefinitely.
The legacy worker `stop.request` is only a transient signal, never a reservation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import time


class SupervisorLock:
    """A process-owned lock released by the OS even after an unexpected exit."""
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("ScheduleBridge supervisor is already running.") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()
            self.file = None


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def probe_lock(path):
    """Probe the actual OS lock, never trust a reusable/stale PID by itself."""
    try:
        file = Path(path).open("r+b")
    except FileNotFoundError:
        return "missing", None
    except OSError as error:
        return "error", str(error)
    with file:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(file, fcntl.LOCK_UN)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                return "held", None
            return "error", str(error)
    return "free", None


def read_maintenance(path):
    """Return the bounded effective deadline; a broken file cannot pause forever."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        deadline = datetime.fromisoformat(value["resume_at"].replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            raise ValueError("maintenance deadline must include a timezone")
        return min(deadline.timestamp(), Path(path).stat().st_mtime + 3600), None
    except FileNotFoundError:
        return None, None
    except (ValueError, TypeError, KeyError, AttributeError, OSError) as error:
        return None, str(error)


def service_status(root):
    root = Path(root)
    errors = []
    worker_lock, worker_error = probe_lock(root / "worker.lock")
    supervisor_lock, supervisor_error = probe_lock(root / "supervisor.lock")
    for name, error in (("worker.lock", worker_error), ("supervisor.lock", supervisor_error)):
        if error:
            errors.append(name + ": " + error)
    maintenance, error = read_maintenance(root / "maintenance.json")
    if error:
        errors.append("maintenance.json: " + error)
    snapshot = None
    try:
        snapshot = json.loads((root / "supervisor-status.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as error:
        errors.append("supervisor-status.json: " + str(error))
    return {
        "worker_running": worker_lock == "held", "supervisor_running": supervisor_lock == "held",
        "worker_lock": worker_lock, "supervisor_lock": supervisor_lock,
        "disabled": (root / "service.disabled").exists(),
        "supervisor_stop_requested": (root / "supervisor.stop").exists(),
        "maintenance_until": datetime.fromtimestamp(maintenance, timezone.utc).isoformat()
            if maintenance and maintenance > time.time() else None,
        "errors": errors, "snapshot": snapshot,
    }


class Supervisor:
    def __init__(self, config_path, *, worker_path=None, interval=1,
                 restart_base=2, restart_max=60, stable_seconds=60):
        self.config_path = Path(config_path).resolve()
        config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self.root = Path(config["data_dir"]).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.worker_path = Path(worker_path or Path(__file__).with_name("worker.py")).resolve()
        self.interval = interval
        self.restart_base = restart_base
        self.restart_max = restart_max
        self.stable_seconds = stable_seconds
        self.child = None
        self.child_started = 0
        self.stop_started = None
        self.next_start = 0
        self.failures = 0
        self.starts = 0
        self.last_exit = None
        self.last_status = None
        self.last_status_at = 0
        self.disabled_file = self.root / "service.disabled"
        self.maintenance_file = self.root / "maintenance.json"
        self.stop_file = self.root / "stop.request"

    def pause_reason(self):
        if self.disabled_file.exists():
            return "stopped"
        try:
            content = self.maintenance_file.read_bytes()
        except FileNotFoundError:
            return None
        expires, error = read_maintenance(self.maintenance_file)
        if error:
            logging.warning("Invalid maintenance deadline; resuming the worker")
        if expires and expires > time.time():
            return "maintenance"
        # Do not erase a maintenance deadline replaced since it was read.
        try:
            if self.maintenance_file.read_bytes() == content:
                self.maintenance_file.unlink()
        except FileNotFoundError:
            pass
        return None

    def status(self, state):
        now = time.monotonic()
        worker_pid = self.child.pid if self.child and self.child.poll() is None else None
        signature = (state, worker_pid, self.starts, self.last_exit)
        if signature != self.last_status or now - self.last_status_at >= 10:
            atomic_json(self.root / "supervisor-status.json", {
                "version": 1, "state": state, "supervisor_pid": os.getpid(),
                "worker_pid": worker_pid, "starts": self.starts,
                "last_exit_code": self.last_exit,
                "updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            self.last_status, self.last_status_at = signature, now

    def start_child(self):
        # Child startup errors (including import failures) remain diagnosable.
        output_path = self.root / "worker-startup.log"
        if output_path.exists() and output_path.stat().st_size > 2_000_000:
            output_path.write_bytes(b"")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with output_path.open("ab") as output:
            self.child = subprocess.Popen(
                [sys.executable, str(self.worker_path), "--config", str(self.config_path)],
                cwd=self.worker_path.parent, stdin=subprocess.DEVNULL,
                stdout=output, stderr=output, creationflags=flags,
                start_new_session=os.name != "nt",
            )
        self.child_started = time.monotonic()
        self.stop_started = None
        self.starts += 1
        logging.info("Started queue worker PID %s", self.child.pid)

    def backoff(self):
        delay = min(self.restart_max, self.restart_base * (2 ** min(self.failures, 10)))
        self.failures += 1
        self.next_start = time.monotonic() + delay
        logging.warning("Worker exited (%s); restarting in %.1f seconds", self.last_exit, delay)

    def step(self):
        paused = self.pause_reason()
        now = time.monotonic()
        if paused:
            if not self.stop_file.exists():
                self.stop_file.write_text("stop", encoding="ascii")
            if self.child and self.child.poll() is None:
                if self.stop_started is None:
                    self.stop_started = now
            self.status(paused)
            return
        # A stale maintenance/legacy stop signal must not strand new requests.
        self.stop_file.unlink(missing_ok=True)
        if self.child and self.child.poll() is None:
            self.stop_started = None
            self.status("running")
            return
        if self.child:
            self.last_exit = self.child.returncode
            intentional = self.stop_started is not None
            if now - self.child_started >= self.stable_seconds:
                self.failures = 0
            self.child = None
            self.stop_started = None
            if intentional:
                self.next_start = 0
            else:
                self.backoff()
        if time.monotonic() < self.next_start:
            self.status("backoff")
            return
        # A previous supervisor can exit while its worker is still finishing a
        # request. Observe its lock instead of launching duplicate workers.
        existing, error = probe_lock(self.root / "worker.lock")
        if error:
            raise RuntimeError("Cannot inspect worker lock: " + error)
        if existing == "held":
            self.status("running")
            return
        try:
            self.start_child()
        except OSError:
            logging.exception("Cannot start queue worker")
            self.last_exit = None
            self.backoff()
            self.status("backoff")
            return
        self.status("running")

    def run(self):
        with SupervisorLock(self.root / "supervisor.lock"):
            pid_path = self.root / "supervisor.pid"
            pid_path.write_text(str(os.getpid()), encoding="ascii")
            try:
                while True:
                    try:
                        if (self.root / "supervisor.stop").exists():
                            self.stop_file.write_text("stop", encoding="ascii")
                            worker_lock, error = probe_lock(self.root / "worker.lock")
                            if error:
                                raise RuntimeError(error)
                            child_alive = self.child and self.child.poll() is None
                            if not child_alive and worker_lock != "held":
                                self.status("supervisor_stopped")
                                return
                            self.status("shutting_down")
                            time.sleep(self.interval)
                            continue
                        self.step()
                    except Exception:
                        logging.exception("Supervisor check failed; retrying")
                    time.sleep(self.interval)
            finally:
                try:
                    if pid_path.read_text(encoding="ascii") == str(os.getpid()):
                        pid_path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass


def ensure_running(supervisor, *, resume=False):
    if resume:
        for name in ("service.disabled", "maintenance.json", "stop.request", "supervisor.stop"):
            (supervisor.root / name).unlink(missing_ok=True)
    status = service_status(supervisor.root)
    if (status["worker_lock"] == "error" or status["supervisor_lock"] == "error" or
            status["supervisor_running"] or status["supervisor_stop_requested"]):
        return status
    executable = Path(sys.executable)
    if os.name == "nt" and executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS |
             subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    process = subprocess.Popen(
        [str(executable), str(Path(__file__).resolve()), "--config", str(supervisor.config_path)],
        cwd=Path(__file__).resolve().parent, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, start_new_session=os.name != "nt",
    )
    for _ in range(30):
        status = service_status(supervisor.root)
        if (status["supervisor_running"] and not status["errors"]) or process.poll() is not None:
            break
        time.sleep(0.1)
    if not status["supervisor_running"]:
        status["errors"].append("Supervisor did not acquire its lock; inspect supervisor.log.")
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="Report local process locks and pause state as JSON")
    mode.add_argument("--ensure-running", action="store_true", help="Start the supervisor if absent; preserve user stop")
    parser.add_argument("--resume", action="store_true", help="With --ensure-running, explicitly resume a stopped service")
    args = parser.parse_args()
    if args.resume and not args.ensure_running:
        parser.error("--resume requires --ensure-running")
    supervisor = Supervisor(args.config)
    if args.status or args.ensure_running:
        result = ensure_running(supervisor, resume=args.resume) if args.ensure_running else service_status(supervisor.root)
        print(json.dumps(result, ensure_ascii=True))
        return 1 if result["errors"] else 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[RotatingFileHandler(supervisor.root / "supervisor.log",
                                                      maxBytes=2_000_000, backupCount=2,
                                                      encoding="utf-8")])
    try:
        supervisor.run()
    except RuntimeError:
        logging.exception("Supervisor did not start")
        raise


if __name__ == "__main__":
    sys.exit(main())
