const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {BridgeClient, todayKST, renderHistory, workerStatus, rowStatus, mount} = require('../bridge-client.js');

const repo = 'jdyece25-byte/schedule-requests';
const token = 'github_pat_test_queue_only';
const date = new Date('2026-09-14T16:05:10.123Z');
const response = (status, value = {}) => ({ok: status >= 200 && status < 300, status, json: async () => value});
const file = value => response(200, {encoding: 'base64', content: Buffer.from(JSON.stringify(value)).toString('base64')});
function memory(seed = {}) {
  const map = new Map(Object.entries(seed));
  return {getItem: key => map.get(key) || null, setItem: (key, value) => map.set(key, value), map};
}
function setup(handler, options = {}) {
  const storage = options.storage || memory({cfg_bridge_pat: token});
  const calls = [];
  let uuids = 0;
  const client = new BridgeClient({storage, now: () => date,
    uuid: () => '12345678-1234-4123-8123-' + String(++uuids).padStart(12, '0'),
    onCompleted: options.onCompleted,
    fetch: async (url, init) => {
      calls.push({url, init});
      if (url === 'https://api.github.com/user') return response(200, {login: options.login || 'jdyece25-byte'});
      if (url === 'https://api.github.com/repos/' + repo) return response(200, {private: options.private !== false, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
      return handler(url, init, calls);
    }});
  return {client, calls, storage, uuidCount: () => uuids};
}
function setupUI(handler = () => response(404), storage = memory({cfg_bridge_pat: token})) {
  const elements = new Map();
  const documentListeners = new Map();
  const windowListeners = new Map();
  const calls = [];
  const document = {
    hidden: true,
    activeElement: null,
    getElementById(id) {
      if (!elements.has(id)) {
        const classes = new Set();
        elements.set(id, {id, value: '', textContent: '', style: {}, querySelectorAll: () => [],
          classList: {toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name), contains: name => classes.has(name)}});
      }
      return elements.get(id);
    },
    querySelector: () => ({id: 'v-edit'}),
    querySelectorAll: () => [],
    addEventListener: (name, listener) => documentListeners.set(name, listener)
  };
  const root = {document, localStorage: storage, addEventListener: (name, listener) => windowListeners.set(name, listener), fetch: async (url, init) => {
    calls.push({url, init});
    if (url.endsWith('/user')) return response(200, {login: 'jdyece25-byte'});
    if (url === 'https://api.github.com/repos/' + repo) return response(200, {private: true, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
    return handler(url, init);
  }};
  return {mounted: mount(root), document, documentListeners, windowListeners, storage, calls};
}
const puts = calls => calls.filter(call => call.init.method === 'PUT');

test('a previously blank tab submits with settings saved by another tab', async () => {
  const storage = memory();
  const stale = setup(() => response(201), {storage});
  const settingsTab = setup(() => {throw new Error('unexpected network');}, {storage});
  settingsTab.client.saveSettings({repo, pat: token, agent: 'claude'});

  const row = await stale.client.submit('request from the previously blank tab');

  assert.equal(row.delivery, 'queued');
  assert.equal(row.request.agent, 'claude');
  assert.equal(row.repo, repo);
  assert.ok(stale.calls.length > 0);
  assert.ok(stale.calls.every(call => call.init.headers.Authorization === 'Bearer ' + token));
  assert.equal(settingsTab.calls.length, 0);
});

test('a new request adopts another tab\'s replacement repository, token and agent', async () => {
  const storage = memory({cfg_bridge_pat: token});
  const nextRepo = 'jdyece25-byte/other-private-requests';
  const nextToken = 'github_pat_test_replacement';
  const stale = setup((url, init) => {
    if (url === 'https://api.github.com/repos/' + nextRepo) return response(200, {private: true, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
    assert.equal(init.method, 'PUT');
    return response(201);
  }, {storage});
  const settingsTab = setup(() => response(404), {storage});
  settingsTab.client.saveSettings({repo: nextRepo, pat: nextToken, agent: 'claude'});

  const row = await stale.client.submit('request with replacement settings');

  assert.equal(row.repo, nextRepo);
  assert.equal(row.request.agent, 'claude');
  assert.ok(stale.calls.every(call => call.init.headers.Authorization === 'Bearer ' + nextToken));
  assert.equal(puts(stale.calls)[0].url, 'https://api.github.com/repos/' + nextRepo + '/contents/requests/' + row.request.id + '.json');
});

test('retry reloads a replacement token and preserves the immutable request', async () => {
  const storage = memory({cfg_bridge_pat: token});
  const nextToken = 'github_pat_test_retry_replacement';
  let rejectWrite = true;
  const stale = setup((url, init) => init.method === 'PUT' ? response(rejectWrite ? 403 : 201) : response(404), {storage});
  await assert.rejects(stale.client.submit('retry after replacing credentials'));
  const original = stale.client.rows[0].request;
  const beforeRetry = stale.calls.length;
  const settingsTab = setup(() => response(404), {storage});
  settingsTab.client.saveSettings({repo, pat: nextToken, agent: 'claude'});
  rejectWrite = false;

  const row = await stale.client.retry(original.id);

  assert.equal(row.delivery, 'queued');
  assert.deepEqual(row.request, original);
  assert.equal(row.request.agent, 'codex');
  assert.equal(stale.uuidCount(), 1);
  assert.ok(stale.calls.slice(beforeRetry).every(call => call.init.headers.Authorization === 'Bearer ' + nextToken));
  assert.equal(puts(stale.calls)[0].init.body, puts(stale.calls)[1].init.body);
});

test('refresh in a previously blank tab reads settings saved by another tab', async () => {
  const storage = memory();
  const heartbeat = {version: 1, target_repo: 'jdyece25-byte/schedule', agent: 'codex', agents: ['codex', 'claude'], updated_at: date.toISOString()};
  const stale = setup(url => url.endsWith('/worker.json') ? file(heartbeat) : response(404), {storage});
  const settingsTab = setup(() => response(404), {storage});
  settingsTab.client.saveSettings({repo, pat: token, agent: 'claude'});

  await stale.client.refresh();

  assert.deepEqual(stale.client.worker, heartbeat);
  assert.equal(stale.client.settings.agent, 'claude');
  assert.ok(stale.calls.length > 0);
  assert.ok(stale.calls.every(call => call.init.headers.Authorization === 'Bearer ' + token));
});

test('cleared browser settings prevent submit, retry and refresh from using cached credentials', async () => {
  const {client, calls, storage} = setup(() => response(403));
  await assert.rejects(client.submit('request before clearing settings'));
  const originalId = client.rows[0].request.id;
  const beforeClear = calls.length;
  storage.map.clear();

  await assert.rejects(client.submit('request after clearing settings'));
  await assert.rejects(client.retry(originalId));
  await client.refresh();

  assert.equal(calls.length, beforeClear);
  assert.equal(client.configured, false);
});

test('settings saved on another device do not configure an isolated browser', async () => {
  const phone = setup(() => {throw new Error('unexpected phone network');}, {storage: memory()});
  const desktop = setup(() => {throw new Error('unexpected desktop network');}, {storage: memory()});
  desktop.client.saveSettings({repo, pat: token, agent: 'claude'});

  await assert.rejects(phone.client.submit('request from an unconfigured device'));

  assert.equal(phone.calls.length, 0);
  assert.equal(phone.client.configured, false);
  assert.equal(desktop.client.configured, true);
});

test('settings changes cannot redirect an in-flight request or its receipt verification', async () => {
  const nextRepo = 'jdyece25-byte/next-private-requests';
  const nextToken = 'github_pat_test_next_request';
  let releaseWrite;
  let writeStarted;
  const pendingWrite = new Promise(resolve => {releaseWrite = resolve;});
  const started = new Promise(resolve => {writeStarted = resolve;});
  let original;
  const {client, calls} = setup(async (url, init) => {
    if (url === 'https://api.github.com/repos/' + nextRepo) return response(200, {private: true, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
    if (init.method === 'PUT' && !original) {
      original = JSON.parse(Buffer.from(JSON.parse(init.body).content, 'base64').toString('utf8'));
      writeStarted();
      await pendingWrite;
      throw new TypeError('response lost after write');
    }
    if (init.method === 'PUT') return response(201);
    assert.equal(url, 'https://api.github.com/repos/' + repo + '/contents/requests/' + original.id + '.json');
    return file(original);
  });
  const first = client.submit('request already in flight');
  await started;
  client.saveSettings({repo: nextRepo, pat: nextToken, agent: 'claude'});
  await client.refresh();
  releaseWrite();
  const received = await first;
  const originalCalls = calls.slice();

  assert.equal(received.delivery, 'queued');
  assert.equal(received.repo, repo);
  assert.equal(received.request.agent, 'codex');
  assert.ok(originalCalls.every(call => call.init.headers.Authorization === 'Bearer ' + token));
  const next = await client.submit('request after the settings changed');
  assert.equal(next.repo, nextRepo);
  assert.equal(next.request.agent, 'claude');
  assert.ok(calls.slice(originalCalls.length).every(call => call.init.headers.Authorization === 'Bearer ' + nextToken));
});

test('connection test saves and checks the currently displayed form settings', async () => {
  const {mounted, document, documentListeners, storage, calls} = setupUI();
  const inputToken = 'github_pat_test_current_form';
  document.getElementById('cfg-bridge-pat').value = inputToken;
  document.getElementById('cfg-bridge-agent').value = 'claude';
  const button = {disabled: false, dataset: {bridgeAction: 'connection-test'}};

  await documentListeners.get('click')({target: {closest: () => button}});

  assert.equal(storage.getItem('cfg_bridge_pat'), inputToken);
  assert.equal(storage.getItem('cfg_bridge_agent'), 'claude');
  assert.equal(mounted.client.settings.pat, inputToken);
  assert.ok(calls.length > 0);
  assert.ok(calls.every(call => call.init.headers.Authorization === 'Bearer ' + inputToken));
  assert.equal(button.disabled, false);
});

test('connection test reports Contents read failure instead of successful connection', async () => {
  const {mounted, document, documentListeners} = setupUI(() => response(403));
  const button = {disabled: false, dataset: {bridgeAction: 'connection-test'}};

  await documentListeners.get('click')({target: {closest: () => button}});

  const status = document.getElementById('bridge-settings-status');
  assert.match(mounted.client.lastError, /HTTP 403/);
  assert.equal(status.textContent, mounted.client.lastError);
  assert.equal(status.classList.contains('bridge-error'), true);
});

test('storage and focus events synchronize saved settings without erasing an unchanged form draft', () => {
  const storage = memory();
  const {mounted, document, windowListeners} = setupUI(undefined, storage);
  const otherTab = setup(() => response(404), {storage});
  otherTab.client.saveSettings({repo, pat: token, agent: 'claude'});

  windowListeners.get('storage')({key: 'cfg_bridge_pat'});

  const patInput = document.getElementById('cfg-bridge-pat');
  assert.equal(patInput.value, token);
  assert.equal(document.getElementById('cfg-bridge-agent').value, 'claude');
  assert.equal(mounted.client.configured, true);
  patInput.value = 'github_pat_test_unsaved_draft';
  windowListeners.get('focus')();
  assert.equal(patInput.value, 'github_pat_test_unsaved_draft');
  storage.map.clear();
  windowListeners.get('storage')({key: null});
  assert.equal(patInput.value, '');
  assert.equal(mounted.client.configured, false);
});

test('missing authentication and non-fine-grained tokens never create a request', async () => {
  for (const pat of ['', 'ghp_classic']) {
    const {client, calls} = setup(() => {throw new Error('unexpected network');}, {storage: memory({cfg_bridge_pat: pat})});
    await assert.rejects(client.submit('내일 약속 추가'), /토큰/);
    assert.equal(calls.length, 0);
    assert.equal(client.rows.length, 0);
  }
});

test('public repositories and another account are rejected before PUT', async () => {
  for (const options of [{private: false}, {login: 'another-user'}]) {
    const {client, calls} = setup(() => {throw new Error('unexpected write');}, options);
    await assert.rejects(client.submit('private text'), /비공개|소유자/);
    assert.equal(puts(calls).length, 0);
    assert.equal(client.rows.length, 0);
  }
});

test('successful request is immutable, KST dated, token-free and only then queued', async () => {
  const {client, calls} = setup((url, init) => {
    assert.equal(init.method, 'PUT');
    assert.equal(client.rows[0].delivery, 'sending');
    return response(201, {});
  });
  const row = await client.submit('  내일 10시에 과외 🗓  ');
  assert.equal(row.delivery, 'queued');
  const write = puts(calls)[0];
  const body = JSON.parse(write.init.body);
  const payload = JSON.parse(Buffer.from(body.content, 'base64').toString('utf8'));
  assert.equal(body.sha, undefined);
  assert.deepEqual(Object.keys(payload).sort(), ['version', 'id', 'text', 'agent', 'created_at', 'today', 'timezone', 'parent_id'].sort());
  assert.equal(payload.id, '20260914T160510123Z-12345678-1234-4123-8123-000000000001');
  assert.equal(payload.today, '2026-09-15');
  assert.equal(payload.timezone, 'Asia/Seoul');
  assert.equal(payload.text, '내일 10시에 과외 🗓');
  assert.equal(payload.parent_id, null);
  assert.ok(!JSON.stringify(payload).includes(token));
  assert.equal(write.url, 'https://api.github.com/repos/' + repo + '/contents/requests/' + payload.id + '.json');
  assert.equal(todayKST(date), '2026-09-15');
});

test('failed write retains text and never reports queued', async () => {
  const {client} = setup(() => response(403));
  await assert.rejects(client.submit('보존할 요청'), /접수 실패/);
  assert.equal(client.rows[0].delivery, 'rejected');
  assert.equal(rowStatus(client.rows[0])[1], '접수 실패');
  assert.equal(client.rows[0].request.text, '보존할 요청');
});

test('lost PUT response checks the same ID and recognizes the successful write', async () => {
  let stored;
  const {client, calls} = setup((url, init) => {
    if (init.method === 'PUT') { stored = JSON.parse(Buffer.from(JSON.parse(init.body).content, 'base64').toString('utf8')); throw new TypeError('network lost'); }
    assert.ok(url.endsWith('/requests/' + stored.id + '.json'));
    return file(stored);
  });
  const row = await client.submit('네트워크가 끊긴 요청');
  assert.equal(row.delivery, 'queued');
  assert.equal(puts(calls).length, 1);
});

test('unreceived PUT is retried with identical path and body after GET confirms missing', async () => {
  let writes = 0;
  const {client, calls, uuidCount} = setup((url, init) => {
    if (init.method !== 'PUT') return response(404);
    if (++writes === 1) throw new TypeError('offline');
    return response(201);
  });
  await client.submit('한 번만 처리할 요청');
  assert.equal(puts(calls).length, 2);
  assert.equal(puts(calls)[0].url, puts(calls)[1].url);
  assert.equal(puts(calls)[0].init.body, puts(calls)[1].init.body);
  assert.equal(uuidCount(), 1);
});

test('ambiguous request survives reload and retry verifies its original ID', async () => {
  const first = setup(() => { throw new TypeError('offline'); });
  await assert.rejects(first.client.submit('연결 복구 후 확인'), /offline/);
  const original = first.client.rows[0].request;
  assert.equal(first.client.rows[0].delivery, 'uncertain');
  const second = setup((url, init) => { assert.notEqual(init.method, 'PUT'); return file(original); }, {storage: first.storage});
  await second.client.retry(original.id);
  assert.equal(second.client.rows[0].delivery, 'queued');
  assert.equal(second.uuidCount(), 0);
  assert.equal(puts(second.calls).length, 0);
});

test('a conflicting file at the same ID is never overwritten or counted as received', async () => {
  let stored;
  const {client, calls} = setup((url, init) => {
    if (init.method === 'PUT') { stored = JSON.parse(Buffer.from(JSON.parse(init.body).content, 'base64').toString('utf8')); throw new TypeError('lost response'); }
    return file({...stored, text: 'different text'});
  });
  await assert.rejects(client.submit('original text'), /다른 내용/);
  assert.equal(client.rows[0].delivery, 'uncertain');
  assert.equal(puts(calls).length, 1);
});

test('concurrent clicks result in one request; clarification gets a new child ID', async () => {
  let release;
  const gate = new Promise(resolve => {release = resolve;});
  const {client, calls} = setup(async () => {await gate; return response(201);});
  const first = client.submit('원래 요청');
  await assert.rejects(client.submit('원래 요청'), /보내는 중/);
  release();
  const original = await first;
  const child = await client.submit('오후 2시입니다', original.request.id);
  assert.notEqual(original.request.id, child.request.id);
  assert.equal(child.request.parent_id, original.request.id);
  assert.equal(puts(calls).length, 2);
});

test('missing result stays queued and a stale heartbeat is visibly offline', async () => {
  const {client} = setup((url, init) => {
    if (init.method === 'PUT') return response(201);
    if (url.endsWith('/worker.json')) return file({version: 1, target_repo: 'jdyece25-byte/schedule', agent: 'codex', updated_at: '2026-09-14T16:00:00Z'});
    return response(404);
  });
  await client.submit('대기 요청');
  await client.refresh();
  assert.equal(rowStatus(client.rows[0])[0], 'queued');
  assert.match(workerStatus(client), /오프라인 또는 지연/);
});

test('completed result reads both public files at the exact commit without private auth', async () => {
  const sha = 'a'.repeat(40);
  let snapshot;
  let current;
  const events = [{id: 'new', d: '2026-09-15', n: '새 일정'}];
  const travel = {locations: {}, times: {}, modes: {}};
  const {client, calls} = setup((url, init) => {
    if (init.method === 'PUT') return response(201);
    if (url.includes('/results/')) return file({version: 1, id: current.request.id, state: 'completed', message: '완료', questions: [], warnings: [], commit_sha: sha, updated_at: date.toISOString()});
    if (url.endsWith('/commits/main')) {assert.equal(init.headers.Authorization, undefined); return response(200, {sha});}
    if (url.includes('/schedule/contents/')) {
      assert.equal(init.headers.Authorization, undefined);
      assert.ok(url.endsWith('?ref=' + sha));
      return file(url.includes('/events.json') ? events : travel);
    }
    return response(404);
  }, {onCompleted: async (events, travel) => {snapshot = {events, travel};}});
  current = await client.submit('새 일정');
  await client.refresh();
  assert.deepEqual(snapshot, {events, travel});
  assert.equal(current.syncState, 'synced');
  assert.equal(client.syncedCommit, sha);
  assert.deepEqual(calls.filter(call => call.url.includes('/schedule/contents/')).map(call => call.url).sort(),
    ['events.json', 'travel.json'].map(name => 'https://api.github.com/repos/jdyece25-byte/schedule/contents/DB/' + name + '?ref=' + sha));
  const publicCount = calls.filter(call => call.url.includes('/schedule/contents/')).length;
  await client.refresh();
  assert.equal(calls.filter(call => call.url.includes('/schedule/contents/')).length, publicCount);
});

test('status rendering escapes request text, result messages, warnings and questions', () => {
  const malicious = '<img src=x onerror="alert(1)">';
  const html = renderHistory([{request: {id: '20260914T160510123Z-12345678-1234-4123-8123-000000000001', text: malicious, agent: 'codex', created_at: date.toISOString()}, delivery: 'queued', result: {state: 'needs_input', message: malicious, questions: [malicious], warnings: [malicious]}}]);
  assert.ok(!html.includes('<img'));
  assert.equal(html.split('&lt;img').length - 1, 4);
  assert.ok(html.includes('data-bridge-action="reply"'));
  assert.ok(html.includes('추가 확인 필요'));
});

test('completed sync retains newer manual commits and blocks divergent main history', async () => {
  const resultSha = 'a'.repeat(40);
  const mainSha = 'b'.repeat(40);
  for (const comparison of ['ahead', 'diverged']) {
    let applied = false;
    let row;
    const {client, calls} = setup((url, init) => {
      if (init.method === 'PUT') return response(201);
      if (url.includes('/results/')) return file({version: 1, id: row.request.id, state: 'completed', commit_sha: resultSha, updated_at: date.toISOString()});
      if (url.endsWith('/commits/main')) return response(200, {sha: mainSha});
      if (url.includes('/compare/')) {
        assert.ok(url.endsWith(resultSha + '...' + mainSha));
        assert.equal(init.headers.Authorization, undefined);
        return response(200, {status: comparison});
      }
      if (url.includes('/schedule/contents/')) {
        assert.ok(url.endsWith('?ref=' + mainSha));
        return file(url.includes('/events.json') ? [] : {locations: {}, times: {}});
      }
      return response(404);
    }, {onCompleted: async () => {applied = true;}});
    row = await client.submit('이미 반영된 요청');
    await client.refresh();
    assert.equal(row.result.state, 'completed');
    assert.equal(applied, comparison === 'ahead');
    if (comparison === 'diverged') {
      assert.equal(row.syncState, 'failed');
      assert.match(row.syncError, /일정 화면 동기화 대기/);
      assert.equal(calls.filter(call => call.url.includes('/schedule/contents/')).length, 0);
    }
  }
});

test('inline natural language actions route only to bridge and handle missing script', async () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
  const parse = html.match(/async function parseNL\(\)\{([\s\S]*?)\n\}/)[0];
  const cal = html.match(/async function calNLParse\(\)\{([\s\S]*?)\n\}/)[0];
  const received = [];
  const element = {textContent: '', style: {}};
  const context = vm.createContext({window: {ScheduleBridge: {submitFrom: source => received.push(source)}}, setStatus: (...args) => received.push(args), document: {getElementById: () => element}});
  vm.runInContext(parse + '\n' + cal, context);
  await vm.runInContext('parseNL()', context);
  await vm.runInContext('calNLParse()', context);
  assert.deepEqual(received, ['edit', 'cal']);
  context.window.ScheduleBridge = null;
  await vm.runInContext('calNLParse()', context);
  assert.match(element.textContent, /새로고침/);
});

test('one worker can support both agents and legacy heartbeats describe missing configuration', () => {
  const {client} = setup(() => response(404));
  client.settings.agent = 'claude';
  client.worker = {version: 1, target_repo: 'jdyece25-byte/schedule', agent: 'codex', agents: ['codex', 'claude'], updated_at: date.toISOString()};
  assert.match(workerStatus(client), /PC Claude Code 연결됨/);
  delete client.worker.agents;
  assert.equal(workerStatus(client), 'PC에 Claude Code 설정이 필요합니다.');
  client.settings.agent = 'codex';
  assert.match(workerStatus(client), /PC Codex 연결됨/);
});

test('terminal failures resend as a new request preserving parent and agent', async () => {
  const {client, calls} = setup(() => response(201));
  const parent = await client.submit('parent request');
  const original = await client.submit('failed reply', parent.request.id, 'claude');
  original.result = {state: 'failed', message: 'retry after setup'};
  await assert.rejects(client.retry(original.request.id), /같은 ID/);
  assert.ok(renderHistory([original]).includes('data-bridge-action="resend"'));
  assert.ok(!renderHistory([original]).includes('data-bridge-action="retry"'));
  const next = await client.resend(original.request.id);
  assert.notEqual(next.request.id, original.request.id);
  assert.equal(next.request.text, original.request.text);
  assert.equal(next.request.parent_id, parent.request.id);
  assert.equal(next.request.agent, 'claude');
  assert.equal(original.result.state, 'failed');
  assert.equal(puts(calls).length, 3);
  assert.ok(!renderHistory([{...original, result: {state: 'needs_input'}}]).includes('data-bridge-action="resend"'));
});

test('requests over the backend character limit are rejected without truncating or writing', async () => {
  const {client, calls} = setup(() => response(201));
  await assert.rejects(client.submit('a'.repeat(6001)), /6,000자/);
  assert.equal(calls.length, 0);
  const row = await client.submit('🗓'.repeat(6000));
  assert.equal([...row.request.text].length, 6000);
});
