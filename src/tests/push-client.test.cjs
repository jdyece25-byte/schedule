const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {PushClient, capability, keyBytes, preferences} = require('../push-client.js');
const {notificationData} = require('../sw.js');
const QUEUE = 'jdyece25-byte/schedule-requests';
const ID = '12345678-1234-4123-8123-123456789abc';
const NOW = new Date('2026-09-14T04:00:00Z');
const KEY = Buffer.from([4, ...new Array(64).fill(21)]).toString('base64url');
const PREFS = {deadline: true, daily: false, changes: true, departure: false, notice: true};
const response = (status, value) => ({ok: status >= 200 && status < 300, status, json: async () => value});

test('deadline and departure reminders default on and preserve an explicit opt-out', () => {
  assert.equal(preferences({}).deadline, true);
  assert.equal(preferences({}).departure, true);
  assert.equal(preferences({deadline: false, departure: false}).deadline, false);
  assert.equal(preferences({deadline: false, departure: false}).departure, false);
});

test('school notice preference defaults on only when absent and preserves explicit off', () => {
  assert.equal(preferences({deadline: false}).notice, true);
  assert.equal(preferences({notice: true}).notice, true);
  for (const notice of [false, null, 'true', 1]) assert.equal(preferences({notice}).notice, false);
});

function fixture(options = {}) {
  const store = new Map(Object.entries({cfg_bridge_pat: 'github_pat_fake', cfg_bridge_repo: QUEUE, ...(options.storage || {})}));
  const storage = {getItem: key => store.get(key) || null, setItem: (key, value) => store.set(key, value)};
  const calls = [];
  let record = options.record || null;
  let serial = 0;
  let nativeSubscription = options.existing ? makeSubscription(options.existingKey || KEY, options.expirationTime) : null;
  let permissionCalls = 0;
  let subscribeCalls = 0;
  let unsubscribed = 0;
  let failConflict = options.conflict;
  function makeSubscription(key = KEY, expirationTime = null) {
    const endpoint = 'https://push.example.invalid/device-' + ++serial;
    return {endpoint, expirationTime, options: {applicationServerKey: keyBytes(key).buffer},
      toJSON: () => ({endpoint, expirationTime, keys: {p256dh: 'browser-public-encryption-key', auth: 'browser-auth-key'}}),
      unsubscribe: async () => { unsubscribed++; nativeSubscription = null; return true; }};
  }
  const registration = {pushManager: {
    getSubscription: async () => nativeSubscription,
    subscribe: async args => {
      subscribeCalls++;
      assert.equal(args.userVisibleOnly, true);
      assert.deepEqual(args.applicationServerKey, keyBytes(KEY));
      nativeSubscription = makeSubscription();
      return nativeSubscription;
    }
  }};
  const root = {isSecureContext: true, location: {href: 'https://jdyece25-byte.github.io/schedule/'}, localStorage: storage,
    navigator: {userAgent: 'Android Chrome', platform: 'Linux', serviceWorker: {register: async (url, opts) => { calls.push({register: url, opts}); return registration; }, ready: Promise.resolve(registration)}},
    Notification: {permission: options.permission || 'default', requestPermission: () => { permissionCalls++; root.Notification.permission = options.grant || 'granted'; return Promise.resolve(root.Notification.permission); }},
    PushManager: function () {}, crypto: {randomUUID: () => ID}, matchMedia: () => ({matches: false}),
    fetch: async (url, init) => {
      calls.push({url, init});
      if (url.endsWith('/push-config.json')) return response(200, {version: 1, queueRepo: QUEUE, targetRepo: 'jdyece25-byte/schedule', vapidPublicKey: KEY});
      assert.ok(url.startsWith('https://api.github.com/'));
      if (url.endsWith('/user')) return response(200, {login: 'jdyece25-byte'});
      if (url.endsWith('/repos/' + QUEUE)) return response(200, {private: options.private !== false, owner: {login: 'jdyece25-byte'}, permissions: {push: true}});
      assert.equal(url, 'https://api.github.com/repos/' + QUEUE + '/contents/subscriptions/' + ID + '.json');
      if (init.method === 'PUT') {
        if (options.writeFail) return response(options.writeFail, {});
        if (failConflict) { failConflict = false; record = {...JSON.parse(Buffer.from(JSON.parse(init.body).content, 'base64').toString()), created_at: '2026-09-01T00:00:00Z'}; return response(409, {}); }
        const body = JSON.parse(init.body);
        if (record) assert.equal(body.sha, 'current-sha');
        else assert.equal(body.sha, undefined);
        record = JSON.parse(Buffer.from(body.content, 'base64').toString('utf8'));
        return response(200, {});
      }
      return record ? response(200, {sha: 'current-sha', encoding: 'base64', content: Buffer.from(JSON.stringify(record)).toString('base64')}) : response(404, {});
    }};
  const client = new PushClient(root, {now: () => NOW});
  return {root, client, store, calls, record: () => record, native: () => nativeSubscription, permissionCalls: () => permissionCalls, subscribeCalls: () => subscribeCalls, unsubscribed: () => unsubscribed};
}
const puts = fixture => fixture.calls.filter(c => c.init?.method === 'PUT');

