# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built artifacts directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Composer "Status" button (experiment).
#
# Open WebUI (0.12) has no hook for adding a button to the composer row:
# filter chips there only toggle a filter or open its valves. So the button is
# installed by an `execute` event, the same unsandboxed in-page JS channel the
# Status dialog already uses. Chat.svelte runs an `execute` event only when
# its chat_id is the chat the tab shows AND its message_id is already in that
# tab's history; anything else is silently dropped. The snippet:
#
#   * is idempotent: it records itself on `window.__openclawComposerBtn` with a
#     version, so a repeat delivery only refreshes its config, and a newer
#     version tears the old one down before installing itself;
#   * keeps the button present with one MutationObserver (debounced), across
#     re-renders and chat switches (OWUI is a SPA, the composer is rebuilt
#     freely), and gives up for good if it ever has to re-insert the button
#     in a tight loop;
#   * shows the button only on `/c/<id>` pages whose chat model is one of this
#     Pipe's models (one chat fetch per chat, cached for the page lifetime),
#     and removes it everywhere else;
#   * places it right after the "OpenClaw Thinking" filter chip, or at the end
#     of the composer's chip group when that chip is off; when neither anchor
#     exists it shows nothing;
#   * on click POSTs to the Status Action with the same body shape the dialog's
#     own Compact button uses, so the existing Action flow opens the real
#     dialog;
#   * never throws into the page: errors become one console warning each and,
#     for a failed click, a small toast.
#
# It is delivered three ways, all cheap and all idempotent:
#   (a) the Status Action emits it after every status run (one spark click
#       installs it);
#   (b) the Pipe emits it at the start of each turn;
#   (c) the Pipe's composer watcher pushes it to one socket as soon as that
#       socket opens a chat on one of this Pipe's models (Open WebUI 0.12
#       records each socket's open chat in SESSION_POOL[sid]["chat_ids"]).
#
# (a) and (b) target whatever message the emitter is bound to. (c) targets the
# chat's latest *user* message on purpose: OWUI's event handler writes back a
# shallow copy of the targeted message after running the code, and a user
# message is the one kind that never streams, so the push can never clobber a
# reply that is streaming into the same tab.
# ---------------------------------------------------------------------------

_COMPOSER_BUTTON_VERSION = "3"

# The Status Action's function id (install_action.py's FUNCTION_ID; the
# dialog's own JS calls the same fixed id) and the Pipe's default function id.
# A Pipe installed under another id still works: it adds its own id (from the
# module name OWUI gives it) and the prefix of every model it serves.
_COMPOSER_ACTION_ID = "openclaw_status_action"
_COMPOSER_DEFAULT_MODEL_PREFIX = "openclaw_gateway."

# Name of the Thinking filter's chip (install_filter.py registers the filter
# under this name, and OWUI renders it as the chip icon's alt text). The button
# goes right after that chip when it is on screen.
_COMPOSER_THINKING_CHIP_LABEL = "OpenClaw Thinking"

