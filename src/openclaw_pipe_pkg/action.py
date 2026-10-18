# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_status_action.py directly.
# Source of truth: src/openclaw_pipe_pkg/action.py + build.py
# ------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Milestone 1: prove a second OWUI Function can reach the OpenClaw gateway
# and read live session data, without disturbing the existing Pipe or its
# status-line code (emit.py / pipe.py are untouched by this fragment).
#
# Connection strategy, in order:
#   1. Reuse the Pipe's already-connected `_GatewayConnection` singleton.
#      The Pipe stashes it on `open_webui.socket.main` (a stable OWUI
#      module both Functions can import) purely so ITS OWN future redeploys
#      can find and clean up an orphaned previous connection -- see
#      `_STALE_CONN_ATTR` / `_remember_gateway_connection` in gateway.py.
#      This fragment only ever READS that attribute, never writes it: if we
#      also wrote there, whichever of {Pipe, Action} connected most
#      recently would silently "win" the slot, and the Pipe's own
#      `_reap_stale_gateway_connection()` could tear down a connection it
#      doesn't actually own on its next redeploy. Read-only avoids that
#      collision entirely.
#   2. If the Pipe hasn't connected yet in this process (e.g. right after a
#      container restart, before any chat turn happened -- in practice
#      unlikely, since this Action's button only appears above an existing
#      assistant message, which means the Pipe already ran at least once),
#      open our own connection. It still reuses the same STATE_DIR-persisted
#      device identity/token the Pipe already has, so this never triggers a
#      second `openclaw devices approve`. This fallback connection is owned
#      entirely by this module (its own singleton, never stashed on the
#      shared attribute) so it can never collide with the Pipe's bookkeeping
#      either.
#
# This fragment reuses gateway.py verbatim (see build.py's ACTION artifact),
# without the turn writer: its fallback connection only makes requests.

_action_fallback_connection: "_GatewayConnection | None" = None
_action_fallback_lock = asyncio.Lock()


async def _get_action_connection(valves_getter):
    """Return (connection, source) where source is "pipe" or "action-own"."""
    try:
        import open_webui.socket.main as _owui_socket_main
        pipe_conn = getattr(_owui_socket_main, _STALE_CONN_ATTR, None)
        if pipe_conn is not None:
            await pipe_conn.ensure_connected()
            return pipe_conn, "pipe"
    except Exception as ex:
        pipe_log(f"[status-action] could not reuse Pipe connection: {ex}")

    global _action_fallback_connection
    if _action_fallback_connection is None:
        async with _action_fallback_lock:
            if _action_fallback_connection is None:
                conn = _GatewayConnection(valves_getter)
                await conn.ensure_connected()
                _action_fallback_connection = conn
                return conn, "action-own"
    await _action_fallback_connection.ensure_connected()
    return _action_fallback_connection, "action-own"


# ---------------------------------------------------------------------------
# Milestone 2 (+ later, per-section independent loading): a real dialog
# instead of a toast, rendered via the "execute" event (runs unsandboxed in
# the actual OWUI page, not a sandboxed Rich UI iframe) so it can use
# OWUI's own live Tailwind classes and CSS custom properties directly --
# exact design-system match for free, no hand-tuned color palette, no
# guessing which of OWUI's several themes is active. The trade-off
# (unsandboxed JS) is acceptable here because the code is 100%
# admin-authored, not influenced by any untrusted input; the only dynamic
# values are our own gateway's numbers, passed in as a single JSON blob per
# section (never string-interpolated) so nothing can break out of the data
# payload.
#
# Each of Context, Rate Limits, and Subagents loads and fills independently:
# three separate skeleton placeholders open
# immediately, and each is replaced by its own `execute` fill event as soon
# as ITS OWN gateway call resolves, rather than one combined
# fetch-everything-then-render-once step where a slow call holds up
# everything else. Subagents is fully independent (sessions.list only, no
# dependency on the other two). Rate Limits genuinely needs the active
# provider's name, which only comes from sessions.describe -- rather than
# faking independence there, its fill waits on Context's own resolution of
# that RPC (shared Task, not a second request) before adding usage.status
# on top; it still has its own skeleton and fills in on its own schedule,
# separate from Context's.
#
# Shared JS helpers (bar rendering, color thresholds, section headers, the
# Compact button's click handler) are defined once in _MODAL_OPEN_JS_TEMPLATE and
# stashed on `window.__openclawStatus` rather than duplicated in each
# section's fill template -- each fill is its own separate `execute` call
# (a fresh top-level script, no shared scope with the others), so without
# this every section would need its own copy of ~30 lines of identical
# helper code.
# ---------------------------------------------------------------------------

