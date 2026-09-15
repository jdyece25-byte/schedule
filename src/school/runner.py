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
import time
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
TERMINAL = {'completed', 'conflict'}
REFERENCE_KINDS = {'etl_file', 'etl_external_file', 'etl_page', 'etl_module', 'etl_syllabus'}
STALE_SOURCE_WARNING = '[이전 원문 참고: 최신 자료를 읽지 못했습니다. 날짜·시각 변경 근거로 사용하지 마세요.]'


def source_metadata(value, *, indexed=False):
    return {'title': value.get('title'), 'kind': value.get('source_kind' if indexed else 'kind'),
            'updated_at': value.get('updated_at')}


def reference_history(item, source):
    review = item.get('review', {})
    if (item.get('state') == 'info' and item.get('initial_review') and item.get('notify') is False and source.get('historical_import') is True
            and source.get('kind') in REFERENCE_KINDS and not item.get('candidates')
            and review.get('state') not in ('queued', 'processing') and not item.get('knowledge_stale')):
        item.update(state='baseline', reason='기존 참고 자료로 보관했습니다. 원문 읽기 상태와 수집기의 미확인 항목을 확인해 주세요.')


def preserve_failed_read(existing, source):
    """A temporary empty read cannot replace verified private source evidence."""
    if not existing or source.get('extraction_status', 'parsed') in ('parsed', 'summary') or str(source.get('content') or '').strip():
        return False
    previous = existing.get('read_previous') if isinstance(existing.get('read_previous'), dict) else existing
    knowledge = previous.get('knowledge') if isinstance(previous.get('knowledge'), dict) else {}
    if not (previous.get('extraction_status') in ('parsed', 'summary', 'no_text', 'truncated', 'partial_document') or knowledge.get('status') in ('parsed', 'summary') or knowledge.get('excerpts') or previous.get('candidates')):
        return False
    stale = (bool(existing.get('knowledge_stale')) or source_metadata(previous, indexed=True) != source_metadata(source)
             or source.get('term_conflict') is True or source.get('extraction_status') == 'prior_term')
    status = {'state': source.get('extraction_status', 'unreadable'), 'checked_at': stamp(),
              'source_updated_at': source.get('updated_at'), 'stale': stale}
    if stale:
        failed_hash = digest({'unreadable_revision': source_metadata(source), 'previous_source_hash': previous['content_hash'],
                              'term_conflict': bool(source.get('term_conflict') or source.get('extraction_status') == 'prior_term')})
        if existing.get('content_hash') != failed_hash:
            prior = deepcopy(previous)
            for key in ('read_status', 'diagnostic', 'read_previous', 'knowledge_stale'):
                prior.pop(key, None)
            existing.update(read_previous=prior, content_hash=failed_hash, notice_hash=failed_hash,
                            title=source.get('title'), source_kind=source.get('kind'), updated_at=source.get('updated_at'),
                            state='needs_review', candidates=[], first_seen_at=stamp(), notify=True,
                            reason='원문 정보가 변경됐지만 최신 내용을 읽지 못했습니다. 학교 원문을 확인해 주세요.')
            existing.pop('review', None)
        old_knowledge = deepcopy(previous.get('knowledge', {'version': 2, 'excerpts': []}))
        old_knowledge.update(status=status['state'], incomplete=True,
                             excerpts=[STALE_SOURCE_WARNING, *old_knowledge.get('excerpts', [])])
        existing.update(knowledge=old_knowledge, knowledge_stale=True)
    existing['read_status'] = status
    existing['diagnostic'] = ('최신 원문 읽기에 실패하여 이전 자료를 참고용으로만 보존했습니다.' if stale
                              else '이번 원문 읽기를 완료하지 못해 마지막으로 확인한 자료와 처리 상태를 보존했습니다.')
    return True