_COMPOSER_BUTTON_JS_TEMPLATE = r"""
(function () {
  const CFG = __OPENCLAW_COMPOSER_CONFIG__;
  const KEY = '__openclawComposerBtn';
  try {
    const prev = window[KEY];
    if (prev && prev.version === CFG.version) {
      if (typeof prev.configure === 'function') prev.configure(CFG);
      return;
    }
    if (prev && typeof prev.teardown === 'function') {
      try { prev.teardown(); } catch (e) { /* the old copy is going away anyway */ }
    }
  } catch (e) {
    console.warn('[openclaw-composer] guard failed', e);
    return;
  }

  const BTN_ID = 'openclaw-composer-status-button';
  // A chat that is not OpenClaw is looked at again after this long (its model
  // can be switched); an OpenClaw chat stays OpenClaw for the page lifetime.
  const NEGATIVE_TTL_MS = 600000;
  // Re-inserting the button more often than this means something keeps
  // removing it (a layout this snippet does not understand): give up.
  const LOOP_LIMIT = 20;
  const LOOP_WINDOW_MS = 10000;
  const SPARK_PATH = 'M9.813 15.904 9 18.75l-.813-2.846a4.5 4.5 0 0 0-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 0 0 3.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 0 0 3.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 0 0-3.09 3.09ZM18.259 8.715 18 9.75l-.259-1.035a3.375 3.375 0 0 0-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 0 0 2.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 0 0 2.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 0 0-2.456 2.456ZM16.894 20.567 16.5 21.75l-.394-1.183a2.25 2.25 0 0 0-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 0 0 1.423-1.423l.394-1.183.394 1.183a2.25 2.25 0 0 0 1.423 1.423l1.183.394-1.183.394a2.25 2.25 0 0 0-1.423 1.423Z';

  const state = {
    prefixes: [],
    // Informational only: the Action does not use it for the status path.
    sessionId: null,
    cache: {},
    inflight: {},
    observer: null,
    timer: null,
    busy: false,
    inserts: [],
    warned: {},
    dead: false,
  };

  function warnOnce(tag, err) {
    if (state.warned[tag]) return;
    state.warned[tag] = true;
    try { console.warn('[openclaw-composer] ' + tag, err); } catch (e) { /* no console */ }
  }

  function configure(cfg) {
    (cfg.modelPrefixes || []).forEach(function (p) {
      if (typeof p === 'string' && p && state.prefixes.indexOf(p) === -1) state.prefixes.push(p);
    });
    if (cfg.sessionId) state.sessionId = cfg.sessionId;
    // A delivery names the chat it was sent for, and is only ever sent for
    // an OpenClaw chat, so that chat needs no lookup.
    if (typeof cfg.chatId === 'string' && cfg.chatId) {
      state.cache[cfg.chatId] = { ok: true, vouched: true, at: Date.now() };
    }
    schedule();
  }

  function token() {
    try { return localStorage.getItem('token') || ''; } catch (e) { return ''; }
  }

  function currentChatId() {
    const m = /^\/c\/([^\/?#]+)\/?$/.exec(location.pathname);
    if (!m) return null;
    const id = decodeURIComponent(m[1]);
    // Temporary chats are never saved, so there is nothing to look up.
    if (id.indexOf('local:') === 0) return null;
    return id;
  }

  function isOpenclawModel(id) {
    return typeof id === 'string' && state.prefixes.some(function (p) { return id.indexOf(p) === 0; });
  }

  function chatModel(chat) {
    const c = (chat && chat.chat) || {};
    const candidates = Array.isArray(c.models) ? c.models.slice() : [];
    const h = c.history || {};
    const cur = h.messages && h.currentId ? h.messages[h.currentId] : null;
    if (cur && cur.model) candidates.push(cur.model);
    for (let i = 0; i < candidates.length; i++) {
      if (isOpenclawModel(candidates[i])) return candidates[i];
    }
    return null;
  }

  // Latest user message on the current branch, else the current leaf. The
  // Action's dialog events target this id, and a user message never streams,
  // so a dialog opened mid-reply cannot race the reply's own updates.
  function targetMessageId(chat) {
    const h = ((chat && chat.chat) || {}).history || {};
    const msgs = h.messages || {};
    let id = h.currentId;
    for (let i = 0; id && i < 500; i++) {
      const m = msgs[id];
      if (!m) break;
      if (m.role === 'user') return id;
      id = m.parentId;
    }
    return h.currentId || null;
  }

  function fetchChat(chatId) {
    return fetch('/api/v1/chats/' + encodeURIComponent(chatId), {
      headers: { 'Authorization': 'Bearer ' + token() },
    }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  function classify(chatId) {
    if (state.inflight[chatId]) return;
    state.inflight[chatId] = true;
    fetchChat(chatId).then(function (chat) {
      const prev = state.cache[chatId];
      if (prev && prev.vouched) return;
      state.cache[chatId] = { ok: !!chatModel(chat), at: Date.now() };
    }, function () {
      const prev = state.cache[chatId];
      if (prev && prev.vouched) return;
      state.cache[chatId] = { ok: false, at: Date.now() };
    }).then(function () {
      delete state.inflight[chatId];
      schedule();
    });
  }

  function toast(text) {
    try {
      const dark = document.documentElement.classList.contains('dark');
      const el = document.createElement('div');
      el.setAttribute('role', 'status');
      el.textContent = text;
      el.style.cssText = 'position:fixed;top:16px;left:50%;transform:translateX(-50%);'
        + 'z-index:10001;max-width:min(420px,90vw);padding:10px 14px;border-radius:12px;'
        + 'font-size:13px;line-height:1.4;box-shadow:0 4px 16px rgba(0,0,0,0.18);';
      el.style.background = dark ? '#1f2937' : '#ffffff';
      el.style.color = dark ? '#f3f4f6' : '#111827';
      el.style.border = '1px solid ' + (dark ? '#374151' : '#e5e7eb');
      document.body.appendChild(el);
      setTimeout(function () { el.remove(); }, 4000);
    } catch (e) {
      warnOnce('toast failed', text);
    }
  }

  // The POST body the Status Action receives. Same shape as the dialog's own
  // Compact button, minus `mode`, so the Action takes its normal status path.
  function actionBody(chatId, chat) {
    let model = chatModel(chat);
    const c = (chat && chat.chat) || {};
    const entry = state.cache[chatId];
    if (!model && entry && entry.vouched && Array.isArray(c.models) && typeof c.models[0] === 'string') {
      // A workspace model built on an OpenClaw model: its id has none of the
      // Pipe's prefixes, but the Pipe itself vouched for this chat.
      model = c.models[0];
    }
    const messageId = targetMessageId(chat);
    if (!model || !messageId) return null;
    return {
      model: model,
      chat_id: chatId,
      id: messageId,
      session_id: state.sessionId || 'openclaw-composer',
    };
  }

  function openStatus(btn) {
    if (state.busy) return;
    const chatId = currentChatId();
    if (!chatId) return;
    state.busy = true;
    btn.style.opacity = '0.6';
    fetchChat(chatId).then(function (chat) {
      const body = actionBody(chatId, chat);
      if (!body) throw new Error('this chat has no OpenClaw message yet');
      return fetch('/api/chat/actions/' + CFG.actionId, {
        method: 'POST',
        headers: {
          'Authorization': 'Bearer ' + token(),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(body),
      });
    }).then(function (r) {
      if (r && !r.ok) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          throw new Error((j && j.detail) || ('HTTP ' + r.status));
        });
      }
    }).catch(function (err) {
      console.warn('[openclaw-composer] status failed', err);
      toast('OpenClaw status: ' + ((err && err.message) || err));
    }).then(function () {
      state.busy = false;
      btn.style.opacity = '';
    });
  }

  function makeButton() {
    const b = document.createElement('button');
    b.type = 'button';
    b.id = BTN_ID;
    b.title = 'OpenClaw status';
    b.setAttribute('aria-label', 'OpenClaw status');
    b.dataset.openclawVersion = CFG.version;
    b.className = 'p-[0.375rem] flex gap-1 items-center text-sm rounded-full '
      + 'transition-colors duration-300 focus:outline-hidden shrink-0 '
      + 'bg-transparent text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-800';
    b.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" '
      + 'stroke-width="1.75" stroke="currentColor" aria-hidden="true" class="size-4">'
      + '<path stroke-linecap="round" stroke-linejoin="round" d="' + SPARK_PATH + '"/></svg>'
      // Icon-only below `sm`, like Open WebUI's own composer chips: at phone
      // width the chip row's overflow cut the label to "Stat" (ELI-123). The
      // title and aria-label still name it. Both classes ship in Open WebUI's CSS.
      + '<span class="pr-0.5 hidden sm:inline">Status</span>';
    b.addEventListener('click', function (e) {
      e.preventDefault();
      e.stopPropagation();
      openStatus(b);
    });
    return b;
  }

  // {parent, after}: insert right after `after` inside `parent`, or at the end
  // of `parent` when `after` is null. null when no known anchor is on screen.
  function findSlot() {
    const integ = document.getElementById('integration-menu-button');
    const form = integ ? integ.closest('form') : null;
    if (!form) return null;
    const imgs = form.querySelectorAll('img[alt]');
    for (let i = 0; i < imgs.length; i++) {
      if (imgs[i].getAttribute('alt') !== CFG.thinkingChipLabel) continue;
      const chip = imgs[i].closest('button');
      const wrap = chip ? chip.parentElement : null;
      if (wrap && wrap.parentElement && form.contains(wrap.parentElement)) {
        return { parent: wrap.parentElement, after: wrap };
      }
    }
    // The chip is only rendered while the filter is on. Fall back to the end
    // of the chip group the composer always renders next to it.
    const row = integ.closest('div.justify-between');
    if (!row || !form.contains(row)) return null;
    const chips = row.querySelector(':scope > div.flex-1');
    const groups = chips ? chips.querySelectorAll(':scope > div.shrink-0') : [];
    if (groups.length) return { parent: groups[groups.length - 1], after: null };
    return null;
  }

  function removeAll() {
    const all = document.querySelectorAll('#' + BTN_ID);
    for (let i = 0; i < all.length; i++) all[i].remove();
  }

  function ensure() {
    if (state.dead) return;
    const chatId = currentChatId();
    let show = false;
    if (chatId) {
      const c = state.cache[chatId];
      if (!c || (!c.ok && Date.now() - c.at > NEGATIVE_TTL_MS)) classify(chatId);
      show = !!(c && c.ok);
    }
    const slot = show ? findSlot() : null;
    if (!slot) {
      removeAll();
      return;
    }
    const all = document.querySelectorAll('#' + BTN_ID);
    let btn = all.length ? all[0] : null;
    for (let i = 1; i < all.length; i++) all[i].remove();
    if (btn && btn.dataset.openclawVersion !== CFG.version) {
      btn.remove();
      btn = null;
    }
    const placed = btn && (slot.after
      ? slot.after.nextElementSibling === btn
      : slot.parent.lastElementChild === btn);
    if (placed) return;
    const now = Date.now();
    state.inserts = state.inserts.filter(function (t) { return now - t < LOOP_WINDOW_MS; });
    state.inserts.push(now);
    if (state.inserts.length > LOOP_LIMIT) {
      warnOnce('the composer keeps removing the button; giving up until reload', null);
      teardown();
      state.dead = true;
      return;
    }
    if (!btn) btn = makeButton();
    if (slot.after) slot.after.insertAdjacentElement('afterend', btn);
    else slot.parent.appendChild(btn);
  }

  function schedule() {
    if (state.timer || state.dead) return;
    state.timer = setTimeout(function () {
      state.timer = null;
      try { ensure(); } catch (e) { warnOnce('ensure failed', e); }
    }, 150);
  }

  function teardown() {
    if (state.observer) state.observer.disconnect();
    state.observer = null;
    if (state.timer) clearTimeout(state.timer);
    state.timer = null;
    window.removeEventListener('popstate', schedule);
    try { removeAll(); } catch (e) { /* page is going away */ }
  }

  try {
    window[KEY] = {
      version: CFG.version,
      configure: configure,
      teardown: teardown,
      ensure: ensure,
      // For live checks from the console; not used by the page.
      debug: function () {
        const chatId = currentChatId();
        return {
          version: CFG.version, chatId: chatId, prefixes: state.prefixes.slice(),
          cached: chatId ? state.cache[chatId] || null : null, dead: state.dead,
          slot: !!findSlot(), buttons: document.querySelectorAll('#' + BTN_ID).length,
          inserts: state.inserts.length,
        };
      },
      actionBody: actionBody,
    };
    state.observer = new MutationObserver(schedule);
    state.observer.observe(document.body, { childList: true, subtree: true });
    window.addEventListener('popstate', schedule);
    configure(CFG);
  } catch (e) {
    warnOnce('install failed', e);
  }
})();
"""