_MODAL_OPEN_JS_TEMPLATE = r"""
(function() {
  const IDENTITY = __OPENCLAW_IDENTITY__;
  const existing = document.getElementById('openclaw-status-modal-root');
  if (existing) existing.remove();

  const isDark = document.documentElement.classList.contains('dark');

  // Used-percent -> color: green while healthy, amber approaching the
  // limit, red once it's mostly consumed. Same thresholds for every bar.
  //
  // Inline hex, not Tailwind utility classes: Tailwind only ships the
  // utility classes it finds referenced somewhere in ITS OWN build's
  // source, so a class this injected code invents (rather than one OWUI's
  // own frontend already uses) can silently have zero CSS behind it --
  // confirmed live: 'bg-rose-500' is never referenced anywhere in
  // open-webui/open-webui's own source, so that utility class was never
  // generated in OWUI's compiled stylesheet, and a bar using it rendered
  // as an invisible 0-color fill even at 86% width. Inline styles have no
  // dependency on the host page's Tailwind content scan at all.
  function barColor(usedPercent) {
    if (usedPercent >= 85) return '#f43f5e';
    if (usedPercent >= 60) return '#f59e0b';
    return '#10b981';
  }

  function makeBar(usedPercent) {
    const track = document.createElement('div');
    track.className = 'h-1.5 w-full rounded-full overflow-hidden';
    track.style.backgroundColor = isDark ? '#1f2937' : '#f3f4f6';
    const fill = document.createElement('div');
    const clamped = Math.max(0, Math.min(100, usedPercent));
    fill.className = 'h-full rounded-full';
    fill.style.backgroundColor = barColor(usedPercent);
    fill.style.width = clamped + '%';
    track.appendChild(fill);
    return track;
  }

  function sectionHeader(text) {
    const h = document.createElement('div');
    h.className = 'font-semibold tracking-wide uppercase '
      + 'text-gray-400 dark:text-gray-500 mb-1.5';
    h.style.fontSize = '10px';  // inline: see the panel's width below
    h.textContent = text;
    return h;
  }

  function errorEl(message) {
    const err = document.createElement('div');
    err.className = 'text-sm';
    err.style.color = isDark ? '#fb7185' : '#e11d48';
    err.textContent = message;
    return err;
  }

  // A section still loading: its title (when given) over a pulse.
  function skeletonSection(id, title) {
    const el = document.createElement('div');
    el.id = id;
    el.className = 'mb-4';
    if (title) el.appendChild(sectionHeader(title));
    // Marks the section as still loading until a fill replaces it.
    const pulse = document.createElement('div');
    pulse.dataset.openclawLoading = '1';
    pulse.innerHTML = '<div class="animate-pulse space-y-2">'
      + '<div class="h-3 w-2/3 bg-gray-200 dark:bg-gray-700 rounded"></div>'
      + '<div class="h-3 w-full bg-gray-200 dark:bg-gray-700 rounded"></div>'
      + '</div>';
    el.appendChild(pulse);
    return el;
  }

  // When the data was fetched (epoch ms), on the browser's clock like the
  // rate-limit resets: the Open WebUI server's timezone is not the reader's
  // (ELI-140).
  function clockTime(ms) {
    return new Date(ms).toLocaleTimeString([], {
      hour: '2-digit', minute: '2-digit', second: '2-digit'
    });
  }

  // "Open in OpenClaw": this session's page in the Control UI, in a new tab
  // (CONTROL_UI_URL valve, ELI-141).
  function controlUiLink(url) {
    const a = document.createElement('a');
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.className = 'text-xs text-blue-600 dark:text-blue-400 hover:underline whitespace-nowrap';
    a.textContent = 'Open in OpenClaw ↗';
    return a;
  }

  function touchFooter(fetchedAt, source) {
    const f = document.getElementById('openclaw-status-footer');
    if (f) f.textContent = 'Updated ' + clockTime(fetchedAt) + ' · via ' + source;
  }

  // Fetches back to this same Action (same-origin, unsandboxed execute
  // context -- no iframe/postMessage plumbing needed) with a synthetic
  // mode="compact" marker. Reads OWUI's own stored auth token directly
  // (confirmed against OWUI's frontend source: it keeps its bearer token
  // at localStorage['token']) rather than assuming cookie auth. The
  // chat/message/session identity is the *original* click's (captured at
  // open time, in IDENTITY -- these never change over the dialog's
  // lifetime and never need an RPC, unlike everything else here), so the
  // Python side's __event_emitter__ calls for this follow-up route back
  // to this same open tab. The fetch response itself is ignored -- all UI
  // updates arrive as separate execute events pushed from the Python side
  // as the compact operation progresses.
  function triggerCompact() {
    const token = localStorage.getItem('token');
    fetch('/api/chat/actions/openclaw_status_action', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: IDENTITY.owuiModel,
        chat_id: IDENTITY.chatId,
        id: IDENTITY.messageId,
        session_id: IDENTITY.sessionId,
        dialogId: IDENTITY.dialogId,
        mode: 'compact',
      }),
    }).catch(function(err) {
      const wrap = document.getElementById('openclaw-status-sections');
      if (!wrap) return;
      wrap.innerHTML = '';
      wrap.appendChild(errorEl('Could not start compact: ' + err));
    });
  }

  // TaskSummary timestamps can arrive as either an ISO string or an epoch-ms
  // integer (schema allows both) -- normalize once here rather than in every
  // caller. Elapsed time is computed client-side from raw ms (not a
  // server-formatted duration) for the same reason resetAtMs already is:
  // avoids clock-skew between whenever the RPC resolved and whenever the
  // user actually reads the row.
  function toMs(v) {
    if (v == null) return null;
    if (typeof v === 'number') return v;
    const parsed = new Date(v).getTime();
    return isNaN(parsed) ? null : parsed;
  }

  function elapsedTime(startedAt, endedAt) {
    const startMs = toMs(startedAt);
    if (startMs == null) return '';
    const endMs = toMs(endedAt) || Date.now();
    const deltaS = Math.max(0, Math.floor((endMs - startMs) / 1000));
    const days = Math.floor(deltaS / 86400);
    const hours = Math.floor((deltaS % 86400) / 3600);
    const minutes = Math.floor((deltaS % 3600) / 60);
    if (days) return days + 'd' + String(hours).padStart(2, '0') + 'h';
    if (hours) return hours + 'h' + String(minutes).padStart(2, '0') + 'm';
    if (minutes) return minutes + 'm';
    return deltaS + 's';
  }

  // The dialog's own requests back to this same Action, the way
  // triggerCompact() makes one: same-origin, OWUI's stored token, and the
  // opening click's identity, so the Action's pushed fills reach this tab.
  function askAction(extra) {
    return fetch('/api/chat/actions/openclaw_status_action', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + localStorage.getItem('token'),
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(Object.assign({
        model: IDENTITY.owuiModel,
        chat_id: IDENTITY.chatId,
        id: IDENTITY.messageId,
        session_id: IDENTITY.sessionId,
        dialogId: IDENTITY.dialogId,
      }, extra)),
    });
  }

  // A sub-agent's status as a coloured chip (ELI-155): the run statuses of
  // a child session, and the states an in-process agent's card tells.
  const CHIP = {
    queued: ['Queued', '#9ca3af'], running: ['Running', '#3b82f6'], completed: ['Done', '#10b981'],
    failed: ['Failed', '#f43f5e'], cancelled: ['Cancelled', '#f59e0b'], timed_out: ['Timed out', '#f59e0b'],
    ended: ['Ended', '#9ca3af'], launched: ['Background', '#9ca3af'], returned: ['Returned', '#10b981'],
    'cut off': ['Cut off', '#f59e0b'], scheduled: ['Scheduled', '#9ca3af'], skipped: ['Skipped', '#f59e0b'],
  };
  function setChip(chip, status) {
    const c = CHIP[status] || [String(status || 'unknown'), '#9ca3af'];
    chip.textContent = c[0];
    chip.style.color = c[1];
    chip.style.backgroundColor = c[1] + '22';
    return chip;
  }
  function statusChip(status) {
    const chip = document.createElement('span');
    chip.dataset.openclawChip = '1';
    chip.className = 'shrink-0 rounded-full font-semibold whitespace-nowrap';
    chip.style.fontSize = '11px';
    chip.style.padding = '1px 7px';
    return setChip(chip, status);
  }

  // Inline, for the same reason as barColor(): a class OWUI's build never
  // scanned can carry no CSS.
  function oneLine(el) {
    el.style.whiteSpace = 'nowrap';
    el.style.overflow = 'hidden';
    el.style.textOverflow = 'ellipsis';
    el.style.minWidth = '0';
    return el;
  }

  function clampLines(el, lines) {
    el.style.display = '-webkit-box';
    el.style.webkitBoxOrient = 'vertical';
    el.style.webkitLineClamp = String(lines);
    el.style.overflow = 'hidden';
    return el;
  }

  // A text clamped to `lines`, with "Show all". Whether it is open is kept
  // in `state[name]`, so a refill does not close it again.
  function textBlock(name, text, lines, state) {
    const wrap = document.createElement('div');
    wrap.dataset.openclawBlock = name;
    const body = document.createElement('div');
    body.dataset.openclawBlockText = '1';
    body.className = 'text-sm text-gray-700 dark:text-gray-200 whitespace-pre-wrap break-words';
    body.textContent = text;
    clampLines(body, lines);
    wrap.appendChild(body);
    const more = document.createElement('button');
    more.type = 'button';
    more.dataset.openclawMore = name;
    more.className = 'text-xs text-blue-600 dark:text-blue-400 mt-1';
    function show(open) {
      body.style.webkitLineClamp = open ? 'unset' : String(lines);
      more.textContent = open ? 'Show less' : 'Show all';
    }
    more.onclick = function() { state[name] = !state[name]; show(state[name]); };
    show(!!state[name]);
    wrap.appendChild(more);
    return wrap;
  }

  // The sub-agent drawer: a compact header, then the run as a read-only chat,
  // as the Pipe's history renderer draws it for the chat.

  // Open WebUI's tool card (ToolCallDisplay.svelte): its markup, classes and
  // icons, so a card here looks like one in the chat.
  const ICON = {
    done: '<svg class="size-4" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" aria-hidden="true">'
      + '<path stroke-linecap="round" stroke-linejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"/></svg>',
    none: '<svg class="size-3.5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
      + '<path fill-rule="evenodd" d="M12 6.75a5.25 5.25 0 0 1 6.775-5.025.75.75 0 0 1 .313 1.248l-3.32 3.319c.063.475.276.934.641 1.299.365.365.824.578 1.3.64l3.318-3.319a.75.75 0 0 1 1.248.313 5.25 5.25 0 0 1-5.472 6.756c-1.018-.086-1.87.1-2.309.634L7.344 21.3A3.298 3.298 0 1 1 2.7 16.657l8.684-7.151c.533-.44.72-1.291.634-2.309A5.342 5.342 0 0 1 12 6.75ZM4.117 19.125a.75.75 0 0 1 .75-.75h.008a.75.75 0 0 1 .75.75v.008a.75.75 0 0 1-.75.75h-.008a.75.75 0 0 1-.75-.75v-.008Z" clip-rule="evenodd"/></svg>',
    down: '<svg class="size-3" fill="none" viewBox="0 0 24 24" stroke-width="3.5" stroke="currentColor" aria-hidden="true">'
      + '<path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5"/></svg>',
    up: '<svg class="size-3" fill="none" viewBox="0 0 24 24" stroke-width="3.5" stroke="currentColor" aria-hidden="true">'
      + '<path stroke-linecap="round" stroke-linejoin="round" d="m4.5 15.75 7.5-7.5 7.5 7.5"/></svg>',
  };

  function parseJson(text) {
    let value = text;
    while (typeof value === 'string') {
      try { value = JSON.parse(value); } catch (e) { break; }
    }
    return value;
  }

  function cardLabel(text) {
    const el = document.createElement('div');
    el.className = 'text-[0.625rem] uppercase tracking-wider font-normal text-gray-400 dark:text-gray-500 mb-1.5 px-1';
    el.textContent = text;
    return el;
  }

  function cardPre(text, json) {
    const pre = document.createElement('pre');
    pre.className = json
      ? 'text-xs text-gray-600 dark:text-gray-300 whitespace-pre font-mono bg-gray-50 dark:bg-gray-900 rounded-lg p-2 overflow-x-auto'
      : 'text-xs text-gray-600 dark:text-gray-300 whitespace-pre-wrap break-words font-mono';
    pre.textContent = text;
    return pre;
  }

  // One tool call: collapsed, its name and a done mark; open, its input and
  // output. `opened[id]` keeps it open across refills, keyed by the call.
  function toolCard(c, opened) {
    const card = document.createElement('div');
    card.dataset.openclawCard = c.id || '';
    const btn = document.createElement('div');
    btn.dataset.openclawCardButton = '1';
    btn.setAttribute('role', 'button');
    btn.className = 'py-1 text-[0.9375rem] text-gray-500 hover:text-gray-700 dark:hover:text-gray-300 transition '
      + 'w-full min-w-0 cursor-pointer';
    const row = document.createElement('div');
    row.className = 'w-full min-w-0 max-w-full font-normal flex items-center gap-1.5';
    const mark = document.createElement('div');
    mark.className = c.result == null ? 'text-gray-400 dark:text-gray-500' : 'text-emerald-500 dark:text-emerald-400';
    mark.innerHTML = c.result == null ? ICON.none : ICON.done;
    row.appendChild(mark);
    const label = document.createElement('div');
    label.className = 'flex-1 min-w-0 line-clamp-1';
    const name = document.createElement('span');
    name.dataset.openclawCardName = '1';
    name.className = 'text-black dark:text-white';
    name.textContent = c.name;
    label.appendChild(name);
    row.appendChild(label);
    const chevron = document.createElement('div');
    chevron.className = 'flex shrink-0 self-center translate-y-[1px]';
    row.appendChild(chevron);
    btn.appendChild(row);
    card.appendChild(btn);

    const body = document.createElement('div');
    body.className = 'border border-gray-50 dark:border-gray-850/30 rounded-2xl my-1.5 p-2.5 space-y-2';
    const input = document.createElement('div');
    input.appendChild(cardLabel('Input'));
    const args = parseJson(c.args || '');
    if (args && typeof args === 'object' && !Array.isArray(args)) {
      const list = document.createElement('div');
      list.className = 'px-1 space-y-0.5';
      Object.keys(args).forEach(function(key) {
        const line = document.createElement('div');
        line.className = 'flex gap-2 text-xs py-0.5';
        const k = document.createElement('span');
        k.className = 'font-normal text-gray-600 dark:text-gray-400 shrink-0';
        k.textContent = key;
        const v = document.createElement('span');
        v.className = 'text-gray-800 dark:text-gray-200 break-all';
        v.textContent = typeof args[key] === 'object' ? JSON.stringify(args[key]) : String(args[key]);
        line.appendChild(k);
        line.appendChild(v);
        list.appendChild(line);
      });
      input.appendChild(list);
    } else if (c.args) {
      input.appendChild(cardPre(String(c.args), false));
    }
    body.appendChild(input);
    const output = document.createElement('div');
    output.dataset.openclawCardOutput = '1';
    output.appendChild(cardLabel('Output'));
    if (c.result == null) {
      const none = document.createElement('div');
      none.className = 'text-xs text-gray-400 dark:text-gray-500 px-1';
      none.textContent = 'No result.';
      output.appendChild(none);
    } else {
      const parsed = parseJson(c.result);
      output.appendChild(parsed !== null && typeof parsed === 'object'
        ? cardPre(JSON.stringify(parsed, null, 2), true) : cardPre(c.result, false));
    }
    body.appendChild(output);
    card.appendChild(body);

    function show(open) {
      body.style.display = open ? 'block' : 'none';
      chevron.innerHTML = open ? ICON.up : ICON.down;
      card.dataset.open = open ? 'true' : 'false';
    }
    btn.onclick = function() { opened[c.id] = !opened[c.id]; show(opened[c.id]); };
    show(!!opened[c.id]);
    return card;
  }

  // A message's text, with a safe subset of Markdown: paragraphs, lists,
  // code blocks, **bold**, `code` and http(s) links. Built with textContent,
  // so nothing in the text is ever HTML.
  function inline(parent, text) {
    text.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))/).forEach(function(part) {
      if (!part) return;
      let el;
      if (/^\*\*[^*]+\*\*$/.test(part)) {
        el = document.createElement('strong');
        el.textContent = part.slice(2, -2);
      } else if (/^`[^`]+`$/.test(part)) {
        el = document.createElement('code');
        el.className = 'rounded px-1.5 bg-gray-100 dark:bg-gray-800 font-mono text-xs';
        el.textContent = part.slice(1, -1);
      } else if (/^\[[^\]]+\]\(https?:\/\//.test(part)) {
        el = document.createElement('a');
        el.textContent = part.slice(1, part.indexOf(']('));
        el.href = part.slice(part.indexOf('](') + 2, -1);
        el.target = '_blank';
        el.rel = 'noopener noreferrer';
        el.className = 'text-blue-600 dark:text-blue-400 underline';
      } else {
        el = document.createTextNode(part);
      }
      parent.appendChild(el);
    });
  }

  function prose(text) {
    const wrap = document.createElement('div');
    wrap.dataset.openclawText = '1';
    wrap.className = 'text-gray-800 dark:text-gray-200 break-words mb-2';
    wrap.style.fontSize = '15px';
    wrap.style.lineHeight = '1.6';
    String(text).split(/(```[\s\S]*?(?:```|$))/).forEach(function(chunk) {
      if (!chunk.trim()) return;
      if (chunk.indexOf('```') === 0) {
        const code = chunk.replace(/^```[^\n]*\n?/, '').replace(/```$/, '');
        const pre = cardPre(code.replace(/\n$/, ''), true);
        pre.className += ' mb-2';
        wrap.appendChild(pre);
        return;
      }
      chunk.split(/\n\s*\n/).forEach(function(block) {
        const lines = block.split('\n').filter(function(l) { return l.trim(); });
        if (!lines.length) return;
        const bullets = lines.every(function(l) { return /^\s*[-*] /.test(l); });
        const numbers = lines.every(function(l) { return /^\s*\d+[.)] /.test(l); });
        if (bullets || numbers) {
          const list = document.createElement(bullets ? 'ul' : 'ol');
          list.className = (bullets ? 'list-disc ' : '') + 'pl-5 mb-2';
          if (numbers) list.style.listStyleType = 'decimal';
          lines.forEach(function(l) {
            const li = document.createElement('li');
            inline(li, l.replace(/^\s*(?:[-*]|\d+[.)]) /, ''));
            list.appendChild(li);
          });
          wrap.appendChild(list);
          return;
        }
        const p = document.createElement('p');
        p.className = 'mb-2 whitespace-pre-wrap';
        if (/^#{1,6} /.test(lines[0])) {
          p.className += ' font-semibold';
          lines[0] = lines[0].replace(/^#{1,6} /, '');
        }
        inline(p, lines.join('\n'));
        wrap.appendChild(p);
      });
    });
    return wrap;
  }

  // A turn someone sent the sub-agent after its task, as a chat shows a
  // user message: a bubble on the right.
  function userTurn(text) {
    const row = document.createElement('div');
    row.dataset.openclawUser = '1';
    row.className = 'flex justify-end mb-3';
    const bubble = document.createElement('div');
    bubble.className = 'rounded-3xl bg-gray-50 dark:bg-gray-850 px-4 py-2 text-gray-800 dark:text-gray-200 '
      + 'whitespace-pre-wrap break-words';
    bubble.style.maxWidth = '85%';
    bubble.style.fontSize = '15px';
    bubble.textContent = text;
    row.appendChild(bubble);
    return row;
  }

  function note(kind, text, sub) {
    const el = document.createElement('div');
    el.dataset.openclawNote = kind;
    el.className = 'text-sm text-gray-500 dark:text-gray-400 mt-2 mb-2';
    const line = document.createElement('div');
    line.className = 'flex items-center gap-2';
    if (kind === 'working') {
      const dot = document.createElement('span');
      dot.className = 'inline-block rounded-full bg-blue-500 animate-pulse shrink-0';
      dot.style.width = '8px';
      dot.style.height = '8px';
      line.appendChild(dot);
    }
    const t = document.createElement('span');
    t.textContent = text;
    line.appendChild(t);
    el.appendChild(line);
    if (sub) {
      const s = document.createElement('div');
      s.dataset.openclawLatest = '1';
      s.className = 'text-xs text-gray-400 dark:text-gray-500 mt-1 whitespace-pre-wrap break-words';
      s.textContent = sub;
      clampLines(s, 3);
      el.appendChild(s);
    }
    return el;
  }

  // The header from what is known: the row at open, then each refill.
  function paintDrawer(root, task) {
    if (task.status) {
      const chip = document.getElementById('openclaw-drawer-chip');
      if (chip) setChip(chip, task.status);
      root.dataset.live = (task.status === 'running' || task.status === 'queued') ? 'true' : 'false';
    }
    const time = document.getElementById('openclaw-drawer-time');
    if (time) time.textContent = elapsedTime(task.startedAt, task.endedAt);
    if (task.task) root.__texts.task = task.task;
    if (task.model) root.__texts.model = task.model;
    const head = document.getElementById('openclaw-drawer-task');
    if (head && root.__texts.task && root.dataset.kind !== 'agent') {
      head.innerHTML = '';
      head.appendChild(textBlock('task', root.__texts.task, 2, root.__open));
      // "Show all" only where the clamp hid something (laid out by now).
      const body = head.children[0].children[0], more = head.children[0].children[1];
      if (!root.__open.task && body.scrollHeight <= body.clientHeight) more.style.display = 'none';
    }
    const meta = document.getElementById('openclaw-drawer-meta');
    if (meta) {
      meta.innerHTML = '';
      if (root.__texts.model) meta.appendChild(document.createTextNode(root.__texts.model));
      const parent = root.__parent;
      if (parent && parent.title) {
        meta.appendChild(document.createTextNode((root.__texts.model ? ' · ' : '') + 'from '));
        let link;
        if (parent.chatId) {
          link = document.createElement('a');
          link.href = '/c/' + parent.chatId;
        } else {
          link = document.createElement('button');
          link.type = 'button';
          link.onclick = function() {
            openSubagentDrawer({ id: parent.childKey, childSessionKey: parent.childKey, title: parent.title });
          };
        }
        link.dataset.openclawParent = '1';
        link.className = 'underline';
        link.textContent = parent.title;
        meta.appendChild(link);
      }
      if (root.__controlUiUrl) {
        const link = controlUiLink(root.__controlUiUrl);
        link.style.marginLeft = '8px';
        meta.appendChild(link);
      }
    }
  }

  // The run: every item, then where it stands. Keeps the reader's place: at
  // the bottom it follows the run, scrolled up it stays put.
  function fillDrawer(DATA) {
    const root = document.getElementById('openclaw-drawer-root');
    const stream = document.getElementById('openclaw-drawer-stream');
    if (!root || !stream || root.dataset.taskId !== DATA.childSessionKey) return;
    if (DATA.parent) root.__parent = DATA.parent;
    if (DATA.task) paintDrawer(root, DATA.task);
    const scroller = stream.parentElement;
    const first = !root.__filled;
    const atBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 40;
    const top = scroller.scrollTop;
    root.__filled = true;

    stream.innerHTML = '';
    if (DATA.error) {
      stream.appendChild(errorEl(DATA.error));
      return;
    }
    const earlier = DATA.earlier;
    if (earlier) {
      const box = document.createElement('div');
      box.dataset.openclawEarlier = '1';
      box.className = 'text-xs text-gray-400 dark:text-gray-500 mb-3';
      const t = document.createElement('span');
      t.textContent = earlier.count
        ? earlier.count + ' earlier step' + (earlier.count === 1 ? '' : 's') + ' not shown' + (earlier.more ? ', and more' : '')
        : 'Earlier steps not shown';
      box.appendChild(t);
      if (!root.__earlier) {
        const more = document.createElement('button');
        more.type = 'button';
        more.dataset.openclawShowEarlier = '1';
        more.className = 'text-blue-600 dark:text-blue-400 ml-2';
        more.textContent = 'Show earlier steps';
        more.onclick = function() {
          root.__earlier = true;
          if (root.__ask) root.__ask();
        };
        box.appendChild(more);
      }
      stream.appendChild(box);
    }
    const items = DATA.items || [];
    items.forEach(function(it) {
      if (it.kind === 'user') stream.appendChild(userTurn(it.text));
      else if (it.kind === 'text') stream.appendChild(prose(it.text));
      else if (it.kind === 'tool') stream.appendChild(toolCard(it, root.__cards));
    });
    const status = DATA.task && DATA.task.status;
    if (DATA.task && DATA.task.error) {
      const err = errorEl(DATA.task.error);
      err.dataset.openclawRunError = '1';
      stream.appendChild(err);
    }
    if (DATA.note === 'cli-running') {
      stream.appendChild(note('working', 'Working… tool calls appear when this run ends', DATA.latest));
    } else if (DATA.note === 'cli-stopped') {
      stream.appendChild(note('stopped', 'Stopped: its tool calls were not saved.'
        + ' A claude-cli run saves them when it ends.'));
    } else if (status === 'running' || status === 'queued') {
      const steps = items.filter(function(it) { return it.kind === 'tool'; }).length;
      stream.appendChild(note('working', 'Working… (step ' + (steps + 1) + ')'));
    } else if (DATA.noteText) {
      stream.appendChild(note(DATA.note, DATA.noteText));
    }
    if (!stream.children.length) {
      const empty = document.createElement('div');
      empty.className = 'text-gray-400 dark:text-gray-500 text-xs';
      empty.textContent = 'No messages.';
      stream.appendChild(empty);
    }
    scroller.scrollTop = (first || atBottom) ? scroller.scrollHeight : top;
    const footer = document.getElementById('openclaw-drawer-footer');
    if (footer && DATA.fetchedAt) footer.textContent = 'Updated ' + clockTime(DATA.fetchedAt) + ' · via ' + DATA.source;
  }

  // A full-screen sheet with a ‹ Back button on a phone, a side panel on a
  // wide screen. `task.kind` "child" is a Gateway child session: it fills
  // from the Action, and while it runs the drawer asks again every few
  // seconds until it ends or the drawer closes. "agent" is an in-process
  // Agent call: its task and result, from its card, nothing to fetch.
  function openDrawer(task) {
    const old = document.getElementById('openclaw-drawer-root');
    if (old) (old.__close || function() { old.remove(); })();
    const child = task.kind !== 'agent';
    const phone = (window.innerWidth || 1024) < 768;

    const root = document.createElement('div');
    root.id = 'openclaw-drawer-root';
    root.className = 'fixed inset-0';
    // Inline: Open WebUI's CSS has no `z-[10000]` rule, so with the class the
    // drawer had no z-index at all and opened behind the Status dialog
    // (z-[9999]), which then swallowed every click on it (ELI-118).
    root.style.zIndex = '10000';
    root.dataset.taskId = task.childSessionKey || task.id;
    root.dataset.kind = child ? 'child' : 'agent';
    root.__texts = {};
    root.__open = {};
    root.__cards = {};
    root.__controlUiUrl = task.controlUiUrl || null;
    const onKey = function(e) { if (e.key === 'Escape') close(); };
    function close() {
      if (root.__poll) clearInterval(root.__poll);
      root.__poll = null;
      root.remove();
      document.removeEventListener('keydown', onKey);
    }
    root.__close = close;

    const backdrop = document.createElement('div');
    backdrop.className = 'absolute inset-0';
    backdrop.style.background = 'rgba(0,0,0,0.25)';
    backdrop.onclick = close;
    root.appendChild(backdrop);

    const panel = document.createElement('div');
    panel.className = 'absolute right-0 top-0 bottom-0 '
      + 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 flex flex-col';
    if (phone) {
      panel.style.width = '100%';
      panel.style.left = '0';
    } else {
      // A wide viewport keeps the sub-agent list glanceable beside it.
      panel.className += ' shadow-xl border-l border-gray-100 dark:border-gray-800';
      panel.style.width = 'min(480px, 92vw)';
    }
    panel.addEventListener('click', function(e) { e.stopPropagation(); });
    root.appendChild(panel);

    // The header: name, status and time; model and where it came from; the
    // head of its task.
    const header = document.createElement('div');
    header.className = 'px-4 pt-3 pb-3 border-b border-gray-100 dark:border-gray-800';
    const top = document.createElement('div');
    top.className = 'flex items-center gap-2';
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.onclick = close;
    if (phone) {
      closeBtn.textContent = '‹ Back';
      closeBtn.className = 'text-sm text-blue-600 dark:text-blue-400 shrink-0 pr-1';
      top.appendChild(closeBtn);
    }
    const titleEl = document.createElement('div');
    titleEl.dataset.openclawTitle = '1';
    titleEl.className = 'text-sm font-semibold flex-1';
    titleEl.textContent = task.title || task.id;
    oneLine(titleEl);
    top.appendChild(titleEl);
    const chip = statusChip(task.status);
    chip.id = 'openclaw-drawer-chip';
    top.appendChild(chip);
    const time = document.createElement('span');
    time.id = 'openclaw-drawer-time';
    time.className = 'text-xs text-gray-500 dark:text-gray-400 shrink-0';
    top.appendChild(time);
    if (!phone) {
      closeBtn.textContent = '×';
      closeBtn.className = 'text-xl leading-none text-gray-400 hover:text-gray-700 '
        + 'dark:hover:text-gray-200 px-1 shrink-0';
      top.appendChild(closeBtn);
    }
    header.appendChild(top);
    const meta = document.createElement('div');
    meta.id = 'openclaw-drawer-meta';
    meta.className = 'text-xs text-gray-500 dark:text-gray-400 mt-1';
    oneLine(meta);
    header.appendChild(meta);
    const taskHead = document.createElement('div');
    taskHead.id = 'openclaw-drawer-task';
    taskHead.className = 'mt-2';
    header.appendChild(taskHead);
    panel.appendChild(header);

    // The one scroller: the run.
    const content = document.createElement('div');
    content.className = 'flex-1 px-4 py-3';
    content.style.overflowY = 'auto';
    content.style.minHeight = '0';
    const stream = document.createElement('div');
    stream.id = 'openclaw-drawer-stream';
    content.appendChild(stream);
    panel.appendChild(content);
    if (child) stream.appendChild(skeletonSection('openclaw-drawer-loading'));

    if (child) {
      const footer = document.createElement('div');
      footer.id = 'openclaw-drawer-footer';
      footer.className = 'text-[11px] text-gray-400 dark:text-gray-600 px-4 py-2 '
        + 'border-t border-gray-100 dark:border-gray-800';
      footer.textContent = ' ';
      panel.appendChild(footer);
    }

    document.addEventListener('keydown', onKey);
    document.body.appendChild(root);
    paintDrawer(root, task);
    if (!child) {
      // Its task as the turn that started it, its result as the reply.
      const items = [];
      if (task.task) items.push({ kind: 'user', text: task.task });
      if (task.result) items.push({ kind: 'text', text: task.result });
      fillDrawer({ childSessionKey: root.dataset.taskId, items: items,
                   note: task.status === 'running' ? 'working' : 'agent', noteText: {
        launched: 'Running in the background: its report comes as a message of its own.',
        'cut off': 'Cut off: the run ended before it returned.', running: 'Working…' }[task.status] });
      return;
    }

    // Each refresh is one Action request that fills the drawer once; the
    // next waits for it to come back. A fill that ends the run stops it.
    let inflight = false;
    let firstAsk = true;
    function ask() {
      if (inflight) return;
      inflight = true;
      const done = function() { inflight = false; };
      askAction({ mode: 'subagent-detail', opened: true, taskId: root.dataset.taskId,
                  first: firstAsk, earlier: !!root.__earlier }).then(done, done);
      firstAsk = false;
    }
    root.__ask = ask;
    function refresh() {
      if (!root.isConnected || root.dataset.live === 'false') {
        if (root.__poll) clearInterval(root.__poll);
        root.__poll = null;
        return;
      }
      ask();
    }
    if (task.server) firstAsk = false;
    else ask();
    if (root.dataset.live === 'true') root.__poll = setInterval(refresh, IDENTITY.subagentPollMs || 2500);
  }

  function openSubagentDrawer(task) {
    openDrawer(Object.assign({}, task, { kind: 'child' }));
  }

  function openAgentDetail(agent) {
    openDrawer({ kind: 'agent', id: agent.id, title: agent.title, status: agent.state,
                 task: agent.task, result: agent.result, startedAt: agent.startedAt });
  }

  // "Subagents (3)", or "(20 of 31)" when the list is capped.
  function countLabel(name, shown, total) {
    return name + ' (' + (total > shown ? shown + ' of ' + total : shown) + ')';
  }

  // A section that folds (ELI-163): its title with its count, and a chevron.
  // It opens when something in it runs at its first fill and is closed
  // otherwise; a tap wins after that, so a refill leaves it as it is.
  // `onOpen`: what to fetch the first time it is open (once per dialog).
  const folds = {};
  const foldFetched = {};
  function collapsible(name, label, live, onOpen) {
    if (!(name in folds)) folds[name] = !!live;
    const wrap = document.createElement('div');
    wrap.dataset.openclawFold = name;
    const head = document.createElement('button');
    head.type = 'button';
    head.dataset.openclawFoldHead = name;
    head.className = 'w-full flex items-center gap-2 text-left mb-1.5 '
      + 'text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200';
    // Divs, not spans: a 10 px span in a list is its time cell.
    const text = document.createElement('div');
    text.className = 'flex-1 font-semibold tracking-wide uppercase';
    text.style.fontSize = '10px';  // inline: see sectionHeader()
    text.textContent = label;
    head.appendChild(text);
    const chevron = document.createElement('div');
    chevron.className = 'flex shrink-0';
    head.appendChild(chevron);
    wrap.appendChild(head);
    const body = document.createElement('div');
    body.dataset.openclawFoldBody = name;
    wrap.appendChild(body);
    function show(open) {
      body.style.display = open ? 'block' : 'none';
      chevron.innerHTML = open ? ICON.up : ICON.down;
      head.setAttribute('aria-expanded', open ? 'true' : 'false');
      wrap.dataset.open = open ? 'true' : 'false';
      if (open && onOpen && !foldFetched[name]) {
        foldFetched[name] = true;
        onOpen();
      }
    }
    head.onclick = function() { folds[name] = !folds[name]; show(folds[name]); };
    show(folds[name]);
    return { wrap: wrap, body: body };
  }

  // A section's error, under its own title, in its own place.
  function sectionError(name, message) {
    const section = document.getElementById('openclaw-status-section-' + name);
    if (!section) return;
    section.innerHTML = '';
    section.className = 'mb-4';
    const title = SECTIONS.find(function(s) { return s[0] === name; });
    if (title) section.appendChild(sectionHeader(title[1]));
    section.appendChild(errorEl(message));
  }

  // The dialog's sections, drawn at open with their titles: each fills on
  // its own (ELI-163), so the dialog never waits for its slowest source.
  const SECTIONS = __OPENCLAW_SECTIONS__;
  function skeletons(wrap) {
    SECTIONS.forEach(function(s) {
      wrap.appendChild(skeletonSection('openclaw-status-section-' + s[0], s[1]));
    });
  }

  // One list row (ELI-155): a one-line title, a status chip and the time,
  // then at most two lines of preview. A row with `open` is a button that
  // opens its detail.
  function listRow(o) {
    const el = document.createElement(o.open ? 'button' : 'div');
    if (o.open) {
      el.type = 'button';
      el.onclick = o.open;
    }
    el.dataset[o.kind] = '1';
    el.className = 'w-full block text-left py-1.5 px-1.5 -mx-1.5 rounded-lg'
      + (o.open ? ' hover:bg-gray-50 dark:hover:bg-gray-800/60' : '');
    const top = document.createElement('div');
    top.className = 'flex items-center gap-2';
    const titleEl = document.createElement('div');
    titleEl.dataset.openclawTitle = '1';
    titleEl.className = 'text-sm text-gray-700 dark:text-gray-200 flex-1';
    titleEl.textContent = o.title;
    top.appendChild(oneLine(titleEl));
    top.appendChild(statusChip(o.status));
    if (o.time) top.appendChild(o.time);
    el.appendChild(top);
    if (o.preview) {
      const preview = document.createElement('div');
      preview.dataset.openclawPreview = '1';
      preview.className = 'text-xs text-gray-400 dark:text-gray-500 break-words mt-0.5';
      preview.textContent = o.preview;
      el.appendChild(clampLines(preview, 2));
    }
    return el;
  }

  function timeCell(text) {
    const cell = document.createElement('span');
    cell.className = 'text-gray-400 dark:text-gray-500 shrink-0';
    cell.style.fontSize = '10px';
    cell.textContent = text;
    return cell;
  }

  // A run's time: counting up while it runs (the dialog's ticker rewrites a
  // cell marked with its start), its duration once it ended.
  function elapsedCell(startedAt, endedAt, running) {
    const cell = timeCell(elapsedTime(startedAt, endedAt));
    const startMs = toMs(startedAt);
    if (running && !endedAt && startMs != null) cell.dataset.openclawStartedMs = String(startMs);
    return cell;
  }

  function noteEl(text) {
    const el = document.createElement('div');
    el.className = 'text-xs text-gray-400 dark:text-gray-500 mb-1';
    el.textContent = text;
    return el;
  }

  window.__openclawStatus = {
    identity: IDENTITY, isDark: isDark, barColor: barColor, makeBar: makeBar,
    sectionHeader: sectionHeader, errorEl: errorEl,
    skeletonSection: skeletonSection, touchFooter: touchFooter, clockTime: clockTime, controlUiLink: controlUiLink,
    triggerCompact: triggerCompact, elapsedTime: elapsedTime, toMs: toMs,
    openSubagentDrawer: openSubagentDrawer, openAgentDetail: openAgentDetail, openDrawer: openDrawer,
    fillDrawer: fillDrawer, statusChip: statusChip, oneLine: oneLine, clampLines: clampLines,
    countLabel: countLabel, collapsible: collapsible, listRow: listRow, timeCell: timeCell,
    elapsedCell: elapsedCell, noteEl: noteEl, sectionError: sectionError, skeletons: skeletons,
    askAction: askAction,
  };

  const root = document.createElement('div');
  root.id = 'openclaw-status-modal-root';
  root.className = 'fixed inset-0 z-[9999] flex items-center justify-center';
  root.style.background = 'rgba(0,0,0,0.4)';

  const panel = document.createElement('div');
  panel.className = 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 '
    + 'rounded-2xl shadow-xl border border-gray-100 dark:border-gray-800 p-5';
  // Inline, not a `w-[380px] max-w-[90vw]` utility class: same reasoning as
  // barColor() below -- an arbitrary-value class OWUI's own Tailwind build
  // never scanned out of its own source can silently carry zero CSS.
  // min() keeps this mobile-first: a narrow viewport wins the 88vw side (a
  // visible margin, not edge-to-edge), a wide one wins the 380px cap.
  panel.style.width = 'min(380px, 88vw)';
  // Up to 20 sub-agents of each kind (ELI-152) outgrow a phone screen: the
  // panel is capped to the screen and its sections scroll between the title
  // bar and the footer, which stay in view (ELI-163).
  panel.style.maxHeight = '88vh';
  panel.style.display = 'flex';
  panel.style.flexDirection = 'column';
  panel.addEventListener('click', function(e) { e.stopPropagation(); });

  // Dismissed: tell the Action, so its open request stops reading for a
  // dialog nobody sees (ELI-163). Best effort; its follow is bounded anyway.
  function closeDialog() {
    if (!root.isConnected) return;
    root.remove();
    document.removeEventListener('keydown', onKey);
    if (IDENTITY.dialogId) askAction({ mode: 'status-closed' }).catch(function() {});
  }

  const header = document.createElement('div');
  header.className = 'flex items-center justify-between mb-3';
  header.style.flexShrink = '0';
  const title = document.createElement('div');
  title.className = 'text-sm font-semibold';
  title.textContent = 'Claw';
  header.appendChild(title);
  const headerEnd = document.createElement('div');
  headerEnd.className = 'flex items-center gap-3';
  if (IDENTITY.controlUiUrl) headerEnd.appendChild(controlUiLink(IDENTITY.controlUiUrl));
  header.appendChild(headerEnd);
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.textContent = '×';
  closeBtn.className = 'text-xl leading-none text-gray-400 hover:text-gray-700 '
    + 'dark:hover:text-gray-200 px-1';
  closeBtn.onclick = closeDialog;
  headerEnd.appendChild(closeBtn);
  panel.appendChild(header);

  const sections = document.createElement('div');
  sections.id = 'openclaw-status-sections';
  sections.style.overflowY = 'auto';
  sections.style.minHeight = '0';
  sections.style.flex = '1 1 auto';
  skeletons(sections);
  panel.appendChild(sections);

  const footer = document.createElement('div');
  footer.id = 'openclaw-status-footer';
  footer.className = 'text-[11px] text-gray-400 dark:text-gray-600 pt-3 '
    + 'border-t border-gray-100 dark:border-gray-800';
  footer.textContent = ' ';
  footer.style.flexShrink = '0';
  panel.appendChild(footer);

  root.appendChild(panel);
  root.addEventListener('click', closeDialog);
  const onKey = function(e) {
    if (e.key === 'Escape') closeDialog();
  };
  document.addEventListener('keydown', onKey);

  // A running sub-agent's elapsed time counts up while the dialog is open
  // (ELI-120): the Subagents fill writes it once, so a child spawned a
  // second before the dialog opened kept saying "1s". The fill marks those
  // cells with data-openclaw-started-ms; one ticker per dialog rewrites
  // them every second and stops itself once the dialog is gone. Reopening
  // replaces the previous dialog, so its ticker is cleared first.
  if (window.__openclawStatusTicker) clearInterval(window.__openclawStatusTicker);
  const ticker = setInterval(function() {
    if (!root.isConnected) {
      clearInterval(ticker);
      return;
    }
    root.querySelectorAll('[data-openclaw-started-ms]').forEach(function(el) {
      el.textContent = elapsedTime(Number(el.dataset.openclawStartedMs), null);
    });
  }, 1000);
  window.__openclawStatusTicker = ticker;

  document.body.appendChild(root);
})();
"""

