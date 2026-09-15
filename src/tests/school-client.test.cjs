const {test} = require('node:test');
const assert = require('node:assert/strict');
const {SchoolClient, cleanEvent, safeSourceURL, collectorText, render, noticeHTML, mount} = require('../school-client.js');
const REPO = 'jdyece25-byte/schedule-requests';
const ID = '12345678-1234-4123-8123-123456789abc';
const NOW = new Date('2026-09-14T04:00:00Z');
const candidate = {id: 'candidate-one', kind: 'deadline', event: {id: 'event-one', d: '2026-09-20', n: '수업 보고서', t: 'deadline', s: 1440, loc: 'eTL'}, confidence: 'review', auto_eligible: false, evidence: '9월 20일까지 보고서를 제출하세요.', action: 'add'};
const item = {id: 'source-one', content_hash: 'a'.repeat(64), course: '테스트 수업', title: '보고서 제출 안내', source_url: 'https://etl.snu.ac.kr/mod/forum/discuss.php?d=42', updated_at: '2026-09-14T03:00:00Z', state: 'needs_review', candidates: [candidate], event_ids: []};
const choice = {id: candidate.id, input: {d: '2026-09-21', n: '수정한 보고서 이름', s: '24:00', e: '', loc: '온라인', tentative: true}};
const index = {version: 1, updated_at: NOW.toISOString(), collectors: {local: {state: 'ok', last_checked: NOW.toISOString()}, etl: {state: 'auth_required', last_checked: '2026-09-12T04:00:00Z', message: 'do not show credential detail'}}, items: [item]};
const response = (status, value = {}) => ({ok: status >= 200 && status < 300, status, json: async () => value});
const file = value => response(200, {encoding: 'base64', content: Buffer.from(JSON.stringify(value)).toString('base64')});
function fixture(handler = () => response(404), options = {}) {
  const store = new Map(Object.entries({cfg_bridge_pat: 'github_pat_fake_school', cfg_bridge_repo: REPO, ...options.storage}));
  const storage = {getItem: key => store.get(key) || null, setItem: (key, val) => store.set(key, val)};
  const calls = []; let uuids = 0;
  const root = {localStorage: storage, crypto: {randomUUID: () => {uuids++; return ID;}}, fetch: async (url, init) => {
    calls.push({url, init});
    if (url.endsWith('/user')) return response(200, {login: options.login || 'jdyece25-byte'});
    if (url === 'https://api.github.com/repos/' + REPO) return response(200, {private: options.private !== false, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
    return handler(url, init, calls);
  }};
  const client = new SchoolClient(root, {now: () => NOW});
  return {client, root, calls, store, uuids: () => uuids};
}
const puts = f => f.calls.filter(call => call.init.method === 'PUT');
const decisionBody = call => JSON.parse(Buffer.from(JSON.parse(call.init.body).content, 'base64').toString('utf8'));
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise(done => {resolve = done;}); return {promise, resolve}; }
function mountedFixture(handler = url => url.endsWith('/school/index.json') ? file(index) : response(404), options = {}) {
  const f = fixture(handler, options);
  const elements = new Map(), windowListeners = new Map(), documentListeners = new Map(), intervals = new Map(), timers = [];
  let now = NOW.getTime(), currentView = options.view || 'home', redraws = 0, closed = 0;
  const add = (map, name, fn) => map.set(name, [...(map.get(name) || []), fn]);
  const element = id => {
    const classes = new Set(); const listeners = new Map();
    return {id, textContent: '', value: id === 'school-filter' ? 'review' : '', dataset: {}, disabled: false, focused: false, scrolled: false,
      classList: {toggle: (key, value) => value ? classes.add(key) : classes.delete(key), add: key => classes.add(key), contains: key => classes.has(key)},
      addEventListener: (name, fn) => add(listeners, name, fn),
      querySelectorAll: () => [], querySelector: () => null,
      focus() {this.focused = true; document.activeElement = this;}, scrollIntoView() {this.scrolled = true;}};
  };
  const document = {hidden: options.hidden || false, activeElement: null,
    getElementById(id) {if (!elements.has(id)) elements.set(id, element(id)); return elements.get(id);},
    querySelector: selector => selector === '.view.on' ? {id: 'v-' + currentView} : null,
    addEventListener: (name, fn) => add(documentListeners, name, fn)};
  const list = document.getElementById('school-list'); let html = '', articles = [];
  Object.defineProperty(list, 'innerHTML', {get: () => html, set: value => {
    html = value; articles = [...value.matchAll(/<article[^>]+data-source-index="(\d+)"/g)].map(match => {
      const node = element('article-' + match[1]); node.dataset.sourceIndex = match[1]; return node;
    });
  }});
  list.querySelectorAll = selector => selector === '[data-source-index]' ? articles : [];
  const root = {...f.root, document, location: {hash: options.hash || ''}, scheduleDataReady: options.scheduleDataReady || Promise.resolve(),
    history: {replaceState: (a, b, hash) => {root.location.hash = hash;}},
    addEventListener: (name, fn) => add(windowListeners, name, fn),
    setTimeout: fn => {timers.push(fn); return timers.length;},
    setInterval: (fn, ms) => {intervals.set(1, {fn, ms}); return 1;}, clearInterval: id => intervals.delete(id),
    closePanel: () => {closed++;},
    sw: view => {currentView = view; return root.ScheduleSchool?.onView(view);},
    refreshSchoolCalendar: () => {redraws++; root.ScheduleSchool?.getCalendar([]);},
    SchoolCalendar: {project: (data, events) => ({byDate: {'2026-09-20': [{source_id: data.items[0]?.id, eventCount: events.length}]}, undatedCount: 0})}
  };
  root.ScheduleSchool = mount(root, {now: () => new Date(now)});
  const fire = async (map, name, event = {}) => {for (const fn of map.get(name) || []) await fn(event); await tick();};
  return {...f, root, document, elements, api: root.ScheduleSchool, client: root.ScheduleSchool.client,
    advance: ms => {now += ms;}, redraws: () => redraws, closed: () => closed, articles: () => articles,
    fireWindow: (name, event) => fire(windowListeners, name, event), fireDocument: (name, event) => fire(documentListeners, name, event),
    poll: () => intervals.get(1)?.fn(), intervals,
    runTimers: async () => {while (timers.length) {await timers.shift()(); await tick();}},
  };
}

test('private school index refresh uses current browser settings and never caches notice bodies or tokens', async () => {
  const f = fixture(url => url.endsWith('/school/index.json') ? file(index) : response(404));
  await f.client.refresh();
  assert.equal(f.client.index.items[0].title, item.title);
  f.store.set('cfg_bridge_pat', 'github_pat_replacement');
  await f.client.refresh();
  assert.equal(f.calls.at(-1).init.headers.Authorization, 'Bearer github_pat_replacement');
  assert.ok(f.calls.every(c => c.init.cache === 'no-store'));
  assert.equal(f.store.size, 2);
});

test('missing index is collection pending; authentication-required and stale collectors are honest and omit raw error details', async () => {
  const f = fixture(); await f.client.refresh();
  assert.equal(f.client.index, null);
  assert.match(collectorText(null), /수집 대기/);
  const text = collectorText(index, NOW);
  assert.match(text, /eTL: 다시 로그인 필요/);
  assert.match(text, /24시간 이상 갱신 없음/);
  assert.ok(!text.includes('credential detail'));
});

test('public repo or another account is rejected before private index reads or decisions', async () => {
  for (const options of [{private: false}, {login: 'someone-else'}]) {
    const f = fixture(() => {throw new Error('private path must not be called');}, options);
    await assert.rejects(f.client.refresh());
    await assert.rejects(f.client.decide(item, 'approve', [choice]));
    assert.equal(puts(f).length, 0);
    assert.equal(f.client.index, null);
  }
});

test('cleared tokens, classic tokens and changed destination block reads and writes before network', async () => {
  for (const storage of [{cfg_bridge_pat: ''}, {cfg_bridge_pat: 'ghp_fake'}, {cfg_bridge_repo: 'jdyece25-byte/schedule'}]) {
    const f = fixture(() => response(404), {storage});
    await assert.rejects(f.client.refresh());
    await assert.rejects(f.client.decide(item, 'approve', [choice]));
    assert.equal(f.calls.length, 0);
  }
});

test('explicit chosen edits create an immutable source-bound private decision without claiming schedule applied', async () => {
  const f = fixture((url, init) => init.method === 'PUT' ? response(201) : response(404));
  const row = await f.client.decide(item, 'approve', [choice]);
  assert.equal(row.state, 'accepted');
  assert.equal(puts(f).length, 1);
  const call = puts(f)[0]; const body = decisionBody(call);
  assert.equal(call.url, 'https://api.github.com/repos/' + REPO + '/contents/school/decisions/' + ID + '.json');
  assert.equal(JSON.parse(call.init.body).sha, undefined);
  assert.equal(body.source_id, item.id); assert.equal(body.source_hash, item.content_hash);
  assert.equal(body.created_at, NOW.toISOString());
  assert.deepEqual(body.candidates[0], {id: candidate.id, action: 'add', event: {id: 'event-one', d: '2026-09-21', n: '수정한 보고서 이름', t: 'deadline', s: 1440, loc: '온라인', status: 'tentative'}});
  assert.ok(!JSON.stringify(body).includes('github_pat'));
  assert.match(noticeHTML(item, 0, row), /처리 요청 접수/);
  assert.match(noticeHTML(item, 0, row), /아직 일정 반영 완료가 아닙니다/);
  assert.equal(f.store.size, 2);
});

test('lost successful PUT response verifies exactly the same immutable ID without resending', async () => {
  let received;
  const f = fixture((url, init) => {
    if (init.method === 'PUT') { received = decisionBody({init}); throw new Error('lost response'); }
    return file(received);
  });
  const row = await f.client.decide(item, 'approve', [choice]);
  assert.equal(row.state, 'accepted'); assert.equal(puts(f).length, 1); assert.equal(f.uuids(), 1);
});

test('unreceived PUT retries with an identical request body after read confirms absent', async () => {
  let writes = 0;
  const f = fixture((url, init) => {
    if (init.method === 'PUT') { if (!writes++) throw new Error('not received'); return response(201); }
    return response(404);
  });
  await f.client.decide(item, 'approve', [choice]);
  assert.equal(puts(f).length, 2);
  assert.equal(puts(f)[0].init.body, puts(f)[1].init.body);
  assert.equal(puts(f)[0].url, puts(f)[1].url);
});

test('ambiguous request keeps its immutable choice and retry uses replacement credentials', async () => {
  let failing = true; let received;
  const f = fixture((url, init) => {
    if (init.method === 'PUT') {received = decisionBody({init}); throw new Error('unavailable');}
    if (failing) throw new Error('unavailable');
    return file(received);
  });
  await assert.rejects(f.client.decide(item, 'approve', [choice]));
  assert.equal(f.client.pending.get(item.id).state, 'uncertain');
  failing = false; f.store.set('cfg_bridge_pat', 'github_pat_new');
  const row = await f.client.retry(item.id);
  assert.equal(row.state, 'accepted'); assert.equal(f.uuids(), 1);
  assert.equal(f.calls.at(-1).init.headers.Authorization, 'Bearer github_pat_new');
  assert.equal(puts(f).length, 1);
});

test('a different file occupying the decision ID is never overwritten or called accepted', async () => {
  const f = fixture((url, init) => init.method === 'PUT' ? response(409) : file({version: 1, id: ID, action: 'ignore'}));
  await assert.rejects(f.client.decide(item, 'approve', [choice]), /내용이 달라/);
  assert.equal(puts(f).length, 1);
  assert.equal(f.client.pending.get(item.id).state, 'uncertain');
});

test('a rejected write leaves an explicit same-request retry and never reports receipt', async () => {
  const f = fixture((url, init) => init.method === 'PUT' ? response(403) : response(404));
  await assert.rejects(f.client.decide(item, 'approve', [choice]), /전송 실패/);
  const row = f.client.pending.get(item.id);
  assert.equal(row.state, 'rejected'); assert.match(noticeHTML(item, 0, row), /같은 요청 상태 확인/);
  await assert.rejects(f.client.decide(item, 'approve', [choice]), /앞선 요청/);
});

test('missing selection, invalid date, blank name, invalid time and unknown candidate block approval', async () => {
  const f = fixture();
  for (const choices of [[], [{...choice, input: {...choice.input, d: '2026-02-30'}}], [{...choice, input: {...choice.input, n: '  '}}], [{...choice, input: {...choice.input, s: '25:00'}}], [{...choice, id: 'not-from-source'}]]) {
    await assert.rejects(f.client.decide(item, 'approve', choices));
  }
  assert.equal(f.calls.length, 0);
});

test('cancellation and update require known target; deletion is explicit and preserves target association', () => {
  const cancelled = {...candidate, kind: 'cancellation', action: 'delete', event: {...candidate.event, t: 'class'}};
  assert.throws(() => cleanEvent(cancelled, choice.input), /기존 일정/);
  assert.throws(() => cleanEvent({...candidate, action: 'update'}, choice.input), /기존 일정/);
  const event = cleanEvent({...cancelled, target_id: 'specific-existing-event'}, choice.input);
  assert.equal(event.action, 'delete'); assert.equal(event.target_id, 'specific-existing-event');
  assert.deepEqual(event.event, cancelled.event);
  const html = noticeHTML({...item, candidates: [{...cancelled, target_id: 'specific-existing-event'}]}, 0);
  assert.match(html, /기존 일정 삭제/);
  assert.match(html, /data-school-field="d"[^>]*readonly/);
  assert.doesNotMatch(html, /data-school-field="selected" checked/);
});

test('notice with no candidates can be acknowledged with an immutable ignore decision', async () => {
  const empty = {...item, state: 'info', candidates: []};
  const f = fixture((url, init) => init.method === 'PUT' ? response(201) : response(404));
  await f.client.decide(empty, 'ignore');
  assert.deepEqual(decisionBody(puts(f)[0]).candidates, []);
  assert.match(noticeHTML(empty, 0), /확인했어요/);
  assert.doesNotMatch(noticeHTML(empty, 0), /data-school-action="approve"/);
});

test('refresh recognizes completed backend state and releases old pending status for new source content', async () => {
  let data = index;
  const f = fixture((url, init) => init.method === 'PUT' ? response(201) : file(data));
  await f.client.decide(item, 'approve', [choice]);
  data = {...index, items: [{...item, state: 'applied'}]}; await f.client.refresh();
  assert.equal(f.client.pending.size, 0);
  assert.match(render(data, f.client.pending, 'latest'), /반영 완료/);
  assert.doesNotMatch(render(data, f.client.pending, 'latest'), /처리 요청 접수/);
});

test('backend conflict result releases receipt so revised choices can be submitted', async () => {
  const conflict = {...index, items: [{...item, state: 'conflict', reason: '기존 일정이 변경되었습니다.'}]};
  const f = fixture((url, init) => init.method === 'PUT' ? response(201) : url.endsWith('/school/index.json') ? file(conflict) : file({version: 1, state: 'conflict'}));
  await f.client.decide(item, 'approve', [choice]); await f.client.refresh();
  assert.equal(f.client.pending.size, 0);
  assert.match(render(f.client.index, f.client.pending), /기존 일정이 변경되었습니다/);
  assert.doesNotMatch(render(f.client.index, f.client.pending), /아직 일정 반영 완료가 아닙니다/);
});

test('partial approval keeps unselected candidates available after completed decision acknowledgement', async () => {
  const remaining = {...candidate, id: 'second-candidate', event: {...candidate.event, n: '남은 보고서'}};
  const two = {...item, candidates: [candidate, remaining]};
  let current = {...index, items: [two]};
  const f = fixture((url, init) => init.method === 'PUT' ? response(201) : url.endsWith('/school/index.json') ? file(current) : file({version: 1, state: 'completed'}));
  await f.client.decide(two, 'approve', [choice]);
  current = {...index, items: [{...item, candidates: [remaining], event_ids: ['created-event'], state: 'needs_review'}]};
  await f.client.refresh();
  assert.equal(f.client.pending.size, 0);
  assert.match(render(f.client.index, f.client.pending), /남은 보고서/);
  assert.match(render(f.client.index, f.client.pending), /data-school-action="approve"/);
});

test('update clears blank times and old display/location fields and can confirm a tentative event', () => {
  const original = {...candidate, action: 'update', target_id: 'existing', event: {...candidate.event, lid: 'old-location', status: 'tentative'}};
  const result = cleanEvent(original, {...choice.input, s: '', e: '', loc: 'new location', tentative: false});
  assert.equal(result.event.s, null); assert.equal(result.event.e, null);
  assert.equal(result.event.lid, ''); assert.equal(result.event.ti, ''); assert.equal(result.event.status, 'confirmed');
});

test('already linked candidates are visible without an approval selector', () => {
  const html = noticeHTML({...item, candidates: [{...candidate, action: 'link', target_id: 'existing'}]}, 0);
  assert.match(html, /기존 일정과 연결됨/);
  assert.doesNotMatch(html, /data-school-field="selected"/);
});

test('untrusted notice fields stay text, prior history is collapsed, and links remove credential query parameters', () => {
  const evil = '<img src=x onerror=alert(1)>';
  const unsafe = {...item, course: evil, title: evil, reason: evil, source_url: 'javascript:alert(1)', candidates: [{...candidate, reason: evil, evidence: evil, event: {...candidate.event, n: evil, loc: evil}}]};
  const html = render({...index, items: [unsafe, {...item, id: 'older', state: 'baseline'}]}, new Map());
  assert.ok(!html.includes('<img') && !html.includes('href="javascript:'));
  assert.match(html, /&lt;img/);
  assert.match(html, /<details class="school-history"><summary>/);
  const selectedInputs = [...html.matchAll(/<input[^>]+data-school-field="selected"[^>]*>/g)].map(x => x[0]);
  assert.ok(selectedInputs.every(input => !input.includes('checked')));
  const link = safeSourceURL('https://etl.snu.ac.kr/mod/forum/discuss.php?d=42&token=secret&sesskey=private#auth');
  assert.equal(link, 'https://etl.snu.ac.kr/mod/forum/discuss.php?d=42');
  for (const url of ['https://snu.ac.kr.evil.test/', 'https://evilsnu.ac.kr/', 'http://etl.snu.ac.kr/', 'https://u:p@etl.snu.ac.kr/', 'https://etl.snu.ac.kr:8443/']) assert.equal(safeSourceURL(url), '');
});

test('failed private refresh and cleared credentials immediately invalidate previously loaded data', async () => {
  let fail = false;
  const f = fixture(url => fail ? response(503) : file(index));
  await f.client.refresh(); assert.ok(f.client.index);
  fail = true; await assert.rejects(f.client.refresh(), /읽기 실패/);
  assert.equal(f.client.index, null); assert.equal(f.client.indexCredentials, null);
  fail = false; await f.client.refresh(); f.store.delete('cfg_bridge_pat');
  await assert.rejects(f.client.refresh(), /토큰/);
  assert.equal(f.client.index, null);
});

test('an in-flight private response cannot restore data after the token changed', async () => {
  const started = deferred(), finish = deferred();
  const f = fixture(async url => {started.resolve(); await finish.promise; return file(index);});
  const loading = f.client.refresh(); await started.promise;
  f.store.set('cfg_bridge_pat', 'github_pat_replacement'); finish.resolve();
  await assert.rejects(loading, /연결 설정이 변경/);
  assert.equal(f.client.index, null); assert.equal(f.client.indexCredentials, null);
});

test('calendar autoload waits for scheduleDataReady and exposes private projection plus collector warnings', async () => {
  const ready = deferred(); const f = mountedFixture(undefined, {scheduleDataReady: ready.promise});
  await tick(); await f.poll(); await f.api.onView('cal'); assert.equal(f.calls.length, 0);
  ready.resolve(); await f.api.initialization;
  assert.equal(f.api.getCalendarStatus().state, 'ready');
  assert.match(f.api.getCalendarStatus().text, /eTL 다시 로그인 필요/);
  assert.deepEqual(f.api.getCalendar([{}]).byDate['2026-09-20'][0], {source_id: item.id, eventCount: 1});
  assert.ok(f.redraws() >= 2);
  assert.equal(f.store.size, 2);
  f.api.dispose(); assert.equal(f.intervals.size, 0);
});

test('missing configuration never makes automatic auth requests and returns an empty projection', async () => {
  const f = mountedFixture(undefined, {storage: {cfg_bridge_pat: ''}});
  await f.api.initialization; await f.poll(); await f.fireWindow('focus'); await f.api.onView('school');
  assert.equal(f.calls.length, 0);
  assert.deepEqual(f.api.getCalendar([]), {byDate: {}, undatedCount: 0});
  assert.equal(f.api.getCalendarStatus().state, 'unconfigured');
  assert.match(f.elements.get('school-list').innerHTML, /연결 설정/);
});

test('polling runs only in visible calendar/home/school and respects the sixty-second refresh interval', async () => {
  const f = mountedFixture(); await f.api.initialization;
  const reads = () => f.calls.filter(c => c.url.endsWith('/school/index.json')).length;
  assert.equal(reads(), 1); await f.api.onView('cal'); await f.poll(); assert.equal(reads(), 1);
  f.advance(61000); f.document.hidden = true; await f.poll(); assert.equal(reads(), 1);
  f.document.hidden = false; await f.fireDocument('visibilitychange'); assert.equal(reads(), 2);
  await f.api.onView('edit'); f.advance(61000); await f.poll(); assert.equal(reads(), 2);
  await f.api.onView('home'); assert.equal(reads(), 3);
  await f.api.onView('school'); assert.equal(reads(), 4);
});

test('read failure clears calendar rows and school markup, reports error and throttles automatic retries', async () => {
  let fail = false;
  const f = mountedFixture(url => fail ? response(503) : file(index)); await f.api.initialization;
  fail = true; await f.api.refresh();
  assert.equal(f.api.getCalendarStatus().state, 'error');
  assert.deepEqual(f.api.getCalendar([]), {byDate: {}, undatedCount: 0});
  assert.ok(!f.elements.get('school-list').innerHTML.includes(item.title));
  const count = f.calls.length; await f.api.onView('cal'); await f.fireWindow('focus'); await f.poll();
  assert.equal(f.calls.length, count);
  fail = false; f.advance(61000); await f.poll(); assert.equal(f.api.getCalendarStatus().state, 'ready');
});

test('storage logout clears private views even while hidden, and same-tab login is adopted on focus', async () => {
  const f = mountedFixture(); await f.api.initialization;
  f.document.hidden = true; f.store.delete('cfg_bridge_pat');
  await f.fireWindow('storage', {key: 'cfg_bridge_pat'});
  assert.equal(f.api.getCalendarStatus().state, 'unconfigured');
  assert.deepEqual(f.api.getCalendar([]).byDate, {});
  assert.ok(!f.elements.get('school-list').innerHTML.includes(item.title));
  const before = f.calls.length; await f.poll(); assert.equal(f.calls.length, before);
  f.store.set('cfg_bridge_pat', 'github_pat_next'); f.document.hidden = false;
  await f.fireWindow('focus');
  assert.equal(f.api.getCalendarStatus().state, 'ready');
  assert.equal(f.calls.at(-1).init.headers.Authorization, 'Bearer github_pat_next');
});

test('changed credentials during loading discard the old response and queue a fresh authenticated read', async () => {
  const entered = deferred(), finish = deferred(); let first = true;
  const f = mountedFixture(async () => {if (first) {first = false; entered.resolve(); await finish.promise;} return file(index);});
  await entered.promise;
  f.store.set('cfg_bridge_pat', 'github_pat_changed'); await f.fireWindow('storage', {key: 'cfg_bridge_pat'});
  assert.deepEqual(f.api.getCalendar([]), {byDate: {}, undatedCount: 0});
  finish.resolve(); await f.api.initialization;
  assert.equal(f.client.index, null);
  await f.runTimers();
  assert.equal(f.api.getCalendarStatus().state, 'ready');
  const reads = f.calls.filter(c => c.url.endsWith('/school/index.json'));
  assert.equal(reads.length, 2);
  assert.equal(reads[1].init.headers.Authorization, 'Bearer github_pat_changed');
});

test('calendar source action opens and focuses a notice older than the latest one hundred without injecting its ID', async () => {
  const sourceId = 'older-source"] [onclick="bad';
  const older = {...item, id: sourceId, updated_at: '2026-01-01T00:00:00Z', title: 'older target'};
  const many = {...index, items: [...Array.from({length: 105}, (_, i) => ({...item, id: 'source-' + i})), older]};
  const f = mountedFixture(() => file(many)); await f.api.initialization;
  const button = {disabled: false, dataset: {schoolAction: 'calendar-source', schoolSource: sourceId}, closest: selector => selector === '[data-school-action]' ? button : null};
  await f.fireDocument('click', {target: button});
  assert.equal(f.closed(), 1); assert.equal(f.elements.get('school-filter').value, 'latest');
  const focused = f.articles().find(article => article.dataset.sourceIndex === '105');
  assert.ok(focused && focused.focused && focused.scrolled);
  assert.equal(focused.tabIndex, -1); assert.ok(focused.classList.contains('school-source-focus'));
  assert.ok(!f.elements.get('school-list').innerHTML.includes(sourceId));
  assert.match(f.elements.get('school-list').innerHTML, /older target/);
});

test('404 school index is waiting, not an authentication error or a successful collection', async () => {
  const f = mountedFixture(() => response(404)); await f.api.initialization;
  assert.equal(f.api.getCalendarStatus().state, 'waiting');
  assert.match(f.api.getCalendarStatus().text, /수집 대기/);
  assert.deepEqual(f.api.getCalendar([]), {byDate: {}, undatedCount: 0});
});