def _composer_own_function_id():
    """This artifact's Open WebUI function id, from the module name OWUI's
    loader gives it (`function_<id>_<32 hex>`, utils/plugin.py), or None
    outside Open WebUI."""
    m = re.match(r"^function_(.+)_[0-9a-f]{32}$", globals().get("__name__") or "")
    return m.group(1) if m else None


def _composer_model_prefixes(model_id=None, own_id=None) -> list:
    """The model-id prefixes the button treats as OpenClaw chats: the default
    Pipe id, the Pipe's own function id when known, and the function id of
    `model_id` (a model this Pipe actually served)."""
    prefixes = [_COMPOSER_DEFAULT_MODEL_PREFIX]
    extra = []
    if isinstance(own_id, str) and own_id:
        extra.append(own_id + ".")
    if isinstance(model_id, str) and "." in model_id:
        extra.append(model_id.split(".", 1)[0] + ".")
    for p in extra:
        if p not in prefixes:
            prefixes.append(p)
    return prefixes


def _render_composer_button_js(*, session_id=None, model_prefixes=None, chat_id=None) -> str:
    """The install snippet with its config embedded as one JSON literal (same
    no-interpolation rule as action.py's `_json_for_js`: `<` escaped so the
    payload can never close an enclosing script tag)."""
    config = {
        "version": _COMPOSER_BUTTON_VERSION,
        "actionId": _COMPOSER_ACTION_ID,
        "modelPrefixes": list(model_prefixes or [_COMPOSER_DEFAULT_MODEL_PREFIX]),
        "sessionId": session_id if isinstance(session_id, str) and session_id else None,
        "chatId": chat_id if isinstance(chat_id, str) and chat_id else None,
        "thinkingChipLabel": _COMPOSER_THINKING_CHIP_LABEL,
    }
    payload = json.dumps(config).replace("<", "\\u003c")
    return _COMPOSER_BUTTON_JS_TEMPLATE.replace("__OPENCLAW_COMPOSER_CONFIG__", payload)


