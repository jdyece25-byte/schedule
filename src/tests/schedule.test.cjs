const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const root = path.join(__dirname, '..', '..');
const events = JSON.parse(fs.readFileSync(path.join(root, 'DB', 'events.json'), 'utf8'));
const travel = JSON.parse(fs.readFileSync(path.join(root, 'DB', 'travel.json'), 'utf8'));
const html = fs.readFileSync(path.join(root, 'src', 'index.html'), 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1].split('// ── INIT')[0];
const fall = events.filter(e => e.d >= '2026-09-14');

function element() {
  let content = '';
  const el = {children: [], dataset: {}, style: {}, className: '', textContent: '', value: '',
    classList: { add() {}, remove() {} },
    appendChild(child) { this.children.push(child); return child; }
  };
  Object.defineProperty(el, 'innerHTML', {get() { return content; }, set(v) { content = v; el.children = []; }});
  return el;
}
function app(fetchOverride) {
  const elements = new Map();
  const get = id => {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);};
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : ['2026-09-14T09:00:00+09:00'])); }
    static now() { return new Date('2026-09-14T09:00:00+09:00').getTime(); }
  }
  const context = vm.createContext({
    Date: FixedDate, TextEncoder, TextDecoder, Uint8Array,
    atob: x => Buffer.from(x, 'base64').toString('binary'),
    btoa: x => Buffer.from(x, 'binary').toString('base64'),
    setTimeout, clearTimeout, setInterval, clearInterval,
    window: {innerWidth: 390}, localStorage: {getItem() {return '';}, setItem() {}},
    document: {getElementById: get, createElement: element, querySelectorAll() {return [];}, querySelector() {return null;}},
    fetch: fetchOverride || (async url => ({ok: true, json: async () => JSON.parse(JSON.stringify(url.includes('events.json') ? events : url.includes('travel.json') ? travel : []))}))
  });
  new vm.Script(source).runInContext(context);
  return {context, get, run: code => vm.runInContext(code, context)};
}

test('published application loads schedule data exclusively from DB', async () => {
  const urls = [];
  const a = app(async url => {
    urls.push(url);
    const name = url.split('/').at(-1);
    return {ok: true, json: async () => name === 'events.json' ? events : name === 'travel.json' ? travel : []};
  });
  await a.run('loadData()');
  assert.deepEqual(urls.sort(), ['./DB/events.json', './DB/plan.json', './DB/travel.json']);
  assert.equal(a.run('EVENTS.length'), events.length);
});

test('new schedule dates, IDs, midnight times and locations are valid', () => {
  assert.equal(events.filter(e => e.d < '2026-09-14').length, 76);
  assert.ok(fall.length >= 276);
  assert.equal(new Set(fall.map(e => e.id)).size, fall.length);
  for (const e of fall) {
    assert.ok(e.d <= '2026-12-31');
    assert.equal(new Date(e.d + 'T12:00:00Z').toISOString().slice(0, 10), e.d);
    if (e.s != null) assert.ok(Number.isInteger(e.s) && e.s >= 0 && e.s < 1440, e.id);
    if (e.e != null) assert.ok(Number.isInteger(e.e) && e.s != null && e.e > e.s && e.e <= 1440, e.id);
    if (e.lid) assert.ok(travel.locations[e.lid], e.id);
    if (e.series?.includes('online-') || e.series === '2026fall-clinic-fri') assert.equal(e.e, 1440);
  }
  assert.equal(fall.filter(e => e.series === '2026fall-clinic-thu').length, 16);
  assert.equal(fall.filter(e => e.series === '2026fall-tutor-jung').length, 15);
  assert.ok(fall.filter(e => e.series === '2026fall-macro').every(e => e.status === 'tentative'));
});

test('biweekly labs, power-market exceptions and one-off appointments match instructions', () => {
  assert.deepEqual(fall.filter(e => e.series === '2026fall-em-lab').map(e => e.d),
    ['2026-09-18','2026-10-02','2026-10-16','2026-10-30','2026-11-13','2026-11-27','2026-12-11','2026-12-25']);
  const power = fall.filter(e => e.series?.startsWith('2026fall-power-'));
  for (const d of ['2026-09-23','2026-09-28','2026-09-30','2026-10-14']) assert.ok(!power.some(e => e.d === d));
  for (const d of ['2026-09-14','2026-09-21','2026-10-05','2026-10-12']) assert.equal(power.find(e => e.d === d).e, 1170);
  for (const d of ['2026-09-16','2026-10-07']) assert.equal(power.find(e => e.d === d).e, 1095);
  const gyodae = fall.filter(e => e.id.startsWith('tutor-gyodae-'));
  assert.equal(gyodae.length, 1);
  assert.equal(gyodae[0].d, '2026-09-21');
  assert.equal(gyodae[0].s, 600);
  assert.equal(gyodae[0].e, 780);
  const meeting = fall.find(e => e.id.startsWith('professor-research-'));
  assert.equal(meeting.loc, 'Zoom'); assert.equal(meeting.s, undefined);
  assert.equal(fall.find(e => e.id.startsWith('bk21-report-')).s, undefined);
  assert.match(fall.find(e => e.id === 'tutor-minhee-jiyo-2026-09-20').no, /온라인/);
  assert.equal(fall.find(e => e.id === 'tutor-minhee-jiyo-2026-09-27').lid, 'daechi');
});

