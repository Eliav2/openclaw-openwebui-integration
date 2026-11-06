// Web Push for an Open WebUI behind Cloudflare (docs/install/web-push.md).
// Bind this Worker to exactly two routes of your Open WebUI hostname:
//   <host>/oc-sw.js          the service worker, served by the Worker
//   <host>/static/loader.js  Open WebUI's own loader.js with the page script appended
// Open WebUI loads /static/loader.js on every page, so no HTML is rewritten,
// and the Worker never runs on /api, /ws or /_app.
import SW from "../../../web/oc-sw.js";
import PAGE from "../../../web/oc-push.js";

const ON = "1", OFF = "0";   // a switch's values (OFFLINE, CHAT_CACHE)
const JS = { "content-type": "text/javascript; charset=utf-8", "cache-control": "no-cache" };

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/oc-sw.js") {
      // OFFLINE = "1" turns on offline open (docs/install/web-push.md); unset, it is off.
      // CHAT_CACHE = "1" (with OFFLINE on) opens a chat opened before from its kept copy; unset, off.
      const sw = SW.replaceAll("__OC_OFFLINE__", env.OFFLINE === ON ? ON : OFF)
        .replaceAll("__OC_CHAT_CACHE__", env.CHAT_CACHE === ON ? ON : OFF);
      return new Response(sw, { headers: { ...JS, "service-worker-allowed": "/" } });
    }
    if (url.pathname !== "/static/loader.js") return fetch(req);
    const res = await fetch(req);
    const own = res.ok ? await res.text() : "";
    const page = PAGE.replaceAll("__VAPID_PUBLIC_KEY__", env.VAPID_PUBLIC_KEY).replaceAll("__FUNCTION_ID__", env.FUNCTION_ID);
    return new Response(own + "\n;" + page, { headers: JS });
  },
};
