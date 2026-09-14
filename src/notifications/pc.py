"""Optional fast push sender, independent of the running ScheduleBridge worker.

Cloud cron remains authoritative for clock reminders. This process observes the
public repository every ten seconds and uses the same private delivery ledger.
No schedule files, worker controls, or GitHub credentials are written locally.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.bridge.github import GitHub
from src.bridge.supervisor import SupervisorLock, atomic_json, probe_lock


def protected_bytes(value, *, decrypt=False):
    """Windows DPAPI, bound to the current signed-in user; never write plaintext."""
    if os.name != "nt":
        raise RuntimeError("The PC push helper requires Windows DPAPI")

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(value)
    source = Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError("Windows could not protect/unprotect the notification key")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def status(root):
    lock, error = probe_lock(root / "push.lock")
    snapshot = None
    try:
        snapshot = json.loads((root / "status.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        pass
    return {"running": lock == "held", "disabled": (root / "disabled").exists(),
            "lock_error": bool(error), "snapshot": snapshot}


def invoke_sender(config, root):
    environment = dict(os.environ)
    environment["VAPID_PRIVATE_KEY"] = protected_bytes((root / "vapid.dpapi").read_bytes(), decrypt=True).decode("ascii")
    environment["VAPID_SUBJECT"] = config["subject"]
    command = [sys.executable, "-B", str(Path(__file__).with_name("sender.py")),
               "--queue-repo", config["queue_repo"], "--target-repo", config["target_repo"], "--changes-only"]
    # Errors can contain endpoint URLs in third-party libraries. Do not log
    # subprocess output; the private delivery ledger records retry outcomes.
    try:
        result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=150, creationflags=subprocess.CREATE_NO_WINDOW)
        return result.returncode == 0
    finally:
        environment.pop("VAPID_PRIVATE_KEY", None)


def run(config, root, *, once=False):
    last_head = None
    last_attempt = 0.0
    running_source = Path(__file__).read_bytes()
    github = GitHub()
    with SupervisorLock(root / "push.lock"):
        while not (root / "disabled").exists():
            # The minute recovery task picks up installed helper upgrades. Exit
            # only this optional sender after its current pass; never the worker.
            if Path(__file__).read_bytes() != running_source:
                atomic_json(root / "status.json", {"updated_at": datetime.now(timezone.utc).isoformat(),
                                                   "healthy": False, "last_check": "reloading"})
                return
            result = {"updated_at": datetime.now(timezone.utc).isoformat(), "healthy": True}
            try:
                head = github.head(config["target_repo"], "main")
                # A periodic retry also covers an Actions lease held at change
                # time, new phone subscriptions, and transient push failures.
                if head != last_head or time.monotonic() - last_attempt >= 60:
                    successful = invoke_sender(config, root)
                    last_attempt = time.monotonic()
                    if successful:
                        last_head = head
                    result["healthy"] = successful
                result["last_check"] = "ok" if result["healthy"] else "retry_pending"
            except Exception:
                # No exception repr: URLs, encrypted subscription keys and
                # credentials must not enter logs or public health reports.
                result.update(healthy=False, last_check="retry_pending")
            atomic_json(root / "status.json", result)
            if once:
                return
            time.sleep(10)


def ensure_running(config_path, root):
    config_path = Path(config_path).resolve()
    current = status(root)
    if current["running"] or current["disabled"] or current["lock_error"]:
        return current
    executable = Path(sys.executable)
    if executable.with_name("pythonw.exe").is_file():
        executable = executable.with_name("pythonw.exe")
    process = subprocess.Popen(
        [str(executable), "-B", str(Path(__file__).resolve()), "--config", str(config_path)],
        cwd=Path(__file__).parent, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    for _ in range(30):
        current = status(root)
        if current["running"] or process.poll() is not None:
            break
        time.sleep(0.1)
    return current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--ensure-running", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8-sig"))
    root = args.config.resolve().parent
    if args.ensure_running or args.status:
        current = ensure_running(args.config, root) if args.ensure_running else status(root)
        print(json.dumps(current))
        return 0 if current["running"] or current["disabled"] else 1
    try:
        run(config, root, once=args.once)
    except RuntimeError:
        if status(root)["running"]:
            return 0
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