# Fired only on a post-compact refresh (_run_status(show_loading=False)),
# never on the initial open. _MODAL_COMPACTING_JS (below) replaces
# #openclaw-status-sections' entire contents with its spinner, which
# destroys the three individual section ids each fill function targets --
# this recreates fresh skeletons in their place before the refreshed RPCs
# are fired, without touching the overlay/header/window.__openclawStatus
# (already correctly set up from the original open).
_RESET_SECTIONS_JS = r"""
(function() {
  const wrap = document.getElementById('openclaw-status-sections');
  const S = window.__openclawStatus;
  if (!wrap || !S) return;
  // The refresh's reads are a new dialog's: the page's later requests name it.
  S.identity.dialogId = __OPENCLAW_DIALOG_ID__;
  wrap.innerHTML = '';
  S.skeletons(wrap);
})();
"""

# Static (no data), fired the moment a compact is triggered -- see
# Action._run_compact. Deliberately a distinct, prominent "Compacting..."
# state rather than reusing the generic loading skeleton: compaction is an
# LLM call that can take a while, so the dialog should look like it's
# doing something specific and ongoing, not like a normal fetch that's
# about to finish in a second. Replaces the whole sections wrapper (not a
# specific section) since this is a dialog-wide state, not a per-query one.
_MODAL_COMPACTING_JS = r"""
(function() {
  const wrap = document.getElementById('openclaw-status-sections');
  if (!wrap) return;
  wrap.innerHTML = '';

  const isDark = document.documentElement.classList.contains('dark');

  const box = document.createElement('div');
  box.className = 'flex flex-col items-center justify-center py-6 text-center';

  const spinner = document.createElement('div');
  spinner.className = 'animate-spin rounded-full h-6 w-6 border-2 mb-3';
  spinner.style.borderColor = isDark ? '#4b5563' : '#d1d5db';
  spinner.style.borderTopColor = 'transparent';
  box.appendChild(spinner);

  const title = document.createElement('div');
  title.className = 'text-sm font-medium text-gray-700 dark:text-gray-200';
  title.textContent = 'Compacting…';
  box.appendChild(title);

  const sub = document.createElement('div');
  sub.className = 'text-xs text-gray-400 dark:text-gray-500 mt-1';
  sub.textContent = 'This can take a moment for long conversations.';
  box.appendChild(sub);

  wrap.appendChild(box);
})();
"""