test('initialization registers a narrowly scoped uncached worker without requesting permission or subscribing', async () => {
  const f = fixture();
  await f.client.init();
  assert.equal(f.permissionCalls(), 0);
  assert.equal(f.subscribeCalls(), 0);
  assert.equal(puts(f).length, 0);
  assert.deepEqual(f.calls.find(c => c.register), {register: './sw.js', opts: {scope: './', updateViaCache: 'none'}});
  const config = f.calls.find(c => c.url?.endsWith('/push-config.json'));
  assert.equal(config.init.headers, undefined);
  assert.equal(config.init.cache, 'no-store');
});

test('permission is requested synchronously from the enable gesture and a private subscription is saved', async () => {
  const f = fixture();
  await f.client.init();
  const enabling = f.client.enable(PREFS);
  assert.equal(f.permissionCalls(), 1);
  assert.equal(puts(f).length, 0);
  await enabling;
  assert.equal(f.record().device_id, ID);
  assert.equal(f.record().enabled, true);
  assert.deepEqual(f.record().preferences, PREFS);
  assert.equal(f.record().timezone, 'Asia/Seoul');
  assert.equal(f.record().subscription.endpoint, f.native().endpoint);
  assert.equal(f.record().updated_at, NOW.toISOString());
  assert.equal(f.client.state.enabled, true);
  const local = f.store.get('schedule_push_settings_v1');
  assert.ok(!local.includes('endpoint') && !local.includes('auth') && !local.includes('github_pat'));
  assert.ok(f.calls.filter(c => c.url?.startsWith('https://api.github.com')).every(c => c.init.headers.Authorization === 'Bearer github_pat_fake' && c.init.cache === 'no-store'));
});

test('public repository validation blocks native subscription and every private record write', async () => {
  const f = fixture({private: false}); await f.client.init();
  await assert.rejects(f.client.enable(PREFS), /비공개/);
  assert.equal(puts(f).length, 0);
  assert.equal(f.unsubscribed(), 0);
  assert.equal(f.subscribeCalls(), 0);
  assert.equal(f.client.state.enabled, false);
});

test('denied permission does not subscribe or write and retry is explicit', async () => {
  const f = fixture({grant: 'denied'}); await f.client.init();
  await assert.rejects(f.client.enable(PREFS), /허용되지/);
  assert.equal(f.subscribeCalls(), 0); assert.equal(puts(f).length, 0);
  await assert.rejects(f.client.enable(PREFS), /차단/);
  assert.equal(f.permissionCalls(), 1);
});

test('failed write does not save local success or leave a newly created subscription running', async () => {
  const f = fixture({writeFail: 403}); await f.client.init();
  await assert.rejects(f.client.enable(PREFS), /저장 실패/);
  assert.equal(f.client.state.enabled, false);
  assert.equal(f.store.has('schedule_push_settings_v1'), false);
  assert.equal(f.unsubscribed(), 1);
});

