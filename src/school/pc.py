"""Watch course files and approved notice decisions without stopping other services."""
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

POLL_SECONDS = 5
COLLECT_SECONDS = 900

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.bridge.github import GitHub
from src.bridge.supervisor import SupervisorLock, atomic_json, probe_lock
from src.notifications.pc import protected_bytes


def status(root):
    value = None
    try:
        value = json.loads((root / 'status.json').read_text(encoding='utf-8'))
    except (FileNotFoundError, ValueError):
        pass
    lock, _ = probe_lock(root / 'school.lock')
    return {'running': lock == 'held', 'disabled': (root / 'disabled').exists(), 'snapshot': value}


def pending_decisions(tree):
    return any(path.startswith('school/decisions/') and path.endswith('.json') and
               path.replace('school/decisions/', 'school/decision-results/', 1) not in tree for path in tree)


def invoke(config_path, collect):
    """Run an isolated collector/decision pass without exposing provider output."""
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    source_path = root / 'sources.json'
    if collect:
        sources, _ = GitHub().read_json(config['target_repo'], 'DB/school-sources.json')
        if sources is not None:
            atomic_json(source_path, sources)
    environment = dict(os.environ)
    secret = root / 'etl.dpapi'
    if collect and secret.is_file():
        environment['ETL_API_TOKEN'] = protected_bytes(secret.read_bytes(), decrypt=True).decode('utf-8')
    command = [sys.executable, '-B', str(Path(__file__).with_name('runner.py')), '--sources', str(source_path)]
    if collect:
        command.extend(['--local-root', config['local_root']])
    else:
        command.append('--decisions-only')
        environment.pop('ETL_API_TOKEN', None)
    try:
        result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=420, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    finally:
        environment.pop('ETL_API_TOKEN', None)
    try:
        report = json.loads(result.stdout)
        if not isinstance(report, dict):
            report = {}
    except (ValueError, UnicodeError):
        report = {}
    return result.returncode, report


def run(config_path):
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    initial_code = Path(__file__).read_bytes()
    last_collect = float('-inf')
    last_index = None
    last_head = None
    tree = {}
    tasks = {}
    errors = {}
    retry_at = {}
    failures = {}
    collector_state = {}
    item_count = 0
    api = GitHub()
    # Collection can spend minutes reading eTL/PDFs. A separate, bounded slot
    # keeps checking decisions during those reads; the runner's short lease
    # serializes only actual private/public writes.
    with SupervisorLock(root / 'school.lock'), ThreadPoolExecutor(max_workers=2) as pool:
        while not (root / 'disabled').exists():
            if Path(__file__).read_bytes() != initial_code:
                return
            outcome = {'updated_at': datetime.now(timezone.utc).isoformat(), 'healthy': True}
            try:
                for mode, task in list(tasks.items()):
                    if not task['future'].done():
                        continue
                    del tasks[mode]
                    try:
                        code, report = task['future'].result()
                    except Exception:
                        code, report = 1, {'status': 'error', 'stage': 'child'}
                    if code == 0:
                        errors.pop(mode, None)
                        failures.pop(mode, None)
                        retry_at.pop(mode, None)
                        last_index = task['index_sha']
                        if isinstance(report.get('collectors'), dict):
                            collector_state = report['collectors']
                        if isinstance(report.get('items'), int):
                            item_count = report['items']
                        if mode == 'collect':
                            last_collect = time.monotonic()
                            request = root / 'collect.request'
                            if request.exists() and request.stat().st_mtime_ns == task['request_version']:
                                request.unlink()
                    else:
                        # Contention/temporary errors are visibly retrying;
                        # genuine auth/configuration errors remain unhealthy.
                        errors[mode] = {'state': 'retry_pending' if code == 75 else 'error',
                                        'stage': report.get('stage', mode), 'http_status': report.get('http_status')}
                        failures[mode] = failures.get(mode, 0) + 1
                        delay = 5 if mode == 'decisions' and code == 75 else min(300, 15 * 2 ** min(failures[mode], 5))
                        retry_at[mode] = time.monotonic() + delay
                head = api.head(config['queue_repo'])
                if head != last_head:
                    tree = api.tree(config['queue_repo'], head)
                    last_head = head
                inbox_changed = tree.get('school/index.json') != last_index
                if 'decisions' not in tasks and (pending_decisions(tree) or inbox_changed) and time.monotonic() >= retry_at.get('decisions', 0):
                    tasks['decisions'] = {'future': pool.submit(invoke, config_path, False), 'index_sha': tree.get('school/index.json')}
                request = root / 'collect.request'
                collect = time.monotonic() - last_collect >= COLLECT_SECONDS or request.exists()
                if 'collect' not in tasks and collect and time.monotonic() >= retry_at.get('collect', 0):
                    tasks['collect'] = {'future': pool.submit(invoke, config_path, True), 'index_sha': tree.get('school/index.json'),
                                        'request_version': request.stat().st_mtime_ns if request.exists() else None}
                outcome.update(healthy=not errors, state='retry_pending' if errors else 'working' if tasks else 'idle',
                               collectors=collector_state, items=item_count, active=list(tasks), errors=errors,
                               pending_decisions=pending_decisions(tree))
                outcome['etl_key_configured'] = (root / 'etl.dpapi').is_file()
            except Exception:
                outcome.update(healthy=False, state='retry_pending')
            atomic_json(root / 'status.json', outcome)
            time.sleep(POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--ensure-running', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    config = args.config.resolve()
    root = config.parent
    if args.ensure_running:
        current = status(root)
        if not current['running'] and not current['disabled']:
            pythonw = Path(sys.executable).with_name('pythonw.exe')
            process = subprocess.Popen([str(pythonw), '-B', str(Path(__file__).resolve()), '--config', str(config)],
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
            for _ in range(30):
                current = status(root)
                if current['running'] or process.poll() is not None:
                    break
                time.sleep(.1)
        print(json.dumps(current))
        return 0 if current['running'] or current['disabled'] else 1
    if args.status:
        print(json.dumps(status(root)))
        return 0
    run(config)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
