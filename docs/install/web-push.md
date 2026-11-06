# Push notifications

Web Push sends a notification to your phone or computer when a reply finishes, also when Open WebUI is closed. It shows the chat's title and opens the chat on a tap. You can answer from the notification and mute a chat. The reply itself stays on your server unless you turn on the [preview](#preview).

Applies to: Web Push function, Pipe

Valves: the function's own (below), and the Pipe's [`PUBLISH_CHAT_FINISHED`](../configuration.md#publish_chat_finished)

It has three parts, all in [`push/`](https://github.com/Eliav2/openclaw-openwebui-integration/tree/main/push):

- **The Web Push function** (`push/openclaw_web_push.py`): an Open WebUI event function. On each `chat.finished` it sends a push to each of the user's devices.
- **The service worker** (`push/web/oc-sw.js`), served at `/oc-sw.js`: it receives the push, shows the notification and runs its Reply and Mute buttons.
- **The page script** (`push/web/oc-push.js`), served as `/static/loader.js`: it registers the service worker, offers "Enable notifications", saves each device's subscription, and adds the [mute bell](#mute-a-chat) to a chat. Open WebUI signs in and signs up without loading a page, so the script also runs again on Open WebUI's in-app navigation whenever the signed-in token has changed.

Open WebUI loads `/static/loader.js` on every page, but it rebuilds `/static` on every start, so both scripts are served by the proxy in front of Open WebUI: a [Cloudflare Worker](#serve-the-scripts-with-a-cloudflare-worker) or [nginx](#serve-the-scripts-with-nginx). Nothing else in Open WebUI changes.

## What you need

- Open WebUI on HTTPS (browsers allow service workers only there, or on `localhost`).
- A proxy in front of it where you can serve two paths.
- Outbound HTTPS from the Open WebUI container to the push services (`fcm.googleapis.com`, `web.push.apple.com`, `updates.push.services.mozilla.com`).
- On iPhone and iPad, iOS 16.4 or later, with Open WebUI added to the Home Screen. Safari tabs get no Web Push.

## 1. Make a VAPID key pair

The push services check each push against this key pair. Make it once, where Open WebUI can read the private key, for example in its data volume:

```bash
mkdir -p <data volume>/web-push
openssl ecparam -name prime256v1 -genkey -noout -out <data volume>/web-push/vapid_private.pem
openssl ec -in <data volume>/web-push/vapid_private.pem -pubout -outform DER | tail -c 65 | base64 | tr '/+' '_-' | tr -d '=\n'
```

The last command prints the public key (87 characters). The public key is not a secret: the page script gets it. Keep a backup of the private key; a new one makes every device subscribe again.

The private key stays in a file, not in a valve: Open WebUI stores valves unencrypted unless `ENABLE_VALVE_ENCRYPTION` is on.

## 2. Install the function

In **Admin Panel > Functions**, add a function with the content of `push/openclaw_web_push.py`, id `openclaw_web_push`, and turn it on. Open WebUI installs its one requirement, `pywebpush`, on the first load (unless `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS` is off; then install it into the container yourself).

Its valves:

| Valve | Default | What it does |
|---|---|---|
| [`VAPID_PRIVATE_KEY_FILE`](../configuration.md#vapid_private_key_file) | `$DATA_DIR/web-push/vapid_private.pem` (`/app/backend/data/web-push/vapid_private.pem` in the image) | The private key, as the container sees it. |
| [`VAPID_SUBJECT`](../configuration.md#vapid_subject) | Open WebUI's `WEBUI_URL` | A `mailto:` or `https:` contact for the push services. Apple rejects pushes without a real one. |
| [`TTL_SECONDS`](../configuration.md#ttl_seconds) | `3600` | How long a push service keeps a notification for a device that is offline. |
| [`URGENCY`](../configuration.md#urgency) | `normal` | RFC 8030 urgency: `very-low`, `low`, `normal` or `high`. |
| [`PIPE_MODEL_PREFIXES`](../configuration.md#pipe_model_prefixes) | `openclaw_gateway.` | Models served by the OpenClaw Pipe (its function id and a dot). For these only the Pipe's own `chat.finished` is sent; see below. |
| [`PREVIEW_CHARS`](../configuration.md#preview_chars) | `0` (off) | Up to this many characters of the reply, as plain text, in the notification. Read the [privacy note](#preview) first. |
| [`NOTIFICATION_ACTIONS`](../configuration.md#notification_actions) | `reply,mute` | The notification's buttons, in order, from `reply`, `read`, `mute`; see [Buttons](#buttons). |

Its user valves are written by the page script and the service worker, not by hand: `subscriptions` (one entry per device) and `mutes` (muted chats).

## 3. Turn on the Pipe's event

Open WebUI's own `chat.finished` fires when the chat request ends. For OpenClaw that misses the replies the agent starts (cron, `sessions_send`, sub-agent results) and fires too early for a reply that outlives its request. Turn on the Pipe's [`PUBLISH_CHAT_FINISHED`](../configuration.md#publish_chat_finished): the Pipe then publishes its own `chat.finished` once each reply is saved (see [Telling other functions a reply finished](../features/agent-turns.md#telling-other-functions-a-reply-finished)), and the function sends that one for OpenClaw models. Other models need nothing: Open WebUI's own event is right for them.

## 4. Serve the two scripts

The page script has two placeholders to replace: `__VAPID_PUBLIC_KEY__` (the public key from step 1) and `__FUNCTION_ID__` (`openclaw_web_push`).

### Serve the scripts with a Cloudflare Worker

`push/examples/cloudflare-worker/` is a Cloudflare Worker for exactly two routes of your Open WebUI hostname: `/oc-sw.js`, which it serves itself, and `/static/loader.js`, which it fetches from Open WebUI and appends the page script to. It never runs on `/api`, `/ws` or `/_app`.

```bash
cd push/examples/cloudflare-worker
cp wrangler.example.jsonc wrangler.jsonc   # your hostname, zone, public key and function id
npx wrangler deploy
```

The API token needs Workers Scripts: Edit (account) and Workers Routes: Edit (zone).

Behind Cloudflare Access, add a second Access application with a **Bypass** policy for `/oc-sw.js` and the icons (`/static/favicon.png`, `/static/logo.png`, `/static/apple-touch-icon.png`, `/static/web-app-manifest-*.png`). The browser checks for a new service worker without your Access session, and iOS fetches the Home Screen icon without it. These are static files with no data in them. Do not bypass all of `/static`.

### Serve the scripts with nginx

`push/examples/nginx.conf` serves both files from a folder next to nginx, with the placeholders replaced, for example:

```bash
mkdir -p /srv/open-webui-push
cp push/web/oc-sw.js /srv/open-webui-push/
sed -e "s/__VAPID_PUBLIC_KEY__/<public key>/g" -e "s/__FUNCTION_ID__/openclaw_web_push/g" \
  push/web/oc-push.js > /srv/open-webui-push/oc-push.js
```

The page script replaces Open WebUI's `loader.js`, which is empty unless you put something in it. If you did, append it to `oc-push.js`.

Open WebUI serves `/static/loader.js` with no `Cache-Control` header, so browsers cache it for a while: on Cloudflare for the zone's Browser Cache TTL (4 hours by default), otherwise for a heuristic time. A device that loaded the page before you started serving the page script keeps the old, empty file until that runs out, and shows no button: nothing of this setup runs on it before then. Both examples serve the new file with `Cache-Control: no-cache`, so this happens only once, at the switch, and every later change of the scripts reaches a device on its next page load. To avoid the wait altogether, serve the page script first, with the function still off: a device that loads Open WebUI then has the script, and turning the function on later needs nothing on the device. Its **Enable notifications** button shows while the function is off, but Open WebUI refuses to save a device for a function that is off; a device enabled then is saved on its next page load after the function is on. On Cloudflare, a Cache Rule for `/static/loader.js` with Browser TTL "Respect origin" added a day ahead does the same. To skip the wait on one device, clear the browser's cached files (not its cookies or site data) and open Open WebUI again.

## 5. Turn it on, on each device

Open Open WebUI. While the browser has not been asked yet, an **Enable notifications** button shows at the top of the page. Click it and allow notifications. The × hides the button for good on that device. On iPhone and iPad, add Open WebUI to the Home Screen first and open it from there.

Each device is saved in its user's own list, so two people on one Open WebUI each get their own notifications.

## How it behaves

- **One notification per chat, stacked.** A newer reply in the same chat joins the notification that is still showing (its tag is `chat-<chat id>`), as in a messaging app: the title counts the replies (`Trip plan · 3 new messages`) and, with previews on, the body keeps the latest 5 previews, one line each, newest last. Each new reply sounds once. Opening the chat, Mark as read or swiping the notification away closes it, and the next reply starts again from one. Two chats with the same title are two notifications.
- **Not while you use it.** While Open WebUI is open, visible and focused on a device, whatever chat is open, that device shows nothing, as in a messaging app; Open WebUI's own sidebar shows the new reply. In the background, closed or with the screen off, it notifies. Other devices still show it. iOS always shows it, because WebKit revokes a subscription whose pushes show nothing.
- **Title, and the reply only with the preview.** The push carries the chat's title, its id, the link and the function's id. Open WebUI's `chat.finished` data holds the whole reply in `message` (Open WebUI 0.13.0 and the Pipe's event); the function forwards only the [preview](#preview), and only when it is on.
- **A crab claw in the status bar.** Android draws a notification's small icon (the `badge`) from its transparency only, so the service worker sends a white claw on a transparent background; the picture beside the text is the same claw in colour. Both are inside `oc-sw.js` (drawn from `push/web/icons/*.svg` by `make_icons.py`), so they need no file of their own in `/static` and no Access bypass.
- **Opened means read.** When a chat is on screen on a device, opened in any way (the notification, the sidebar, a link, the app coming back to the front on it), that device closes the chat's notification. Other devices keep theirs until the chat is opened there.
- **Tap to open.** A tap on the notification focuses an open Open WebUI window and opens the chat inside it, as a click in the sidebar does (no page reload; already on that chat, nothing changes), or opens a new window when none is open. A page with an older page script does not answer the service worker; after 3 seconds that window loads the chat instead. Behind Cloudflare Access with an expired session, you sign in first and land in the chat.
- **Buttons.** Reply and Mute 8 h by default, or Mark as read; see [Buttons](#buttons).
- **A new service worker** takes over by itself: on the next page load, and when an app left open comes back to the front. Nothing to clear or reinstall.
- **After an Open WebUI update.** Open WebUI removes every service worker when it updates. The next time you open it on a device, it registers again with a new subscription, which replaces the old one. Replies that finish before that are not notified on that device.
- **Another user on the same device.** When someone else signs in on a device, the page script drops the old subscription and makes a new one for them. Signing out alone does not: the device keeps getting the previous user's notifications until someone signs in again, or until notifications are turned off for the site.
- **Gone subscriptions.** A device whose push service answers 404 or 410 is removed from the user's list.

What the function did is in Open WebUI's log:

```
INFO     | function_openclaw_web_push_<hash>:send_all:<line> - web push: sent to fcm.googleapis.com
INFO     | function_openclaw_web_push_<hash>:event:<line> - web push: chat 1a2b3c4d is muted, nothing sent
INFO     | function_openclaw_web_push_<hash>:event:<line> - web push: chat 1a2b3c4d: folder 5e6f7a8b is muted, nothing sent
```

### Preview

With [`PREVIEW_CHARS`](../configuration.md#preview_chars) above 0 the notification shows the start of the reply instead of "New reply": plain text, tool and status blocks, images and files left out, a code block as "[code]", cut on a word. Expanded, Android shows the whole preview. A push holds about 4 KB, so a long preview is shortened to fit.

Privacy: the text is on the lock screen and in the notification shade of every device you enabled, where anyone holding the phone can read it. It passes through the browser's push service (Google, Apple or Mozilla) encrypted end to end (`aes128gcm`); the push service cannot read it. Android can hide notification text on the lock screen: Settings > Notifications > "Sensitive notifications" off. That is why the preview is off by default.

### Buttons

Android Chrome shows two buttons under a notification (`Notification.maxActions` is 2), and adds a third of its own. The function's [`NOTIFICATION_ACTIONS`](../configuration.md#notification_actions) valve picks them, in order (default `reply,mute`; `reply,read` swaps Mute for Mark as read):

- **Reply** (`reply`) opens a text field in the notification. What you type is sent to the chat as your message, and the agent answers as if you had typed it in the app. It is sent the way Open WebUI 0.13.0's chat page sends a message (one `/api/chat/completions` with the new message under the chat's last one), with the session cookie of your sign-in. While the chat's reply is still running, the message reaches it like one sent from another tab: the OpenClaw Pipe steers it into the run, or queues it until the run ends (see [Messages sent while the agent works](../features/reliability.md#messages-sent-while-the-agent-works)). If it cannot be sent (signed out, Access session expired, offline, an Open WebUI that answers differently), the chat opens with your text in the message box and a line saying it was not sent. It depends on Open WebUI 0.13.0's chat API; with another version, check that a reply from the notification appears in the chat.
- **Mute 8 h** (`mute`): no notifications from this chat for 8 hours, on any of your devices (see below).
- **Mark as read** (`read`): the chat's unread dot in Open WebUI's sidebar goes, on every device, without opening the chat. Open WebUI 0.13.0 keeps that dot on the server (the chat's `last_read_at`) and sets it for one chat only through its Socket.IO event `events:chat` `{type: "last_read_at"}`, the one its chat page sends when you open a chat. The service worker gets your session's token from `GET /api/v1/auths/` (the session cookie), sends that event over a WebSocket to `/ws/socket.io`, waits for Open WebUI's answer and closes; the token is not kept. If it cannot (signed out, offline), the chat opens instead, which reads it.
- **Unsubscribe** is Chrome's own: it turns notifications off for the whole site on that device. To turn them on again, allow notifications for the site in Chrome's site settings and open Open WebUI.

Other browsers show the buttons differently or not at all; a browser without a text field opens the chat with the message box focused. iOS shows no buttons: a tap opens the chat.

### Mark a chat read from the chat list

On a device with notifications on, the ⋯ menu of a chat in Open WebUI's sidebar has **Mark as read**, right after Open WebUI's own "Mark as unread": the dot goes without opening the chat, through the same event as the notification's button. It is added by the page script to Open WebUI 0.13.0's menu, recognised by its structure and the "Mark as unread" icon, not by text (the menu is translated). If a later Open WebUI draws the menu differently, nothing is added and the browser console says once "Open WebUI's chat menu is not the one this script knows"; the menu itself is never changed. `push/e2e` tests this contract against the pinned Open WebUI, so an upgrade that moves the menu fails there.

### Mute a chat

In a chat, a bell at the right of Open WebUI's header (before the chat menu ⋯, when you have one) shows whether the chat notifies, and until when it is muted. Click it for **Mute for 1 hour**, **Mute for 8 hours**, **Mute always** or **Unmute**. A mute that ends on another day names the day ("Muted until Sat 09:05"). The same menu opens from **Mute notifications** in the chat's ⋯ menu in the sidebar, after Mark as read; when the chat is muted that item says until when. The bell and the item show only once a device of yours is enabled.

A mute is kept on the server, in the function's `mutes` user valve, so it applies to every device of yours, iPhone included: the function checks it before it encrypts anything, and sends nothing for a muted chat. It is per user: muting a shared chat mutes it only for you.

### Mute a folder

In a chat that is in a folder, the bell's menu has a second part for that folder: **Mute folder for 1 hour**, **for 8 hours**, **always**, or **Unmute folder**. A folder's own ⋯ menu, in the sidebar and on the folder's page, has **Mute folder** after Open WebUI's "Mark all as read" (or "Muted …" when it is muted), which opens the same choices for that folder. A folder's mute covers the chats in it and in every folder inside it, so a folder for tests or automated chats can stay quiet while you keep your own chats. The bell of a chat muted through its folder says so, for example "Muted (folder QA)"; when the mute comes from a folder above, the menu names it and unmutes that one.

Folder mutes are kept like chat mutes, in the `folder_mutes` user valve (`{folder id: until}`). Only while one is in effect does the function read the chat's folder and your folders before it sends.

The ⋯ menu items are added the same way as [Mark as read](#mark-a-chat-read-from-the-chat-list): to Open WebUI 0.13.0's menus, found by their structure and the "Mark as unread" or "Mark all as read" icon. A menu drawn differently gets nothing, and the console says once which menu it did not recognise (except on a folder's page, where its ⋯ cannot be told from the page's other menus, such as the icon picker: there it is only left alone); Open WebUI's own items are never changed, and ours come back if Open WebUI redraws the menu while it is open.

The bell and the Mute button read all your user valves and write them back with one changed (Open WebUI replaces them all on each write). Two changes at the same time can lose one of them (see [Known limits](#known-limits)).

### iPhone and iPad

- Every push shows a notification, also with Open WebUI in front (WebKit's rule).
- No buttons: no Reply, no Mute in the notification. Mute with the bell.
- The preview, muted chats and muted folders work as on Android.

## Offline open

Off by default. Turned on, the service worker also keeps Open WebUI itself and the chats you read last, so the app opens on a slow network, or with none, read-only. Turn it on with the Cloudflare Worker's `OFFLINE` variable set to `"1"` (in `vars` of your `wrangler.jsonc`), or, with nginx, by replacing `__OC_OFFLINE__` with `1` in your copy of `oc-sw.js` (`sed -i s/__OC_OFFLINE__/1/ /srv/open-webui-push/oc-sw.js`).

What it does, request by request (only `GET` requests to your own Open WebUI; everything else goes straight to the network):

| Request | How |
|---|---|
| A page (`/`, `/c/<id>`, any route: Open WebUI is one page) | The kept page at once, when it was kept with its Open WebUI version; `/api/version` is asked alongside it. Another version: everything kept is deleted and the page reloads once onto the server's own app. A redirect instead of a version (an expired Cloudflare Access session): the kept page is deleted and the reload shows the login. No page kept yet: the network. |
| Open WebUI's hashed files (`/_app/immutable/`) | Kept on first use and then read from the device: their names change with their content. |
| `/static/`, `/assets/`, your settings, models, config, banners, tags, functions, skills | The kept copy at once, refreshed in the background for the next open. |
| The chat list, pinned chats, folders, each chat you open, `/api/version`, `/_app/version.json`, `/manifest.json` | The network; the kept copy only when it fails. A chat opened before: the kept copy at once with `CHAT_CACHE` on ([below](#a-chat-opened-before-at-once)). |
| Profile pictures (models, users) | The kept copy at once, refreshed in the background. |

A network that answers nothing at all (lie-fi) is given up on after 4.2 s (twice the slowest answer measured on Slow 3G); then the kept copy is used. If the page kept that way turns out to be from another Open WebUI version, the page reloads.

- **Only clean answers are kept:** a `200` from your own site, never a redirect (a profile picture may come through one to Open WebUI's own icon). A page is kept only if it is Open WebUI's (it loads `/_app/immutable/` files), so a Cloudflare Access login page or an error page never becomes the app.
- **Per user:** the API answers and pictures are kept per signed-in user (in a cache named after a hash of the user's token). Signing in again or as someone else starts a new one and deletes the others.
- **Updates:** a new Open WebUI version deletes everything kept (see the page row). A new build's page also drops the old build's hashed files.
- **Sizes:** at most 400 app files and 300 API answers per user, oldest first.
- **Read-only offline:** with no network a pill says "Offline: saved chats, read-only" and the message box is locked, so nothing is typed that cannot be sent. Text already in the box stays and can be sent once the network is back.
- **A read cut on its way** (a change of network fails every request in flight): the service worker asks it once more at once, on the new network, because Open WebUI does not and would stay half drawn.
- **Start reads cut by a network change, offline open off too:** the page script asks Open WebUI's start reads (config, user, settings, models, the chat list, the chat) once more when the network cuts one on its way; Open WebUI asks them only once and would stay half drawn.
- **After a network change:** a change of network while Open WebUI loads (Wi-Fi to mobile) can fail one of its files, and Open WebUI then shows "500 Internal Error" for good. The page script reloads once, as soon as the server can be reached again (at once, or on the next `online` or network change). It reloads only once per tab until a load gets to the app. This works with offline open on or off.
- **Turning it off** (`OFFLINE` unset or not `"1"`): the next service worker that a device loads deletes everything it kept, and again on the next page load.

### A chat opened before, at once

Off by default; it needs offline open on. Turn it on with the Worker's `CHAT_CACHE` variable set to `"1"`, or, with nginx, by replacing `__OC_CHAT_CACHE__` with `1` in your copy of `oc-sw.js`. Open WebUI reads a whole chat (every message, as JSON: megabytes for a long chat) on every open and shows none of it before the last byte, so on a slow network a long chat takes as long to open as its size takes to download.

With it on, a chat you opened before on that device opens from the copy kept of it, at once, and the network's copy is fetched behind it:

- **The fresh copy replaces it by itself.** Open WebUI reads an opened chat a second time once it has joined the chat's live updates (and again after a reconnect), and merges what it gets into the chat on screen. That second read waits for the network's copy, so the newest messages, a reply that came while the app was closed included, appear as soon as it arrives. If it arrived before that read, the read gets it from the device: one download.
- **Marked until then:** a pill says "Updating…" and the message box is locked, as offline: a message sent on an old copy could branch off an old point of the chat. No network for the second read: the kept copy stays on screen (Open WebUI would otherwise leave the chat), and the next read (the reconnect) tries again.
- **Per user, never shared:** the kept chats are in the signed-in user's cache (named after a hash of the user's token, as above). Signing in as someone else deletes the other user's cache, and Open WebUI's sign-out (`/api/v1/auths/signout`) deletes every user's. A chat the server answers with an error (deleted, or no longer yours) is deleted from the device.
- **Sizes:** a chat bigger than 32 MB is not kept (it would go through the service worker's memory on every open; the biggest chats we measured were 31, 20 and 16 MB). All the kept chats of a user together stay under 128 MB: the chat opened longest ago goes first.
- **Turning it off** (`CHAT_CACHE` unset or not `"1"`): the next service worker a device loads opens chats from the network again; the copies already kept serve only offline, as any kept read.

## Known limits

- **Two mute changes at the same time can lose one.** The bell, the menus and the notification's Mute button each read your user valves and write them all back with their one change, because Open WebUI's user-valve update replaces the whole set. If a second change reads them before the first one's write has landed, the second write puts back the old value of the first: the two read-and-write round trips overlap, so how long the window lasts depends on the network (longer on a slow phone connection). Any two writers can do this: two devices, two tabs, or a notification's Mute button next to an open tab. If a mute did not stick, change it again.

## Turning it off

Each part is separate:

- Turn the function off: nothing is sent.
- `PREVIEW_CHARS` back to `0`: notifications say "New reply" again.
- Remove the Cloudflare Worker's routes or the nginx locations: pages load the stock `loader.js`. Service workers that are already registered stay idle until Open WebUI's next update removes them.
- Turn the Pipe's `PUBLISH_CHAT_FINISHED` off: OpenClaw replies are no longer notified.

## Tests

`tests/test_web_push_unit.py` covers the payload (never the reply), which events are sent, and a real encrypted send to a local push service. `push/e2e/` drives Google Chrome against the real push service (FCM), the Cloudflare Worker example under `wrangler dev` and a throwaway Open WebUI:

```bash
push/e2e/stack.sh up /tmp/web-push-e2e openclaw_pipe.py
E2E_RUN=/tmp/web-push-e2e WEB_PUSH_E2E=1 xvfb-run -a uv run --with playwright==1.64.0 --with pytest pytest -q push/e2e/test_web_push_e2e.py
push/e2e/stack.sh down /tmp/web-push-e2e
```

It needs Google Chrome (Chromium has no push service), Docker, `bunx` and outbound internet.
