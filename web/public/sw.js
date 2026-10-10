// Service worker: shows Web Push notifications and opens the app on the tapped task or alert.
// No caching: JARVIS is useless offline.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { data = { body: event.data.text() }; }
  const title = data.title || "JARVIS", body = data.body || "", url = data.url || "/";
  // Always show a notification: iOS revokes subscriptions of web apps that receive silent pushes.
  event.waitUntil(self.registration.showNotification(title, {
    body,
    tag: data.tag || undefined,  // same tag replaces the older notification
    renotify: !!data.tag,
    icon: "/static/icon-192.png",
    data: { url },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const { title, body } = event.notification, url = event.notification.data?.url || "/";
  const target = new URL(url, self.location.origin);
  if (target.origin !== self.location.origin) return;  // same-origin only
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const open = windows.find((c) => new URL(c.url).origin === self.location.origin);
    if (open) {
      // Page already open (maybe mid-conversation): focus it and hand over the item without a reload.
      open.postMessage({ type: "notification", url: target.pathname + target.search, title, body });
      try { await open.focus(); return; } catch {}
    }
    target.hash = new URLSearchParams({ title, body }).toString();  // read and cleared by the page
    await self.clients.openWindow(target.href);
  })());
});
