/* Axiom Vision hub service worker: web push for alerts. It caches nothing (the hub is always online-first) and is
   scoped to "/" only — never under a site's /s/<id>/ path. */
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (e) => {
  let data = {};
  try { data = e.data ? e.data.json() : {}; } catch { data = { title: "Axiom Vision", body: e.data ? e.data.text() : "" }; }
  e.waitUntil(self.registration.showNotification(data.title || "Axiom Vision", {
    body: data.body || "", data: { url: data.url || "/" }, tag: `${data.kind || "alert"}:${data.site_id || ""}`, renotify: true,
    icon: "/icons/icon-192.png", badge: "/icons/icon-192.png",
  }));
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || "/";
  e.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((wins) => {
    const w = wins.find((x) => new URL(x.url).origin === self.location.origin);
    if (w) { w.navigate(url); return w.focus(); }
    return self.clients.openWindow(url);
  }));
});
