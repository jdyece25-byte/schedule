(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SchedulePush = api.mount(root);
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const QUEUE = 'jdyece25-byte/schedule-requests';
  const TARGET = 'jdyece25-byte/schedule';
  const DEVICE_KEY = 'schedule_push_device_v1';
  const STATE_KEY = 'schedule_push_settings_v1';
  const KINDS = ['deadline', 'daily', 'changes', 'departure', 'notice'];
  const defaults = () => Object.fromEntries(KINDS.map(key => [key, true]));
  const preferences = value => Object.fromEntries(KINDS.map(key => [key, key === 'notice' && Object.hasOwn(value || {}, key) ? value[key] === true : value?.[key] !== false]));
  const encode = value => btoa(Array.from(new TextEncoder().encode(JSON.stringify(value, null, 2)), b => String.fromCharCode(b)).join(''));
  const decode = file => JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(file.content.replace(/\s/g, '')), c => c.charCodeAt(0))));
  const keyBytes = key => Uint8Array.from(atob(key.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - key.length % 4) % 4)), c => c.charCodeAt(0));
  const sameKey = (left, right) => left && left.byteLength === right.byteLength && new Uint8Array(left).every((byte, index) => byte === right[index]);
  const installed = root => Boolean(root.navigator.standalone || root.matchMedia?.('(display-mode: standalone)').matches);
  const isIOS = root => /iPhone|iPad|iPod/.test(root.navigator.userAgent) || (root.navigator.platform === 'MacIntel' && root.navigator.maxTouchPoints > 1);
  function capability(root) {
    if (isIOS(root) && !installed(root)) return 'iPhone·iPad는 iOS 16.4 이상에서 Safari의 공유 → 홈 화면에 추가 후, 홈 화면 아이콘으로 열어야 알림을 켤 수 있습니다.';
    if (!root.isSecureContext || !root.navigator.serviceWorker || !root.PushManager || !root.Notification) return '이 브라우저는 웹 푸시를 지원하지 않습니다. Android는 최신 Chrome·삼성 인터넷, iPhone은 iOS 16.4 이상 홈 화면 앱을 이용해 주세요.';
    return '';
  }
  function subscriptionJSON(subscription) {
    const value = subscription?.toJSON();
    if (!value || typeof value.endpoint !== 'string' || !value.endpoint.startsWith('https://') || typeof value.keys?.p256dh !== 'string' || typeof value.keys?.auth !== 'string') throw new Error('브라우저 알림 구독을 읽지 못했습니다. 다시 켜 주세요.');
    return {endpoint: value.endpoint, expirationTime: value.expirationTime ?? null, keys: {p256dh: value.keys.p256dh, auth: value.keys.auth}};
  }
  class PushClient {
    constructor(root, options = {}) {
      this.root = root;
      this.storage = options.storage || root.localStorage;
      this.fetch = options.fetch || root.fetch.bind(root);
      this.now = options.now || (() => new Date());
      this.uuid = options.uuid || (() => root.crypto.randomUUID());
      this.state = {enabled: false, preferences: defaults()};
      this.config = null;
      this.registration = null;
      this.subscription = null;
      this.busy = false;
      this.timeoutMs = options.timeoutMs || 15000;
    }
    read(key) { try { return this.storage.getItem(key) || ''; } catch { return ''; } }
    credentials() {
      const repo = this.read('cfg_bridge_repo') || QUEUE;
      const pat = this.read('cfg_bridge_pat').trim();
      if (repo.toLowerCase() !== QUEUE.toLowerCase()) throw new Error('알림은 jdyece25-byte/schedule-requests 비공개 저장소를 사용합니다. 아래 일정 요청 연결을 확인해 주세요.');
      if (!pat.startsWith('github_pat_')) throw new Error('이 기기의 아래 일정 요청 연결에 요청 전용 GitHub 토큰을 저장해 주세요.');
      return {repo: QUEUE, pat};
    }
    deviceId() {
      let id = this.read(DEVICE_KEY);
      if (!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(id)) {
        id = this.uuid();
        if (!/^[a-f0-9-]{36}$/i.test(id)) throw new Error('기기 ID를 생성하지 못했습니다.');
        this.storage.setItem(DEVICE_KEY, id);
      }
      return id;
    }
    async request(url, options = {}, credentials = null) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      const headers = credentials ? {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', Authorization: 'Bearer ' + credentials.pat, ...options.headers} : options.headers;
      try { return await this.fetch(url, {...options, headers, cache: 'no-store', signal: controller.signal}); }
      finally { clearTimeout(timer); }
    }
    async validate(credentials) {
      const url = 'https://api.github.com';
      const responses = await Promise.all([this.request(url + '/user', {}, credentials), this.request(url + '/repos/' + QUEUE, {}, credentials)]);
      if (responses.some(r => !r.ok)) throw new Error('GitHub 연결 실패: 요청 저장소 선택과 Contents 읽기·쓰기 권한을 확인해 주세요.');
      const [user, repo] = await Promise.all(responses.map(r => r.json()));
      if (repo.private !== true) throw new Error('구독 정보는 비공개 저장소에만 저장할 수 있습니다. 저장을 중단했습니다.');
      if (user.login?.toLowerCase() !== 'jdyece25-byte' || repo.owner?.login?.toLowerCase() !== 'jdyece25-byte' || repo.permissions?.push === false) throw new Error('요청 저장소 소유자의 Contents 읽기·쓰기 권한이 필요합니다.');
    }
    async init() {
      const reason = capability(this.root);
      if (reason) throw new Error(reason);
      const response = await this.request(new URL('push-config.json', this.root.location.href).href);
      if (!response.ok) throw new Error('알림 서버 설정을 불러오지 못했습니다. 잠시 후 새로고침해 주세요.');
      const config = await response.json();
      if (config.version !== 1 || config.queueRepo !== QUEUE || config.targetRepo !== TARGET || !/^[A-Za-z0-9_-]{87}$/.test(config.vapidPublicKey || '') || keyBytes(config.vapidPublicKey)[0] !== 4) throw new Error('알림 서버 설정이 올바르지 않습니다.');
      this.config = config;
      await this.root.navigator.serviceWorker.register('./sw.js', {scope: './', updateViaCache: 'none'});
      this.registration = await this.root.navigator.serviceWorker.ready;
      this.subscription = await this.registration.pushManager.getSubscription();
      try {
        const saved = JSON.parse(this.read(STATE_KEY) || '{}');
        this.state = {enabled: Boolean(saved.enabled && this.subscription), preferences: preferences(saved.preferences)};
      } catch { /* Preferences are optional, never recover tokens from other storage. */ }
      return this;
    }
    async readRecord(credentials, id) {
      const response = await this.request('https://api.github.com/repos/' + QUEUE + '/contents/subscriptions/' + id + '.json', {}, credentials);
      if (response.status === 404) return null;
      if (!response.ok) throw new Error('알림 설정 읽기 실패 (HTTP ' + response.status + ')');
      const file = await response.json();
      if (file.encoding !== 'base64' || typeof file.content !== 'string' || typeof file.sha !== 'string') throw new Error('알림 설정 응답을 읽지 못했습니다.');
      const data = decode(file);
      if (data.device_id !== id || data.version !== 1) throw new Error('기기 구독 정보가 일치하지 않아 저장을 중단했습니다.');
      return {sha: file.sha, data};
    }
    async persist(enabled, wanted, subscription, credentials, testRequestedAt = null) {
      await this.validate(credentials);
      const id = this.deviceId();
      for (let attempt = 0; attempt < 3; attempt++) {
        const existing = await this.readRecord(credentials, id);
        const now = this.now().toISOString();
        const data = {version: 1, device_id: id, subscription: subscription ? subscriptionJSON(subscription) : existing?.data.subscription ?? null, enabled,
          preferences: preferences(wanted), created_at: existing?.data.created_at || now, updated_at: now, timezone: 'Asia/Seoul'};
        if (testRequestedAt || typeof existing?.data.test_requested_at === 'string') data.test_requested_at = testRequestedAt || existing.data.test_requested_at;
        if (enabled && !data.subscription) throw new Error('알림 구독이 없습니다. 이 기기 알림 켜기를 눌러 주세요.');
        const body = {message: 'Update notification device ' + id, content: encode(data)};
        if (existing) body.sha = existing.sha;
        const response = await this.request('https://api.github.com/repos/' + QUEUE + '/contents/subscriptions/' + id + '.json', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}, credentials);
        if (response.ok) {
          this.state = {enabled, preferences: data.preferences};
          // Browser owns endpoint/keys; local settings contain neither them nor credentials.
          try { this.storage.setItem(STATE_KEY, JSON.stringify(this.state)); } catch { /* Remote save remains successful. */ }
          return;
        }
        if (![409, 422].includes(response.status) || attempt === 2) throw new Error('알림 설정 저장 실패 (HTTP ' + response.status + '). 저장되지 않았습니다. 연결과 Contents 쓰기 권한을 확인해 주세요.');
      }
    }
    async enable(wanted) {
      if (this.busy) throw new Error('알림 설정을 저장 중입니다.');
      if (!this.registration || !this.config) throw new Error('알림 준비 중입니다. 잠시 후 다시 눌러 주세요.');
      const credentials = this.credentials();
      if (this.root.Notification.permission === 'denied') throw new Error('알림이 차단되어 있습니다. 휴대폰의 이 앱·사이트 알림 설정에서 허용한 뒤 다시 눌러 주세요.');
      this.busy = true;
      let created = false;
      try {
        // This call runs directly in the user's click handler, before any network await.
        const permission = await this.root.Notification.requestPermission();
        if (permission !== 'granted') throw new Error('알림 권한이 허용되지 않았습니다. 알림 설정은 저장하지 않았습니다.');
        await this.validate(credentials);
        const remote = await this.readRecord(credentials, this.deviceId());
        const key = keyBytes(this.config.vapidPublicKey);
        this.subscription = await this.registration.pushManager.getSubscription();
        if (this.subscription && (remote?.data.enabled === false || remote?.data.disabled_reason === 'subscription_expired' || !sameKey(this.subscription.options?.applicationServerKey, key) || (this.subscription.expirationTime && this.subscription.expirationTime <= this.now().getTime()))) {
          await this.subscription.unsubscribe();
          this.subscription = null;
        }
        if (!this.subscription) {
          this.subscription = await this.registration.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: key});
          created = true;
        }
        await this.persist(true, wanted, this.subscription, credentials);
      } catch (error) {
        if (created && this.subscription) { try { await this.subscription.unsubscribe(); this.subscription = null; } catch { /* A failed remote write never claims saved. */ } }
        if (!this.subscription) this.state.enabled = false;
        throw error;
      } finally { this.busy = false; }
    }
    async save(wanted) {
      if (this.busy) throw new Error('알림 설정을 저장 중입니다.');
      if (!this.subscription || !this.state.enabled) throw new Error('먼저 이 기기 알림 켜기를 눌러 주세요.');
      const credentials = this.credentials();
      this.busy = true;
      try { await this.persist(true, wanted, this.subscription, credentials); }
      finally { this.busy = false; }
    }
    async disable(wanted) {
      if (this.busy) throw new Error('알림 설정을 저장 중입니다.');
      const credentials = this.credentials();
      this.busy = true;
      try {
        // Turn off the remote sender first; a failed write must not silently claim success.
        await this.persist(false, wanted, this.subscription, credentials);
        if (this.subscription) { await this.subscription.unsubscribe(); this.subscription = null; }
      } finally { this.busy = false; }
    }
    async test() {
      if (this.busy) throw new Error('알림 설정을 저장 중입니다.');
      if (!this.subscription || !this.state.enabled) throw new Error('먼저 이 기기 알림 켜기를 눌러 주세요.');
      const credentials = this.credentials();
      this.busy = true;
      try { await this.persist(true, this.state.preferences, this.subscription, credentials, this.now().toISOString()); }
      finally { this.busy = false; }
    }
    async refresh() {
      if (this.busy) throw new Error('알림 설정을 저장 중입니다.');
      if (!this.registration) throw new Error('알림 준비 중입니다. 잠시 후 다시 눌러 주세요.');
      const credentials = this.credentials();
      this.busy = true;
      try {
        await this.validate(credentials);
        this.subscription = await this.registration.pushManager.getSubscription();
        const record = await this.readRecord(credentials, this.deviceId());
        this.state = {enabled: Boolean(record?.data.enabled && this.subscription && this.root.Notification.permission === 'granted' && (!this.subscription.expirationTime || this.subscription.expirationTime > this.now().getTime()) && record.data.subscription?.endpoint === this.subscription.endpoint && sameKey(this.subscription.options?.applicationServerKey, keyBytes(this.config.vapidPublicKey))), preferences: preferences(record?.data.preferences)};
        return this.state;
      } finally { this.busy = false; }
    }
  }
  function mount(root) {
    const document = root.document;
    const panel = document.getElementById('push-settings-panel');
    if (!panel) return null;
    const byId = id => document.getElementById(id);
    const client = new PushClient(root);
    let prompt = null;
    let ready = false;
    const notify = (message, error = false) => { byId('push-status').textContent = message; byId('push-status').classList.toggle('bridge-error', error); };
    const wanted = () => Object.fromEntries(KINDS.map(key => [key, byId('push-' + key).checked]));
    const paintState = () => {
      byId('push-enable').textContent = client.state.enabled ? '이 기기 구독 다시 연결' : '이 기기 알림 켜기';
      byId('push-state').textContent = client.state.enabled ? '이 기기 알림 켜짐' : '이 기기 알림 꺼짐 · 켜기를 눌러 연결하세요';
    };
    const paint = () => { KINDS.forEach(key => { byId('push-' + key).checked = client.state.preferences[key]; }); paintState(); };
    const buttons = busy => {
      panel.querySelectorAll('button[data-push-action]').forEach(button => { button.disabled = busy || (!ready && button.dataset.pushAction !== 'install' && button.dataset.pushAction !== 'calendar-copy'); });
      panel.querySelectorAll('input[type="checkbox"]').forEach(input => { input.disabled = busy; });
    };
    const ics = new URL('events.ics', root.location.href);
    byId('push-calendar-url').value = ics.href;
    byId('push-calendar-link').href = 'webcal://' + ics.host + ics.pathname;
    byId('push-calendar-download').href = ics.href;
    const guide = isIOS(root) ? 'iPhone·iPad: iOS 16.4 이상 → Safari 공유 → 홈 화면에 추가 → 홈 화면 아이콘으로 실행 → 이 기기 알림 켜기. 홈 화면 앱에서도 아래 요청 연결 토큰을 한 번 저장해 주세요.' : 'Android: Chrome ⋮ → 홈 화면에 추가·앱 설치, 또는 삼성 인터넷 메뉴 ☰ → 현재 페이지 추가 → 홈 화면. 설치 후 이 기기 알림 켜기를 눌러 허용해 주세요.';
    byId('push-install-guide').textContent = guide;
    root.addEventListener('beforeinstallprompt', event => { event.preventDefault(); prompt = event; byId('push-install').textContent = '홈 화면에 앱 설치'; });
    root.addEventListener('appinstalled', () => { prompt = null; byId('push-install').textContent = '홈 화면 설치됨'; });
    panel.addEventListener('click', async event => {
      const button = event.target.closest('[data-push-action]');
      if (!button || button.disabled) return;
      const action = button.dataset.pushAction;
      if (action === 'install') {
        try {
          if (prompt) { const current = prompt; prompt = null; await current.prompt(); await current.userChoice; }
          else notify(installed(root) ? '홈 화면 앱으로 실행 중입니다.' : guide);
        } catch { notify(guide); }
        return;
      }
      if (action === 'calendar-copy') {
        try { await root.navigator.clipboard.writeText(ics.href); notify('캘린더 구독 주소를 복사했습니다.'); }
        catch { byId('push-calendar-url').select(); notify('아래 주소를 길게 눌러 복사해 주세요.'); }
        return;
      }
      buttons(true);
      notify('알림 설정 확인 중…');
      try {
        if (action === 'enable') await client.enable(wanted());
        else if (action === 'save') await client.save(wanted());
        else if (action === 'disable') await client.disable(wanted());
        else if (action === 'test') await client.test();
        else if (action === 'refresh') await client.refresh();
        paint();
        notify(action === 'refresh' ? '이 기기의 구독 상태를 확인했습니다.' : action === 'test' ? '테스트 발송을 요청했습니다. 알림창을 확인하세요.' : action === 'disable' ? '이 기기의 알림을 껐습니다.' : '비공개 저장소에 알림 설정을 저장했습니다.');
      } catch (error) { paintState(); notify(error.name === 'AbortError' ? '응답이 늦어 저장 여부를 확인하지 못했습니다. 상태 확인 후 다시 시도해 주세요.' : error.message || '알림 연결에 실패했습니다.', true); }
      finally { buttons(false); }
    });
    panel.addEventListener('change', event => { if (event.target.matches('input[type="checkbox"]')) notify('종류별 설정을 변경했습니다. 알림 종류 저장을 눌러 적용해 주세요.'); });
    root.navigator.serviceWorker?.addEventListener('message', event => { if (event.data?.type === 'schedule-push-reconnect') notify('브라우저 알림 구독이 만료되었습니다. 이 기기 알림 켜기·다시 연결을 눌러 갱신해 주세요.', true); });
    paint(); buttons(false);
    const initialization = client.init().then(async () => {
      paint();
      if (client.read('cfg_bridge_pat')) {
        try { await client.refresh(); paint(); notify('이 기기의 알림 설정을 확인했습니다.'); }
        catch (error) { notify(error.message, true); }
      } else notify('아래 일정 요청 연결을 저장한 뒤 이 기기 알림 켜기를 눌러 주세요.');
      ready = true; buttons(false);
    }).catch(error => { notify(error.message, true); buttons(false); });
    return {client, initialization};
  }
  return {PushClient, mount, capability, keyBytes, sameKey, preferences};
});