async def _emit_composer_button(__event_emitter__, *, session_id=None, model_id=None,
                                chat_id=None, model_prefixes=None) -> bool:
    """Emit the install snippet through an OWUI event emitter. Best effort:
    returns False instead of raising, because a cosmetic button must never
    fail the turn or the Action that carries it."""
    if not __event_emitter__:
        return False
    try:
        code = _render_composer_button_js(
            session_id=session_id,
            model_prefixes=model_prefixes or _composer_model_prefixes(model_id),
            chat_id=chat_id,
        )
        await __event_emitter__({"type": "execute", "data": {"code": code}})
        return True
    except Exception as ex:
        pipe_log(f"[composer] could not emit the status button: {ex}")
        return False


def _composer_latest_user_message_id(history: dict):
    """Python twin of the snippet's targetMessageId(): the latest user message
    on the current branch, else the current leaf, else None."""
    if not isinstance(history, dict):
        return None
    messages = history.get("messages") or {}
    current = history.get("currentId")
    msg_id = current
    for _ in range(500):
        if not msg_id:
            break
        msg = messages.get(msg_id)
        if not isinstance(msg, dict):
            break
        if msg.get("role") == "user":
            return msg_id
        msg_id = msg.get("parentId")
    return current or None


def _composer_chat_is_openclaw(chat_data: dict, prefixes) -> bool:
    """Python twin of the snippet's chatModel(): any of the chat's models, or
    the current leaf's model, starts with one of `prefixes`."""
    if not isinstance(chat_data, dict):
        return False
    candidates = list(chat_data.get("models") or [])
    history = chat_data.get("history") or {}
    leaf = (history.get("messages") or {}).get(history.get("currentId")) or {}
    if isinstance(leaf, dict) and leaf.get("model"):
        candidates.append(leaf["model"])
    return any(
        isinstance(m, str) and any(m.startswith(p) for p in prefixes)
        for m in candidates
    )
