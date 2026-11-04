// oc-push.js: the Web Push page script for Open WebUI (docs/install/web-push.md).
// Serve it as Open WebUI's /static/loader.js (every page loads that file), or
// append it to yours. Replace the two placeholders when you serve it:
//   __VAPID_PUBLIC_KEY__  the VAPID public key, base64url (not a secret)
//   __FUNCTION_ID__       the id of the Web Push event function in Open WebUI
// It registers /oc-sw.js and keeps this device's push subscription in that
// function's user valves, through Open WebUI's own API with the user's token.
(() => {
  const VAPID = "__VAPID_PUBLIC_KEY__", FN = "__FUNCTION_ID__";
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) return;
  const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) ||
    (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  // iOS has Web Push only in a web app added to the Home Screen.
  if (ios && !standalone) return;

  const store = {
    get: (k) => { try { return localStorage.getItem(k); } catch (_) { return null; } },
    set: (k, v) => { try { localStorage.setItem(k, v); } catch (_) {} },
  };
  const keyBytes = (b64) => Uint8Array.from(atob(b64.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
  const sameKey = (buf) => {
    if (!buf) return false;
    const a = new Uint8Array(buf), b = keyBytes(VAPID);
    return a.length === b.length && a.every((x, i) => x === b[i]);
  };
  const call = (path, body) => fetch(path, {
    method: body ? "POST" : "GET",
    headers: { Authorization: "Bearer " + store.get("token"), "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  }).then((r) => (r.ok ? r.json() : Promise.reject(new Error(path + " " + r.status))));
  const valves = (suffix, body) => call("/api/v1/functions/id/" + FN + "/valves/user" + suffix, body);

  async function save(sub) {
    let list = [];
    try { list = JSON.parse((await valves(""))?.subscriptions || "[]"); } catch (_) {}
    const s = sub.toJSON();
    if (list.some((x) => x.endpoint === s.endpoint)) return "kept";
    let device = store.get("ocPushDevice");
    if (!device) { device = crypto.randomUUID(); store.set("ocPushDevice", device); }
    // This device's earlier subscription is replaced, not kept beside the new one.
    list = list.filter((x) => x.device !== device);
    list.push({ ...s, device, ua: navigator.userAgent.slice(0, 120), at: Date.now() });
    await valves("/update", { subscriptions: JSON.stringify(list) });
    return "saved";
  }

  async function ensure() {
    if (!store.get("token")) return "no-token";
    // Open WebUI unregisters every service worker when it updates; this
    // registers ours again on the next page load.
    const reg = await navigator.serviceWorker.register("/oc-sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    if (Notification.permission !== "granted") return "no-permission";
    const user = (await call("/api/v1/auths/"))?.id;
    let sub = await reg.pushManager.getSubscription();
    // Another user signed in on this device, or the key changed: a new
    // subscription, so the old one stops receiving (its push service answers
    // 410 and the function drops it).
    if (sub && (store.get("ocPushUser") !== user || !sameKey(sub.options?.applicationServerKey))) {
      await sub.unsubscribe();
      sub = null;
    }
    sub ||= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(VAPID) });
    store.set("ocPushUser", user);
    return save(sub);
  }

  // The permission prompt needs a click (iOS requires one).
  async function enable() {
    return (await Notification.requestPermission()) === "granted" ? ensure() : "denied";
  }

  function offer() {
    if (Notification.permission !== "default" || store.get("ocPushOffer") === "dismissed" || !store.get("token") ||
      document.getElementById("oc-push-offer")) return;
    const bar = document.createElement("div");
    bar.id = "oc-push-offer";
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Notifications");
    bar.style.cssText = "position:fixed;top:60px;left:50%;transform:translateX(-50%);z-index:9999;display:flex;gap:4px;" +
      "font:14px system-ui,sans-serif;background:#111;color:#fff;border-radius:999px;padding:4px;box-shadow:0 2px 8px #0004";
    const btn = (text, label) => {
      const b = document.createElement("button");
      b.textContent = text;
      b.setAttribute("aria-label", label);
      b.style.cssText = "background:none;border:0;color:inherit;font:inherit;padding:6px 10px;border-radius:999px;cursor:pointer;white-space:nowrap";
      bar.appendChild(b);
      return b;
    };
    btn("Enable notifications", "Enable notifications for replies").onclick = async () => {
      bar.remove();
      window.ocPushState = await enable().catch((e) => "error:" + e.message);
    };
    btn("×", "Dismiss").onclick = () => { store.set("ocPushOffer", "dismissed"); bar.remove(); };
    document.body.appendChild(bar);
  }

  // One run at a time; each run is for the token it started with.
  let running = null, ranFor;
  const run = () => running ||= (ranFor = store.get("token"), ensure())
    .catch((e) => "error:" + e.message).then((state) => {
      running = null;
      // The token changed while this run was on its way: the result is stale.
      const t = store.get("token");
      if (t && t !== ranFor) return run();
      window.ocPushState = state;
      if (state === "no-permission") offer();
      return state;
    });

  // Open WebUI signs in, signs up and signs out without a page load: it stores
  // the token, then navigates on the client (SvelteKit calls history.pushState
  // or replaceState). Each navigation checks whether the token changed since the
  // last run, and runs again if so.
  const navigated = () => { const t = store.get("token"); if (t && t !== ranFor) run(); };
  for (const k of ["pushState", "replaceState"]) {
    const orig = history[k];
    history[k] = function (...args) {
      const r = orig.apply(this, args);
      navigated();
      return r;
    };
  }
  addEventListener("popstate", navigated);

  window.ocPushEnable = enable;
  window.ocPushReady = run();
})();
