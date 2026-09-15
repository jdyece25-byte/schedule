const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {project} = require('../school-calendar.js');

const event = {id: 'existing-one', d: '2026-09-22', n: '테스트 수업 과제', t: 'deadline', s: 1380, loc: '온라인'};
const candidate = {id: 'proposal-one', kind: 'deadline', action: 'add', event: {id: 'proposed-one', d: '2026-09-24', t: 'deadline', s: 1439}};
const notice = {id: 'source-one', title: '제출 안내', course: '테스트 수업', state: 'needs_review', updated_at: '2026-09-15T12:00:00Z', candidates: [candidate]};
const run = (items, events = []) => project({version: 1, items}, events);
const only = result => Object.values(result.byDate).flat()[0];
const freeze = value => {if (value && typeof value === 'object') {Object.values(value).forEach(freeze); Object.freeze(value);} return value;};

test('actual candidate deadline wins over posting date and stays a review overlay', () => {
  const result = run([notice]);
  assert.deepEqual(Object.keys(result.byDate), ['2026-09-24']);
  assert.deepEqual(only(result), {sourceId: notice.id, title: notice.title, course: notice.course, date: '2026-09-24', dateLabel: '마감', kind: 'deadline', state: 'needs_review', review: true, eventId: null, time: '23:59', location: ''});
  assert.equal(result.undatedCount, 0);
});

test('undated notices use strict KST posting dates and never label them as deadlines', () => {
  for (const timestamp of ['2026-09-15T15:00:00Z', '2026-09-16T00:00:00+09:00', '2026-09-15T11:00:00-04:00']) {
    const row = only(run([{...notice, state: 'info', candidates: [], updated_at: timestamp}]));
    assert.equal(row.date, '2026-09-16'); assert.equal(row.dateLabel, '공지 게시·수정일');
    assert.equal(row.kind, 'notice'); assert.equal(row.time, ''); assert.equal(row.review, false);
  }
});

test('applied exact links follow current DB dates despite stale proposed dates', () => {
  const item = {...notice, state: 'applied', candidates: [{...candidate, action: 'link', target_id: event.id}], event_ids: [event.id, event.id]};
  const result = run([item], [event]);
  assert.deepEqual(Object.keys(result.byDate), [event.d]);
  assert.equal(result.byDate[event.d].length, 1);
  assert.equal(only(result).eventId, event.id); assert.equal(only(result).time, '23:00');
  assert.equal(only(result).location, event.loc); assert.equal(only(result).review, false);
});

test('candidate event ID and receipt event IDs link without relying on names or array positions', () => {
  for (const fields of [{candidates: [{...candidate, event: {...candidate.event, id: event.id}}]}, {candidates: [], event_ids: [event.id]}]) {
    const row = only(run([{...notice, state: 'applied', ...fields}], [event]));
    assert.equal(row.eventId, event.id); assert.equal(row.date, event.d);
  }
  const row = only(run([notice], [{...event, n: candidate.event.n, d: candidate.event.d}]));
  assert.equal(row.eventId, null);
});

test('one-to-one applied receipt follows a user-edited date even when candidate ID is provisional', () => {
  const result = run([{...notice, state: 'applied', event_ids: [event.id]}], [event]);
  assert.deepEqual(Object.keys(result.byDate), [event.d]);
  assert.equal(only(result).eventId, event.id);
  assert.equal(only(result).review, false);
});

test('multiple applied receipts use exact source/candidate provenance instead of receipt order', () => {
  const secondCandidate = {...candidate, id: 'proposal-two', event: {...candidate.event, d: '2026-09-25'}};
  const first = {...event, school: {source_id: notice.id, candidate_id: candidate.id}};
  const second = {...event, id: 'existing-two', d: '2026-09-23', school: {source_id: notice.id, candidate_id: secondCandidate.id}};
  const result = run([{...notice, state: 'applied', candidates: [candidate, secondCandidate], event_ids: [second.id, first.id]}], [first, second]);
  assert.deepEqual(Object.keys(result.byDate), [first.d, second.d]);
  assert.equal(result.byDate[first.d][0].eventId, first.id);
  assert.equal(result.byDate[second.d][0].eventId, second.id);
});

test('pending move appears on old and new dates while a deletion only uses the exact old occurrence', () => {
  const moved = {...candidate, action: 'update', target_id: event.id};
  const result = run([{...notice, candidates: [moved]}], [event]);
  assert.deepEqual(Object.keys(result.byDate), [event.d, candidate.event.d]);
  assert.equal(result.byDate[event.d][0].eventId, event.id);
  assert.equal(result.byDate[candidate.event.d][0].eventId, null);
  assert.ok(Object.values(result.byDate).flat().every(r => r.review && r.dateLabel === '변경 확인'));
  const deleted = run([{...notice, candidates: [{...moved, action: 'delete', kind: 'cancellation'}]}], [event]);
  assert.deepEqual(Object.keys(deleted.byDate), [event.d]);
});

