"""Private notice inbox and verified DB publishing; independent of ScheduleBridge."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.bridge.github import GitHub, GitHubError
from src.school.reconcile import apply, digest, prepare
from src.school.sources import collect_local, collect_etl
from src.school.extract import extract_candidates

QUEUE = 'jdyece25-byte/schedule-requests'
TARGET = 'jdyece25-byte/schedule'
INDEX = 'school/index.json'
LEASE = 'school/lease.json'
SAFE_ID = re.compile(r'[A-Za-z0-9_-]{1,100}\Z')


class CollectorBusy(RuntimeError):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class Importer:
    def __init__(self, github, config, *, queue=QUEUE, target=TARGET, inbox_only=False):
        self.api, self.config, self.queue, self.target = github, config, queue, target
        self.owner = uuid.uuid4().hex
        self.lease_sha = None
        self.index_sha = None
        self.index = None
        self.inbox_only = inbox_only

    def acquire(self):
        if self.queue == self.target or self.api.api('repos/' + self.queue).get('private') is not True:
            raise ValueError('School notices require the private request repository')
        lease, sha = self.api.read_json(self.queue, LEASE)
        if lease and datetime.fromisoformat(lease['until'].replace('Z', '+00:00')) > datetime.now(timezone.utc):
            raise CollectorBusy('Another school collector is active')
        self.lease_sha = self.api.put_json(self.queue, LEASE, {
            'owner': self.owner, 'until': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}, sha,
            message='Reserve school notice synchronization')
        self.index, self.index_sha = self.api.read_json(self.queue, INDEX)
        if self.index is None:
            self.index = {'version': 1, 'updated_at': stamp(), 'collectors': {}, 'items': []}
        if self.index.get('version') != 1 or not isinstance(self.index.get('items'), list):
            raise ValueError('Unsupported school index')

    def renew(self):
        lease, sha = self.api.read_json(self.queue, LEASE)
        if not lease or lease.get('owner') != self.owner:
            raise RuntimeError('School collector ownership changed')
        self.lease_sha = self.api.put_json(self.queue, LEASE, {
            'owner': self.owner, 'until': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}, sha,
            message='Renew school notice synchronization')

    def release(self):
        lease, sha = self.api.read_json(self.queue, LEASE)
        if lease and lease.get('owner') == self.owner:
            self.api.put_json(self.queue, LEASE, {'owner': None, 'until': stamp()}, sha,
                              message='Complete school notice synchronization')

    def save_index(self, extra=None):
        self.renew()
        self.index['updated_at'] = stamp()
        files = dict(extra or {})
        files[INDEX] = json.dumps(self.index, ensure_ascii=False, indent=2) + '\n'
        for attempt in range(5):
            base = self.api.head(self.queue)
            _, current_sha = self.api.read_json(self.queue, INDEX, base)
            if current_sha != self.index_sha:
                raise RuntimeError('School inbox was edited concurrently; changes were preserved')
            try:
                commit = self.api.commit_files(self.queue, 'main', base, files, 'Update private school notice inbox')
                _, self.index_sha = self.api.read_json(self.queue, INDEX, commit)
                return
            except GitHubError as error:
                if error.status not in (0, 409, 422) or attempt == 4:
                    raise
                current, current_sha = self.api.read_json(self.queue, INDEX)
                if current == self.index:
                    self.index_sha = current_sha
                    return

    def public_source(self, item):
        return {**item, 'term_start': self.config['term']['start'], 'term_end': self.config['term']['end']}

    def publish(self, item, candidates, operation_id, *, approved):
        if self.inbox_only:
            raise RuntimeError('The cloud collector cannot write the public schedule')
        receipt_path = 'DB/school-applied/' + digest(operation_id) + '.json'
        payload = [{key: c.get(key) for key in ('id', 'action', 'event', 'target_id', 'target_hash')}
                   for c in candidates] if approved else sorted(c['id'] for c in candidates)
        payload_hash = digest({'source_id': item['id'], 'source_hash': item['content_hash'],
                               'candidates': payload, 'approved': approved})
        for attempt in range(4):
            self.renew()
            base = self.api.head(self.target)
            receipt, _ = self.api.read_json(self.target, receipt_path, base)
            if receipt is not None:
                if (not isinstance(receipt, dict) or receipt.get('version') != 1
                        or receipt.get('operation_hash') != digest(operation_id)
                        or receipt.get('payload_hash') != payload_hash
                        or not isinstance(receipt.get('event_ids'), list)
                        or not all(isinstance(identifier, str) for identifier in receipt['event_ids'])):
                    raise ValueError('이전에 처리한 요청과 내용이 다릅니다. 최신 공지에서 다시 요청해 주세요.')
                return receipt['event_ids']
            events, _ = self.api.read_json(self.target, 'DB/events.json', base)
            updated, event_ids = apply(events, candidates, self.public_source(item),
                                       approved=approved, config=self.config)
            files = {receipt_path: json.dumps({'version': 1, 'operation_hash': digest(operation_id), 'payload_hash': payload_hash,
                                               'event_ids': event_ids, 'applied_at': stamp()}) + '\n'}
            if updated != events:
                files['DB/events.json'] = json.dumps(updated, ensure_ascii=False, indent=2) + '\n'
            try:
                self.api.commit_files(self.target, 'main', base, files, 'Apply verified school schedule changes')
                return event_ids
            except GitHubError as error:
                if error.status not in (0, 409, 422) or attempt == 3:
                    raise
        raise RuntimeError('Schedule repository remained busy')

    def consume(self, results):
        items = {item['id']: item for item in self.index['items']}
        known = self.index.setdefault('known_sources', {})
        extra = {}
        events, _ = self.api.read_json(self.target, 'DB/events.json', self.api.head(self.target))
        for collector, result in results.items():
            first_local = collector == 'local' and not self.index['collectors'].get('local', {}).get('initialized')
            for source in result.get('sources', []):
                existing = items.get(source['id'])
                if existing and existing['content_hash'] == source['content_hash']:
                    continue
                if not existing and known.get(source['id']) == source['content_hash']:
                    continue
                known[source['id']] = source['content_hash']
                candidates = [prepare(candidate, events, self.config) for candidate in extract_candidates(source, self.config)]
                definition = next((c for c in self.config['courses'] if c['key'] == source['course']), {})
                item = {'id': source['id'], 'content_hash': source['content_hash'],
                        'course': definition.get('name', source['course']), 'course_key': source['course'],
                        'title': source['title'], 'source_url': source.get('source_url', ''),
                        'source_kind': source['kind'], 'updated_at': source['updated_at'],
                        'first_seen_at': stamp(), 'candidates': candidates, 'event_ids': [],
                        'state': 'needs_review' if candidates else 'info',
                        'reason': '원문과 날짜를 확인한 뒤 선택한 일정만 적용하세요.' if candidates else '새 자료·공지입니다. 내용을 확인해 주세요.',
                        'extraction_status': source.get('extraction_status', 'parsed')}
                if first_local:
                    item.update(state='baseline', candidates=[], reason='기존 자료를 관찰 기준으로 등록했습니다. 이미 검증된 일정을 다시 추가하지 않습니다.')
                elif candidates and all(c.get('action') == 'link' for c in candidates):
                    item.update(state='applied', event_ids=[c['target_id'] for c in candidates if c.get('target_id')],
                                reason='같은 일정이 이미 등록되어 있어 기존 ID와 연결했습니다.')
                elif candidates and all(c.get('auto_eligible') or c.get('action') == 'link' for c in candidates):
                    if self.inbox_only:
                        item.update(state='ready', reason='eTL의 명시된 마감입니다. PC 연결 시 검증 후 일정에 반영합니다.')
                    else:
                        try:
                            item['event_ids'] = self.publish(item, candidates, source['id'] + ':' + source['content_hash'], approved=False)
                            item.update(state='applied', reason='eTL에 명시된 제출 마감을 검증해 적용했습니다.')
                            events, _ = self.api.read_json(self.target, 'DB/events.json', self.api.head(self.target))
                        except ValueError as error:
                            item.update(state='conflict', reason=str(error))
                if not first_local:
                    # Raw source is private, never copied to public event notes.
                    extra['school/sources/' + source['id'] + '.json'] = json.dumps(source, ensure_ascii=False) + '\n'
                items[item['id']] = item
            previous = self.index['collectors'].get(collector, {})
            self.index['collectors'][collector] = {
                'state': result['status'], 'last_checked': stamp(),
                'initialized': bool(previous.get('initialized') or result['status'] in ('ok', 'partial')),
                'source_count': len(result.get('sources', [])), 'issue_count': len(result.get('issues', [])),
                'issues': result.get('issues', [])[:50],
                'last_success': stamp() if result['status'] in ('ok', 'partial') else previous.get('last_success')}
        ordered = sorted(items.values(), key=lambda item: item.get('first_seen_at', ''), reverse=True)
        # Never discard unreviewed notices. Hash tombstones prevent archived
        # baseline/completed sources from reappearing as new on later scans.
        pending = [item for item in ordered if item['state'] not in ('baseline', 'applied', 'ignored')]
        history = [item for item in ordered if item['state'] in ('baseline', 'applied', 'ignored')][:500]
        self.index['items'] = pending + history
        self.save_index(extra)

    def ready(self):
        if self.inbox_only:
            return
        changed = False
        for item in self.index['items']:
            if item.get('state') != 'ready':
                continue
            events, _ = self.api.read_json(self.target, 'DB/events.json', self.api.head(self.target))
            candidates = [prepare(candidate, events, self.config) for candidate in item['candidates']]
            item['candidates'] = candidates
            if not all(c.get('auto_eligible') or c.get('action') == 'link' for c in candidates):
                item.update(state='needs_review', reason='기존 일정과 달라 확인이 필요합니다.')
            else:
                try:
                    item['event_ids'] = self.publish(item, candidates, item['id'] + ':' + item['content_hash'], approved=False)
                    item.update(state='applied', reason='eTL의 명시된 제출 마감을 검증해 적용했습니다.')
                except ValueError as error:
                    item.update(state='conflict', reason=str(error))
            changed = True
        if changed:
            self.save_index()

    def decisions(self):
        if self.inbox_only:
            return False
        tree = self.api.tree(self.queue)
        paths = sorted(path for path in tree if re.fullmatch(r'school/decisions/[A-Za-z0-9_-]{1,100}\.json', path))
        changed = False
        outcomes = {}
        items = {item['id']: item for item in self.index['items']}
        for path in paths:
            identifier = Path(path).stem
            outcome_path = 'school/decision-results/' + identifier + '.json'
            if outcome_path in tree:
                continue
            decision, _ = self.api.read_json(self.queue, path)
            try:
                if not isinstance(decision, dict) or decision.get('version') != 1 or decision.get('id') != identifier:
                    raise ValueError('잘못된 확인 요청입니다.')
                item = items.get(decision.get('source_id'))
                if not item or decision.get('source_hash') != item['content_hash']:
                    raise ValueError('원문이 변경되었습니다. 최신 공지를 다시 확인해 주세요.')
                if decision.get('action') == 'ignore':
                    item.update(state='ignored', reason='사용자가 확인한 공지입니다.')
                elif decision.get('action') == 'approve':
                    originals = {c['id']: c for c in item['candidates']}
                    selected = decision.get('candidates')
                    if not isinstance(selected, list) or not 1 <= len(selected) <= 30:
                        raise ValueError('적용할 일정을 선택해 주세요.')
                    prepared, seen = [], set()
                    for choice in selected:
                        original = originals.get(choice.get('id'))
                        if not original or original['id'] in seen or choice.get('action') != original.get('action'):
                            raise ValueError('일정 후보가 일치하지 않습니다.')
                        if choice.get('target_id') != original.get('target_id'):
                            raise ValueError('수정·삭제 대상이 일치하지 않습니다.')
                        seen.add(original['id'])
                        candidate = deepcopy(original)
                        if not isinstance(choice.get('event'), dict):
                            raise ValueError('일정 내용을 확인해 주세요.')
                        if original.get('action') in ('delete', 'link') and choice['event'] != original['event']:
                            raise ValueError('삭제·연결 대상의 내용은 변경할 수 없습니다. 최신 일정을 확인해 주세요.')
                        candidate['event'] = choice['event']
                        prepared.append(candidate)
                    event_ids = self.publish(item, prepared, 'decision:' + identifier, approved=True)
                    remaining = [c for c in item['candidates'] if c['id'] not in seen and c.get('action') != 'link']
                    item.update(state='needs_review' if remaining else 'applied', candidates=remaining,
                                event_ids=list(dict.fromkeys(item.get('event_ids', []) + event_ids)),
                                reason='선택한 일정을 반영했습니다. 남은 후보를 확인해 주세요.' if remaining else '사용자가 확인한 일정을 반영했습니다.')
                else:
                    raise ValueError('지원하지 않는 확인 요청입니다.')
                outcome = {'version': 1, 'state': 'completed', 'updated_at': stamp()}
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                outcome = {'version': 1, 'state': 'conflict', 'updated_at': stamp(),
                           'message': str(error) if isinstance(error, ValueError) else '확인 요청 형식을 다시 확인해 주세요.'}
                item = items.get(decision.get('source_id')) if isinstance(decision, dict) else None
                if item and decision.get('source_hash') == item['content_hash']:
                    item.update(state='conflict', reason=outcome['message'])
            outcomes[outcome_path] = json.dumps(outcome, ensure_ascii=False) + '\n'
            changed = True
        if changed:
            self.save_index(outcomes)
        return changed


def run_once(config, local_root=None, *, collect=True, github=None, token=None, inbox_only=False):
    api = github or GitHub()
    importer = Importer(api, config, inbox_only=inbox_only)
    try:
        importer.acquire()
        if collect:
            results = {'etl': collect_etl(config, token=token)}
            if local_root:
                results['local'] = collect_local(local_root, config)
            importer.consume(results)
        importer.ready()
        importer.decisions()
        return {'status': 'ok', 'collectors': {key: value['state'] for key, value in importer.index['collectors'].items()},
                'items': len(importer.index['items'])}
    finally:
        if importer.lease_sha is not None:
            importer.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--local-root', type=Path)
    parser.add_argument('--decisions-only', action='store_true')
    parser.add_argument('--inbox-only', action='store_true', help='Private cloud inbox only; PC publishes verified DB changes')
    args = parser.parse_args()
    try:
        config = json.loads(args.sources.read_text(encoding='utf-8-sig'))
        result = run_once(config, args.local_root, collect=not args.decisions_only,
                          token=os.environ.get('ETL_API_TOKEN'), inbox_only=args.inbox_only)
        print(json.dumps(result))
        return 0
    except CollectorBusy:
        print(json.dumps({'status': 'busy', 'message': 'Another collector owns this pass; retry later'}))
        return 75
    except Exception as error:
        print(json.dumps({'status': 'error', 'type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