test('calendar supports September, next month, year transitions and leap years', async () => {
  const a = app(); await a.run('loadData()');
  a.run('calendarYear=2026;calendarMonth=8;buildCal()');
  let cells = a.get('calGrid').children;
  assert.equal(cells.filter(e => e.dataset.date).length, 30);
  assert.equal(cells.findIndex(e => e.dataset.date === '2026-09-01'), 2);
  assert.equal(a.get('calendar-month').textContent, '2026년 9월');
  a.run('changeMonth(1)');
  cells = a.get('calGrid').children;
  assert.equal(cells.filter(e => e.dataset.date).length, 31);
  assert.equal(cells.findIndex(e => e.dataset.date === '2026-10-01'), 4);
  a.run('calendarMonth=11;changeMonth(1)');
  assert.equal(a.get('calendar-month').textContent, '2027년 1월');
  a.run('calendarYear=2028;calendarMonth=1;buildCal()');
  assert.ok(a.get('calGrid').children.some(e => e.dataset.date === '2028-02-29'));
  a.run('calendarYear=2026;calendarMonth=8;buildList();buildHome();buildPlan();initEditSettings()');
  assert.equal(a.get('listWrap').children.length, 17);
  assert.ok(a.get('listWrap').children.every(e => e.children[0].innerHTML.includes('9월')));
  const nextMilestone = fall.filter(e => ['exam', 'deadline', 'meeting'].includes(e.t)).sort((a, b) => a.d.localeCompare(b.d))[0];
  assert.ok(a.get('hc-cal-sub').innerHTML.includes(nextMilestone.n));
  assert.match(a.get('v-plan').innerHTML, /거시경제이론/);
  assert.doesNotMatch(a.get('v-plan').innerHTML, /공학수학2|기초회로이론 및 실험/);
  assert.match(a.get('ef-lid').innerHTML, /gyodae/);
});

test('conflicts ignore archived events and detect online overlaps', async () => {
  const a = app(); await a.run('loadData()');
  const conflicts = a.run('computeConflicts()');
  assert.ok(conflicts.every(e => e.ds >= '2026-09-14'));
  assert.ok(!conflicts.some(e => e.kind === 'overlap'));
  assert.ok(conflicts.some(e => e.ds === '2026-09-21' && e.a.lid === 'gyodae' && e.gap === 60));
  a.run("EVENTS.push({d:'2026-09-14',t:'tutor',n:'Test overlap',s:1380,e:1440});rebuildEM()");
  assert.ok(a.run("computeConflicts().some(c=>c.kind==='overlap'&&c.ds==='2026-09-14')"));
  a.run('buildAlerts()');
  assert.match(a.get('v-alert').innerHTML, /시간 미정/);
});

test('stale browser save is rejected without overwriting newer schedules', async () => {
  const a = app(); await a.run('loadData()');
  let puts = 0;
  a.context.fetch = async (_url, options) => {
    if (options.method === 'PUT') puts++;
    return {ok: true, json: async () => ({sha: 'remote-new', content: Buffer.from(JSON.stringify([...events, {d:'2026-10-01',n:'Remote addition'}])).toString('base64')})};
  };
  const result = await a.run("cfg.pat='test-only';EVENTS.push({d:'2026-10-02',n:'Local addition'});githubSave('events.json',EVENTS)");
  assert.equal(result.ok, false); assert.equal(puts, 0);
  assert.match(result.msg, /다른 곳에서 일정이 변경/);
});

test('fresh browser save retains loaded locations and uses the checked revision', async () => {
  const a = app(); await a.run('loadData()');
  const puts = [];
  const urls = [];
  a.context.fetch = async (url, options) => {
    urls.push(url);
    if (options.method === 'PUT') {puts.push(JSON.parse(options.body));return {ok:true};}
    return {ok: true, json: async () => ({sha:'checked-revision', content:Buffer.from(JSON.stringify(url.endsWith('events.json')?events:travel)).toString('base64')})};
  };
  assert.equal(await a.run("cfg.pat='test-only';EVENTS.push({d:'2026-10-02',n:'Local addition'});saveAllData()"), true);
  assert.equal(puts.length, 1);
  assert.equal(puts[0].sha, 'checked-revision');
  assert.ok(urls.length > 0);
  assert.ok(urls.every(url => /^https:\/\/api\.github\.com\/repos\/jdyece25-byte\/schedule\/contents\/DB\/(events|travel)\.json$/.test(url)));
  assert.equal(a.run('LOC_NAMES.gyodae'), '교대역');
});