test('Contents conflict retries with current sha and preserves original creation time', async () => {
  const f = fixture({conflict: true}); await f.client.init(); await f.client.enable(PREFS);
  assert.equal(puts(f).length, 2);
  assert.equal(f.record().created_at, '2026-09-01T00:00:00Z');
  assert.deepEqual(f.record().preferences, PREFS);
});

test('failed replacement cannot keep reporting an old enabled device after native unsubscribe', async () => {
  const other = Buffer.from([4, ...new Array(64).fill(22)]).toString('base64url');
  const f = fixture({existing: true, existingKey: other, writeFail: 403, storage: {schedule_push_settings_v1: JSON.stringify({enabled: true, preferences: PREFS})}});
  await f.client.init(); assert.equal(f.client.state.enabled, true);
  await assert.rejects(f.client.enable(PREFS), /저장 실패/);
  assert.equal(f.client.state.enabled, false); assert.equal(f.native(), null);
});

test('existing matching subscription is reused; key rotation and expiry require explicit re-registration', async () => {
  const f = fixture({existing: true}); await f.client.init(); await f.client.enable(PREFS);
  assert.equal(f.subscribeCalls(), 0); assert.equal(f.unsubscribed(), 0);
  const other = Buffer.from([4, ...new Array(64).fill(22)]).toString('base64url');
  const changed = fixture({existing: true, existingKey: other}); await changed.client.init();
  assert.equal(changed.subscribeCalls(), 0);
  await changed.client.enable(PREFS);
  assert.equal(changed.subscribeCalls(), 1); assert.equal(changed.unsubscribed(), 1);
  const expired = fixture({existing: true, expirationTime: NOW.getTime() - 1}); await expired.client.init(); await expired.client.enable(PREFS);
  assert.equal(expired.subscribeCalls(), 1); assert.equal(expired.unsubscribed(), 1);
});

test('turn off writes disabled state to private repo before unsubscribing; category changes and test requests persist', async () => {
  const f = fixture(); await f.client.init(); await f.client.enable(PREFS);
  const changed = {...PREFS, daily: true}; await f.client.save(changed);
  assert.deepEqual(f.record().preferences, changed);
  await f.client.test(); assert.equal(f.record().test_requested_at, NOW.toISOString());
  await f.client.save(changed); assert.equal(f.record().test_requested_at, NOW.toISOString());
  await f.client.disable(changed);
  assert.equal(f.record().enabled, false); assert.equal(f.unsubscribed(), 1);
  assert.equal(f.client.state.enabled, false);
});

test('server-expired endpoint is replaced even when the browser retains it without an expiry time', async () => {
  const f = fixture({existing: true, record: {version: 1, device_id: ID, enabled: false, disabled_reason: 'subscription_expired', subscription: {endpoint: 'https://push.example.invalid/device-1'}}});
  await f.client.init(); await f.client.enable(PREFS);
  assert.equal(f.unsubscribed(), 1); assert.equal(f.subscribeCalls(), 1);
  assert.equal(f.record().enabled, true); assert.equal(f.record().disabled_reason, undefined);
  assert.equal(f.record().subscription.endpoint, 'https://push.example.invalid/device-2');
});

test('settings token change is read for each action and public destination is refused before permission', async () => {
  const f = fixture({storage: {cfg_bridge_repo: 'jdyece25-byte/schedule'}}); await f.client.init();
  await assert.rejects(f.client.enable(PREFS), /비공개 저장소/);
  assert.equal(f.permissionCalls(), 0);
  f.store.set('cfg_bridge_repo', QUEUE); f.store.set('cfg_bridge_pat', 'github_pat_replacement');
  await f.client.enable(PREFS);
  assert.equal(puts(f)[0].init.headers.Authorization, 'Bearer github_pat_replacement');
});

test('iPhone requires a home screen context; Android Chrome and Samsung use standard capability detection', () => {
  const {root} = fixture();
  assert.equal(capability(root), '');
  root.navigator.userAgent = 'Android SamsungBrowser'; assert.equal(capability(root), '');
  root.navigator.userAgent = 'iPhone'; assert.match(capability(root), /iOS 16.4.*홈 화면/);
  root.navigator.standalone = true; assert.equal(capability(root), '');
  delete root.PushManager; assert.match(capability(root), /지원하지/);
});

