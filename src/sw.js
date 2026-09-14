'use strict';
// No fetch handler or CacheStorage: schedules, tokens and private API responses
// are always handled by the page/network, never stored in this worker.
const TITLES = Object.freeze({deadline: '마감 알림', daily: '오늘 일정', changes: '일정 변경 반영', departure: '이동 출발 30분 전'});
const clean = value => typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, ' ').trim().slice(0, 120) : '';
function notificationData(payload, scope) {
  const kind = Object.hasOwn(TITLES, payload?.kind) ? payload.kind : 'changes';
  const items = Array.isArray(payload?.items) ? payload.items.slice(0, 10) : [];
  const body = items.map(item => [clean(item?.name), clean(item?.time), clean(item?.location)].filter(Boolean).join(' · ')).filter(Boolean).join('\n').slice(0, 1500);
  const base = new URL(scope);
  let url = base.href;
  try {
    const requested = new URL(payload?.url || './', base);
    if (requested.origin === base.origin && requested.pathname.startsWith(base.pathname) && !requested.username && !requested.password) url = requested.origin + requested.pathname;
  } catch { /* A notification click must remain inside this app. */ }
  const tag = typeof payload?.tag === 'string' && /^[a-zA-Z0-9:_-]{1,180}$/.test(payload.tag) ? payload.tag : undefined;
  return {title: TITLES[kind], options: {body, icon: new URL('icon-192.png', base).href, badge: new URL('badge-96.png', base).href, tag, data: {url}, lang: 'ko', dir: 'auto'}};
}
if (typeof module === 'object' && module.exports) module.exports = {notificationData};
if (typeof self === 'object' && self.addEventListener) {
  self.addEventListener('install', event => { event.waitUntil(self.skipWaiting()); });
  self.addEventListener('activate', event => { event.waitUntil(self.clients.claim()); });
  self.addEventListener('push', event => {
    let payload = {};
    try { payload = event.data?.json() || {}; } catch { /* Still show a visible, safe notification. */ }
    const notification = notificationData(payload, self.registration.scope);
    event.waitUntil(self.registration.showNotification(notification.title, notification.options));
  });
  self.addEventListener('notificationclick', event => {
    event.notification.close();
    const safe = notificationData({url: event.notification.data?.url}, self.registration.scope).options.data.url;
    event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(async clients => {
      const existing = clients.find(client => client.url.startsWith(self.registration.scope));
      if (existing) { if (existing.navigate) await existing.navigate(safe); return existing.focus(); }
      return self.clients.openWindow(safe);
    }));
  });
  self.addEventListener('pushsubscriptionchange', event => {
    // Reconnecting requires the page's private credentials and an explicit click.
    event.waitUntil(self.clients.matchAll({type: 'window', includeUncontrolled: true}).then(clients => clients.forEach(client => client.postMessage({type: 'schedule-push-reconnect'}))));
  });
}