test('load failures do not silently show the old June schedule', async () => {
  const a = app(async () => {throw new Error('offline');});
  await a.run('loadData()');
  assert.equal(a.run('EVENTS.length'), 0);
  a.run('buildHome()');
  assert.match(a.get('hc-today-sub').innerHTML, /불러오지 못/);
});

test('manual midnight input is 24:00 and failed saves retain the form without duplicating events', async () => {
  const a = app(); await a.run('loadData()');
  a.get('ef-date').value='2026-09-22';
  a.get('ef-name').value='Midnight test';
  a.get('ef-type').value='tutor';
  a.get('ef-start').value='22:00';
  a.get('ef-end').value='00:00';
  let submitted;
  a.context.fetch=async (_url, options)=>{
    if(options.method==='PUT'){
      submitted=JSON.parse(Buffer.from(JSON.parse(options.body).content,'base64').toString('utf8'));
      return {ok:false,status:409};
    }
    return {ok:true,json:async()=>({sha:'test',content:Buffer.from(JSON.stringify(events)).toString('base64')})};
  };
  await a.run("cfg.pat='test-only';addEventFromForm()");
  const midnight=submitted.find(e=>e.n==='Midnight test');
  assert.equal(midnight.e,1440);assert.equal(midnight.ti,'22:00–24:00');
  assert.equal(a.get('ef-name').value,'Midnight test');
  assert.equal(a.run("EVENTS.filter(e=>e.n==='Midnight test').length"),0);
});

test('untrusted event and location text stays text across calendar, details, home, alerts and previews', () => {
  const a = app();
  const attack = '<img src=x onerror="globalThis.stolen=localStorage.getItem(\'cfg_bridge_pat\')">';
  a.context.attack = attack;
  a.run(`EVENTS=[
    {id:'evil-exam',d:TODAY,t:'exam',status:'tentative',n:attack,ti:attack,loc:attack,no:attack,lid:'one',s:0,e:1},
    {id:'evil-tutor',d:TODAY,t:'tutor',n:attack,ti:attack,loc:attack,no:attack,lid:'two',s:2,e:1440}
  ];TT={'one-two':5};TM={'one-two':attack};LOC_NAMES={one:attack,two:attack};PLAN=[];dataReady=true;
  rebuildEM();buildCal();selDay(TODAY);buildToday();buildHome();buildAlerts();buildList();initEditSettings();
  renderNLPreview({type:'event',data:EVENTS});`);
  const walk = node => [node, ...node.children.flatMap(walk)];
  const chips = walk(a.get('calGrid')).filter(node => node.className === 'chip');
  assert.equal(chips.length, 2);
  for (const chip of chips) {
    assert.equal(chip.textContent, attack);
    assert.equal(chip.innerHTML, '');
  }
  for (const id of ['panelBody','v-today','hc-today-sub','hc-cal-sub','v-alert','calendar-notes','nl-preview','ef-lid']) {
    const markup = a.get(id).innerHTML;
    assert.ok(!markup.includes('<img'), id);
    assert.ok(markup.includes('&lt;img'), id);
  }
  for (const node of walk(a.get('listWrap'))) assert.ok(!node.innerHTML.includes('<img'));
  a.run("renderNLPreview({type:'travel',data:[{from:'one',to:'two',min:attack,mode:attack}]})");
  assert.ok(!a.get('nl-preview').innerHTML.includes('<img'));
  assert.ok(a.get('nl-preview').innerHTML.includes('&lt;img'));
  assert.equal(a.context.stolen, undefined);
});

test('plan display escapes model text and rejects color attribute injection', () => {
  const a = app();
  const attack = '<svg onload="globalThis.stolen=true">';
  const colorAttack = '#fff" onmouseover="globalThis.stolen=true';
  a.context.attack = attack;
  a.context.colorAttack = colorAttack;
  a.run(`EVENTS=[{d:TODAY,t:'class',n:attack,no:attack}];
    PLAN=[{d:TODAY,date:attack,total:attack,exam:attack,alert:attack,note:attack,blocks:[{c:colorAttack,s:attack,d:attack,h:attack}]}];
    rebuildEM();buildPlan();buildToday();buildHome();`);
  for (const id of ['v-plan','v-today','hc-plan-sub']) {
    const markup = a.get(id).innerHTML;
    assert.ok(!markup.includes('<svg'), id);
    assert.ok(!markup.includes('onmouseover='), id);
    assert.ok(markup.includes('&lt;svg'), id);
  }
  assert.match(a.get('v-plan').innerHTML, /background:#888888/);
  assert.equal(a.run("safeColor('#aabbcc')"), '#aabbcc');
});