# Dialog-wide error (connection failures, "busy" guard, compact timeout) --
# also replaces the whole sections wrapper rather than one section, same
# reasoning as _MODAL_COMPACTING_JS.
_SECTIONS_ERROR_JS_TEMPLATE = r"""
(function() {
  const MESSAGE = __OPENCLAW_ERROR_MESSAGE__;
  const wrap = document.getElementById('openclaw-status-sections');
  if (!wrap) return;
  wrap.innerHTML = '';
  const S = window.__openclawStatus;
  if (S) {
    wrap.appendChild(S.errorEl(MESSAGE));
  } else {
    wrap.textContent = MESSAGE;
  }
})();
"""

_CONTEXT_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-status-section-context');
  const S = window.__openclawStatus;
  if (!section || !S) return;
  if (DATA.error) {
    S.sectionError('context', DATA.error);
    return;
  }
  section.innerHTML = '';
  section.className = 'mb-4';

  const sub = document.createElement('div');
  sub.className = 'text-xs text-gray-500 dark:text-gray-400 mb-3';
  sub.textContent = DATA.provider + (DATA.model ? (' · ' + DATA.model) : '');
  section.appendChild(sub);

  if (!DATA.context) {
    const empty = document.createElement('div');
    empty.className = 'text-gray-400 dark:text-gray-500 text-xs';
    empty.textContent = 'No context data available for this session yet.';
    section.appendChild(empty);
    return;
  }

  const headerRow = document.createElement('div');
  headerRow.className = 'flex items-center justify-between mb-1.5';
  const headerLabel = document.createElement('div');
  headerLabel.className = 'font-semibold tracking-wide uppercase '
    + 'text-gray-400 dark:text-gray-500';
  headerLabel.style.fontSize = '10px';
  headerLabel.textContent = 'Context';
  headerRow.appendChild(headerLabel);
  if (S.identity.chatId && S.identity.messageId && S.identity.sessionId) {
    const compactBtn = document.createElement('button');
    compactBtn.type = 'button';
    compactBtn.textContent = 'Compact';
    compactBtn.className = 'font-medium px-2 py-0.5 rounded-full '
      + 'border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 '
      + 'hover:text-gray-800 dark:hover:text-gray-100';
    compactBtn.style.fontSize = '10px';
    compactBtn.onclick = S.triggerCompact;
    headerRow.appendChild(compactBtn);
  }
  section.appendChild(headerRow);

  section.appendChild(S.makeBar(DATA.context.pct));
  const label = document.createElement('div');
  label.className = 'text-xs text-gray-500 dark:text-gray-400 mt-1.5';
  label.textContent = DATA.context.usedTokens + ' / ' + DATA.context.totalTokens
    + ' tokens · ' + DATA.context.pct + '%';
  section.appendChild(label);

  if (DATA.goal) {
    const goalWrap = document.createElement('div');
    goalWrap.className = 'mt-3 pt-3 border-t border-gray-100 dark:border-gray-800';
    goalWrap.appendChild(S.sectionHeader('Goal'));
    const line = document.createElement('div');
    line.className = 'text-sm text-gray-700 dark:text-gray-200';
    line.textContent = DATA.goal.line;
    goalWrap.appendChild(line);
    if (DATA.goal.pct !== null) {
      const barWrap = document.createElement('div');
      barWrap.className = 'mt-1.5';
      barWrap.appendChild(S.makeBar(DATA.goal.pct));
      goalWrap.appendChild(barWrap);
    }
    section.appendChild(goalWrap);
  }

  S.touchFooter(DATA.fetchedAt, DATA.source);
})();
"""

# Always gets its own section when we have a real provider to report on --
# not gated on windows.length > 0. The gateway's usage.status can
# transiently report zero windows for the active provider while a run is
# in flight (its rate-limit cache is most likely refreshed from that run's
# own API response headers, so there's a real gap while one is active) --
# silently omitting the section in that case looked exactly like a missing
# feature rather than a temporary data gap. Showing an explicit note instead turns that into an
# understood, expected state.
_LIMITS_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-status-section-limits');
  const S = window.__openclawStatus;
  if (!section || !S) return;
  section.innerHTML = '';

  section.className = 'mb-4';
  section.appendChild(S.sectionHeader('Rate limits'));
  if (DATA.error) {
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

  if (!DATA.provider || DATA.provider === '?') {
    section.appendChild(S.noteEl('This session has no provider yet.'));
    return;
  }

  if (DATA.windows.length === 0) {
    const note = document.createElement('div');
    note.className = 'text-xs text-gray-400 dark:text-gray-500';
    note.textContent = 'No rate-limit data available right now'
      + (DATA.sessionActive ? ' (a response is in progress).' : '.');
    section.appendChild(note);
  }
  DATA.windows.forEach(function(w, i) {
    const row = document.createElement('div');
    row.className = i > 0 ? 'mt-3' : '';
    const top = document.createElement('div');
    top.className = 'flex items-center justify-between text-xs '
      + 'text-gray-600 dark:text-gray-300 mb-1';
    const labelEl = document.createElement('span');
    labelEl.textContent = w.label;
    const pctEl = document.createElement('span');
    pctEl.textContent = Math.round(w.usedPercent) + '% used';
    top.appendChild(labelEl);
    top.appendChild(pctEl);
    row.appendChild(top);
    row.appendChild(S.makeBar(w.usedPercent));
    if (w.resetIn) {
      const reset = document.createElement('div');
      reset.className = 'text-[11px] text-gray-400 dark:text-gray-500 mt-1';
      let text = 'resets in ' + w.resetIn;
      if (w.resetAtMs) {
        // Formatted in the browser's own local timezone, not computed
        // server-side -- the gateway/OWUI container's system timezone and
        // the person actually looking at this dialog aren't guaranteed to
        // be the same, so doing this client-side is the only way to get
        // it right regardless of where either runs.
        const abs = new Date(w.resetAtMs).toLocaleTimeString([], {
          hour: '2-digit', minute: '2-digit'
        });
        text += ' · ' + abs;
      }
      reset.textContent = text;
      row.appendChild(reset);
    }
    section.appendChild(row);
  });

  S.touchFooter(DATA.fetchedAt, DATA.source);
})();
"""

# The Subagents section (ELI-152/155/163): a fold titled with its count, open
# at its first fill when a child runs. A fold opened for the first time with
# its rows still `pending` (the open read no transcripts for a section
# nobody had opened) asks the Action for them (mode "subagent-rows"). Each
# row opens the per-subagent drawer (S.openSubagentDrawer) with the fields it
# already knows, so the drawer paints its header before any RPC resolves.
_SUBAGENTS_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const S = window.__openclawStatus;
  const section = document.getElementById('openclaw-status-section-subagents');
  if (!section || !S) return;
  if (DATA.error) {
    S.sectionError('subagents', DATA.error);
    return;
  }
  section.innerHTML = '';
  section.className = 'mb-4';
  const tasks = DATA.tasks || [];
  const live = tasks.some(function(t) { return t.status === 'running' || t.status === 'queued'; });
  // A failed ask (a dropped phone connection, a proxy's error page) says so
  // in the fold with a Retry; a good one is answered by the Action's fill.
  function loadRows() {
    S.askAction({ mode: 'subagent-rows' }).then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
    }).catch(function(err) {
      fold.body.innerHTML = '';
      fold.body.appendChild(S.errorEl('Could not load the sub-agents: ' + ((err && err.message) || err)));
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.className = 'mt-1 text-xs underline text-gray-500 dark:text-gray-400';
      retry.textContent = 'Retry';
      retry.onclick = function() {
        fold.body.innerHTML = '';
        fold.body.appendChild(S.skeletonSection('openclaw-status-subagents-loading'));
        loadRows();
      };
      fold.body.appendChild(retry);
    });
  }
  const fold = S.collapsible('subagents', S.countLabel('Subagents', tasks.length, DATA.total || 0), live,
    DATA.pending ? loadRows : null);
  section.appendChild(fold.wrap);
  if (!tasks.length) {
    fold.body.appendChild(S.noteEl('No sub-agents in this chat.'));
  } else if (DATA.pending) {
    fold.body.appendChild(S.skeletonSection('openclaw-status-subagents-loading'));
  }
  if (!DATA.pending) tasks.forEach(function(t) {
    const running = t.status === 'running';
    fold.body.appendChild(S.listRow({
      kind: 'openclawRow', title: t.title || t.id, status: t.status,
      time: S.elapsedCell(t.startedAt, t.endedAt, running),
      preview: running ? t.progressSummary : (t.terminalSummary || t.error),
      open: function() { S.openSubagentDrawer(t); },
    }));
  });
  S.touchFooter(DATA.fetchedAt, DATA.source);
})();
"""

# Claude Code's in-process Agent calls (ELI-152): not Gateway sessions, so
# no duration, only what the chat's tool cards tell; a tap shows its task and
# result (ELI-155). Read from Open WebUI's copy of the chat alone, so this
# section fills even when the Gateway does not answer.
_AGENTS_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const S = window.__openclawStatus;
  const section = document.getElementById('openclaw-status-section-agents');
  if (!section || !S) return;
  if (DATA.error) {
    S.sectionError('agents', DATA.error);
    return;
  }
  section.innerHTML = '';
  section.className = 'mb-4';
  const agents = DATA.agents || [];
  const live = agents.some(function(a) { return a.state === 'running'; });
  const fold = S.collapsible('agents', S.countLabel('In-process agents', agents.length, DATA.total || 0), live);
  section.appendChild(fold.wrap);
  // A long chat is read from its recent messages only (DATA.recent of them).
  const where = DATA.recent ? 'in this chat\'s last ' + DATA.recent + ' messages' : 'in this chat';
  fold.body.appendChild(S.noteEl(agents.length
    ? 'Claude Code Agent calls ' + where + ', as their tool cards show them.'
    : 'No Claude Code Agent calls ' + where + '.'));
  agents.forEach(function(a) {
    // When its reply started, on the reader's clock.
    const started = S.toMs(a.startedAt);
    let when = null;
    if (started != null) {
      when = S.timeCell(new Date(started).toLocaleString([], {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
      }));
      when.title = 'Started';
    }
    fold.body.appendChild(S.listRow({
      kind: 'openclawAgentRow', title: a.title, status: a.state, time: when, preview: a.result,
      open: function() { S.openAgentDetail(a); },
    }));
  });
})();
"""

# The Background section (ELI-163): this chat's background commands, media
# jobs, automation runs and background agents, and the automations bound to
# it. A fold like Subagents. A source this Gateway
# lacks says so in a note; one that failed says so too, and the rest shows.
_BACKGROUND_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const S = window.__openclawStatus;
  const section = document.getElementById('openclaw-status-section-background');
  if (!section || !S) return;
  if (DATA.error) {
    S.sectionError('background', DATA.error);
    return;
  }
  section.innerHTML = '';
  section.className = 'mb-4';
  const rows = DATA.rows || [];
  const isLive = function(r) { return r.status === 'running' || r.status === 'queued'; };
  const live = rows.some(isLive);
  const fold = S.collapsible('background', S.countLabel('Background', rows.length, DATA.total || 0), live);
  section.appendChild(fold.wrap);

  // The row's second line: what it is, then what it says. The title keeps
  // the whole first line.
  function line(kind, detail) {
    return [kind, detail].filter(Boolean).join(' · ') || null;
  }
  // An automation: its error when it failed, else when it runs next.
  function automationDetail(r) {
    if (r.status === 'failed') {
      return (r.failedInARow > 1 ? 'failed ' + r.failedInARow + ' times in a row: ' : '') + (r.error || '');
    }
    if (r.status === 'running' || !r.nextRunAtMs) return null;
    return 'next run ' + new Date(r.nextRunAtMs).toLocaleString([], {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
    });
  }
  function row(r) {
    if (r.job) {
      return S.listRow({ kind: 'openclawBgRow', title: r.title, status: r.status,
                         time: r.status === 'running' ? S.elapsedCell(r.startedAt, null, true) : null,
                         preview: line(r.kind, automationDetail(r)) });
    }
    return S.listRow({
      kind: 'openclawBgRow', title: r.title, status: r.status,
      time: S.elapsedCell(r.startedAt, r.endedAt, isLive(r)), preview: line(r.kind, r.preview),
      open: r.agent ? function() { S.openAgentDetail(r.agent); } : null,
    });
  }

  (DATA.errors || []).forEach(function(e) { fold.body.appendChild(S.errorEl(e)); });
  if (!DATA.tasksAvailable) {
    fold.body.appendChild(S.noteEl('Background commands and media jobs are not available on this Gateway.'));
  }
  if (!rows.length) fold.body.appendChild(S.noteEl('Nothing in the background for this chat.'));
  rows.forEach(function(r) { fold.body.appendChild(row(r)); });
  S.touchFooter(DATA.fetchedAt, DATA.source);
})();
"""

# One section's error in its place (S.sectionError): a failed source
# leaves every other section as it is.
_SECTION_ERROR_JS_TEMPLATE = r"""
(function() {
  const S = window.__openclawStatus;
  if (S && S.sectionError) S.sectionError(__OPENCLAW_SECTION__, __OPENCLAW_ERROR_MESSAGE__);
})();
"""


# The drawer opened from here rather than by a row tap: an old sub-agent
# message's toolbar click. `S.openDrawer` (_MODAL_OPEN_JS_TEMPLATE) builds it;
# `server` because this request fills it next.
_DRAWER_OPEN_JS_TEMPLATE = r"""
(function() {
  const S = window.__openclawStatus;
  if (S && S.openDrawer) S.openDrawer(Object.assign({ kind: 'child', server: true }, __OPENCLAW_TASK__));
})();
"""

# Whole-drawer error (task not found, connect failure): the run's place
# says what went wrong. `KEY`: only the drawer showing that sub-agent.
_DRAWER_ERROR_JS_TEMPLATE = r"""
(function() {
  const MESSAGE = __OPENCLAW_ERROR_MESSAGE__;
  const KEY = __OPENCLAW_TASK_KEY__;
  const S = window.__openclawStatus;
  const root = document.getElementById('openclaw-drawer-root');
  const stream = document.getElementById('openclaw-drawer-stream');
  if (!stream || (KEY && (!root || root.dataset.taskId !== KEY))) return;
  stream.innerHTML = '';
  stream.appendChild(S ? S.errorEl(MESSAGE) : document.createTextNode(MESSAGE));
})();
"""

# One read of the sub-agent: its header and its run (S.fillDrawer). A fill
# for a drawer since closed, or another sub-agent's, is dropped there.
_DRAWER_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const S = window.__openclawStatus;
  if (S && S.fillDrawer && document.getElementById('openclaw-drawer-stream')) S.fillDrawer(DATA);
})();
"""


def _json_for_js(data) -> str:
    """Never string-interpolate individual fields directly into JS source.
    json.dumps always produces a syntactically valid JS expression, so this
    is the one substitution point that needs to be safe against whatever
    the gateway's numbers/text happen to contain (e.g. a goal description
    with quotes or backslashes).

    json.dumps does NOT escape "<" by default, so a value containing a
    literal "</script>" would terminate an enclosing <script> tag early if
    this code ever ends up placed into HTML rather than passed straight to
    a JS execution call -- escaping "<" to the equivalent \\u003c unicode
    escape is a no-op for JSON.parse but closes that off regardless of how
    the execute event happens to deliver this string."""
    return json.dumps(data).replace("<", "\\u003c")