test('notification body ignores arbitrary body, title, credentials and request metadata', () => {
  const n = notificationData({kind: 'daily', title: 'private title', body: 'private body', token: 'secret', items: [{name: '과목', time: '09:30–10:45', location: '301동', notes: 'private notes', token: 'secret'}]}, 'https://example.test/schedule/');
  assert.equal(n.title, '오늘 일정');
  assert.equal(n.options.body, '과목 · 09:30–10:45 · 301동');
  assert.ok(!JSON.stringify(n).includes('private') && !JSON.stringify(n).includes('secret'));
});

test('school notice pushes use a fixed title and only course and posted time', () => {
  const n = notificationData({kind: 'notice', title: 'private title', body: 'private text', source_url: 'https://private.invalid/',
    url: './#school', items: [{name: '테스트 과목', time: '2026-09-14 09:30', location: '', title: 'private title', evidence: 'private evidence'}]},
  'https://example.test/schedule/');
  assert.equal(n.title, '학교 공지 · 확인 필요');
  assert.equal(n.options.body, '테스트 과목 · 2026-09-14 09:30');
  assert.equal(n.options.data.url, 'https://example.test/schedule/#school');
  assert.ok(!JSON.stringify(n).includes('private'));
});

test('tentative notification items append only the fixed confirmation marker', () => {
  const scope = 'https://example.test/schedule/';
  const item = {name: '과목', time: '09:30–10:45', location: '301동', status: 'tentative', notes: 'private notes'};
  const marked = notificationData({kind: 'deadline', items: [item]}, scope);
  assert.equal(marked.options.body, '과목 · 09:30–10:45 · 301동 · 확인 필요');
  for (const status of ['confirmed', 'private status', 'TENTATIVE', null, {toString: () => 'tentative'}]) {
    assert.equal(notificationData({kind: 'daily', items: [{...item, status}]}, scope).options.body, '과목 · 09:30–10:45 · 301동');
  }
  assert.ok(!JSON.stringify(marked).includes('private'));
});

test('notification click URLs cannot escape app scope or retain query credentials', () => {
  const scope = 'https://example.test/schedule/';
  for (const url of ['https://evil.test/', '../other/', '//evil.test/', '/schedule-evil/', 'javascript:alert(1)', 'https://user:pass@example.test/schedule/']) {
    assert.equal(notificationData({url}, scope).options.data.url, scope);
  }
  assert.equal(notificationData({url: './?token=secret#private'}, scope).options.data.url, scope);
  assert.equal(notificationData({url: './index.html'}, scope).options.data.url, scope + 'index.html');
  assert.equal(notificationData({url: './?token=secret#school'}, scope).options.data.url, scope + '#school');
  assert.equal(notificationData({url: './#school?secret'}, scope).options.data.url, scope);
  assert.equal(notificationData({url: 'https://evil.test/#school'}, scope).options.data.url, scope);
  assert.equal(notificationData({kind: '__proto__', tag: 'bad\nsecret'}, scope).title, '일정 변경 반영');
});

test('service worker shows all received pushes visibly and never intercepts fetch or caches private data', async () => {
  const listeners = new Map(); const notifications = [];
  const scope = 'https://example.test/schedule/';
  const source = fs.readFileSync(path.join(__dirname, '../sw.js'), 'utf8');
  vm.runInNewContext(source, {URL, self: {registration: {scope, showNotification: async (...args) => notifications.push(args)}, addEventListener: (name, cb) => listeners.set(name, cb)}});
  assert.equal(listeners.has('fetch'), false);
  let pending;
  listeners.get('push')({data: {json: () => ({kind: 'deadline', items: [{name: '보고서', time: '내일', location: ''}]})}, waitUntil: promise => pending = promise});
  await pending; assert.equal(notifications[0][0], '마감 알림'); assert.equal(notifications[0][1].body, '보고서 · 내일');
  listeners.get('push')({data: {json: () => {throw new Error('bad json');}}, waitUntil: promise => pending = promise});
  await pending; assert.equal(notifications.length, 2);
});
