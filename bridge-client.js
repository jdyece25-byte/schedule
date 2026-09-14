(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ScheduleBridge = api.mount(root);
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const DEFAULT_REPO = 'jdyece25-byte/schedule-requests';
  const TARGET_REPO = 'jdyece25-byte/schedule';
  const HISTORY_KEY = 'schedule_bridge_history_v1';
  const ID_PATTERN = /^\d{8}T\d{9}Z-[a-f0-9-]{36}$/i;
  const RESULT_STATES = new Set(['processing', 'completed', 'needs_input', 'failed']);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const encode = value => {
    let binary = '';
    new TextEncoder().encode(JSON.stringify(value, null, 2)).forEach(byte => { binary += String.fromCharCode(byte); });
    return btoa(binary);
  };
  const decode = file => JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(file.content.replace(/\s/g, '')), c => c.charCodeAt(0))));
  const todayKST = date => new Date(date.getTime() + 9 * 3600000).toISOString().slice(0, 10);
  const requestId = (date, uuid) => date.toISOString().replace(/[-:.]/g, '') + '-' + uuid;
  const messageFor = error => error?.name === 'AbortError' ? '응답 시간이 초과됐습니다.' : error.message || '연결에 실패했습니다.';

  class BridgeClient {
    constructor(options = {}) {
      this.fetch = options.fetch || globalThis.fetch.bind(globalThis);
      this.storage = options.storage || globalThis.localStorage;
      this.now = options.now || (() => new Date());
      this.uuid = options.uuid || (() => globalThis.crypto.randomUUID());
      this.onChange = options.onChange || (() => {});
      this.onCompleted = options.onCompleted || (async () => {});
      this.timeoutMs = options.timeoutMs || 15000;
      this.settings = {repo: this.read('cfg_bridge_repo') || DEFAULT_REPO, pat: this.read('cfg_bridge_pat'), agent: this.read('cfg_bridge_agent') === 'claude' ? 'claude' : 'codex'};
      try { this.history = JSON.parse(this.read(HISTORY_KEY) || '[]'); } catch { this.history = []; }
      if (!Array.isArray(this.history)) this.history = [];
      this.history = this.history.filter(row => row && ID_PATTERN.test(row.request?.id) && typeof row.request.text === 'string' && typeof row.repo === 'string').slice(0, 20);
      this.history.forEach(row => { if (row.delivery === 'sending') row.delivery = 'uncertain'; });
      this.worker = null;
      this.lastError = '';
      this.syncedCommit = '';
      this.sending = false;
      this.refreshing = false;
    }
    read(key) { try { return this.storage.getItem(key) || ''; } catch { return ''; } }
    get rows() { return this.history.filter(row => row.repo.toLowerCase() === this.settings.repo.toLowerCase()).sort((a, b) => b.request.id.localeCompare(a.request.id)).slice(0, 20); }
    get configured() { return Boolean(this.settings.pat && this.settings.repo); }
    changed() {
      this.history.sort((a, b) => b.request.id.localeCompare(a.request.id));
      this.history = this.history.slice(0, 20);
      this.storage.setItem(HISTORY_KEY, JSON.stringify(this.history));
      this.onChange(this);
    }
    saveSettings(settings) {
      const repo = settings.repo.trim() || DEFAULT_REPO;
      if (!/^[\w-]+\/[\w.-]+$/.test(repo) || repo.toLowerCase() === TARGET_REPO.toLowerCase()) throw new Error('비공개 요청 저장소를 owner/repo 형식으로 입력하세요.');
      const pat = settings.pat.trim();
      if (pat && !pat.startsWith('github_pat_')) throw new Error('요청 저장소만 선택한 Fine-grained GitHub 토큰을 사용하세요.');
      this.storage.setItem('cfg_bridge_repo', repo);
      this.storage.setItem('cfg_bridge_pat', pat);
      this.storage.setItem('cfg_bridge_agent', settings.agent === 'claude' ? 'claude' : 'codex');
      this.settings = {repo, pat, agent: settings.agent === 'claude' ? 'claude' : 'codex'};
      this.worker = null;
      this.lastError = '';
      this.syncedCommit = '';
      this.onChange(this);
    }
    async api(path, options = {}, publicRead = false) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      try {
        const headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', ...options.headers};
        if (!publicRead) headers.Authorization = 'Bearer ' + this.settings.pat;
        return await this.fetch('https://api.github.com' + path, {...options, headers, signal: controller.signal, cache: 'no-store'});
      } finally { clearTimeout(timer); }
    }
    async validate() {
      if (!this.settings.pat) throw new Error('편집 탭의 일정 요청 연결에 토큰을 저장해 주세요.');
      if (!this.settings.pat.startsWith('github_pat_')) throw new Error('요청 저장소 전용 Fine-grained 토큰이 필요합니다.');
      if (!/^[\w-]+\/[\w.-]+$/.test(this.settings.repo) || this.settings.repo.toLowerCase() === TARGET_REPO.toLowerCase()) throw new Error('비공개 요청 저장소 주소를 확인하세요.');
      const responses = await Promise.all([this.api('/user'), this.api('/repos/' + this.settings.repo)]);
      if (!responses[0].ok || !responses[1].ok) throw new Error('GitHub 연결 실패: 토큰, 저장소 선택 및 Contents 권한을 확인하세요.');
      const [user, repo] = await Promise.all(responses.map(response => response.json()));
      if (repo.private !== true) throw new Error('요청은 비공개 저장소에만 보낼 수 있습니다. 저장소를 Private으로 설정하세요.');
      if (!user.login || user.login.toLowerCase() !== String(repo.owner?.login).toLowerCase() || user.login.toLowerCase() !== this.settings.repo.split('/')[0].toLowerCase()) throw new Error('현재 GitHub 계정이 요청 저장소의 소유자여야 합니다.');
      if (repo.permissions?.push === false) throw new Error('요청 저장소 Contents 쓰기 권한이 필요합니다.');
      return user.login;
    }
    async readFile(path, {missing = false, publicRead = false, ref = ''} = {}) {
      const repo = publicRead ? TARGET_REPO : this.settings.repo;
      const response = await this.api('/repos/' + repo + '/contents/' + path + (ref ? '?ref=' + encodeURIComponent(ref) : ''), {}, publicRead);
      if (missing && response.status === 404) return null;
      if (!response.ok) throw new Error('GitHub 읽기 실패 (HTTP ' + response.status + ')');
      const file = await response.json();
      if (!file.content || file.encoding !== 'base64') throw new Error('GitHub 파일 응답을 읽을 수 없습니다.');
      return decode(file);
    }
    async verifyExisting(row) {
      const existing = await this.readFile('requests/' + row.request.id + '.json', {missing: true});
      if (!existing) return false;
      if (existing.id !== row.request.id || existing.text !== row.request.text || existing.parent_id !== row.request.parent_id || existing.agent !== row.request.agent) throw new Error('같은 요청 ID에 다른 내용이 있습니다. 재전송을 중단했습니다.');
      return true;
    }
    async deliver(row, retry = false) {
      let uncertain = retry;
      for (let attempt = 0; attempt < 2; attempt++) {
        if (uncertain) {
          try { if (await this.verifyExisting(row)) return this.receipt(row); }
          catch (error) { row.delivery = 'uncertain'; row.error = messageFor(error); this.changed(); throw error; }
        }
        let response;
        try {
          response = await this.api('/repos/' + this.settings.repo + '/contents/requests/' + row.request.id + '.json', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message: 'Queue schedule request ' + row.request.id, content: encode(row.request)})
          });
        } catch (error) {
          row.error = messageFor(error);
          uncertain = true;
          if (attempt === 0) continue;
          row.delivery = 'uncertain';
          this.changed();
          throw new Error('접수 여부를 확인하지 못했습니다. 아래 같은 요청 재확인으로 이어서 확인하세요.');
        }
        if (response.ok) return this.receipt(row);
        if (response.status >= 500 || response.status === 409 || response.status === 422) {
          uncertain = true;
          row.error = 'GitHub 응답 확인 필요 (HTTP ' + response.status + ')';
          if (attempt === 0) continue;
          try { if (await this.verifyExisting(row)) return this.receipt(row); } catch { /* keep the same ID */ }
          row.delivery = 'uncertain';
        } else {
          row.delivery = 'rejected';
          row.error = '접수 실패 (HTTP ' + response.status + '). 토큰의 Contents 쓰기 권한을 확인하세요.';
        }
        this.changed();
        throw new Error(row.error);
      }
    }
    receipt(row) { row.delivery = 'queued'; row.error = ''; this.changed(); return row; }
    async submit(text, parentId = null, agent = this.settings.agent) {
      text = text.trim();
      if (!text) throw new Error('요청 내용을 입력하세요.');
      if ([...text].length > 6000) throw new Error('요청은 6,000자 이내로 입력하세요.');
      if (parentId && !ID_PATTERN.test(parentId)) throw new Error('원래 요청 ID를 확인할 수 없습니다.');
      if (!['codex', 'claude'].includes(agent)) throw new Error('처리 도구 설정을 확인하세요.');
      if (this.sending) throw new Error('요청을 보내는 중입니다.');
      this.sending = true;
      this.onChange(this);
      try {
        await this.validate();
        const pending = this.rows.find(row => ['sending', 'uncertain', 'rejected'].includes(row.delivery) && row.request.text === text && row.request.parent_id === parentId && row.request.agent === agent);
        if (pending) return await this.deliver(pending, true);
        const date = this.now();
        const request = {version: 1, id: requestId(date, this.uuid()), text, agent, created_at: date.toISOString(), today: todayKST(date), timezone: 'Asia/Seoul', parent_id: parentId};
        const row = {repo: this.settings.repo, request, delivery: 'sending', result: null};
        this.history.unshift(row);
        this.changed(); // Persist the immutable ID before any network write.
        return await this.deliver(row);
      } finally { this.sending = false; this.onChange(this); }
    }
    async retry(id) {
      if (this.sending) throw new Error('요청을 보내는 중입니다.');
      const row = this.rows.find(row => row.request.id === id);
      if (!row) throw new Error('요청 내역을 찾을 수 없습니다.');
      if (!['sending', 'uncertain', 'rejected'].includes(row.delivery)) throw new Error('접수된 요청은 같은 ID로 다시 처리하지 않습니다.');
      this.sending = true;
      this.onChange(this);
      try { await this.validate(); return await this.deliver(row, true); }
      finally { this.sending = false; this.onChange(this); }
    }
    async resend(id) {
      const row = this.rows.find(row => row.request.id === id);
      if (!row || row.result?.state !== 'failed') throw new Error('처리에 실패한 요청만 새 요청으로 다시 보낼 수 있습니다.');
      return this.submit(row.request.text, row.request.parent_id || null, row.request.agent);
    }
    async discover() {
      const response = await this.api('/repos/' + this.settings.repo + '/contents/requests');
      if (response.status === 404) return;
      if (!response.ok) throw new Error('요청 내역 읽기 실패 (HTTP ' + response.status + ')');
      const files = await response.json();
      if (!Array.isArray(files)) throw new Error('요청 폴더 형식을 확인하세요.');
      const names = files.filter(file => file.type === 'file' && ID_PATTERN.test(file.name?.replace(/\.json$/, '')) && file.name.endsWith('.json')).map(file => file.name).sort().reverse().slice(0, 20);
      await Promise.all(names.map(async name => {
        if (this.rows.some(row => row.request.id + '.json' === name)) return;
        const request = await this.readFile('requests/' + name);
        if (request.version !== 1 || request.id + '.json' !== name || typeof request.text !== 'string') return;
        this.history.push({repo: this.settings.repo, request, delivery: 'queued', result: null});
      }));
    }
    async syncCompleted() {
      const latest = this.rows.filter(row => row.result?.state === 'completed' && /^[a-f0-9]{40}$/i.test(row.result.commit_sha || '')).sort((a, b) => String(b.result.updated_at).localeCompare(String(a.result.updated_at)))[0];
      if (!latest || latest.result.commit_sha === this.syncedCommit) return;
      latest.syncState = 'syncing';
      this.onChange(this);
      try {
        // Use one current public revision, retaining manual edits made after this result.
        const headResponse = await this.api('/repos/' + TARGET_REPO + '/commits/main', {}, true);
        if (!headResponse.ok) throw new Error('최신 일정 버전 확인 실패 (HTTP ' + headResponse.status + ')');
        const head = await headResponse.json();
        if (!/^[a-f0-9]{40}$/i.test(head.sha || '')) throw new Error('최신 일정 버전을 확인할 수 없습니다.');
        if (head.sha !== latest.result.commit_sha) {
          const compareResponse = await this.api('/repos/' + TARGET_REPO + '/compare/' + latest.result.commit_sha + '...' + head.sha, {}, true);
          if (!compareResponse.ok) throw new Error('완료 요청의 일정 반영 확인 대기 중');
          const comparison = await compareResponse.json();
          if (!['ahead', 'identical'].includes(comparison.status)) throw new Error('최신 일정에 완료 요청이 포함되는지 확인할 수 없습니다.');
        }
        const [events, travel] = await Promise.all(['events.json', 'travel.json'].map(path => this.readFile(path, {publicRead: true, ref: head.sha})));
        await this.onCompleted(events, travel);
        this.syncedCommit = latest.result.commit_sha;
        latest.syncState = 'synced';
        latest.syncError = '';
      } catch (error) { latest.syncState = 'failed'; latest.syncError = '일정 화면 동기화 대기: ' + messageFor(error); }
    }
    async refresh({discover = false} = {}) {
      if (!this.configured || this.refreshing || this.sending) return;
      this.refreshing = true;
      this.onChange(this);
      try {
        await this.validate();
        this.lastError = '';
        if (discover) await this.discover();
        const reads = [this.readFile('worker.json', {missing: true}).then(worker => { this.worker = worker; })];
        for (const row of this.rows) {
          if (row.delivery !== 'queued' || ['completed', 'failed', 'needs_input'].includes(row.result?.state)) continue;
          reads.push(this.readFile('results/' + row.request.id + '.json', {missing: true}).then(result => {
            if (result && (result.version !== 1 || result.id !== row.request.id || !RESULT_STATES.has(result.state))) throw new Error('처리 결과 형식을 확인할 수 없습니다.');
            row.result = result;
          }));
        }
        const readsDone = await Promise.allSettled(reads);
        const failed = readsDone.find(item => item.status === 'rejected');
        if (failed) this.lastError = messageFor(failed.reason);
        await this.syncCompleted();
        this.changed();
      } catch (error) { this.lastError = messageFor(error); }
      finally { this.refreshing = false; this.onChange(this); }
    }
  }

  function workerStatus(client) {
    if (!client.configured) return '연결 설정을 저장해 주세요.';
    if (client.lastError) return '연결 확인 필요 · ' + client.lastError;
    const worker = client.worker;
    if (!worker || worker.version !== 1 || worker.target_repo !== TARGET_REPO || !['codex', 'claude'].includes(worker.agent)) return 'PC 작업기 연결 미확인 · PC에서 작업기를 실행해야 처리됩니다.';
    const age = client.now().getTime() - Date.parse(worker.updated_at);
    if (!Number.isFinite(age) || age > 120000 || age < -60000) return 'PC 작업기 오프라인 또는 지연 · 요청은 대기하며, PC 연결 후 처리됩니다.';
    const supported = Array.isArray(worker.agents) ? worker.agents : [worker.agent];
    const selectedName = client.settings.agent === 'claude' ? 'Claude Code' : 'Codex';
    if (!supported.includes(client.settings.agent)) return 'PC에 ' + selectedName + ' 설정이 필요합니다.';
    return 'PC ' + selectedName + ' 연결됨 · 약 20초마다 상태 확인';
  }
  function rowStatus(row) {
    if (row.delivery === 'sending') return ['sending', '전송 중 · 접수 전'];
    if (row.delivery === 'uncertain') return ['uncertain', '접수 여부 확인 필요'];
    if (row.delivery === 'rejected') return ['failed', '접수 실패'];
    const state = row.result?.state || 'queued';
    const labels = {queued: '접수됨 · 처리 대기', processing: '처리 중', completed: '처리 완료', needs_input: '추가 확인 필요', failed: '처리 실패'};
    return [RESULT_STATES.has(state) ? state : 'queued', labels[state] || labels.queued];
  }
  function renderHistory(rows) {
    if (!rows.length) return '<p class="bridge-empty">아직 요청이 없습니다. 위 입력창에서 일정을 요청해 보세요.</p>';
    return rows.map(row => {
      const [state, label] = rowStatus(row);
      const result = row.result || {};
      const questions = Array.isArray(result.questions) ? result.questions : [];
      const warnings = Array.isArray(result.warnings) ? result.warnings : [];
      const id = esc(row.request.id);
      return '<article class="bridge-request"><div class="bridge-request-head"><span class="bridge-badge bridge-' + state + '">' + label + '</span><span class="bridge-meta">' + esc(row.request.agent) + ' · ' + esc(new Date(row.request.created_at).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'})) + '</span></div>' +
        '<p class="bridge-request-text">' + esc(row.request.text) + '</p>' +
        (row.request.parent_id ? '<p class="bridge-meta">추가 답변 요청</p>' : '') +
        (row.error ? '<p class="bridge-feedback bridge-error">' + esc(row.error) + '</p>' : '') +
        (result.message ? '<p class="bridge-result">' + esc(result.message) + '</p>' : '') +
        (questions.length ? '<ul class="bridge-questions">' + questions.map(q => '<li>' + esc(q) + '</li>').join('') + '</ul>' : '') +
        (warnings.length ? '<ul class="bridge-warnings">' + warnings.map(w => '<li>' + esc(w) + '</li>').join('') + '</ul>' : '') +
        (result.state === 'completed' && result.commit_sha ? '<p class="bridge-meta">' + (row.syncState === 'synced' ? '일정 화면에 반영됨' : row.syncState === 'syncing' ? '완료된 커밋으로 일정 화면 동기화 중' : row.syncError ? esc(row.syncError) : '최신 완료 요청 기준으로 일정 화면을 동기화합니다.') + '</p>' : '') +
        (['uncertain', 'rejected'].includes(row.delivery) ? '<button class="bridge-secondary" type="button" data-bridge-action="retry" data-request-id="' + id + '">같은 요청 재확인 · 재전송</button>' : '') +
        (result.state === 'failed' ? '<button class="bridge-secondary" type="button" data-bridge-action="resend" data-request-id="' + id + '">새 요청으로 다시 보내기</button>' : '') +
        (result.state === 'needs_input' ? '<div class="bridge-reply"><label for="bridge-reply-' + id + '">추가 답변</label><textarea class="edit-textarea" id="bridge-reply-' + id + '" placeholder="확인 질문에 답해 주세요"></textarea><button type="button" class="btn-primary" data-bridge-action="reply" data-request-id="' + id + '">답변 보내기</button></div>' : '') +
        '</article>';
    }).join('');
  }

  function mount(root) {
    const document = root.document;
    const byId = id => document.getElementById(id);
    const notify = (id, message, error = false) => {
      const element = byId(id);
      if (!element) return;
      element.textContent = message;
      element.style.display = message ? 'block' : 'none';
      element.classList.toggle('bridge-error', error);
    };
    let client;
    let view = document.querySelector('.view.on')?.id?.replace('v-', '') || 'home';
    let timer;
    let renderSignature = '';
    function render() {
      if (!client) return;
      notify('bridge-worker', workerStatus(client));
      byId('bridge-count').textContent = client.rows.length;
      const history = renderHistory(client.rows);
      if (history !== renderSignature) {
        const drafts = new Map(Array.from(byId('bridge-history').querySelectorAll('textarea')).map(el => [el.id, el.value]));
        const active = document.activeElement;
        const focusedId = drafts.has(active?.id) ? active.id : null;
        const selection = focusedId ? [active.selectionStart, active.selectionEnd] : null;
        byId('bridge-history').innerHTML = history;
        drafts.forEach((value, id) => { if (byId(id)) byId(id).value = value; });
        if (focusedId && byId(focusedId)) { byId(focusedId).focus(); byId(focusedId).setSelectionRange(...selection); }
        renderSignature = history;
      }
      byId('nl-btn').disabled = client.sending;
      byId('cal-nl-btn').disabled = client.sending;
      document.querySelectorAll('[data-bridge-action="reply"],[data-bridge-action="retry"],[data-bridge-action="resend"],[data-bridge-action="settings-save"],[data-bridge-action="connection-test"]').forEach(el => { el.disabled = client.sending || client.refreshing; });
      document.querySelectorAll('[data-bridge-action="refresh"]').forEach(el => { el.disabled = client.refreshing || client.sending; });
      const count = client.rows.filter(row => row.delivery === 'queued' && !['completed', 'failed'].includes(row.result?.state)).length;
      byId('bridge-cal-link').textContent = count ? '요청 ' + count + '건 · 내역 보기' : '요청 내역 · 연결 설정';
    }
    try {
      client = new BridgeClient({fetch: root.fetch.bind(root), storage: root.localStorage, onChange: render,
        onCompleted: async (events, travel) => { await root.scheduleDataReady; root.applyBridgeSnapshot(events, travel); }});
    } catch {
      notify('bridge-status', '브라우저 저장소를 사용할 수 없습니다. 브라우저의 사이트 저장 설정을 확인하세요.', true);
      return {submitFrom: async () => notify('bridge-status', '요청 연결을 사용할 수 없습니다.', true), onView() {}};
    }
    byId('cfg-bridge-repo').value = client.settings.repo;
    byId('cfg-bridge-pat').value = client.settings.pat;
    byId('cfg-bridge-agent').value = client.settings.agent;
    function schedule() {
      clearInterval(timer);
      if (!document.hidden && client.configured && ['edit', 'cal'].includes(view)) timer = setInterval(() => client.refresh(), 20000);
    }
    async function refresh(discover = true) {
      await client.refresh({discover});
      if (client.lastError) notify('bridge-status', client.lastError, true);
      else notify('bridge-status', '요청 상태를 확인했습니다.');
    }
    async function submitFrom(source) {
      const cal = source === 'cal';
      const input = byId(cal ? 'cal-nl-input' : 'nl-input');
      const status = cal ? 'cal-nl-status' : 'nl-status';
      const text = input.value;
      notify(status, '요청 전송 중 · 접수 확인 전');
      try {
        await client.submit(text);
        if (input.value === text) input.value = '';
        notify(status, '접수됨 · 편집 탭에서 처리 결과를 확인하세요.');
        if (!document.hidden) await refresh(false);
      } catch (error) { notify(status, messageFor(error), true); }
      schedule();
    }
    document.addEventListener('click', async event => {
      const button = event.target.closest('[data-bridge-action]');
      if (!button || button.disabled) return;
      const action = button.dataset.bridgeAction;
      const status = action.startsWith('settings') || action === 'connection-test' ? 'bridge-settings-status' : 'bridge-status';
      try {
        if (action === 'settings-save') {
          client.saveSettings({repo: byId('cfg-bridge-repo').value, pat: byId('cfg-bridge-pat').value, agent: byId('cfg-bridge-agent').value});
          notify(status, '이 브라우저에 연결 설정을 저장했습니다.');
          schedule();
        } else if (action === 'connection-test') {
          button.disabled = true;
          notify(status, '저장된 연결 설정 확인 중…');
          const user = await client.validate();
          await client.refresh();
          notify(status, user + ' · 비공개 요청 저장소 읽기 연결 확인. 실제 쓰기 권한은 요청 전송 시 확인합니다.');
        } else if (action === 'refresh') await refresh();
        else if (action === 'resend') {
          await client.resend(button.dataset.requestId);
          notify(status, '새 요청으로 접수했습니다.');
          await refresh(false);
        }
        else if (action === 'retry') {
          await client.retry(button.dataset.requestId);
          notify(status, '같은 요청 ID의 접수를 확인했습니다.');
          await refresh(false);
        } else if (action === 'reply') {
          const input = byId('bridge-reply-' + button.dataset.requestId);
          const text = input.value;
          await client.submit(text, button.dataset.requestId);
          const currentInput = byId(input.id);
          if (currentInput?.value === text) currentInput.value = '';
          notify(status, '추가 답변을 접수했습니다.');
          await refresh(false);
        }
      } catch (error) { notify(status, messageFor(error), true); }
      finally { button.disabled = false; render(); }
    });
    document.addEventListener('visibilitychange', () => { schedule(); if (!document.hidden && ['edit', 'cal'].includes(view)) refresh(false); });
    render();
    schedule();
    return {client, submitFrom, onView(next) { view = next; schedule(); if (!document.hidden && ['edit', 'cal'].includes(view)) refresh(true); }};
  }
  return {BridgeClient, todayKST, requestId, rowStatus, workerStatus, renderHistory, mount};
});