def _render_modal_open_js(identity: dict) -> str:
    return (_MODAL_OPEN_JS_TEMPLATE.replace("__OPENCLAW_IDENTITY__", _json_for_js(identity))
            .replace("__OPENCLAW_SECTIONS__", _json_for_js([list(s) for s in _DIALOG_SECTIONS])))


def _render_sections_error_js(message: str) -> str:
    return _SECTIONS_ERROR_JS_TEMPLATE.replace("__OPENCLAW_ERROR_MESSAGE__", _json_for_js(message))


def _render_context_fill_js(data: dict) -> str:
    return _CONTEXT_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_limits_fill_js(data: dict) -> str:
    return _LIMITS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _limits_windows_for(usage, provider) -> list:
    """The active provider's rate-limit windows from a `usage.status`-shaped
    payload, reshaped for the Rate Limits section. Windows with no known
    usedPercent are dropped."""
    windows_data = []
    for p in (usage or {}).get("providers", []):
        if p.get("provider") != provider:
            continue
        for w in p.get("windows") or []:
            used = w.get("usedPercent")
            if used is None:
                continue
            windows_data.append({
                "label": w.get("label"),
                "usedPercent": used,
                "resetIn": _relative_time(w.get("resetAt")),
                # Raw ms timestamp, not a server-formatted clock
                # time -- the absolute time is rendered client-side
                # so it shows in the browser's own local timezone
                # rather than whatever timezone this container is in.
                "resetAtMs": w.get("resetAt"),
            })
        break
    return windows_data


async def _claude_cli_usage_windows(conn) -> list:
    """Fallback for Claude-CLI-only setups. Since OpenClaw 2026.9.1
    (openclaw#129052) `usage.status` never resolves the claude-cli login, so
    with no other Anthropic credential it has no `anthropic` entry at all.
    The companion gateway plugin (gateway-plugins/claude-cli-usage) serves
    the same windows read-only over `claudeCliUsage.status`. A gateway
    without that plugin answers "unknown method"; that is logged and the
    section keeps its empty-windows note."""
    try:
        snapshot = await conn.send_request("claudeCliUsage.status", {}, timeout=10)
    except Exception as ex:
        pipe_log(f"[status-action] claudeCliUsage.status unavailable: {ex}")
        return []
    if (snapshot or {}).get("error"):
        pipe_log(f"[status-action] claudeCliUsage.status: {snapshot['error']}")
    return _limits_windows_for({"providers": [snapshot or {}]}, "anthropic")


# A child session's run status, in the words the Subagents section and drawer
# colour and print (they were written against the removed Tasks API).
_SUBAGENT_STATUS_WORDS = {"done": "completed", "killed": "cancelled", "timeout": "timed_out"}
_RUNTIME_CONTEXT_MARKER_RE = re.compile(r"^<<<(?:BEGIN|END)_OPENCLAW_INTERNAL_CONTEXT>>>$", re.M)
# The header on a line of its own; 2026.10 also names it inside the context sentence.
_SUBAGENT_TASK_HEADER_RE = re.compile(r"^\[Subagent Task\][ \t]*$", re.M)


def _subagent_status(status):
    return _SUBAGENT_STATUS_WORDS.get(status, status)


def _subagent_task_text(text):
    """The task out of a spawn's first message, which the Gateway wraps as
    "[Subagent Context] ... [Subagent Task] <task> Begin. Execute ...".
    2026.10 adds internal-context marker lines around the wrapper. None if
    the text is not a spawn message."""
    parts = _SUBAGENT_TASK_HEADER_RE.split(text) if isinstance(text, str) else []
    if len(parts) < 2:
        return None
    task = _RUNTIME_CONTEXT_MARKER_RE.sub("", parts[-1])
    task = task.split("Begin. Execute the assigned task", 1)[0].strip()
    return task or None


# A spawn's first message in any whitespace form: lastMessagePreview flattens
# it to one line, so the header-on-its-own-line test above does not apply.
_SUBAGENT_SPAWN_RE = re.compile(
    r"^\s*(?:<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>\s*)?\[Subagent Context\].*\[Subagent Task\]", re.S)


def _is_spawn_message(text):
    return isinstance(text, str) and bool(_SUBAGENT_SPAWN_RE.match(text))


def _subagent_task_texts(previews_resp):
    """{child key: task text} from a sessions.preview answer. The preview
    holds a session's newest items, so a long-running child's first message
    may be gone; that child has no entry."""
    out = {}
    for p in (previews_resp or {}).get("previews") or []:
        for item in p.get("items") or []:
            text = _subagent_task_text(item.get("text")) if item.get("role") == "user" else None
            if text and p.get("key"):
                out[p["key"]] = text
                break
    return out


def _subagent_row(row, task_text=None, latest=None):
    """One Subagents row out of a child session row. The title is the spawn
    label, else the task's first sentence, else the key; the whole task is
    the drawer's to show (ELI-155). The preview is the row's own last
    message when the list read carried it, else `latest` (sessions.preview)."""
    status = _subagent_status(row.get("status"))
    if status is None and row.get("endedAt"):
        status = "ended"    # a visible spawn's session carries no run status
    live = status in _LIVE_SUBAGENT_STATUSES
    preview = row.get("lastMessagePreview") or latest
    if _is_spawn_message(preview):
        # Just spawned: the newest message is still the task (ELI-119).
        preview = None
    return {
        "id": row["key"],
        "title": (row.get("label") or "").strip() or (_short_title(task_text) if task_text else row["key"]),
        "task": task_text,
        "status": status,
        "progressSummary": preview if live else None,
        "terminalSummary": preview if not live else None,
        "error": row.get("lastRunError"),
        "childSessionKey": row["key"],
        "startedAt": row.get("startedAt"),
        # An ended child's duration stops at its last update when the
        # Gateway kept no end time.
        "endedAt": row.get("endedAt") or (None if live else row.get("updatedAt")),
    }


_LIVE_SUBAGENT_STATUSES = ("running", "queued")
# How many sub-agents of each kind the section lists, newest first (ELI-152).
_SUBAGENT_LIST_CAP = 20


def _is_child_of(row, parent_key):
    """A session the parent spawned. `sessions.list {spawnedBy}` keeps an
    ended child for 30 minutes only, so the dialog's open reads the whole
    list and picks the children itself (ELI-152)."""
    return row.get("key") not in (None, parent_key) and parent_key in (
        row.get("spawnedBy"), row.get("parentSessionKey"))


def _subagent_final(row):
    """A child row that will not change: its run ended."""
    if not row:
        return False
    status = _subagent_status(row.get("status"))
    return status not in _LIVE_SUBAGENT_STATUSES and bool(status or row.get("endedAt"))


async def _lookup_child(conn, key):
    """One child session's row, by its key."""
    try:
        resp = await conn.send_request("sessions.list", dict(search=key.rsplit(":", 1)[-1], limit=10), timeout=5)
    except Exception as ex:
        pipe_log(f"[status-action] sessions.list (child {key}) failed: {ex}")
        return None
    return next((r for r in (resp or {}).get("sessions") or [] if r.get("key") == key), None)


async def _resolved(value):
    return value


def _subagent_started(row):
    return row.get("startedAt") or row.get("createdAt") or row.get("updatedAt") or 0


# How much of an Agent call's task and result its detail shows.
_AGENT_TEXT_MAX = 4000


def _owui_branch(chat):
    """The messages of a stored chat's current branch, oldest first."""
    history = (chat or {}).get("history") or {}
    messages = history.get("messages") or {}
    branch, seen, mid = [], set(), history.get("currentId")
    while mid and mid not in seen and mid in messages:
        seen.add(mid)
        branch.append(messages[mid])
        mid = messages[mid].get("parentId")
    return branch[::-1]


def _card_text(item):
    out = (item or {}).get("output")
    if isinstance(out, list):
        return "".join(p.get("text") or "" for p in out if isinstance(p, dict))
    return out if isinstance(out, str) else ""


def _agent_call_rows(chat):
    """The chat's in-process Agent calls (Claude Code sub-agents), newest
    first, out of its replies' own tool cards (ELI-152). The Gateway never
    lists them as child sessions, so a card is all there is: the call's
    description, its reply's start time, and a state its result tells.
    "launched" is a background agent whose report comes later on its own."""
    rows = []
    for msg in _owui_branch(chat):
        output = msg.get("output") or []
        results = {o.get("call_id"): o for o in output if o.get("type") == "function_call_output"}
        for item in output:
            name = item.get("name") or ""
            if item.get("type") != "function_call" or not _is_agent_tool(name.replace(TOOL_ERROR_MARK, "")):
                continue
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                args = {}
            args = args if isinstance(args, dict) else {}
            result = results.get(item.get("call_id") or item.get("id"))
            text = _card_text(result).strip()
            if TOOL_ERROR_MARK in name:
                state = "failed"
            elif result is None:
                state = "cut off" if msg.get("done") else "running"
            elif text.startswith(TOOL_NO_RESULT_NOTE):
                state = "cut off"
            elif text.startswith(_AGENT_LAUNCHED_PREFIX):
                state = "launched"
            else:
                state = "returned"
            ts = msg.get("timestamp")
            prompt = args.get("prompt")
            rows.append({
                "id": item.get("call_id") or item.get("id"),
                "title": str(args.get("description") or args.get("subagent_type") or _AGENT_TOOL_NAME),
                "subagentType": args.get("subagent_type"),
                "state": state,
                "startedAt": int(ts * 1000) if isinstance(ts, (int, float)) else None,
                # What its detail shows (ELI-155), capped like a card's text.
                "task": prompt[:_AGENT_TEXT_MAX] if isinstance(prompt, str) and prompt.strip() else None,
                "result": text[:_AGENT_TEXT_MAX] if result is not None and text else None,
                # A background agent holds its reply open until its report is in.
                "replyDone": bool(msg.get("done")),
            })
    return rows[::-1]


# How many of the chat's newest messages (its current branch) In-process
# agents and Background read: their time must not grow with the chat.
_RECENT_MESSAGES = 40


async def _recent_branch(current_id, links, read_rows, read_blob_message, limit=_RECENT_MESSAGES):
    """The last `limit` messages of a chat's current branch, chat-shaped
    ({"history": {"currentId", "messages"}, "truncated"}) for the section
    builders. `links` maps each message that has a row to its parent;
    `read_rows(ids)` reads those messages ({id: message}) in one go;
    `read_blob_message(id)` reads one message the chat's own copy holds but
    no row does yet (Open WebUI writes some straight into the chat)."""
    order, blob, seen, mid = [], {}, set(), current_id
    while mid and mid not in seen and len(order) < limit:
        seen.add(mid)
        if mid in links:
            parent = links[mid]
        else:
            msg = await read_blob_message(mid)
            if not isinstance(msg, dict):
                break
            blob[mid] = msg
            parent = msg.get("parentId")
        order.append(mid)
        mid = parent
    with_rows = [m for m in order if m not in blob]
    rows = await read_rows(with_rows) if with_rows else {}
    messages = {m: {**(blob.get(m) or rows.get(m)), "id": m} for m in order if blob.get(m) or rows.get(m)}
    return {"history": {"currentId": current_id, "messages": messages},
            "truncated": bool(mid) and mid not in seen}


async def _recent_of_chat(chat, limit=_RECENT_MESSAGES):
    """`_recent_branch` of a chat already read whole."""
    if not isinstance(chat, dict):
        return chat
    history = chat.get("history") or {}
    msgs = history.get("messages") or {}

    async def read_rows(ids):
        return {i: msgs[i] for i in ids if isinstance(msgs.get(i), dict)}

    async def no_blob(_mid):
        return None
    links = {k: v.get("parentId") for k, v in msgs.items() if isinstance(v, dict)}
    return {**chat, **await _recent_branch(history.get("currentId"), links, read_rows, no_blob, limit)}


async def _load_owui_recent(chat_id, user_id, limit=_RECENT_MESSAGES):
    """The chat's last `limit` messages when it is this user's, read from
    Open WebUI's message rows: the chat row's head, every row's parent link
    (two short columns), then those messages' rows. Building the whole chat
    costs about 14x its JSON in Python (ELI-115) and grows with the chat.
    None on any failure (the sections then list no Agent calls); the whole
    chat, cut the same way, where the rows cannot be read at all."""
    try:
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat
        from open_webui.models.chat_messages import ChatMessage
    except Exception:
        return await _recent_of_chat(await _load_owui_chat(chat_id, user_id), limit)
    prefix = f"{chat_id}-"
    try:
        async with get_async_db_context() as db:
            head = (await db.execute(select(Chat.user_id, Chat.current_message_id)
                                     .where(Chat.id == chat_id))).first()
            if head is None or head[0] != user_id:
                return None
            current = head[1] or (await db.execute(
                select(Chat.chat[("history", "currentId")]).where(Chat.id == chat_id))).scalar()
            links = {rid[len(prefix):]: parent for rid, parent in (await db.execute(
                select(ChatMessage.id, ChatMessage.parent_id).where(ChatMessage.chat_id == chat_id))).all()
                if isinstance(rid, str) and rid.startswith(prefix)}

            async def read_rows(ids):
                result = await db.execute(
                    select(ChatMessage.id, ChatMessage.parent_id, ChatMessage.role, ChatMessage.output,
                           ChatMessage.done, ChatMessage.created_at)
                    .where(ChatMessage.id.in_([prefix + i for i in ids])))
                return {rid[len(prefix):]: {"parentId": parent, "role": role, "output": output or [],
                                            "done": done, "timestamp": created}
                        for rid, parent, role, output, done, created in result.all()}

            async def read_blob_message(mid):
                return (await db.execute(select(Chat.chat[("history", "messages", mid)])
                                         .where(Chat.id == chat_id))).scalar()
            return await _recent_branch(current, links, read_rows, read_blob_message, limit)
    except Exception as ex:
        pipe_log(f"[status-action] could not read the chat's recent messages: {ex}")
        return None


async def _owui_chat_title(chat_id, user_id):
    """The chat's title when it is this user's: one row, two columns."""
    try:
        from sqlalchemy import select
        from open_webui.internal.db import get_async_db_context
        from open_webui.models.chats import Chat
    except Exception:
        chat = await _load_owui_chat(chat_id, user_id)
        return chat.get("title") if isinstance(chat, dict) else None
    try:
        async with get_async_db_context() as db:
            row = (await db.execute(select(Chat.title, Chat.user_id).where(Chat.id == chat_id))).first()
    except Exception as ex:
        pipe_log(f"[status-action] could not read the chat's title: {ex}")
        return None
    return row[0] if row is not None and row[1] == user_id else None


async def _load_owui_chat(chat_id, user_id):
    """The chat as Open WebUI stores it, when it is this user's; None outside
    Open WebUI or on any failure (the section then lists no Agent calls)."""
    try:
        from open_webui.models.chats import Chats
        row = Chats.get_chat_by_id(chat_id)
        if asyncio.iscoroutine(row):
            row = await row
    except Exception as ex:
        pipe_log(f"[status-action] could not read the chat for its Agent calls: {ex}")
        return None
    if row is None or getattr(row, "user_id", None) != user_id:
        return None
    return getattr(row, "chat", None)


def _render_subagents_fill_js(data: dict) -> str:
    return _SUBAGENTS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_background_fill_js(data: dict) -> str:
    return _BACKGROUND_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_agents_fill_js(data: dict) -> str:
    return _AGENTS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_section_error_js(section: str, message: str) -> str:
    """One section's error in its place, under its own title; the others
    keep what they show."""
    return (_SECTION_ERROR_JS_TEMPLATE.replace("__OPENCLAW_SECTION__", _json_for_js(section))
            .replace("__OPENCLAW_ERROR_MESSAGE__", _json_for_js(message)))


# The dialog's sections in their order: id suffix and title. The page draws
# them all at open (_MODAL_OPEN_JS_TEMPLATE), each fills on its own.
_DIALOG_SECTIONS = (
    ("context", "Context"),
    ("limits", "Rate limits"),
    ("background", "Background"),
    ("subagents", "Subagents"),
    ("agents", "In-process agents"),
)
# The sections the Gateway fills; In-process agents reads Open WebUI only.
_GATEWAY_SECTIONS = ("context", "limits", "background", "subagents")