def transient(error):
    return isinstance(error, GitHubError) and (error.status in (0, 409, 422, 429) or error.status >= 500)


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
        self.stage = 'acquire'
        self.retry_pending = False

    def acquire(self):
        self.stage = 'private_repository'
        if self.queue == self.target or self.api.api('repos/' + self.queue).get('private') is not True:
            raise ValueError('School notices require the private request repository')
        self.stage = 'read_lease'
        lease, sha = self.api.read_json(self.queue, LEASE)
        if lease and datetime.fromisoformat(lease['until'].replace('Z', '+00:00')) > datetime.now(timezone.utc):
            raise CollectorBusy('Another school collector is active')
        try:
            self.stage = 'reserve_lease'
            self.lease_sha = self.api.put_json(self.queue, LEASE, {
                'owner': self.owner, 'until': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}, sha,
                message='Reserve school notice synchronization')
        except GitHubError as error:
            if error.status in (0, 409, 422):
                # A concurrent reservation is ordinary contention. A lost PUT
                # response can also mean this owner successfully reserved it.
                current, current_sha = self.api.read_json(self.queue, LEASE)
                if current and current.get('owner') == self.owner:
                    self.lease_sha = current_sha
                elif error.status in (409, 422) or current and current.get('owner'):
                    raise CollectorBusy('Another school collector reserved this pass') from None
                else:
                    raise
            else:
                raise
        self.stage = 'read_index'
        self.index, self.index_sha = self.api.read_json(self.queue, INDEX)
        if self.index is None:
            self.index = {'version': 1, 'updated_at': stamp(), 'collectors': {}, 'items': []}
        if self.index.get('version') != 1 or not isinstance(self.index.get('items'), list):
            raise ValueError('Unsupported school index')

    def renew(self):
        lease, sha = self.api.read_json(self.queue, LEASE)
        if not lease or lease.get('owner') != self.owner:
            raise CollectorBusy('School collector ownership changed')
        # Verify ownership each time, but avoid a commit for every small write.
        if datetime.fromisoformat(lease['until'].replace('Z', '+00:00')) > datetime.now(timezone.utc) + timedelta(minutes=2):
            self.lease_sha = sha
            return
        self.lease_sha = self.api.put_json(self.queue, LEASE, {
            'owner': self.owner, 'until': (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}, sha,
            message='Renew school notice synchronization')

    def release(self):
        last_error = None
        for attempt in range(5):
            try:
                # Contents writes can conflict with unrelated branch commits.
                # Always reread ownership/SHA; never clear a successor's lease.
                lease, sha = self.api.read_json(self.queue, LEASE)
                if not lease or lease.get('owner') != self.owner:
                    self.lease_sha = None
                    return
                self.api.put_json(self.queue, LEASE, {'owner': None, 'until': stamp()}, sha,
                                  message='Complete school notice synchronization')
                self.lease_sha = None
                return
            except GitHubError as error:
                if error.status not in (0, 409, 422):
                    raise
                last_error = error
                if attempt < 4:
                    time.sleep(.1 * 2 ** attempt)
        # The final PUT may have succeeded with only its response lost.
        lease, _ = self.api.read_json(self.queue, LEASE)
        if not lease or lease.get('owner') != self.owner:
            self.lease_sha = None
            return
        raise last_error

    def save_index(self, extra=None):
        self.renew()
        self.index['updated_at'] = stamp()
        files = dict(extra or {})
        files[INDEX] = json.dumps(self.index, ensure_ascii=False, indent=2) + '\n'
        for attempt in range(5):
            base = self.api.head(self.queue)
            _, current_sha = self.api.read_json(self.queue, INDEX, base)
            if current_sha != self.index_sha:
                raise CollectorBusy('School inbox was edited concurrently; changes were preserved')
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
        from src.school.knowledge import ANALYSIS_VERSION, source_knowledge
        items = {item['id']: item for item in self.index['items']}
        known = self.index.setdefault('known_sources', {})
        extra = {}
        events, _ = self.api.read_json(self.target, 'DB/events.json', self.api.head(self.target))
        for collector, result in results.items():
            first_local = collector == 'local' and not self.index['collectors'].get('local', {}).get('initialized')
            first_etl = collector == 'etl' and not self.index['collectors'].get('etl', {}).get('initialized')
            checkpoint = self.index['collectors'].get(collector, {}).get('last_checked')
            for source in result.get('sources', []):
                existing = items.get(source['id'])
                if preserve_failed_read(existing, source):
                    continue  # Never overwrite the last successful private raw source.
                if existing and source.get('extraction_status', 'parsed') in ('parsed', 'summary'):
                    previous = existing.get('read_previous')
                    if isinstance(previous, dict) and previous.get('content_hash') == source['content_hash']:
                        existing = deepcopy(previous)
                        existing['updated_at'] = source.get('updated_at')
                        items[source['id']] = existing
                    for flag in ('read_status', 'diagnostic', 'read_previous', 'knowledge_stale'):
                        existing.pop(flag, None)
                same_version = bool(existing and existing['content_hash'] == source['content_hash'])
                raw_hash = source.get('raw_content_hash') or source.get('file_hash')
                old_raw_hash = (existing or {}).get('raw_content_hash')
                parser_changed = (source.get('extraction_version') is not None and
                                  source.get('extraction_version') != (existing or {}).get('extraction_version'))
                legacy_parser = not old_raw_hash and (existing or {}).get('extraction_version') is None
                upgrading = bool(existing and (
                    same_version and existing.get('analysis_version') != ANALYSIS_VERSION or
                    parser_changed and (raw_hash and raw_hash == old_raw_hash or legacy_parser)))
                if same_version and not upgrading:
                    reference_history(existing, source)
                    continue
                queued = bool(existing and existing.get('review', {}).get('state') in ('queued', 'processing')
                              and existing['review'].get('source_hash') == existing['content_hash'])
                if upgrading and queued:
                    # An extraction migration must not invalidate a choice that
                    # was already accepted against the original source hash.
                    # Retry enrichment after its terminal decision is saved.
                    continue
                if not existing and known.get(source['id']) == source['content_hash']:
                    continue
                known[source['id']] = source['content_hash']
                candidates = [prepare(candidate, events, self.config) for candidate in extract_candidates(source, self.config)]
                historical = source.get('historical_import') is True
                # External PDFs often have no updated timestamp. A changed
                # verified file body is still a new notice, even when its
                # metadata continues to look like an initial historical import.
                verified_body = bool(source.get('file_hash') or str(source.get('content') or '').strip())
                previously_read = (existing or {}).get('extraction_status') in ('parsed', 'summary', 'truncated', 'partial_document', 'no_text')
                changed_body = bool(existing and previously_read and verified_body and raw_hash and old_raw_hash and raw_hash != old_raw_hash and not upgrading)
                if not existing and collector == 'etl' and source.get('kind') in ('etl_file', 'etl_external_file', 'etl_page', 'etl_module', 'etl_syllabus') and checkpoint:
                    try:
                        historical |= (datetime.fromisoformat(source['updated_at'].replace('Z', '+00:00'))
                                       <= datetime.fromisoformat(checkpoint.replace('Z', '+00:00')))
                    except (ValueError, TypeError, KeyError):
                        historical = True  # Unknown old-file timestamps are never automatic changes.
                if changed_body:
                    historical = False
                initial_review = bool(first_etl or historical or upgrading or (existing and existing.get('initial_review')
                                                   and existing.get('state') != 'applied'))
                if initial_review:
                    for candidate in candidates:
                        if candidate.get('auto_eligible'):
                            candidate.update(auto_eligible=False, confidence='review',
                                             reason='최초 연결 자료입니다. 기존 일정에 반영되었거나 이미 지난 과제인지 확인해 주세요.')
                definition = next((c for c in self.config['courses'] if c['key'] == source['course']), {})
                item = {'id': source['id'], 'content_hash': source['content_hash'],
                        'course': definition.get('name', source['course']), 'course_key': source['course'],
                        'title': source['title'], 'source_url': source.get('source_url', ''),
                        'source_kind': source['kind'], 'updated_at': source['updated_at'],
                        'first_seen_at': existing.get('first_seen_at', stamp()) if upgrading else stamp(),
                        'candidates': candidates, 'event_ids': [],
                        'initial_review': initial_review,
                        'notify': existing.get('notify', True) if upgrading else not (first_etl or historical),
                        'notice_hash': (existing.get('notice_hash') or existing['content_hash']) if upgrading else source['content_hash'],
                        'analysis_version': ANALYSIS_VERSION,
                        'extraction_version': source.get('extraction_version'),
                        'raw_content_hash': raw_hash,
                        'knowledge': source_knowledge(source, self.config),
                        'state': 'needs_review' if candidates else 'info',
                        'reason': '원문과 날짜를 확인한 뒤 선택한 일정만 적용하세요.' if candidates else '새 자료·공지입니다. 내용을 확인해 주세요.',
                        'extraction_status': source.get('extraction_status', 'parsed')}
                if upgrading:
                    acknowledged = existing.get('state') in ('ignored', 'applied', 'baseline')
                    # Reading a document more thoroughly is not a new teacher
                    # announcement and must not undo an owner's earlier choice.
                    item['event_ids'] = deepcopy(existing.get('event_ids', []))
                    if 'review' in existing:
                        item['review'] = deepcopy(existing['review'])
                    if acknowledged:
                        for key in ('state', 'candidates', 'reason'):
                            if key in existing:
                                item[key] = deepcopy(existing[key])
                    elif existing.get('state') == 'conflict':
                        item.update(state='conflict', reason=existing.get('reason', item['reason']))
                elif first_local:
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
                reference_history(item, source)
                items[item['id']] = item
            previous = self.index['collectors'].get(collector, {})
            self.index['collectors'][collector] = {
                'state': result['status'], 'last_checked': stamp(),
                'initialized': bool(previous.get('initialized') or result['status'] in ('ok', 'partial')),
                'source_count': len(result.get('sources', [])), 'issue_count': len(result.get('issues', [])),
                'coverage': deepcopy(result.get('coverage', {})),
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
            review = item.get('review', {})
            if review.get('state') in ('queued', 'processing') and review.get('source_hash') == item.get('content_hash'):
                # A pending explicit edit takes priority over the unedited
                # automatic candidate, including after a transient publish error.
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
        self.stage = 'decisions'
        tree = self.api.tree(self.queue)
        paths = sorted(path for path in tree if re.fullmatch(r'school/decisions/[A-Za-z0-9_-]{1,100}\.json', path))
        changed = False
        items = {item['id']: item for item in self.index['items']}
        for path in paths:
            identifier = Path(path).stem
            outcome_path = 'school/decision-results/' + identifier + '.json'
            if outcome_path in tree:
                continue
            decision = None
            item = None
            try:
                decision, _ = self.api.read_json(self.queue, path)
                if not isinstance(decision, dict) or decision.get('version') != 1 or decision.get('id') != identifier:
                    raise ValueError('잘못된 확인 요청입니다.')
                if not isinstance(decision.get('source_id'), str) or not isinstance(decision.get('source_hash'), str):
                    raise ValueError('확인 요청의 원문 식별 정보를 확인해 주세요.')
                item = items.get(decision.get('source_id'))
                if not item or decision.get('source_hash') != item['content_hash']:
                    raise ValueError('원문이 변경되었습니다. 최신 공지를 다시 확인해 주세요.')
                review = {'state': 'queued', 'decision_id': identifier, 'source_id': item['id'],
                          'source_hash': item['content_hash'], 'action': decision.get('action'), 'updated_at': stamp()}
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
                    old = item.get('review', {})
                    if any(old.get(key) != review[key] for key in ('state', 'decision_id', 'source_hash', 'action')):
                        item['review'] = review
                        self.save_index()
                        changed = True
                    if self.inbox_only:
                        continue
                    event_ids = self.publish(item, prepared, 'decision:' + identifier, approved=True)
                    remaining = [c for c in item['candidates'] if c['id'] not in seen and c.get('action') != 'link']
                    item.update(state='needs_review' if remaining else 'applied', candidates=remaining,
                                event_ids=list(dict.fromkeys(item.get('event_ids', []) + event_ids)),
                                reason='선택한 일정을 반영했습니다. 남은 후보를 확인해 주세요.' if remaining else '사용자가 확인한 일정을 반영했습니다.')
                else:
                    raise ValueError('지원하지 않는 확인 요청입니다.')
                review.update(state='completed', remaining_count=len(item.get('candidates', [])) if item['state'] == 'needs_review' else 0)
                item['review'] = review
                outcome = {'version': 1, **review}
            except GitHubError as error:
                if not transient(error):
                    raise
                # A retryable provider failure must not turn an accepted
                # decision into a terminal conflict or block later decisions.
                self.retry_pending = True
                continue
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                outcome = {'version': 1, 'state': 'conflict', 'updated_at': stamp(),
                           'message': str(error) if isinstance(error, ValueError) else '확인 요청 형식을 다시 확인해 주세요.'}
                item = items.get(decision.get('source_id')) if isinstance(decision, dict) and isinstance(decision.get('source_id'), str) else None
                if isinstance(decision, dict):
                    outcome.update({key: decision.get(key) for key in ('source_id', 'source_hash', 'action')})
                outcome['decision_id'] = identifier
                if item and decision.get('source_hash') == item['content_hash']:
                    item.update(state='conflict', reason=outcome['message'], review={key: value for key, value in outcome.items() if key != 'version'})
            # Each terminal decision and its corresponding index change are
            # one durable commit; later retries cannot erase earlier progress.
            self.save_index({outcome_path: json.dumps(outcome, ensure_ascii=False) + '\n'})
            changed = True
        return changed


def run_once(config, local_root=None, *, collect=True, github=None, token=None, inbox_only=False):
    api = github or GitHub()
    importer = Importer(api, config, inbox_only=inbox_only)
    failure = None
    try:
        # Slow eTL/PDF reads do not reserve the private queue. Independent
        # decision-only passes remain available while collection runs.
        if collect:
            importer.stage = 'collect_sources'
            results = {'etl': collect_etl(config, token=token)}
            if local_root:
                results['local'] = collect_local(local_root, config)
        importer.stage = 'acquire'
        importer.acquire()
        if collect:
            # A known changed-but-unreadable revision invalidates an older
            # queued approval before it can publish stale candidates.
            items = {item['id']: item for item in importer.index['items']}
            changed = False
            for result in results.values():
                for source in result.get('sources', []):
                    changed = preserve_failed_read(items.get(source['id']), source) or changed
            if changed:
                importer.save_index()
        importer.decisions()
        if collect:
            importer.stage = 'consume'
            importer.consume(results)
        importer.stage = 'ready'
        importer.ready()
        importer.decisions()
        return {'status': 'retry_pending' if importer.retry_pending else 'ok',
                'collectors': {key: value['state'] for key, value in importer.index['collectors'].items()},
                'items': len(importer.index['items'])}
    except Exception as error:
        failure = error
        error.stage = importer.stage
        raise
    finally:
        if importer.lease_sha is not None:
            try:
                importer.release()
            except Exception as error:
                # Preserve the original error; otherwise report release failure
                # safely and retry after the bounded lease expires.
                if failure is None:
                    error.stage = 'release'
                    raise


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
        return 75 if result['status'] == 'retry_pending' else 0
    except CollectorBusy:
        print(json.dumps({'status': 'busy', 'message': 'Another collector owns this pass; retry later'}))
        return 75
    except Exception as error:
        detail = {'status': 'retry_pending' if transient(error) else 'error',
                  'type': type(error).__name__, 'stage': getattr(error, 'stage', 'configuration')}
        if isinstance(error, GitHubError):
            detail['http_status'] = error.status
            # Closed reason codes only; never dump GitHub's raw error text.
            message = str(error).lower()
            detail['reason'] = next((code for phrase, code in (
                ('resource not accessible by integration', 'integration_scope'),
                ('resource not accessible by personal access token', 'token_scope'),
                ('rate limit', 'rate_limited'), ('bad credentials', 'invalid_credentials'),
                ('sha', 'file_version_conflict')) if phrase in message), 'github_request_failed')
        print(json.dumps(detail))
        return 75 if transient(error) else 1


if __name__ == '__main__':
    raise SystemExit(main())
