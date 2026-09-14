"""Watch course files and approved notice decisions without stopping other services."""
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

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


def run(config_path):
    root = config_path.parent
    config = json.loads(config_path.read_text(encoding='utf-8-sig'))
    source_path = root / 'sources.json'
    initial_code = Path(__file__).read_bytes()
    last_collect = float('-inf')
    last_index = None
    api = GitHub()
    with SupervisorLock(root / 'school.lock'):
        while not (root / 'disabled').exists():
            if Path(__file__).read_bytes() != initial_code:
                return
            outcome = {'updated_at': datetime.now(timezone.utc).isoformat(), 'healthy': True}
            try:
                collect = time.monotonic() - last_collect >= 900 or (root / 'collect.request').exists()
                tree = api.tree(config['queue_repo'])
                inbox_changed = tree.get('school/index.json') != last_index
                pending = any(path.startswith('school/decisions/') and path.endswith('.json') and
                              path.replace('school/decisions/', 'school/decision-results/', 1) not in tree for path in tree)
                if collect or pending or inbox_changed:
                    if collect:
                        sources, _ = api.read_json(config['target_repo'], 'DB/school-sources.json')
                        if sources is not None:
                            atomic_json(source_path, sources)
                    environment = dict(os.environ)
                    secret = root / 'etl.dpapi'
                    if secret.is_file():
                        environment['ETL_API_TOKEN'] = protected_bytes(secret.read_bytes(), decrypt=True).decode('utf-8')
                    command = [sys.executable, '-B', str(Path(__file__).with_name('runner.py')), '--sources', str(source_path),
                               '--local-root', config['local_root']]
                    if not collect:
                        command.append('--decisions-only')
                    result = subprocess.run(command, env=environment, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                            timeout=420, creationflags=subprocess.CREATE_NO_WINDOW)
                    environment.pop('ETL_API_TOKEN', None)
                    outcome['healthy'] = result.returncode in (0, 75)
                    if result.returncode == 0:
                        last_index = tree.get('school/index.json')
                        safe = json.loads(result.stdout)
                        outcome['collectors'] = safe.get('collectors', {})
                        outcome['items'] = safe.get('items', 0)
                        if collect:
                            last_collect = time.monotonic()
                            (root / 'collect.request').unlink(missing_ok=True)
                outcome['etl_key_configured'] = (root / 'etl.dpapi').is_file()
            except Exception:
                outcome.update(healthy=False, state='retry_pending')
            atomic_json(root / 'status.json', outcome)
            time.sleep(30)


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