# ---------------------------------------------------------------------------
# Open Status dialogs (ELI-163), by the id the open hands the page: whose it
# is, its session, the Subagents rows a folded section has yet to detail,
# and a `closed` flag the page sets when it is dismissed, so nothing is read
# for a dialog nobody looks at. In this process only; an entry lasts the
# dialog's follow bound at most.

_open_dialogs: dict = {}


def _dialog_open(user_id: str, session_key: str) -> tuple:
    now = time.time()
    for k in [k for k, d in _open_dialogs.items() if d["expires"] < now]:
        _open_dialogs.pop(k, None)
    dialog_id = uuid.uuid4().hex
    dialog = {"user": user_id, "session": session_key, "closed": asyncio.Event(),
              "expires": now + _DIALOG_FOLLOW_MAX_S, "rows": [], "total": 0}
    _open_dialogs[dialog_id] = dialog
    return dialog_id, dialog


def _dialog_of(body: dict, user_id: str):
    """The caller's own open dialog the request names, else None."""
    dialog = _open_dialogs.get(body.get("dialogId") or "")
    return dialog if dialog and dialog["user"] == user_id else None


def _subagent_previews(previews_resp) -> tuple:
    """({key: task text}, {key: newest message}) from a sessions.preview
    answer: the spawn's task when the preview still reaches it, and what the
    child said last (none while its newest message is still the spawn)."""
    latest = {}
    for p in (previews_resp or {}).get("previews") or []:
        texts = [i.get("text") for i in p.get("items") or [] if isinstance(i.get("text"), str) and i["text"].strip()]
        if p.get("key") and texts and not _subagent_task_text(texts[-1]):
            latest[p["key"]] = texts[-1].strip()
    return _subagent_task_texts(previews_resp), latest


async def _read_automations(conn) -> "list | None":
    """Every enabled automation, or None on a Gateway without `cron.list`."""
    try:
        resp = await conn.send_request("cron.list", dict(limit=200), timeout=5)
    except Exception as ex:
        if _is_unknown_method(ex):
            return None
        raise
    return (resp or {}).get("jobs") or []


# ---------------------------------------------------------------------------
# The Background section (ELI-163): what runs for this chat outside a reply.
# Read once when the dialog opens, never polled.

# The Gateway's task kinds the section lists, as the row's kind label. Its
# other kinds are listed elsewhere (a sub-agent or ACP child is the
# Subagents section's) or are its own upkeep (context maintenance).
_BACKGROUND_KINDS = {
    "exec": "Command",
    "image_generation": "Image",
    "music_generation": "Music",
    "video_generation": "Video",
    "automation_run": "Automation",
}
# How many finished items of this chat the section keeps, newest first; a
# running one is always listed.
_BACKGROUND_FINISHED_CAP = 5
_LIVE_TASK_STATUSES = ("running", "queued")
# The exec tool's answer for a command left running: "Command still running
# (session good-lagoon, pid 2816)". The session is the task's sourceId.
_EXEC_SESSION_RE = re.compile(r"\(session ([\w.-]+)")


def _is_unknown_method(ex: Exception) -> bool:
    return "unknown method" in str(ex).lower()


async def _read_background_tasks(conn, session_key: str) -> "list | None":
    """The adapter between the section and the Gateway's background work for
    one session, as task rows. OpenClaw 2026.9 lists it with `tasks.list`;
    2026.10 removed that method, and a session's background commands are
    `sessions.processes.list` (media jobs have no per-session list there). A
    Gateway with neither gives None: the section then says it is not
    available here instead of failing."""
    try:
        resp = await conn.send_request("tasks.list", dict(sessionKey=session_key, limit=100), timeout=5)
        return (resp or {}).get("tasks") or []
    except Exception as ex:
        if not _is_unknown_method(ex):
            raise
    try:
        resp = await conn.send_request("sessions.processes.list", dict(key=session_key), timeout=5)
    except Exception as ex:
        if _is_unknown_method(ex):
            return None
        raise
    return [_process_task(p, session_key) for p in (resp or {}).get("processes") or []]


# A 2026.10 process's status as a task's.
_PROCESS_STATUSES = {"running": "running", "completed": "completed", "failed": "failed", "killed": "cancelled"}


def _process_task(process: dict, session_key: str) -> dict:
    """One `sessions.processes.list` row as the task row `_task_row` reads: an
    exec task whose sourceId is the process (the session its tool card
    names), titled by the Gateway's short name until a card gives the
    command, and a finished one's last line of output as its summary."""
    lines = [ln for ln in (process.get("tail") or "").splitlines() if ln.strip()]
    return {
        "id": process.get("processId"), "kind": "exec", "sourceId": process.get("processId"),
        "sessionKey": session_key, "title": process.get("name") or "",
        "status": _PROCESS_STATUSES.get(process.get("status"), process.get("status")),
        "startedAt": process.get("startedAt"), "endedAt": process.get("endedAt"),
        "terminalSummary": lines[-1][:200] if lines else None,
    }


def _exec_commands(chat) -> dict:
    """{exec session id: command} from the chat's own exec tool cards. The
    Gateway's task for a background command is titled "CLI command"; its
    sourceId is the session the card's result names."""
    out = {}
    for msg in _owui_branch(chat):
        output = msg.get("output") or []
        calls = {o.get("id"): o for o in output if o.get("type") == "function_call"}
        for o in output:
            if o.get("type") != "function_call_output":
                continue
            call = calls.get(o.get("call_id")) or {}
            if (call.get("name") or "").replace(TOOL_ERROR_MARK, "").strip() != "exec":
                continue
            m = _EXEC_SESSION_RE.search(_card_text(o))
            try:
                args = json.loads(call.get("arguments") or "{}")
            except ValueError:
                args = {}
            command = args.get("command") if isinstance(args, dict) else None
            if m and isinstance(command, str) and command.strip():
                out[m.group(1)] = command.strip()
    return out


def _belongs_to(task: dict, session_key: str) -> bool:
    """A task this chat started (its requester), or one run in it (an
    automation run's target session)."""
    return session_key in (task.get("sessionKey"), task.get("childSessionKey"), task.get("ownerKey"))


def _task_row(task: dict, commands: dict) -> dict:
    kind = task.get("kind") or ""
    title = (task.get("title") or "").strip()
    if kind == "exec":
        # 2026.9 titles every one "CLI command"; a 2026.10 process has a
        # short name ("make build"). The card's whole command wins.
        own = "" if title == "CLI command" else title
        title = commands.get(task.get("sourceId") or "") or own or "Shell command"
    status = task.get("status")
    live = status in _LIVE_TASK_STATUSES
    return {
        "id": task.get("id") or task.get("taskId"),
        "kind": _BACKGROUND_KINDS.get(kind, "Task"),
        "title": title or _BACKGROUND_KINDS.get(kind, "Task"),
        "status": status,
        "startedAt": task.get("startedAt") or task.get("createdAt"),
        "endedAt": None if live else (task.get("endedAt") or task.get("updatedAt")),
        # A running command's progress is always "Command running": its chip says so.
        "preview": (None if live and kind == "exec" else task.get("progressSummary") if live
                    else (task.get("error") or task.get("terminalSummary"))),
    }


def _job_bound_to(job: dict, session_key: str) -> bool:
    """An automation that runs in this chat: `session:<key>`, or "current"
    with the session it was made in."""
    target = job.get("sessionTarget") or ""
    return target == f"session:{session_key}" or (target == "current" and job.get("sessionKey") == session_key)


def _job_status(job: dict) -> str:
    state = job.get("state") or {}
    if state.get("runningAtMs"):
        return "running"
    last = state.get("lastRunStatus") or state.get("lastStatus")
    return {"ok": "completed", "error": "failed", "skipped": "skipped"}.get(last, "scheduled")


def _automation_row(job: dict) -> dict:
    state = job.get("state") or {}
    status = _job_status(job)
    return {
        "id": job.get("id"),
        "kind": "Automation",
        "job": True,    # a schedule, not one run: its time is its next run
        "title": (job.get("displayName") or job.get("name") or "").strip() or "Automation",
        "status": status,
        "startedAt": state.get("runningAtMs") if status == "running" else None,
        "endedAt": None,
        "error": state.get("lastError") if status == "failed" else None,
        "failedInARow": state.get("consecutiveErrors") or 0,
        "nextRunAtMs": job.get("nextRunAtMs") or state.get("nextRunAtMs"),
        "lastRunAtMs": state.get("lastRunAtMs"),
    }


def _background_rows(tasks, jobs, agents, session_key: str, *, commands: dict) -> dict:
    """The Background section's data. `tasks`: the Gateway's tasks for this
    session (None: the Gateway has no task list). `jobs`: every automation
    (None: not readable). `agents`: this chat's Agent call rows. Only what
    belongs to this chat is listed, whoever asks (ELI-175)."""
    automations_available = jobs is not None
    jobs = [j for j in jobs or [] if j.get("enabled", True)]
    mine = [j for j in jobs if _job_bound_to(j, session_key)]
    job_ids = {j.get("id") for j in mine}
    rows = [_automation_row(j) for j in mine]
    for t in tasks or []:
        if not _belongs_to(t, session_key) or (t.get("kind") or t.get("runtime")) not in _BACKGROUND_KINDS:
            continue
        if t.get("kind") == "automation_run" and t.get("sourceId") in job_ids:
            continue    # its automation's own row says how it went
        rows.append(_task_row(t, commands))
    for a in agents or []:
        if a.get("state") != "launched":
            continue
        rows.append({
            "id": a["id"], "kind": "Agent", "title": a["title"],
            # Its reply stays open until its report is in.
            "status": "completed" if a.get("replyDone") else "running",
            "startedAt": a.get("startedAt"), "endedAt": None, "preview": None,
            "agent": a,
        })
    live = [r for r in rows if r["status"] in _LIVE_TASK_STATUSES]
    rest = sorted((r for r in rows if r["status"] not in _LIVE_TASK_STATUSES),
                  key=lambda r: r.get("startedAt") or r.get("lastRunAtMs") or 0, reverse=True)
    live.sort(key=lambda r: r.get("startedAt") or 0, reverse=True)
    return {
        "rows": live + rest[:_BACKGROUND_FINISHED_CAP],
        "total": len(rows),
        "tasksAvailable": tasks is not None,
        "automationsAvailable": automations_available,
    }


_TRAILING_UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)


def _control_ui_session_url(base: str, session_key: str) -> str | None:
    """The session's page in the OpenClaw Control UI (ELI-141).

    The UI's short-id route, `<base>/chat/<agentId>/<id>`, where `<id>` is the
    hex of the UUID the session key ends in (OpenClaw docs/web/urls.md; the UI
    resolves it with `sessions.resolve {shortId}`). Every key the dialog shows
    ends in one: `agent:<id>:openwebui-<user>-<chat>` and `agent:<id>:subagent:<uuid>`.
    None without an http(s) base or for any other key."""
    base = (base or "").strip().rstrip("/")
    parts = (session_key or "").split(":", 2)
    if not base.lower().startswith(("http://", "https://")) or len(parts) != 3 or parts[0] != "agent":
        return None
    m = _TRAILING_UUID_RE.search(parts[2])
    if not m or not parts[1]:
        return None
    return f"{base}/chat/{parts[1]}/{m.group(1).lower().replace('-', '')}"


def _render_drawer_open_js(task: dict) -> str:
    return _DRAWER_OPEN_JS_TEMPLATE.replace("__OPENCLAW_TASK__", _json_for_js(task))


def _render_drawer_error_js(message: str, task_key: str | None = None) -> str:
    """`task_key`: only the drawer showing that sub-agent takes it."""
    return (_DRAWER_ERROR_JS_TEMPLATE.replace("__OPENCLAW_ERROR_MESSAGE__", _json_for_js(message))
            .replace("__OPENCLAW_TASK_KEY__", _json_for_js(task_key)))


def _render_drawer_fill_js(data: dict) -> str:
    return _DRAWER_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


# How long the open Status dialog follows its running sub-agents' pushed
# changes (a request held open), so a forgotten dialog cannot pin it forever.
_DIALOG_FOLLOW_MAX_S = 900
# How often an open drawer refreshes a running sub-agent. The page polls,
# not a held request, so the poll lives exactly as long as the drawer is open
# and the sub-agent runs (ELI-155).
_SUBAGENT_POLL_INTERVAL_S = 2.5


def _message_text_blocks(message: dict) -> list:
    content = message.get("content")
    if isinstance(content, str):
        return [content.strip()] if content.strip() else []
    return [b["text"].strip() for b in content or []
            if isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()]


def _spawn_message_text(message: dict) -> str | None:
    """A child's spawn message as the wrapped prompt its model got, or None
    when the message is not one. Up to OpenClaw 2026.9 `chat.history` shows
    the wrapped prompt; from 2026.10.10 it shows the bare task and keeps the
    prompt in `__openclaw.modelPromptProjection.text` (ELI-164)."""
    if message.get("role") != "user":
        return None
    text = "\n".join(_message_text_blocks(message))
    if _is_spawn_message(text):
        return text
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    projection = meta.get("modelPromptProjection") if isinstance(meta.get("modelPromptProjection"), dict) else {}
    prompt = projection.get("text")
    return prompt if _is_spawn_message(prompt) else None


def _spawn_task_of(messages: list) -> str | None:
    """The task out of the child's spawn message, when the history read
    still holds it."""
    for m in messages or []:
        wrapped = _spawn_message_text(m)
        if wrapped:
            return _subagent_task_text(wrapped)
    return None


# How the drawer reads a sub-agent's run: the Gateway's largest page
# (CHAT_HISTORY_MAX_ENTRIES), text blocks up to 50000 characters, and a byte
# budget per read: the newest steps that fit, the rest behind "Show earlier
# steps", which reads with the larger budget.
_DRAWER_HISTORY_PAGE = 1000
_DRAWER_TEXT_MAX_CHARS = 50000
_DRAWER_RUN_BYTES = 512 * 1024
_DRAWER_RUN_BYTES_EARLIER = 4 * 1024 * 1024


async def _read_child_history(conn, child_key: str, budget: int, *, all_pages: bool) -> tuple[list, bool]:
    """The child's transcript, oldest first, and whether it is whole. One
    page by default; with `all_pages`, older pages too while the Gateway has
    more and the budget lasts."""
    messages: list = []
    offset, size = 0, 0
    while True:
        params = dict(sessionKey=child_key, limit=_DRAWER_HISTORY_PAGE, maxChars=_DRAWER_TEXT_MAX_CHARS)
        if offset:
            params["offset"] = offset
        resp = await conn.send_request("chat.history", params, timeout=8) or {}
        page = resp.get("messages") or []
        messages = page + messages
        size += len(json.dumps(page))
        more = bool(resp.get("hasMore"))
        nxt = resp.get("nextOffset")
        if not (all_pages and more and page and size < budget and isinstance(nxt, int) and nxt > offset):
            return messages, not more
        offset = nxt


def _drawer_run_items(messages: list) -> list:
    """The child's run as the drawer's read-only chat, oldest first: the
    turns it was sent after its task, and each stretch of its replies as
    the chat's own rebuild draws it (`_history_renderer`): text, and a card
    per tool call with its result."""
    items: list = []
    stretch: list = []

    def flush():
        if not stretch:
            return
        out = _history_renderer(stretch).output
        results = {o.get("call_id"): _card_text(o) for o in out if o.get("type") == "function_call_output"}
        for o in out:
            if o.get("type") == "message":
                text = "".join(c.get("text") or "" for c in o.get("content") or []).strip()
                if text:
                    items.append({"kind": "text", "text": text})
            elif o.get("type") == "function_call":
                # A call item's own id is its call id; only its output carries `call_id`.
                items.append({"kind": "tool", "id": o.get("id"), "name": o.get("name") or "tool",
                              "args": o.get("arguments") or "", "result": results.get(o.get("id"))})
        stretch.clear()

    for m in messages or []:
        text = "\n".join(_message_text_blocks(m)) if m.get("role") == "user" else ""
        if not text:
            stretch.append(m)
            continue
        flush()
        if not _spawn_message_text(m):      # the task itself is the header's
            items.append({"kind": "user", "text": text})
    flush()
    return items


