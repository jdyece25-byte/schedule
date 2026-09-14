"""Validate a user-entered eTL API token, then store it with Windows DPAPI."""
import argparse
import getpass
import json
import os
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.notifications.pc import protected_bytes
from src.school.sources import collect_etl
from src.bridge.github import GitHub


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', action='store_true', help='Also save ETL_API_TOKEN in the private request repository Actions secrets')
    args = parser.parse_args()
    root = Path(os.environ['LOCALAPPDATA']) / 'ScheduleSchool'
    source = root / 'sources.json'
    if not source.is_file():
        raise SystemExit('Install the independent school collector first.')
    config = json.loads(source.read_text(encoding='utf-8-sig'))
    token = getpass.getpass('eTL API access token (hidden; do not enter your account password): ').strip()
    if not token:
        raise SystemExit('No token was entered; configuration was not changed.')
    result = collect_etl(config, token=token)
    if result['status'] not in ('ok', 'partial') or not result.get('courses'):
        raise SystemExit('eTL course access could not be verified. No token was saved.')
    temporary = root / 'etl.dpapi.tmp'
    temporary.write_bytes(protected_bytes(token.encode('utf-8')))
    temporary.replace(root / 'etl.dpapi')
    (root / 'collect.request').write_text('check after authenticated connection', encoding='ascii')
    print('eTL read access verified. Token encrypted for this Windows user. Collection requested.')
    if args.cloud:
        repository = 'jdyece25-byte/schedule-requests'
        if GitHub().api('repos/' + repository).get('private') is not True:
            raise SystemExit('Cloud configuration requires a private repository. Local configuration is saved.')
        result = subprocess.run(['gh', 'secret', 'set', 'ETL_API_TOKEN', '--repo', repository],
                                input=token.encode('utf-8'), capture_output=True, timeout=45,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise SystemExit('Local connection is saved; cloud secret could not be saved. Retry with --cloud.')
        print('Private cloud secret saved. eTL polling will also run while this PC is off.')


if __name__ == '__main__':
    main()
