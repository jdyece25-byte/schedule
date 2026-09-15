(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.ScheduleSchool = api.mount(root);
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const REPO = 'jdyece25-byte/schedule-requests';
  const ACTIVE = new Set(['needs_review', 'ready', 'conflict', 'info']);
  const STATES = {baseline: '기준 자료', needs_review: '확인 필요', ready: '반영 확인', applied: '반영 완료', ignored: '확인 완료', info: '새 공지', conflict: '일정 충돌 · 확인 필요'};
  const TYPES = {deadline: '마감', exam: '시험', lab: '실험·랩', class: '수업', cancellation: '휴강·취소'};
  const COLLECTORS = {local: '학교 공지', etl: 'eTL'};
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const encode = value => btoa(Array.from(new TextEncoder().encode(JSON.stringify(value, null, 2)), b => String.fromCharCode(b)).join(''));
  const decode = file => JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(file.content.replace(/\s/g, '')), c => c.charCodeAt(0))));
  const canonical = value => JSON.stringify(value, function (key, item) { return item && typeof item === 'object' && !Array.isArray(item) ? Object.fromEntries(Object.keys(item).sort().map(k => [k, item[k]])) : item; });
  const uuidOK = value => /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(value || '');
  const shortText = (value, max) => typeof value === 'string' ? value.trim().slice(0, max) : '';
  const validDate = value => /^\d{4}-\d{2}-\d{2}$/.test(value || '') && Number.isFinite(new Date(value + 'T00:00:00Z').getTime()) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  function safeSourceURL(value) {
    try {
      const url = new URL(value);
      if (url.protocol !== 'https:' || (url.hostname !== 'snu.ac.kr' && !url.hostname.endsWith('.snu.ac.kr')) || url.username || url.password || (url.port && url.port !== '443')) return '';
      const allowed = new Set(['id', 'd', 'f', 'a', 'courseid', 'boardId', 'bbsNo', 'nttId', 'nttNo', 'menuNo']);
      const safe = new URL(url.origin + url.pathname);
      for (const [key, val] of url.searchParams) if (allowed.has(key) && /^[A-Za-z0-9_-]{1,80}$/.test(val)) safe.searchParams.append(key, val);
      return safe.href;
    } catch { return ''; }
  }
  function clockValue(minutes) {
    return Number.isInteger(minutes) && minutes >= 0 && minutes <= 1440 ? String(Math.floor(minutes / 60)).padStart(2, '0') + ':' + String(minutes % 60).padStart(2, '0') : '';
  }
  function parseClock(value, label) {
    if (value === '') return undefined;
    if (!/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value) && value !== '24:00') throw new Error(label + '은 09:30 또는 24:00 형식으로 입력해 주세요.');
    const [h, m] = value.split(':').map(Number); return h * 60 + m;
  }
  function timestamp(value) {
    const date = new Date(value);
    return value && Number.isFinite(date.getTime()) ? date.toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit'}) : '확인 기록 없음';
  }
  function collectorText(index, now = new Date()) {
    if (!index) return '수집 대기 · 아직 학교 공지 목록이 없습니다.';
    return Object.entries(COLLECTORS).map(([key, label]) => {
      const c = index.collectors?.[key];
      if (!c) return label + ': 수집 대기';
      const state = c.state === 'auth_required' ? '다시 로그인 필요' : c.state === 'error' ? '수집 오류 · 확인 필요' : c.state === 'partial' ? '일부 수집 · 확인 필요' : ['running', 'checking'].includes(c.state) ? '수집 중' : ['ok', 'idle', 'success', 'healthy'].includes(c.state) ? '마지막 확인' : '수집 상태 확인 필요';
      const checked = new Date(c.last_checked).getTime();
      const stale = Number.isFinite(checked) && now.getTime() - checked > 24 * 60 * 60 * 1000 ? ' · 24시간 이상 갱신 없음' : '';
      return label + ': ' + state + ' · ' + timestamp(c.last_checked) + stale;
    }).join('\n');
  }
  function cleanEvent(candidate, input) {
    if (typeof candidate.id !== 'string' || !candidate.id) throw new Error('일정 후보의 식별 정보가 없습니다. 목록을 새로고침해 주세요.');
    const action = candidate.action || 'add';
    if (!['add', 'update', 'delete'].includes(action)) throw new Error('지원하지 않는 변경 종류입니다. 원문과 일정을 확인해 주세요.');
    if ((action !== 'add' || candidate.kind === 'cancellation') && !shortText(candidate.target_id, 200)) throw new Error('수정·삭제할 기존 일정이 정해지지 않았습니다. 자연어 일정 요청으로 확인해 주세요.');
    if (candidate.kind === 'cancellation' && action !== 'delete') throw new Error('휴강·취소의 삭제 대상이 정해지지 않았습니다.');
    if (action === 'delete') {
      if (!validDate(candidate.event?.d) || !shortText(candidate.event?.n, 300)) throw new Error('삭제할 기존 일정의 날짜와 이름을 확인할 수 없습니다.');
      // Deletion selects this exact candidate; editable fields can never redirect it.
      return {id: candidate.id, event: JSON.parse(JSON.stringify(candidate.event)), action, target_id: candidate.target_id};
    }
    if (!validDate(input.d)) throw new Error('선택한 일정의 날짜를 정확히 입력해 주세요.');
    const name = shortText(input.n, 300);
    if (!name) throw new Error('선택한 일정의 이름을 입력해 주세요.');
    const type = candidate.event?.t || (candidate.kind === 'cancellation' ? 'class' : candidate.kind);
    if (!['deadline', 'exam', 'lab', 'class'].includes(type)) throw new Error('지원하지 않는 일정 유형입니다.');
    const event = {d: input.d, n: name, t: type};
    if (typeof candidate.event?.id === 'string' && candidate.event.id) event.id = candidate.event.id;
    const start = parseClock(input.s ?? '', '시작 시각');
    const end = parseClock(input.e ?? '', '종료 시각');
    if (start !== undefined) event.s = start;
    if (end !== undefined) event.e = end;
    if (action === 'update') {
      if (start === undefined) event.s = null;
      if (end === undefined) event.e = null;
      event.ti = '';
    }
    if (end !== undefined && start === undefined) throw new Error('종료 시각이 있으면 시작 시각도 입력해 주세요.');
    if (start !== undefined && end !== undefined && end <= start) throw new Error('종료 시각은 시작 시각보다 뒤여야 합니다. 자정은 24:00으로 입력하세요.');
    if (typeof input.loc === 'string') event.loc = shortText(input.loc, 300);
    // Preserve the semantic location id only when its visible location was not edited.
    if (candidate.event?.lid && event.loc === (candidate.event.loc || '')) event.lid = candidate.event.lid;
    else if (action === 'update') event.lid = '';
    event.status = input.tentative === true ? 'tentative' : 'confirmed';
    return {id: candidate.id, event, action, ...(candidate.target_id ? {target_id: candidate.target_id} : {})};
  }
  class SchoolClient {
    constructor(root, options = {}) {
      this.root = root; this.storage = options.storage || root.localStorage; this.fetch = options.fetch || root.fetch.bind(root);
      this.now = options.now || (() => new Date()); this.uuid = options.uuid || (() => root.crypto.randomUUID());
      this.timeoutMs = options.timeoutMs || 15000; this.index = null; this.indexCredentials = null; this.epoch = 0; this.busy = false; this.pending = new Map(); this.onChange = options.onChange || (() => {});
    }
    read(key) { try { return this.storage.getItem(key) || ''; } catch { return ''; } }
    credentials() {
      if ((this.read('cfg_bridge_repo') || REPO).toLowerCase() !== REPO.toLowerCase()) throw new Error('학교 공지는 비공개 schedule-requests 저장소를 사용합니다. 편집·설정의 일정 요청 연결을 확인해 주세요.');
      const pat = this.read('cfg_bridge_pat').trim();
      if (!pat.startsWith('github_pat_')) throw new Error('이 기기의 편집·설정 → 일정 요청 연결에 요청 전용 GitHub 토큰을 저장해 주세요.');
      return {pat, repo: REPO};
    }
    credentialsMatch(credentials) { try { const current = this.credentials(); return current.pat === credentials.pat && current.repo === credentials.repo; } catch { return false; } }
    invalidate() { this.index = null; this.indexCredentials = null; this.epoch++; }
    async api(path, credentials, options = {}) {
      const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), this.timeoutMs);
      try { return await this.fetch('https://api.github.com' + path, {...options, cache: 'no-store', signal: controller.signal, headers: {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', Authorization: 'Bearer ' + credentials.pat, ...options.headers}}); }
      finally { clearTimeout(timer); }
    }
    async validate(credentials) {
      const responses = await Promise.all([this.api('/user', credentials), this.api('/repos/' + REPO, credentials)]);
      if (responses.some(r => !r.ok)) throw new Error('학교 공지 연결 실패: 요청 저장소 선택과 Contents 읽기·쓰기 권한을 확인해 주세요.');
      const [user, repo] = await Promise.all(responses.map(r => r.json()));
      if (repo.private !== true) throw new Error('학교 공지는 비공개 저장소에서만 읽고 처리할 수 있습니다.');
      if (user.login?.toLowerCase() !== 'jdyece25-byte' || repo.owner?.login?.toLowerCase() !== 'jdyece25-byte' || repo.permissions?.push === false) throw new Error('비공개 요청 저장소 소유자의 Contents 읽기·쓰기 권한이 필요합니다.');
    }
    async file(path, credentials) {
      const response = await this.api('/repos/' + REPO + '/contents/' + path, credentials);
      if (response.status === 404) return null;
      if (!response.ok) throw new Error('학교 공지 읽기 실패 (HTTP ' + response.status + ')');
      const file = await response.json();
      if (file.encoding !== 'base64' || typeof file.content !== 'string') throw new Error('학교 공지 파일 응답을 읽지 못했습니다.');
      return decode(file);
    }
    async refresh() {
      if (this.busy) throw new Error('학교 공지 처리 요청을 확인 중입니다.');
      this.busy = true; const epoch = this.epoch;
      try {
        const credentials = this.credentials();
        const assertCurrent = () => { if (epoch !== this.epoch || !this.credentialsMatch(credentials)) throw new Error('학교 공지 연결 설정이 변경되었습니다. 새 설정으로 다시 확인해 주세요.'); };
        await this.validate(credentials);
        assertCurrent();
        const index = await this.file('school/index.json', credentials);
        if (index && (index.version !== 1 || !Array.isArray(index.items))) throw new Error('학교 공지 목록 형식을 확인할 수 없습니다.');
        assertCurrent();
        const resolved = [];
        if (index) for (const [source, row] of this.pending) {
          const current = index.items.find(item => item.id === source);
          if (current && (current.content_hash !== row.decision.source_hash || !ACTIVE.has(current.state))) resolved.push(source);
          else if (row.state === 'accepted') {
            const outcome = await this.file('school/decision-results/' + row.decision.id + '.json', credentials);
            assertCurrent();
            if (outcome?.version === 1 && ['completed', 'conflict'].includes(outcome.state)) resolved.push(source);
          }
        }
        assertCurrent();
        this.index = index; this.indexCredentials = credentials;
        resolved.forEach(source => this.pending.delete(source));
        return index;
      } catch (error) { this.invalidate(); throw error;
      } finally { this.busy = false; }
    }
    makeDecision(item, action, choices = []) {
      if (!item || typeof item.id !== 'string' || !item.id || typeof item.content_hash !== 'string' || !item.content_hash) throw new Error('원문 식별 정보가 없습니다. 목록을 새로고침해 주세요.');
      if (!ACTIVE.has(item.state)) throw new Error('이미 처리했거나 기준 자료인 공지입니다. 새 공지를 확인해 주세요.');
      if (!['approve', 'ignore'].includes(action)) throw new Error('지원하지 않는 처리 요청입니다.');
      if (action === 'approve' && !choices.length) throw new Error('반영할 일정을 먼저 선택해 주세요.');
      if (choices.length > 30) throw new Error('한 번에 선택할 수 있는 일정은 30개까지입니다.');
      const candidates = choices.map(choice => {
        const candidate = item.candidates?.find(c => c.id === choice.id);
        if (!candidate) throw new Error('선택한 일정이 원문에 없습니다. 새로고침해 주세요.');
        return cleanEvent(candidate, choice.input);
      });
      if (new Set(candidates.map(c => c.id)).size !== candidates.length) throw new Error('같은 일정을 여러 번 선택할 수 없습니다.');
      const id = this.uuid(); if (!uuidOK(id)) throw new Error('처리 요청 ID를 생성하지 못했습니다.');
      return {version: 1, id, source_id: item.id, source_hash: item.content_hash, action, candidates: action === 'approve' ? candidates : [], created_at: this.now().toISOString()};
    }
    async verify(row, credentials) {
      const existing = await this.file('school/decisions/' + row.decision.id + '.json', credentials);
      if (!existing) return false;
      if (canonical(existing) !== canonical(row.decision)) throw new Error('같은 요청 ID의 내용이 달라 전송을 중단했습니다.');
      return true;
    }
    receipt(row) { row.state = 'accepted'; row.error = ''; return row; }
    async deliver(row, credentials, retry) {
      const path = '/repos/' + REPO + '/contents/school/decisions/' + row.decision.id + '.json';
      const body = JSON.stringify({message: 'Queue school notice decision ' + row.decision.id, content: encode(row.decision)});
      let uncertain = retry;
      for (let attempt = 0; attempt < 2; attempt++) {
        if (uncertain && await this.verify(row, credentials)) return this.receipt(row);
        let response;
        try { response = await this.api(path, credentials, {method: 'PUT', headers: {'Content-Type': 'application/json'}, body}); }
        catch { uncertain = true; if (attempt === 0) continue; row.state = 'uncertain'; throw new Error('접수 여부를 확인하지 못했습니다. 같은 요청 상태 확인을 눌러 주세요.'); }
        if (response.ok) return this.receipt(row);
        if ([409, 422].includes(response.status) || response.status >= 500) {
          uncertain = true; row.state = 'uncertain';
          if (attempt === 0) continue;
          if (await this.verify(row, credentials)) return this.receipt(row);
          throw new Error('접수 여부를 확인하지 못했습니다. 같은 요청 상태 확인을 눌러 주세요.');
        }
        row.state = 'rejected'; throw new Error('처리 요청 전송 실패 (HTTP ' + response.status + '). Contents 쓰기 권한을 확인해 주세요.');
      }
    }
    async decide(item, action, choices = []) {
      if (this.busy) throw new Error('처리 요청을 보내는 중입니다.');
      const previous = this.pending.get(item.id);
      if (previous) throw new Error(previous.state === 'accepted' ? '처리 요청이 이미 접수되었습니다. 목록 새로고침으로 반영 결과를 확인해 주세요.' : '앞선 요청의 접수 여부를 먼저 확인해 주세요.');
      const credentials = this.credentials();
      const decision = this.makeDecision(item, action, choices);
      this.busy = true;
      const row = {decision, state: 'sending', error: ''};
      try { await this.validate(credentials); this.pending.set(item.id, row); return await this.deliver(row, credentials, false); }
      catch (error) { if (row.state === 'sending') row.state = 'uncertain'; row.error = error.message; throw error; }
      finally { this.busy = false; }
    }
    async retry(sourceId) {
      if (this.busy) throw new Error('처리 요청을 보내는 중입니다.');
      const row = this.pending.get(sourceId); if (!row) throw new Error('재확인할 요청이 없습니다.');
      const credentials = this.credentials(); this.busy = true;
      try { await this.validate(credentials); return await this.deliver(row, credentials, true); }
      catch (error) { row.error = error.message; throw error; }
      finally { this.busy = false; }
    }
  }
  function candidateHTML(candidate, index) {
    const event = candidate.event || {};
    const deletion = candidate.action === 'delete' || candidate.kind === 'cancellation';
    const readonly = deletion ? ' readonly' : '';
    const invalidTarget = (deletion || candidate.action === 'update') && !candidate.target_id;
    if (candidate.action === 'link') return '<div class="school-candidate"><p class="school-note">기존 일정과 연결됨 · ' + esc([event.n, event.d, clockValue(event.s), event.loc].filter(Boolean).join(' · ')) + '</p></div>';
    return '<fieldset class="school-candidate" data-candidate-index="' + index + '">' +
      '<legend><label><input type="checkbox" data-school-field="selected"' + (invalidTarget ? ' disabled' : '') + '> ' + esc(deletion ? '기존 일정 삭제' : candidate.action === 'update' ? '기존 일정 수정' : '새 일정 추가') + ' · ' + esc(TYPES[candidate.kind] || '일정') + '</label></legend>' +
      (invalidTarget ? '<p class="school-warning">대상 일정이 정해지지 않아 선택할 수 없습니다. 자연어 요청으로 확인해 주세요.</p>' : '') +
      (deletion && !invalidTarget ? '<p class="school-warning">선택하면 아래의 기존 일정이 삭제됩니다.</p>' : '') +
      '<div class="school-fields"><label>날짜<input class="edit-input" type="date" data-school-field="d" value="' + esc(validDate(event.d) ? event.d : '') + '"' + readonly + '></label>' +
      '<label class="school-wide">일정명<input class="edit-input" maxlength="300" data-school-field="n" value="' + esc(event.n || '') + '"' + readonly + '></label>' +
      '<label>시작·마감 시각<input class="edit-input" inputmode="numeric" placeholder="09:30 · 미정은 빈칸" data-school-field="s" value="' + esc(clockValue(event.s)) + '"' + readonly + '></label>' +
      '<label>종료 시각<input class="edit-input" inputmode="numeric" placeholder="10:45 · 자정 24:00" data-school-field="e" value="' + esc(clockValue(event.e)) + '"' + readonly + '></label>' +
      '<label class="school-wide">장소<input class="edit-input" maxlength="300" data-school-field="loc" value="' + esc(event.loc || '') + '"' + readonly + '></label></div>' +
      '<label class="school-tentative"' + (deletion ? ' hidden' : '') + '><input type="checkbox" data-school-field="tentative"' + (event.status === 'tentative' || candidate.confidence !== 'high' ? ' checked' : '') + '> 확인 필요 상태로 등록</label>' +
      (candidate.reason ? '<p class="school-note">' + esc(candidate.reason) + '</p>' : '') +
      (candidate.evidence ? '<details class="school-evidence"><summary>원문 근거 확인</summary><p>' + esc(typeof candidate.evidence === 'string' ? candidate.evidence : JSON.stringify(candidate.evidence)) + '</p></details>' : '') + '</fieldset>';
  }
  function noticeHTML(item, index, row, history = false) {
    const active = ACTIVE.has(item.state) && !history;
    const url = safeSourceURL(item.source_url);
    const candidates = Array.isArray(item.candidates) ? item.candidates : [];
    const pending = active && row && row.state === 'accepted' && row.decision.source_hash === item.content_hash;
    return '<article class="school-notice" data-source-index="' + index + '"><div class="school-notice-top"><span class="school-badge">' + esc(pending ? '처리 요청 접수' : STATES[item.state] || '확인 필요') + '</span><span class="school-time">' + esc(timestamp(item.updated_at)) + '</span></div>' +
      '<p class="school-course">' + esc(item.course || '학교 공지') + '</p><h3>' + esc(item.title || '제목 없는 공지') + '</h3>' +
      (url ? '<a class="school-source" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">학교 원문 열기 ↗</a>' : '<p class="school-note">확인 가능한 학교 원문 링크가 없습니다.</p>') +
      (item.reason ? '<p class="school-note">' + esc(item.reason) + '</p>' : '') +
      (active && !pending ? candidates.map(candidateHTML).join('') : candidates.map(c => '<p class="school-note">' + esc([c.event?.n, c.event?.d, clockValue(c.event?.s), c.event?.loc].filter(Boolean).join(' · ')) + '</p>').join('')) +
      (row?.error ? '<p class="school-warning">' + esc(row.error) + '</p>' : '') +
      (pending ? '<p class="school-note">처리 요청이 접수되었습니다. 아직 일정 반영 완료가 아닙니다. 목록을 새로고침해 결과를 확인하세요.</p>' : active ? '<div class="school-actions">' +
      (row ? '<button type="button" class="bridge-secondary" data-school-action="retry">같은 요청 상태 확인</button>' : (candidates.length ? '<button type="button" class="btn-primary" data-school-action="approve">선택한 일정 반영 요청</button>' : '') + '<button type="button" class="bridge-secondary" data-school-action="ignore">' + (candidates.length ? '반영하지 않고 확인 완료' : '확인했어요') + '</button>') +
      '<button type="button" class="bridge-secondary" data-school-action="ask">자연어 일정 요청으로 열기</button></div>' : '') + '</article>';
  }
  function render(index, pending, filter = 'review', focusedSourceId = '') {
    if (!index) return '<p class="school-empty">수집 대기 · 아직 학교 공지 목록이 없습니다. 수집기가 공지를 확인하면 여기에 표시됩니다.</p>';
    const items = index.items.map((item, index) => ({item, index})).sort((a, b) => String(b.item.updated_at || '').localeCompare(String(a.item.updated_at || '')));
    const current = items.filter(row => ACTIVE.has(row.item.state));
    const history = items.filter(row => !ACTIVE.has(row.item.state));
    if (filter === 'latest') {
      let latest = items.slice(0, 100);
      const focused = items.find(row => row.item.id === focusedSourceId);
      if (focused && !latest.includes(focused)) latest = [focused, ...latest.slice(0, 99)];
      return latest.length ? latest.map(row => noticeHTML(row.item, row.index, pending.get(row.item.id), !ACTIVE.has(row.item.state))).join('') : '<p class="school-empty">수집된 공지가 없습니다.</p>';
    }
    return (current.length ? current.map(row => noticeHTML(row.item, row.index, pending.get(row.item.id))).join('') : '<p class="school-empty">새로 확인할 공지가 없습니다.</p>') +
      '<details class="school-history"><summary>지난 공지·기준 자료 ' + history.length + '개</summary>' + history.slice(0, 100).map(row => noticeHTML(row.item, row.index, null, true)).join('') + '</details>';
  }
  function mount(root, options = {}) {
    const document = root.document; const panel = document.getElementById('school-panel'); if (!panel) return null;
    const byId = id => document.getElementById(id); const client = new SchoolClient(root, options);
    const relevantViews = new Set(['cal', 'home', 'school']);
    const connection = () => ({repo: (client.read('cfg_bridge_repo') || REPO).toLowerCase(), pat: client.read('cfg_bridge_pat').trim()});
    const equalConnection = (a, b) => a.repo === b.repo && a.pat === b.pat;
    const configured = () => { try { client.credentials(); return true; } catch { return false; } };
    let observed = connection();
    let view = document.querySelector('.view.on')?.id?.replace(/^v-/, '') || 'home';
    let loading = null, refreshAgain = false, lastSuccess = null, lastAttempt = null, renderedIndex = null, focusedSourceId = '', scheduleReady = false;
    let calendarStatus = {state: configured() ? 'waiting' : 'unconfigured', text: configured() ? '학교 공지 확인 대기' : '학교 공지 연결 설정이 필요합니다.'};
    const notify = (message, error = false) => { byId('school-status').textContent = message; byId('school-status').classList.toggle('school-warning', error); };
    const announce = (state, text, redraw = true) => {
      calendarStatus = {state, text};
      const status = byId('calendar-school-status');
      if (status) status.textContent = text;
      if (redraw) root.refreshSchoolCalendar?.();
    };
    const draftKey = (item, candidate) => JSON.stringify([item?.id, item?.content_hash, candidate?.id]);
    const draftFields = ['selected', 'd', 'n', 's', 'e', 'loc', 'tentative'];
    const paint = ({preserveDrafts = false} = {}) => {
      const drafts = new Map(); let activeDraft = null;
      if (preserveDrafts && renderedIndex) {
        byId('school-list').querySelectorAll('[data-source-index]').forEach(article => {
          const item = renderedIndex.items[Number(article.dataset.sourceIndex)];
          article.querySelectorAll('[data-candidate-index]').forEach(fieldset => {
            const candidate = item?.candidates?.[Number(fieldset.dataset.candidateIndex)];
            if (!candidate) return;
            const key = draftKey(item, candidate); const values = {};
            draftFields.forEach(field => {
              const input = fieldset.querySelector('[data-school-field="' + field + '"]');
              if (!input) return;
              values[field] = ['selected', 'tentative'].includes(field) ? input.checked : input.value;
              if (document.activeElement === input) activeDraft = {key, field};
            });
            drafts.set(key, values);
          });
        });
      }
      byId('school-collectors').textContent = client.index ? collectorText(client.index) : ['error', 'unconfigured'].includes(calendarStatus.state) ? calendarStatus.text : collectorText(null);
      byId('school-list').innerHTML = !client.index && ['error', 'unconfigured'].includes(calendarStatus.state) ? '<p class="school-empty">' + esc(calendarStatus.text) + '</p>' : render(client.index, client.pending, byId('school-filter').value, focusedSourceId);
      renderedIndex = client.index;
      if (drafts.size && client.index) byId('school-list').querySelectorAll('[data-source-index]').forEach(article => {
        const item = client.index.items[Number(article.dataset.sourceIndex)];
        article.querySelectorAll('[data-candidate-index]').forEach(fieldset => {
          const candidate = item?.candidates?.[Number(fieldset.dataset.candidateIndex)]; const key = draftKey(item, candidate); const values = drafts.get(key);
          if (!values) return;
          draftFields.forEach(field => {
            const input = fieldset.querySelector('[data-school-field="' + field + '"]');
            if (!input || !(field in values)) return;
            if (['selected', 'tentative'].includes(field)) input.checked = values[field]; else input.value = values[field];
            if (activeDraft?.key === key && activeDraft.field === field) input.focus({preventScroll: true});
          });
        });
      });
      const count = client.index?.items.filter(item => ACTIVE.has(item.state)).length;
      byId('school-home-summary').textContent = count === undefined ? '새 공지·시험·과제 확인' : count ? '확인할 공지 ' + count + '개' : '확인할 새 공지 없음';
    };
    const buttons = () => {
      panel.querySelectorAll('button').forEach(button => { button.disabled = Boolean(loading || client.busy); });
      byId('school-filter').disabled = Boolean(loading || client.busy);
    };
    const syncConnection = (redraw = true) => {
      const current = connection();
      if (equalConnection(current, observed)) return false;
      observed = current; client.invalidate(); lastSuccess = null; lastAttempt = null; focusedSourceId = '';
      refreshAgain = Boolean(loading && configured());
      announce(configured() ? 'waiting' : 'unconfigured', configured() ? '새 연결로 학교 공지 확인 대기' : '학교 공지 연결 설정이 필요합니다.', false);
      paint();
      if (redraw) root.refreshSchoolCalendar?.();
      return true;
    };
    const readyText = () => {
      const warnings = Object.entries(client.index?.collectors || {}).filter(([, collector]) => ['auth_required', 'error', 'partial'].includes(collector.state)).map(([key, collector]) => (COLLECTORS[key] || '수집기') + (collector.state === 'auth_required' ? ' 다시 로그인 필요' : collector.state === 'partial' ? ' 일부 수집' : ' 수집 확인 필요'));
      return warnings.length ? '학교 공지 확인됨 · ' + warnings.join(' · ') : '학교 공지 확인됨';
    };
    const refresh = ({force = true} = {}) => {
      const changed = syncConnection();
      if (!configured()) {
        client.invalidate();
        announce('unconfigured', '학교 공지 연결 설정이 필요합니다.'); paint();
        if (force) notify('이 기기의 편집·설정 → 일정 요청 연결을 저장해 주세요.', true);
        return Promise.resolve(null);
      }
      if (loading) { if (changed) refreshAgain = true; return loading; }
      if (client.busy) { refreshAgain = true; return Promise.resolve(null); }
      const now = client.now().getTime();
      if (!force && ((lastSuccess !== null && now - lastSuccess < 60000) || (lastAttempt !== null && now - lastAttempt < 60000))) return Promise.resolve(client.index);
      lastAttempt = now;
      announce('loading', '학교 공지 확인 중…'); notify('비공개 학교 공지 확인 중…');
      const started = connection();
      loading = (async () => {
        try {
          const index = await client.refresh();
          if (!equalConnection(started, connection())) { syncConnection(); return null; }
          lastSuccess = client.now().getTime();
          announce(index ? 'ready' : 'waiting', index ? readyText() : '학교 공지 수집 대기', false);
          paint({preserveDrafts: true}); root.refreshSchoolCalendar?.();
          notify(index ? '공지 목록을 확인했습니다. 수집기별 상태와 마지막 확인 시각을 함께 확인해 주세요.' : '아직 수집된 목록이 없습니다. 수집 대기 중입니다.');
          return index;
        } catch (error) {
          client.invalidate(); lastSuccess = null;
          const changed = syncConnection(false) || !equalConnection(started, connection());
          if (!configured()) announce('unconfigured', '학교 공지 연결 설정이 필요합니다.', false);
          else if (changed) announce('waiting', '새 연결로 학교 공지 확인 대기', false);
          else if (!changed) announce('error', '학교 공지를 불러오지 못했습니다. 연결을 확인하세요.', false);
          paint(); root.refreshSchoolCalendar?.();
          notify(changed ? calendarStatus.text : error.name === 'AbortError' ? '학교 공지 응답 시간이 초과됐습니다. 다시 확인해 주세요.' : error.message, true);
          return null;
        } finally {
          loading = null; buttons();
          if (refreshAgain) {
            refreshAgain = false;
            if (configured() && !document.hidden && relevantViews.has(view)) root.setTimeout(() => refresh({force: true}), 0);
          }
        }
      })();
      buttons();
      return loading;
    };
    const resume = () => {
      const changed = syncConnection();
      if (scheduleReady && !document.hidden && relevantViews.has(view)) return refresh({force: changed});
      return Promise.resolve(null);
    };
    const open = () => { root.sw('school'); if (root.location.hash !== '#school') root.history.replaceState(null, '', '#school'); };
    const openSource = async sourceId => {
      if (typeof sourceId !== 'string' || !sourceId) return false;
      root.closePanel?.(); open(); await refresh({force: true});
      if (!client.index) return false;
      const position = client.index.items.findIndex(item => item.id === sourceId);
      if (position === -1) { notify('이 공지를 현재 목록에서 찾지 못했습니다. 최신 공지를 확인해 주세요.', true); return false; }
      focusedSourceId = sourceId; byId('school-filter').value = 'latest'; paint({preserveDrafts: true});
      const article = Array.from(byId('school-list').querySelectorAll('[data-source-index]')).find(element => Number(element.dataset.sourceIndex) === position);
      if (!article) return false;
      article.tabIndex = -1; article.classList.add('school-source-focus'); article.focus({preventScroll: true}); article.scrollIntoView({block: 'start', behavior: 'smooth'});
      return true;
    };
    document.addEventListener('click', async event => {
      const button = event.target.closest('[data-school-action]'); if (!button || button.disabled) return;
      const action = button.dataset.schoolAction;
      if (action === 'open') { open(); return; }
      if (action === 'calendar-source') { await openSource(button.dataset.schoolSource); return; }
      if (action === 'settings') { root.sw('edit'); byId('bridge-settings-panel').scrollIntoView({block: 'start', behavior: 'smooth'}); return; }
      if (action === 'refresh') { await refresh(); return; }
      const article = button.closest('[data-source-index]'); const item = client.index?.items[Number(article?.dataset.sourceIndex)];
      if (!item) return;
      if (action === 'ask') { root.sw('edit'); byId('nl-input').value = shortText(item.title, 500) + ' 관련 일정을 확인해 줘'; byId('nl-input').focus(); byId('nl-input').scrollIntoView({block: 'center', behavior: 'smooth'}); return; }
      try {
        const choices = action === 'approve' ? Array.from(article.querySelectorAll('[data-candidate-index]')).filter(fieldset => fieldset.querySelector('[data-school-field="selected"]').checked).map(fieldset => {
          const get = key => fieldset.querySelector('[data-school-field="' + key + '"]');
          return {id: item.candidates[Number(fieldset.dataset.candidateIndex)].id, input: {d: get('d').value, n: get('n').value, s: get('s').value, e: get('e').value, loc: get('loc').value, tentative: get('tentative').checked}};
        }) : [];
        // The immutable body is built from explicit selections; no background approval.
        const operation = action === 'retry' ? client.retry(item.id) : client.decide(item, action, choices);
        buttons(); notify('처리 요청 전송 중…'); await operation; paint();
        notify('처리 요청이 접수되었습니다. 목록 새로고침으로 실제 반영 결과를 확인해 주세요.');
      } catch (error) { if (client.pending.has(item.id)) paint(); notify(error.name === 'AbortError' ? '접수 여부 확인이 필요합니다. 같은 요청 상태 확인을 눌러 주세요.' : error.message, true); }
      finally { buttons(); if (refreshAgain) { refreshAgain = false; root.setTimeout(resume, 0); } }
    });
    byId('school-filter').addEventListener('change', () => { focusedSourceId = ''; paint({preserveDrafts: true}); });
    root.addEventListener('hashchange', () => { if (root.location.hash === '#school') open(); });
    root.addEventListener('storage', event => { if (event.key === null || ['cfg_bridge_repo', 'cfg_bridge_pat'].includes(event.key)) resume(); });
    root.addEventListener('focus', resume);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) resume(); });
    document.addEventListener('click', event => { const button = event.target.closest('[data-bridge-action]'); if (button && ['settings-save', 'connection-test'].includes(button.dataset.bridgeAction)) root.setTimeout(resume, 0); });
    const poll = root.setInterval?.(resume, 60000);
    paint(); announce(calendarStatus.state, calendarStatus.text, false);
    const initialization = Promise.resolve(root.scheduleDataReady).catch(() => {}).then(() => {
      scheduleReady = true;
      if (root.location.hash === '#school') open();
      return resume();
    });
    return {
      client, refresh, initialization, openSource,
      onView: next => { view = next; syncConnection(); if ((scheduleReady || view === 'school') && !document.hidden && relevantViews.has(view)) return refresh({force: view === 'school'}); },
      getCalendar: events => { syncConnection(false); return client.index && root.SchoolCalendar?.project ? root.SchoolCalendar.project(client.index, events) : {byDate: {}, undatedCount: 0}; },
      getCalendarStatus: () => { syncConnection(false); return {...calendarStatus}; },
      dispose: () => { if (poll !== undefined) root.clearInterval?.(poll); }
    };
  }
  return {SchoolClient, cleanEvent, safeSourceURL, collectorText, render, noticeHTML, mount};
});