def _cap_run_items(items: list, budget: int) -> tuple[list, int]:
    """The newest items that fit `budget` bytes, and how many older ones
    did not. The last item always stays; when it alone is over, the end of
    its text does (a reply's end is its conclusion)."""
    sizes = [len(json.dumps(i)) for i in items]
    total, dropped = sum(sizes), 0
    while total > budget and dropped < len(items) - 1:
        total -= sizes[dropped]
        dropped += 1
    kept = items[dropped:]
    if total > budget and kept:
        last = dict(kept[-1])
        field = "result" if last.get("kind") == "tool" else "text"
        text = last.get(field) or ""
        room = max(0, len(text) - (total - budget) - 64)
        last[field] = "…" + text[len(text) - room:] if room else "…"
        kept = kept[:-1] + [last]
    return kept, dropped


_OWUI_SESSION_KEY_RE = re.compile(
    r"^agent:[^:]+:openwebui-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$", re.IGNORECASE)


async def _drawer_parent(conn, session_row: dict, user_id) -> dict | None:
    """Where the sub-agent came from, for the header's "from" link: an Open
    WebUI chat by its title, or the sub-agent that spawned it by its label."""
    key = session_row.get("spawnedBy") or session_row.get("parentSessionKey")
    if not isinstance(key, str) or not key:
        return None
    m = _OWUI_SESSION_KEY_RE.match(key)
    if m:
        title = await _owui_chat_title(m.group(2), user_id) if user_id else None
        return {"title": title or "its chat", "chatId": m.group(2)}
    try:
        desc = await conn.send_request("sessions.describe", dict(key=key), timeout=5)
    except Exception as ex:
        pipe_log(f"[status-action] parent describe failed: {ex}")
        return None
    row = (desc or {}).get("session") or {}
    return {"title": (row.get("label") or row.get("displayName") or "").strip() or key, "childKey": key}


async def _latest_preview(conn, child_key: str) -> str | None:
    """A running child's newest message as the session list previews it:
    what a claude-cli child has said so far, while its transcript is still
    empty."""
    try:
        resp = await conn.send_request(
            "sessions.list", dict(search=child_key.rsplit(":", 1)[-1], limit=10, includeLastMessage=True), timeout=5)
    except Exception as ex:
        pipe_log(f"[status-action] latest preview failed: {ex}")
        return None
    row = next((r for r in (resp or {}).get("sessions") or [] if r.get("key") == child_key), {})
    preview = row.get("lastMessagePreview")
    return preview if isinstance(preview, str) and preview.strip() and not _is_spawn_message(preview) else None


# The toolbar button's icon, the composer chip's paw (ELI-202); without one
# Open WebUI draws its default sparkle. A fixed gray stroke on purpose: Open
# WebUI draws it as an <img>, so currentColor never resolves, and inverts it
# 80% in dark mode; this gray stays a mid gray in both, like its own icons.
ICON_URL = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg%20xmlns='http://www.w3.org/2000/svg'%20viewBox='0%200%2024%2024'%20fill='none'%20"
    "stroke='%236b7280'%20stroke-width='1.6'%20stroke-linecap='round'%20stroke-linejoin='round'"
    "%3E%3Cpath%20d='M11%206a1.5%202%200%201%201-3%200%201.5%202%200%200%201%203%200ZM16%206a"
    "1.5%202%200%201%201-3%200%201.5%202%200%200%201%203%200ZM7%2010.5a1.5%201.75%200%201%201"
    "-3%200%201.5%201.75%200%200%201%203%200ZM20%2010.5a1.5%201.75%200%201%201-3%200%201.5%20"
    "1.75%200%200%201%203%200ZM12%2012.75c-2.25%200-5.25%202.6-5.25%204.875%200%201.45%201.1"
    "%202.375%202.55%202.375.95%200%201.7-.45%202.7-.45s1.75.45%202.7.45c1.45%200%202.55-.925"
    "%202.55-2.375%200-2.275-3-4.875-5.25-4.875Z'/%3E%3C/svg%3E"
)


