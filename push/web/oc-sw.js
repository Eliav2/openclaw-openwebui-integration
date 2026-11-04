// oc-sw.js: the Web Push service worker for Open WebUI (docs/install/web-push.md).
// Serve it at /oc-sw.js. Push only: there is no fetch handler, so it never
// caches or intercepts a request.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

// iOS and iPadOS: every push must show a notification (userVisibleOnly), or
// WebKit revokes the subscription.
const MUST_SHOW = /iPhone|iPad|iPod/.test(self.navigator.userAgent) ||
  (/Macintosh/.test(self.navigator.userAgent) && self.navigator.maxTouchPoints > 1);

self.addEventListener("push", (e) => {
  let p = {};
  try { p = e.data ? e.data.json() : {}; } catch (_) {}
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    // Open WebUI is in front of the user on this device, whatever chat is open:
    // nothing to tell, as in a messaging app (the page has its own cues).
    const inFront = wins.some((c) => c.focused && c.visibilityState === "visible");
    const shown = MUST_SHOW || !inFront;
    if (shown) {
      await self.registration.showNotification(p.title || "Open WebUI", {
        body: "New reply",
        tag: "chat-" + (p.chat_id || "none"),   // one per chat: a newer one replaces it
        renotify: true,
        icon: "/static/favicon.png",
        badge: "/static/favicon.png",
        data: { url: p.url || "/" },
      });
    }
    // Each decision, once acted on, to the open pages (`oc-push` messages), for tests and tools.
    for (const c of wins) c.postMessage({ type: "oc-push", shown, payload: p });
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = new URL(e.notification.data?.url || "/", self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    const win = wins.find((c) => new URL(c.url).origin === self.location.origin);
    if (win) {
      await win.focus();
      return win.navigate(url);
    }
    return self.clients.openWindow(url);
  })());
});

// The push service replaced the subscription: subscribe again with the same
// key. The page saves the new one on its next load (it has the user's token).
self.addEventListener("pushsubscriptionchange", (e) => {
  const options = e.oldSubscription?.options;
  if (options?.applicationServerKey) {
    e.waitUntil(self.registration.pushManager.subscribe(options));
  }
});