test('missing deleted event retains its candidate date without claiming an existing DB link', () => {
  const row = only(run([{...notice, state: 'applied', candidates: [{...candidate, action: 'delete', kind: 'cancellation', target_id: 'missing'}]}]));
  assert.equal(row.date, candidate.event.d); assert.equal(row.eventId, null);
  assert.equal(row.kind, 'cancellation'); assert.equal(row.review, false);
});

test('one source per day merges multiple candidates without inventing a unique linked ID or time', () => {
  const other = {...event, id: 'existing-two', t: 'exam', s: 600};
  const result = run([{...notice, state: 'applied', candidates: [], event_ids: [event.id, other.id]}], [event, other]);
  const row = only(result);
  assert.equal(result.byDate[event.d].length, 1); assert.equal(row.eventId, null);
  assert.equal(row.time, ''); assert.equal(row.kind, 'notice'); assert.equal(row.dateLabel, '일정 관련 공지');
});

test('unapplied high confidence candidates and existing tentative events keep review markers', () => {
  const ready = only(run([{...notice, state: 'ready', candidates: [{...candidate, confidence: 'high', auto_eligible: true}]}]));
  assert.equal(ready.review, true);
  const applied = only(run([{...notice, state: 'applied', candidates: [], event_ids: [event.id]}], [{...event, status: 'tentative'}]));
  assert.equal(applied.review, true);
});

test('invalid dates and timestamps do not roll over or assume the browser timezone', () => {
  for (const value of ['2026-02-30T12:00:00Z', '2026-09-15T24:00:00Z', '2026-09-15T12:00:60Z', '2026-09-15T12:00:00+24:00', '2026-09-15T12:00:00', '2026-09-15', '', null, 42]) {
    const result = run([{...notice, candidates: [{...candidate, event: {...candidate.event, d: '2026-02-30'}}], updated_at: value}]);
    assert.deepEqual(result.byDate, {}); assert.equal(result.undatedCount, 1);
  }
  assert.equal(only(run([{...notice, candidates: [], updated_at: '2028-02-29T15:00:00Z'}])).date, '2028-03-01');
});

test('baseline and ignored sources stay out while active and applied sources remain eligible', () => {
  const items = ['baseline', 'ignored', 'unknown', 'needs_review', 'ready', 'conflict', 'info', 'applied'].map(state => ({...notice, id: state, state}));
  const rows = Object.values(run(items).byDate).flat();
  assert.equal(rows.length, 5); assert.ok(rows.every(row => !['baseline', 'ignored', 'unknown'].includes(row.state)));
});

test('malformed input, duplicate event IDs, and duplicate undated sources are handled defensively', () => {
  for (const index of [null, [], {}, {items: {}}, {items: [null, 3, [], {}, {...notice, id: null}]}]) assert.deepEqual(project(index, null), {byDate: {}, undatedCount: 0});
  const row = only(run([{...notice, candidates: [{...candidate, target_id: event.id}]}], [event, {...event, d: '2026-09-23'}]));
  assert.equal(row.eventId, null); assert.equal(row.date, candidate.event.d);
  const unknown = {...notice, candidates: [], updated_at: null};
  assert.equal(run([unknown, unknown]).undatedCount, 1);
});

test('projection copies only display fields, accepts frozen inputs, and never persists private payloads', () => {
  const secret = 'SYNTHETIC_PRIVATE_BODY';
  const index = freeze({items: [{...notice, source_url: 'https://private.invalid/', content: secret, token: secret, candidates: [{...candidate, evidence: secret}]}]});
  const db = freeze([event]); const before = JSON.stringify({index, db});
  const result = project(index, db); only(result).title = 'caller mutation';
  assert.equal(JSON.stringify({index, db}), before);
  assert.ok(!JSON.stringify(result).includes(secret)); assert.ok(!JSON.stringify(result).includes('private.invalid'));
  assert.deepEqual(Object.keys(only(result)).sort(), ['sourceId','title','course','date','dateLabel','kind','state','review','eventId','time','location'].sort());
});

test('UMD exports inert plain text: HTML escaping belongs to the rendering consumer', () => {
  const root = {};
  const context = {window: root, get localStorage() {throw new Error('must not touch storage');}, fetch() {throw new Error('must not fetch');}};
  vm.runInNewContext(fs.readFileSync(require.resolve('../school-calendar.js'), 'utf8'), context);
  assert.equal(typeof root.SchoolCalendar.project, 'function');
  const title = '<img src=x onerror=alert(1)>';
  assert.equal(only(root.SchoolCalendar.project({items: [{...notice, title}]}, [])).title, title);
});