class Action:
    class Valves(BaseModel):
        GATEWAY_URL: str = Field(
            default="localhost:18789",
            description="Copy this exactly from the Pipe's valves. Gateway as "
                        "host:port, no http:// or ws:// prefix, e.g. "
                        "192.168.1.10:18789. Used only when this Action has to "
                        "open its own connection instead of reusing the Pipe's."
        )
        GATEWAY_TOKEN: str = Field(
            default="",
            description="Copy this exactly from the Pipe's valves. The value of "
                        "`gateway.auth.token` in your OpenClaw config. Used only "
                        "for the fallback connection."
        )
        DEVICE_IDENTITY: str = Field(
            default="",
            description="Copy this exactly from the Pipe's valves -- do not leave "
                        "it empty if the Pipe has one. It is what lets a fallback "
                        "connection be recognised as the same already-approved "
                        "device instead of raising a second pairing request."
        )
        STATE_DIR: str = Field(
            default="/data/openclaw-bridge",
            description="Copy this exactly from the Pipe's valves. It is how a "
                        "fallback connection finds the Pipe's persisted device "
                        "token on disk without re-pairing."
        )
        AGENT_ID: str = Field(
            default="main",
            description="Copy this exactly from the Pipe's valves -- the same "
                        "OpenClaw agent the Pipe routes chats to."
        )
        CONTROL_UI_URL: str = Field(
            default="",
            description="The address you open the OpenClaw Control UI at, e.g. "
                        "https://openclaw.example.com. When set, the Status "
                        "dialog and the sub-agent drawer link to the session in "
                        "it. Empty: no link."
        )

    def __init__(self):
        self.valves = self.Valves()
        self.icon_url = ICON_URL

    async def action(self, body: dict, __user__=None, __event_emitter__=None):
        """Dispatches on body["mode"]: the default ("status", or absent --
        this is what OWUI sends for the actual message-toolbar click) opens
        the dialog and shows live usage; "compact" and "subagent-detail" are
        never sent by OWUI itself -- both are synthetic markers the dialog's
        own in-page buttons fetch back with (see triggerCompact() and
        openSubagentDrawer() in _MODAL_OPEN_JS_TEMPLATE), reusing this same
        endpoint rather than registering a second Action.

        A real toolbar click on an old sub-agent-finished message (written
        by bridge versions before ELI-100) is also mode-less (OWUI itself
        never sends a "mode"), but carries a hidden
        `<!-- openclaw:taskId=... -->` marker in `body["content"]` -- there's deliberately no separate Action/button for
        this: a second OWUI Function, plus flipping the existing one off
        "global", was more infra and config risk than the payoff of a
        visually distinct icon. Detected here and redirected
        straight into the same subagent-detail drawer instead of the
        general status dialog, keyed off content rather than mode."""
        mode = body.get("mode")
        if mode == "compact":
            return await self._run_compact(body, __user__, __event_emitter__)
        if mode == "subagent-detail":
            return await self._run_subagent_detail(body, __user__, __event_emitter__)
        if mode == "status-closed":
            # The page's dialog was dismissed: its open request stops following.
            dialog = _dialog_of(body, (__user__ or {}).get("id") or "unknown")
            if dialog:
                _open_dialogs.pop(body["dialogId"], None)
                dialog["closed"].set()
            return {"status": "ok"}
        if mode == "subagent-rows":
            return await self._run_subagent_rows(body, __user__, __event_emitter__)
        if mode is None:
            marker = _SUBAGENT_TASK_ID_MARKER_RE.search(body.get("content") or "")
            if marker:
                return await self._run_subagent_detail(
                    {**body, "taskId": marker.group(1)}, __user__, __event_emitter__,
                )
        try:
            return await self._run_status(body, __user__, __event_emitter__)
        finally:
            # Every status run (re)delivers the composer Status button, so one
            # spark click is enough to install it in this tab. Sent last so it
            # never delays the dialog; idempotent and best effort (never
            # raises). Not on compact/drawer runs: the dialog that sends those
            # was opened by a status run, which already delivered it. The
            # Action is global (its spark shows under every model's replies),
            # so the chat is only vouched for when its model is the Pipe's.
            model = body.get("model")
            ours = isinstance(model, str) and model.startswith(_COMPOSER_DEFAULT_MODEL_PREFIX)
            await _emit_composer_button(
                __event_emitter__,
                session_id=body.get("session_id"),
                chat_id=body.get("chat_id") if ours else None,
                chat_model=model if ours else None,
            )

    async def _run_status(self, body: dict, __user__, __event_emitter__, *, show_loading=True):
        """Opens the dialog at once with every section's title, then fills
        each section on its own the moment its own data is in (ELI-163): a
        slow or failing source shows in its section only.

        One request per source per open, all sent together: sessions.describe
        (Context, and the provider Rate limits needs), usage.status,
        sessions.list (Subagents: the store's rows, no transcripts read),
        tasks.list and cron.list (Background), and Open WebUI's copy of the
        chat (In-process agents, and Background's commands and agents). Read
        only when needed: the claude-cli usage fallback when usage.status has
        no windows, and the sub-agents' transcripts (sessions.preview) only
        when the Subagents section opens, at once when a child runs, else on
        the reader's first tap (mode "subagent-rows")."""
        chat_id = body.get("chat_id")
        user_id = (__user__ or {}).get("id") or "unknown"

        if not chat_id:
            await __event_emitter__({
                "type": "notification",
                "data": {"type": "error", "content": "OpenClaw status: no chat_id on this message"},
            })
            return {"status": "error", "detail": "missing chat_id"}

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)
        # A refresh from inside the dialog (after a compact) replaces the
        # open's reads: the open stops following.
        previous = _dialog_of(body, user_id)
        if previous:
            _open_dialogs.pop(body["dialogId"], None)
            previous["closed"].set()
        dialog_id, dialog = _dialog_open(user_id, session_key)

        async def emit(code):
            await __event_emitter__({"type": "execute", "data": {"code": code}})

        # chat_id/message_id/session_id/model never depend on any RPC --
        # they're already in `body` -- so they're embedded once at open
        # time (IDENTITY in _MODAL_OPEN_JS_TEMPLATE) rather than threaded
        # through every section's own fill payload.
        if show_loading:
            identity = {
                "chatId": chat_id,
                "messageId": body.get("id"),
                "sessionId": body.get("session_id"),
                "owuiModel": body.get("model"),
                "dialogId": dialog_id,
                "controlUiUrl": _control_ui_session_url(self.valves.CONTROL_UI_URL, session_key),
                "subagentPollMs": int(_SUBAGENT_POLL_INTERVAL_S * 1000),
            }
            await emit(_render_modal_open_js(identity))
        else:
            # Post-compact refresh: the dialog is already open, but
            # _MODAL_COMPACTING_JS replaced the sections with its spinner;
            # put the sections back before the refreshed reads.
            await emit(_RESET_SECTIONS_JS.replace("__OPENCLAW_DIALOG_ID__", _json_for_js(dialog_id)))

        # The chat's newest messages from Open WebUI: no Gateway in it, so it
        # is read before connecting and its section fills even with no
        # Gateway.
        chat_task = asyncio.ensure_future(_load_owui_recent(chat_id, user_id))

        async def fill_agents():
            chat = await chat_task
            agents = _agent_call_rows(chat)
            await emit(_render_agents_fill_js({
                "agents": agents[:_SUBAGENT_LIST_CAP], "total": len(agents),
                "recent": _RECENT_MESSAGES if (chat or {}).get("truncated") else None,
                "fetchedAt": int(time.time() * 1000), "error": None,
            }))

        agents_ready = asyncio.ensure_future(fill_agents())

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            pipe_log(f"[status-action] connect failed: {ex}")
            for section in _GATEWAY_SECTIONS:
                await emit(_render_section_error_js(section, f"Could not connect: {ex}"))
            await agents_ready
            return {"status": "error", "detail": str(ex)}

        # Every source at once; each section awaits only its own.
        desc_task = asyncio.ensure_future(
            conn.send_request("sessions.describe", dict(key=session_key), timeout=5))
        usage_task = asyncio.ensure_future(conn.send_request("usage.status", {}, timeout=5))
        # The parent's child sessions (ELI-152), without reading every
        # session: `spawnedBy` gives the current ones (running, or ended in
        # the last 30 minutes), the child index (state.py) every child the
        # Pipe saw spawned with its last row. A new index is seeded once, from
        # one full list, after the section's first fill.
        current_task = asyncio.ensure_future(conn.send_request(
            "sessions.list", dict(spawnedBy=session_key, limit=50), timeout=5))
        seed_task = None if _subagent_index_seeded() else asyncio.ensure_future(
            conn.send_request("sessions.list", dict(limit=1000), timeout=30))
        tasks_task = asyncio.ensure_future(_read_background_tasks(conn, session_key))
        jobs_task = asyncio.ensure_future(_read_automations(conn))

        def list_children():
            return conn.send_request(
                "sessions.list", dict(spawnedBy=session_key, limit=50, includeLastMessage=True), timeout=5,
            )

        async def fill_context():
            """Also resolves provider/session_row for fill_limits to reuse
            (returned, not re-fetched) -- still reuses emit.py's small
            formatting primitives (_fmt_tokens, _format_goal_line) so
            wording matches the status line exactly."""
            try:
                desc = await desc_task
            except Exception as ex:
                pipe_log(f"[status-action] sessions.describe failed: {ex}")
                await emit(_render_context_fill_js({"error": f"Could not fetch context: {ex}"}))
                return None, {}

            session_row = (desc or {}).get("session") or {}
            provider = session_row.get("modelProvider") or "?"

            context_tokens = session_row.get("contextTokens")
            total_tokens = session_row.get("totalTokens")
            context_data = None
            if context_tokens and total_tokens is not None:
                context_data = {
                    "usedTokens": _fmt_tokens(total_tokens),
                    "totalTokens": _fmt_tokens(context_tokens),
                    "pct": round(total_tokens / context_tokens * 100, 1),
                }

            goal_data = None
            goal = session_row.get("goal")
            if goal:
                goal_line = _format_goal_line(goal)
                if goal_line:
                    tokens_used = goal.get("tokensUsed")
                    budget = goal.get("tokenBudget")
                    goal_pct = (
                        round(tokens_used / budget * 100, 1)
                        if (tokens_used is not None and budget) else None
                    )
                    goal_data = {"line": goal_line, "pct": goal_pct}

            await emit(_render_context_fill_js({
                "provider": provider,
                "model": session_row.get("model") or "",
                "context": context_data,
                "goal": goal_data,
                "source": source,
                "fetchedAt": int(time.time() * 1000),
                "error": None,
            }))
            return provider, session_row

        async def fill_limits():
            # Which provider's windows to show is the session's: Context's
            # read of it, the same request.
            provider, session_row = await context_ready
            if provider is None:
                await emit(_render_limits_fill_js(
                    {"error": "Could not tell which provider this chat uses (its context did not load)."}))
                return
            try:
                usage = await usage_task
            except Exception as ex:
                pipe_log(f"[status-action] usage.status failed: {ex}")
                await emit(_render_limits_fill_js({"error": f"Could not fetch rate limits: {ex}"}))
                return

            windows_data = _limits_windows_for(usage, provider)
            if not windows_data and provider == "anthropic":
                windows_data = await _claude_cli_usage_windows(conn)

            await emit(_render_limits_fill_js({
                "provider": provider,
                "windows": windows_data,
                # Only used to word the empty-windows note -- a live run is
                # the one confirmed case (2026-07-11 live debugging) where
                # usage.status can transiently report zero windows for the
                # active provider, most likely because its rate-limit cache
                # refreshes from that run's own API response headers.
                "sessionActive": session_row.get("status") == "running",
                "source": source,
                "fetchedAt": int(time.time() * 1000),
                "error": None,
            }))

        async def fill_background():
            errors = []
            try:
                tasks = await tasks_task
            except Exception as ex:
                pipe_log(f"[status-action] tasks.list failed: {ex}")
                tasks = []
                errors.append(f"Could not read background tasks: {ex}")
            try:
                jobs = await jobs_task
            except Exception as ex:
                pipe_log(f"[status-action] cron.list failed: {ex}")
                jobs = []
                errors.append(f"Could not read automations: {ex}")
            chat = await chat_task
            data = _background_rows(tasks, jobs, _agent_call_rows(chat), session_key,
                                    commands=_exec_commands(chat))
            await emit(_render_background_fill_js({
                **data, "errors": errors, "source": source,
                "fetchedAt": int(time.time() * 1000), "error": None,
            }))

        # Every child seen so far by key, and what their transcripts told.
        children_seen, task_texts, latest = {}, {}, {}

        async def read_transcripts(keys):
            """The task and newest message of each of `keys` not read yet."""
            unread = [k for k in keys if k not in task_texts]
            if not unread:
                return
            try:
                previews = await conn.send_request(
                    "sessions.preview", dict(keys=unread, limit=20, maxChars=2000), timeout=5,
                )
            except Exception as ex:
                # Titles fall back to the spawn label; the rows still render.
                pipe_log(f"[status-action] sessions.preview (children) failed: {ex}")
                return
            texts, newest = _subagent_previews(previews)
            task_texts.update({k: None for k in unread})
            task_texts.update(texts)
            latest.update(newest)

        async def read_children():
            """The current children, then each indexed child they lack: its
            kept row when it had ended, else a lookup by its key. A failed
            list still shows the indexed children."""
            known = _subagent_children(session_key)
            error = None
            try:
                current = [r for r in ((await current_task) or {}).get("sessions") or []
                           if _is_child_of(r, session_key)]
            except Exception as ex:
                error, current = ex, []
            rows = {r["key"]: r for r in current}
            stale = [k for k, snap in known.items() if k not in rows and not _subagent_final(snap)]
            found = [r for r in await asyncio.gather(*(_lookup_child(conn, k) for k in stale[:_SUBAGENT_LIST_CAP]))
                     if r]
            rows.update({r["key"]: r for r in found})
            rows.update({k: snap for k, snap in known.items() if k not in rows and snap})
            if current or found:
                _subagent_remember(session_key, rows=current + found)
            if error is not None and not rows:
                raise error
            return {"sessions": list(rows.values())}

        async def seeded_children():
            """The seed's children of this chat the section lacks."""
            everything = (await seed_task) or {}
            _subagent_index_mark_seeded(everything.get("sessions") or [])
            return {"sessions": [snap for k, snap in _subagent_children(session_key).items()
                                 if snap and k not in children_seen]}

        def subagent_rows(shown):
            # A child's transcript may not hold its spawn message yet: then
            # the task its spawn call was given titles it (ELI-178).
            spawned = _subagent_tasks(session_key)
            return [{**_subagent_row(r, task_texts.get(r["key"]) or spawned.get(r["key"]),
                                     latest.get(r["key"])),
                     "controlUiUrl": _control_ui_session_url(self.valves.CONTROL_UI_URL, r["key"])}
                    for r in shown]

        async def fill_subagents(children, *, at_open=False):
            """Lists every child session of this chat (ELI-152), ended ones
            included, newest first, capped. Each row carries the fields its
            click hands the detail drawer (see S.openSubagentDrawer /
            Action._run_subagent_detail), so the drawer can paint its header
            instantly before its own RPCs resolve. A row's id is the child
            session key. At open, a section with no running child starts
            folded (the page's rule), so its transcripts wait for a tap.
            Returns the keys of the listed children still running."""
            try:
                children_resp = await children
            except Exception as ex:
                pipe_log(f"[status-action] sessions.list (children) failed: {ex}")
                await emit(_render_subagents_fill_js({"error": f"Could not fetch subagents: {ex}"}))
                return []
            for r in (children_resp or {}).get("sessions") or []:
                if _is_child_of(r, session_key):
                    children_seen[r["key"]] = r
            shown = sorted(children_seen.values(), key=_subagent_started, reverse=True)[:_SUBAGENT_LIST_CAP]
            running = [r["key"] for r in shown if _subagent_status(r.get("status")) in _LIVE_SUBAGENT_STATUSES]
            pending = at_open and not running and bool(shown)
            if not pending:
                await read_transcripts([r["key"] for r in shown])
            dialog["rows"], dialog["total"] = shown, len(children_seen)
            await emit(_render_subagents_fill_js({
                "tasks": subagent_rows(shown), "total": len(children_seen), "pending": pending,
                "source": source, "fetchedAt": int(time.time() * 1000), "error": None,
            }))
            return running

        context_ready = asyncio.ensure_future(fill_context())
        limits_ready = asyncio.ensure_future(fill_limits())
        background_ready = asyncio.ensure_future(fill_background())
        subagents_ready = asyncio.ensure_future(fill_subagents(read_children(), at_open=True))

        results = await asyncio.gather(context_ready, limits_ready, background_ready, subagents_ready,
                                       agents_ready, return_exceptions=True)
        for name, result in zip(("context", "limits", "background", "subagents", "agents"), results):
            if isinstance(result, BaseException):
                pipe_log(f"[status-action] {name} section failed: {result!r}")
                await emit(_render_section_error_js(name, f"Could not load this section: {result}"))
        listed = results[3] if isinstance(results[3], list) else []

        pipe_log(f"[status-action] status dialog filled ({source})")

        if seed_task is not None:
            try:
                before = len(children_seen)
                more = await seeded_children()
                if more["sessions"] and not dialog["closed"].is_set():
                    seen_running = await fill_subagents(_resolved(more))
                    listed = seen_running if len(children_seen) > before else listed
            except Exception as ex:
                pipe_log(f"[status-action] child index seed failed: {ex}")

        # The ticker counts a running child up while the dialog is open, so a
        # child that ends meanwhile must show its end (ELI-127). Hold this
        # request open, as the drawer does, and refill the section on each
        # pushed change to a listed child's run state, until none is listed,
        # the page closes the dialog, or the follow's bound passes. Each event
        # is taken before the read it guards, so no change slips between them.
        deadline = time.time() + _DIALOG_FOLLOW_MAX_S
        closed = dialog["closed"]
        while listed and not closed.is_set():
            changes = [conn.session_change_event(k) for k in listed]
            following = listed
            listed = await fill_subagents(list_children())
            if not listed or set(listed) - set(following):
                continue    # none left, or a new child: take its event first
            waits = [asyncio.ensure_future(ev.wait()) for ev in changes]
            waits.append(asyncio.ensure_future(closed.wait()))
            try:
                done, _pending = await asyncio.wait(
                    waits, timeout=max(deadline - time.time(), 0), return_when=asyncio.FIRST_COMPLETED)
            finally:
                for w in waits:
                    w.cancel()
            if not done:
                break
        return {"status": "ok"}

    async def _run_subagent_rows(self, body: dict, __user__, __event_emitter__):
        """The reader opened a folded Subagents section (ELI-163): read the
        listed children's transcripts, which the open left for this tap, and
        fill the section again. The rows are the open's, kept with its
        dialog, so the list is not read twice."""
        user_id = (__user__ or {}).get("id") or "unknown"
        dialog = _dialog_of(body, user_id)

        async def emit(code):
            await __event_emitter__({"type": "execute", "data": {"code": code}})

        if dialog is None:
            await emit(_render_subagents_fill_js({"error": "This dialog is out of date: open it again."}))
            return {"status": "error", "detail": "unknown dialog"}
        rows = dialog["rows"]
        try:
            conn, source = await _get_action_connection(lambda: self.valves)
            previews = await conn.send_request(
                "sessions.preview", dict(keys=[r["key"] for r in rows], limit=20, maxChars=2000), timeout=5)
        except Exception as ex:
            pipe_log(f"[status-action] sessions.preview (fold) failed: {ex}")
            previews, source = None, "action-own"
        texts, newest = _subagent_previews(previews)
        spawned = _subagent_tasks(dialog["session"])
        await emit(_render_subagents_fill_js({
            "tasks": [{**_subagent_row(r, texts.get(r["key"]) or spawned.get(r["key"]), newest.get(r["key"])),
                       "controlUiUrl": _control_ui_session_url(self.valves.CONTROL_UI_URL, r["key"])}
                      for r in rows],
            "total": dialog["total"], "pending": False,
            "source": source, "fetchedAt": int(time.time() * 1000), "error": None,
        }))
        return {"status": "ok"}

    async def _run_compact(self, body: dict, __user__, __event_emitter__):
        """Triggers /compact on the same session and waits for it to
        finish, then refreshes the dialog with post-compaction numbers.

        Uses chat.send(message="/compact") rather than the gateway's
        sessions.compact RPC: traced sessions.compact's handler and it
        explicitly rejects webchat-classified connections
        (rejectWebchatSessionMutation / isWebchatClient in the installed
        gateway bundle) -- and the Pipe's own connect params identify as
        client.id="webchat", so any connection this Action reuses from the
        Pipe is classified the same way. The RPC's own error message
        confirms the intended alternative: "use chat.send for
        session-scoped updates". This does mean the literal "/compact" text
        becomes a real turn in the session's stored transcript (visible to
        other surfaces that inspect that history), even though it never
        appears in OWUI's own chat bubble UI (a separate HTTP path this
        call never touches) -- a known, accepted trade-off, not an oversight.

        Polls sessions.describe for completion rather than registering a
        run consumer and reading the Pipe's event queue -- see the
        Milestone 4 plan notes for why: this only needs a boolean "still
        running" signal, not live text streaming, so the Pipe's much larger
        consumption loop (idle probes, steering, preview recovery) would be
        a lot of machinery for a narrow need.
        """
        chat_id = body.get("chat_id")
        user_id = (__user__ or {}).get("id") or "unknown"

        if not chat_id:
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js("Missing chat_id")},
            })
            return {"status": "error", "detail": "missing chat_id"}

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(f"Could not connect: {ex}")},
            })
            return {"status": "error", "detail": str(ex)}

        if conn.active_run_id_for_session(session_key):
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(
                    "A response is currently in progress -- wait for it to "
                    "finish before compacting."
                )},
            })
            return {"status": "error", "detail": "session busy"}

        # Immediate, unambiguous visual feedback -- compaction can take a
        # while (it's an LLM summarization call), so the dialog needs a
        # definitive "in progress" state, not a skeleton that looks like
        # it's about to finish in a second.
        await __event_emitter__({"type": "execute", "data": {"code": _MODAL_COMPACTING_JS}})

        try:
            compact_params = _owui_chat_send_params(
                session_key=session_key,
                message="/compact",
                # ACTION_RUN_PREFIX: the turn path never gives this run a message.
                idempotency_key=f"{ACTION_RUN_PREFIX}compact-{chat_id}-{time.time()}",
                owui_chat_id=chat_id,
                owui_user_id=user_id,
            )
            # Explicit, not relying on the gateway's own default for an
            # omitted field: this compact run should never trigger outbound
            # delivery to other configured channels (Slack/Discord/etc.) --
            # same reasoning gateway.py's existing send_stop already applies
            # to its own chat.send("/stop") call.
            compact_params["deliver"] = False
            send_resp = await _run_request(conn, RUN_SEND, compact_params, timeout=30)
        except Exception as ex:
            pipe_log(f"[status-action] compact chat.send failed: {ex}")
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(f"Could not start compact: {ex}")},
            })
            return {"status": "error", "detail": str(ex)}

        pipe_log(f"[status-action] compact started, runId={send_resp.get('runId')}")

        deadline = time.time() + 180  # matches `openclaw sessions compact`'s own default RPC timeout
        status = None
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            try:
                desc = await conn.send_request("sessions.describe", dict(key=session_key), timeout=8)
            except Exception as ex:
                pipe_log(f"[status-action] compact poll failed: {ex}")
                continue
            status = ((desc or {}).get("session") or {}).get("status")
            if status in ("done", "failed", "cancelled"):
                break

        if status not in ("done", "failed", "cancelled"):
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(
                    "Compact timed out -- it may still finish in the background."
                )},
            })
            return {"status": "error", "detail": "timeout"}

        if status != "done":
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(f"Compact {status}.")},
            })
            return {"status": "error", "detail": status}

        pipe_log("[status-action] compact done, refreshing status")
        return await self._run_status(body, __user__, __event_emitter__, show_loading=False)

    async def _run_subagent_detail(self, body: dict, __user__, __event_emitter__):
        """Fills the sub-agent drawer once and returns. The open drawer asks
        again every _SUBAGENT_POLL_INTERVAL_S while its sub-agent runs (its
        own poll, `S.openDrawer`), so the refresh lives exactly as long as
        the drawer is open, with no deadline and nothing left polling for a
        closed one (ELI-155). A row click opens the drawer in the page and
        sends `opened`; without it (an old sub-agent message's toolbar
        click) the drawer is opened from here first."""
        task_id = body.get("taskId")
        if not task_id:
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js("Missing taskId")
            }})
            return {"status": "error", "detail": "missing taskId"}

        if not body.get("opened"):
            await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_open_js({
                "id": task_id, "childSessionKey": task_id, "title": body.get("taskTitle"),
                "status": body.get("taskStatus"),
                "controlUiUrl": _control_ui_session_url(self.valves.CONTROL_UI_URL, task_id),
            })}})

        if not task_id.startswith("agent:"):
            # A Tasks API id from a message written before ELI-100; only the
            # removed tasks.get could resolve it to a child session.
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js("This sub-agent's details are no longer available.")
            }})
            return {"status": "error", "detail": "legacy task id"}

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            pipe_log(f"[status-action] subagent-detail connect failed: {ex}")
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js(f"Could not connect: {ex}", task_id)
            }})
            return {"status": "error", "detail": str(ex)}

        user_id = (__user__ or {}).get("id") if isinstance(__user__, dict) else None
        return await self._fill_subagent_detail(
            conn, source, task_id, __event_emitter__,
            # The header's "from" link is read once, with the drawer's first fill.
            parent_for=user_id if (body.get("first") or not body.get("opened")) else False,
            earlier=bool(body.get("earlier")),
        )

    async def _fill_subagent_detail(self, conn, source, child_key, __event_emitter__, *,
                                    parent_for=False, earlier=False):
        """One read of the child session and its whole run, pushed to the
        drawer: its header, and the run as a read-only chat. `parent_for`:
        the user to read the parent chat's title as, or False to skip it.
        `earlier`: the reader asked for the steps a normal read leaves out."""
        budget = _DRAWER_RUN_BYTES_EARLIER if earlier else _DRAWER_RUN_BYTES
        try:
            desc_resp, (messages, whole) = await asyncio.gather(
                conn.send_request("sessions.describe", dict(key=child_key), timeout=8),
                _read_child_history(conn, child_key, budget, all_pages=earlier),
            )
        except Exception as ex:
            pipe_log(f"[status-action] subagent detail fetch failed: {ex}")
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js(f"Could not fetch sub-agent: {ex}", child_key)
            }})
            return {"status": "error", "detail": str(ex)}

        session_row = (desc_resp or {}).get("session") or {}
        if not session_row:
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js("Sub-agent not found.", child_key)
            }})
            return {"status": "error", "detail": "sub-agent not found"}

        parent = session_row.get("spawnedBy") or session_row.get("parentSessionKey")
        task = _subagent_row({"key": child_key, **session_row},
                             _spawn_task_of(messages) or _subagent_tasks(parent).get(child_key))
        items, dropped = _cap_run_items(_drawer_run_items(messages), budget)
        claude_cli = (session_row.get("agentRuntime") or {}).get("id") == "claude-cli"
        note = latest = None
        if claude_cli and task["status"] in _LIVE_SUBAGENT_STATUSES:
            # A claude-cli run's messages reach its transcript when it ends
            # (measured, Gateway 2026.9.3); until then its newest line is
            # what the session list previews.
            note, latest = "cli-running", await _latest_preview(conn, child_key)
        elif claude_cli and task["status"] == "cancelled":
            note = "cli-stopped"   # stopped before it ended: nothing of it was saved
        parent = await _drawer_parent(conn, session_row, parent_for) if parent_for is not False else None

        await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_fill_js({
            "childSessionKey": child_key,
            "task": {**{k: task[k] for k in ("title", "status", "startedAt", "endedAt", "task", "error")},
                     "model": session_row.get("model")},
            "parent": parent,
            "items": items,
            "earlier": {"count": dropped, "more": not whole} if dropped or not whole else None,
            "note": note, "latest": latest,
            "source": source, "fetchedAt": int(time.time() * 1000), "error": None,
        })}})
        return {"status": "ok"}
