"""Validate a user-entered eTL API token, then store it with Windows DPAPI."""
import argparse
from datetime import datetime, timezone
import getpass
import json
import os
import re
from pathlib import Path
import sys
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.notifications.pc import protected_bytes
from src.school.sources import collect_etl, probe_etl
from src.bridge.github import GitHub
from src.bridge.supervisor import atomic_json


HINTS = {
    'auth_required': 'Token was rejected or API access is denied. Copy a newly generated eTL token.',
    'api_unavailable': 'The eTL API could not be reached. Check the network and try again.',
    'redirect_blocked': 'The API redirected to another address. Check the configured eTL address.',
    'invalid_etl_origin': 'The configured eTL address is invalid.',
    'invalid_api_shape': 'The server did not return the expected Canvas API response.',
    'unsafe_pagination': 'The course list pagination could not be verified.',
    'course_not_found_or_ambiguous': 'Authentication passed, but semester/course matching needs review.',
    'invalid_etl_configuration': 'The semester/course configuration needs review.',
    'api_page_limit': 'The course list exceeded the page limit.',
    'api_item_limit': 'The course list exceeded the item limit.',
    'api_source_count_limit': 'Some sources exceeded the collection limit.',
    'truncated': 'Some documents need manual review because they were too long.',
    'invalid_notice_fields': 'Some notices could not be parsed and need review.',
    'setup_error': 'Local connection setup failed. No credential value was printed.',
    'cloud_setup_error': 'PC connection is saved; cloud secret setup failed. Retry with --cloud.',
}


def safe_diagnostic(probe, result=None):
    """Persist status/counts only: never an API response, profile, URL or token."""
    status = lambda value: value if value in ('ok', 'partial', 'error', 'auth_required') else 'error'
    codes = []
    for value in (probe, result or {}):
        for issue in value.get('issues', []):
            code = issue.get('code') if isinstance(issue, dict) else None
            code = code if isinstance(code, str) and (code in HINTS or re.fullmatch(r'http_[45][0-9][0-9]', code)) else 'collection_error'
            if code not in codes:
                codes.append(code)
    courses = (result or {}).get('courses', [])
    sources = (result or {}).get('sources', [])
    return {'version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'authentication': status(probe.get('status')), 'collection': status(result.get('status')) if result is not None else 'not_checked',
            'matched_course_count': len(courses) if isinstance(courses, list) else 0,
            'source_count': len(sources) if isinstance(sources, list) else 0, 'codes': codes,
            'local_saved': False, 'cloud_saved': False}


def describe(diagnostic):
    lines = ['Authentication: ' + diagnostic['authentication'],
             'Course collection: ' + diagnostic['collection'],
             'Matched courses: ' + str(diagnostic['matched_course_count'])]
    lines += [code + ': ' + HINTS.get(code, 'Some source data needs review.') for code in diagnostic['codes']]
    return '\n'.join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cloud', action='store_true', help='Also save ETL_API_TOKEN in the private request repository Actions secrets')
    args = parser.parse_args(argv)
    root = Path(os.environ['LOCALAPPDATA']) / 'ScheduleSchool'
    source = root / 'sources.json'
    if not source.is_file():
        print('Install the independent school collector first.')
        return 1
    diagnostic = safe_diagnostic({'status': 'error', 'issues': [{'code': 'setup_error'}]})
    try:
        config = json.loads(source.read_text(encoding='utf-8-sig'))
        token = getpass.getpass('eTL API access token (hidden; do not enter your account password): ').strip()
        if not token:
            diagnostic = safe_diagnostic({'status': 'auth_required', 'issues': [{'code': 'auth_required'}]})
            print('No token was entered. Existing connection was preserved.')
            return 1
        probe = probe_etl(config, token)
        diagnostic = safe_diagnostic(probe)
        if probe['status'] != 'ok':
            print('No token was saved. Existing connection was preserved.')
            return 1
        print('Authentication passed. Checking semester/course access...')
        try:
            result = collect_etl(config, token=token)
        except Exception:
            result = {'status': 'error', 'issues': [{'code': 'api_unavailable'}]}
        diagnostic = safe_diagnostic(probe, result)
        temporary = root / ('etl.' + str(os.getpid()) + '.dpapi.tmp')
        temporary.write_bytes(protected_bytes(token.encode('utf-8')))
        temporary.replace(root / 'etl.dpapi')
        diagnostic['local_saved'] = True
        (root / 'collect.request').write_text('check after authenticated connection', encoding='ascii')
        print('Authenticated token encrypted and saved for this Windows user.')
        if args.cloud:
            try:
                repository = 'jdyece25-byte/schedule-requests'
                if GitHub().api('repos/' + repository).get('private') is not True:
                    raise RuntimeError('Private repository required')
                completed = subprocess.run(['gh', 'secret', 'set', 'ETL_API_TOKEN', '--repo', repository],
                                           input=token.encode('utf-8'), capture_output=True, timeout=45,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if completed.returncode:
                    raise RuntimeError('Secret setup failed')
                diagnostic['cloud_saved'] = True
                print('Private cloud secret saved.')
            except Exception:
                diagnostic['codes'].append('cloud_setup_error')
                return 1
        ready = diagnostic['collection'] in ('ok', 'partial') and diagnostic['matched_course_count'] > 0
        print('Collection connected.' if ready else 'Authentication is saved; course collection still needs review.')
        return 0
    except (Exception, KeyboardInterrupt):
        diagnostic['codes'].append('setup_error')
        return 1
    finally:
        # Only safe codes and counts can be shared for troubleshooting.
        try:
            atomic_json(root / 'diagnostic.json', diagnostic)
        except OSError:
            pass
        print(describe(diagnostic))


if __name__ == '__main__':
    raise SystemExit(main())
