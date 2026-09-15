(function (root, factory) {
  'use strict';
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.SchoolCalendar = api;
})(typeof window === 'object' ? window : globalThis, function () {
  'use strict';
  const STATES = new Set(['needs_review', 'ready', 'conflict', 'info', 'applied']);
  const LABELS = {deadline: '마감', exam: '시험', lab: '실험·랩', class: '수업', cancellation: '휴강·취소', notice: '일정 관련 공지'};
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const text = value => typeof value === 'string' ? value : '';
  const id = value => typeof value === 'string' && value.length > 0 ? value : null;

  function validDate(value) {
    if (typeof value !== 'string' || !/^[0-9]{4}-[0-9]{2}-[0-9]{2}$/.test(value) || value.startsWith('0000-')) return false;
    const moment = new Date(value + 'T00:00:00Z');
    return Number.isFinite(moment.getTime()) && moment.toISOString().slice(0, 10) === value;
  }

  function postedDate(value) {
    // An explicit offset is required. Reject rollover dates and 24:00 instead
    // of letting Date.parse silently turn malformed input into another day.
    if (typeof value !== 'string') return null;
    const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|[+-](\d{2}):(\d{2}))$/.exec(value);
    if (!match || !validDate(match[1]) || +match[2] > 23 || +match[3] > 59 || +(match[4] || 0) > 59
        || +(match[6] || 0) > 23 || +(match[7] || 0) > 59) return null;
    const instant = Date.parse(value);
    if (!Number.isFinite(instant)) return null;
    const date = new Date(instant + 9 * 60 * 60 * 1000).toISOString().slice(0, 10);
    return validDate(date) ? date : null;
  }

  function clock(value) {
    return Number.isInteger(value) && value >= 0 && value <= 1440
      ? String(Math.floor(value / 60)).padStart(2, '0') + ':' + String(value % 60).padStart(2, '0') : '';
  }

  function eventTime(event) {
    const start = clock(event.s), end = clock(event.e);
    return start && end && event.e > event.s ? start + '–' + end : start;
  }

  function project(index, events) {
    const byDate = {}, undated = new Set(), dated = new Set(), rows = new Map();
    const eventMap = new Map(), ambiguousIds = new Set();
    for (const event of Array.isArray(events) ? events : []) {
      if (!object(event) || !id(event.id)) continue;
      if (eventMap.has(event.id)) ambiguousIds.add(event.id);
      else eventMap.set(event.id, event);
    }
    const findEvent = identifier => {
      const event = id(identifier) && !ambiguousIds.has(identifier) ? eventMap.get(identifier) : null;
      return event && validDate(event.d) ? event : null;
    };

    function add(item, event, options = {}) {
      if (!validDate(event.d)) return;
      const kind = typeof options.kind === 'string' && Object.hasOwn(LABELS, options.kind) ? options.kind
        : typeof event.t === 'string' && Object.hasOwn(LABELS, event.t) ? event.t : 'notice';
      const row = {
        sourceId: item.id, title: text(item.title) || '제목 없는 공지', course: text(item.course) || '학교 공지',
        date: event.d, dateLabel: options.dateLabel || LABELS[kind], kind, state: item.state,
        review: item.state === 'needs_review' || item.state === 'conflict'
          || options.unapplied === true || event.status === 'tentative',
        eventId: options.eventId || null, time: eventTime(event), location: text(event.loc)
      };
      let day = rows.get(row.date);
      if (!day) rows.set(row.date, day = new Map());
      const prior = day.get(row.sourceId);
      if (prior) {
        prior.review ||= row.review;
        // A source can cover multiple events on a day. A single eventId is
        // meaningful only if every projected row agrees on that exact link.
        if (prior.eventId !== row.eventId) prior.eventId = null;
        if (prior.kind !== row.kind) prior.kind = 'notice';
        if (prior.dateLabel !== row.dateLabel) prior.dateLabel = LABELS.notice;
        if (prior.time !== row.time) prior.time = '';
        if (prior.location !== row.location) prior.location = '';
      } else day.set(row.sourceId, row);
      dated.add(item.id);
    }

    for (const item of object(index) && Array.isArray(index.items) ? index.items : []) {
      if (!object(item) || !id(item.id) || !STATES.has(item.state)) continue;
      let hasDate = false;
      const associated = new Set();
      const candidates = (Array.isArray(item.candidates) ? item.candidates : []).filter(object);
      const receiptEvents = [...new Set(Array.isArray(item.event_ids) ? item.event_ids : [])].map(findEvent).filter(Boolean);
      for (const candidate of candidates) {
        const proposed = object(candidate.event) ? candidate.event : {};
        // Applied API candidates may retain a provisional ID while the receipt
        // records the actual generated ID. Exact provenance or a one-to-one
        // receipt can associate them; array order and title similarity cannot.
        const proven = receiptEvents.filter(e => object(e.school) && id(candidate.id)
          && e.school.candidate_id === candidate.id && e.school.source_id === item.id);
        const receipt = item.state === 'applied' ? proven.length === 1 ? proven[0]
          : candidates.length === 1 && receiptEvents.length === 1 ? receiptEvents[0] : null : null;
        const linked = findEvent(candidate.target_id) || findEvent(proposed.id) || receipt;
        const unapplied = item.state !== 'applied' && candidate.action !== 'link';
        const changing = unapplied && ['update', 'delete'].includes(candidate.action);
        const options = {kind: candidate.kind, unapplied, ...(changing ? {dateLabel: '변경 확인'} : {})};
        if (linked) {
          add(item, linked, {...options, eventId: linked.id});
          associated.add(linked.id); hasDate = true;
          // Pending moves are visible at both the existing occurrence and the
          // proposed destination, without manufacturing a second DB event.
          if (unapplied && candidate.action === 'update' && validDate(proposed.d) && proposed.d !== linked.d) {
            add(item, proposed, options);
          }
        } else if (validDate(proposed.d)) {
          add(item, proposed, options); hasDate = true;
        }
      }
      // Applied selections can remove their candidates from the private index.
      // These explicit IDs remain reliable links; never guess by title/order.
      for (const identifier of Array.isArray(item.event_ids) ? item.event_ids : []) {
        if (associated.has(identifier)) continue;
        const linked = findEvent(identifier);
        if (linked) { add(item, linked, {eventId: linked.id}); hasDate = true; }
      }
      if (!hasDate) {
        const date = postedDate(item.updated_at);
        if (date) add(item, {d: date}, {kind: 'notice', dateLabel: '공지 게시·수정일'});
        else undated.add(item.id);
      }
    }
    for (const [date, day] of [...rows].sort(([a], [b]) => a.localeCompare(b))) byDate[date] = [...day.values()];
    for (const sourceId of dated) undated.delete(sourceId);
    return {byDate, undatedCount: undated.size};
  }

  // Plain data only: callers must escape text or assign it using textContent.
  // This module never renders HTML or touches network/storage/DB state.
  return {project};
});
