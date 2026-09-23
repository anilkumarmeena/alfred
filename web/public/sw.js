// JARVIS service worker: shows Web Push notifications from the task runner (jarvis/notify/push.py),
// and opens the app on the task or alert when one is tapped. No caching: JARVIS needs the Mac anyway.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data.text() }; }
  const title = data.title || "JARVIS", body = data.body || "", url = data.url || "/";
  // Every push must show a notification: iOS revokes the subscription of a web app that stays silent.
  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag: data.tag || undefined,  // a newer notification with the same tag replaces the older one
    renotify: !!data.tag,
    icon: "/static/icon-192.png",
    data: { url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const { title, body } = event.notification, url = event.notification.data?.url || "/";
  const target = new URL(url, self.location.origin);
  if (target.origin !== self.location.origin) return;  // only ever opens JARVIS itself
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((c) => new URL(c.url).origin === self.location.origin);
    if (open) {
      // Already running, maybe mid-conversation: bring it forward and let the page show the item, without a reload.
      open.postMessage({ type: "notification", url: target.pathname + target.search, title, body });
      try { await open.focus(); return; } catch {}
    }
    target.hash = new URLSearchParams({ title, body }).toString();  // the page reads it, then clears the URL
    await self.clients.openWindow(target.href);
  })());
});
