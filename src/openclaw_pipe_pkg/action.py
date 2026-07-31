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
# which includes `_event_loop()`'s proactive-message-delivery behavior: if a
# "final" event arrives on a connection with no registered consumer for its
# session, gateway.py writes the assistant's reply straight into OWUI chat
# history (see `_deliver_proactive_owui_message`). This Action never calls
# `register_consumer()` -- it only ever does request/response RPCs -- so a
# fallback connection would always look "idle" to that logic. In the
# reuse-the-Pipe's-connection path this is a non-issue (we're just borrowing
# a reference to make calls into an event loop the Pipe already owns and
# already registers real consumers on); it only matters for our OWN fallback
# connection's OWN event loop. Each build produces a fully separate Python
# module/namespace, so overriding this module-level flag here affects only
# this artifact's fallback connection, never the Pipe's.
PROACTIVE_DELIVERY_ENABLED = False

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
# everything else. Subagents is fully independent (tasks.list only, no
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
    h.className = 'text-[10px] font-semibold tracking-wide uppercase '
      + 'text-gray-400 dark:text-gray-500 mb-1.5';
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

  function skeletonSection(id) {
    const el = document.createElement('div');
    el.id = id;
    el.className = 'mb-4';
    el.innerHTML = '<div class="animate-pulse space-y-2">'
      + '<div class="h-3 w-2/3 bg-gray-200 dark:bg-gray-700 rounded"></div>'
      + '<div class="h-3 w-full bg-gray-200 dark:bg-gray-700 rounded"></div>'
      + '</div>';
    return el;
  }

  function touchFooter(fetchedAt, source) {
    const f = document.getElementById('openclaw-status-footer');
    if (f) f.textContent = 'Updated ' + fetchedAt + ' · via ' + source;
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

  // Same same-origin/synthetic-mode trick as triggerCompact() above, just a
  // different mode + extra taskId/title/status payload so the drawer
  // (_DRAWER_OPEN_JS_TEMPLATE) can paint its header instantly from data the
  // row already had, before Action._run_subagent_detail's own RPCs resolve.
  function openSubagentDrawer(task) {
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
        mode: 'subagent-detail',
        taskId: task.id,
        taskTitle: task.title,
        taskStatus: task.status,
      }),
    }).catch(function(err) {
      console.error('[openclaw-status] could not open subagent drawer', err);
    });
  }

  window.__openclawStatus = {
    identity: IDENTITY, isDark: isDark, barColor: barColor, makeBar: makeBar,
    sectionHeader: sectionHeader, errorEl: errorEl,
    skeletonSection: skeletonSection, touchFooter: touchFooter,
    triggerCompact: triggerCompact, elapsedTime: elapsedTime,
    openSubagentDrawer: openSubagentDrawer,
  };

  const root = document.createElement('div');
  root.id = 'openclaw-status-modal-root';
  root.className = 'fixed inset-0 z-[9999] flex items-center justify-center';
  root.style.background = 'rgba(0,0,0,0.4)';

  const panel = document.createElement('div');
  panel.className = 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 '
    + 'rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-800 p-5';
  // Inline, not a `w-[380px] max-w-[90vw]` utility class: same reasoning as
  // barColor() below -- an arbitrary-value class OWUI's own Tailwind build
  // never scanned out of its own source can silently carry zero CSS.
  // min() keeps this mobile-first: a narrow viewport wins the 88vw side (a
  // visible margin, not edge-to-edge), a wide one wins the 380px cap.
  panel.style.width = 'min(380px, 88vw)';
  panel.addEventListener('click', function(e) { e.stopPropagation(); });

  const header = document.createElement('div');
  header.className = 'flex items-center justify-between mb-3';
  const title = document.createElement('div');
  title.className = 'text-sm font-semibold';
  title.textContent = 'OpenClaw Status';
  header.appendChild(title);
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.textContent = '×';
  closeBtn.className = 'text-xl leading-none text-gray-400 hover:text-gray-700 '
    + 'dark:hover:text-gray-200 px-1';
  closeBtn.onclick = function() { root.remove(); };
  header.appendChild(closeBtn);
  panel.appendChild(header);

  const sections = document.createElement('div');
  sections.id = 'openclaw-status-sections';
  sections.appendChild(skeletonSection('openclaw-status-section-context'));
  sections.appendChild(skeletonSection('openclaw-status-section-limits'));
  sections.appendChild(skeletonSection('openclaw-status-section-subagents'));
  panel.appendChild(sections);

  const footer = document.createElement('div');
  footer.id = 'openclaw-status-footer';
  footer.className = 'text-[11px] text-gray-400 dark:text-gray-600 pt-3 '
    + 'border-t border-gray-100 dark:border-gray-800';
  footer.textContent = ' ';
  panel.appendChild(footer);

  root.appendChild(panel);
  root.addEventListener('click', function() { root.remove(); });
  const onKey = function(e) {
    if (e.key === 'Escape') {
      root.remove();
      document.removeEventListener('keydown', onKey);
    }
  };
  document.addEventListener('keydown', onKey);

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
  wrap.innerHTML = '';
  wrap.appendChild(S.skeletonSection('openclaw-status-section-context'));
  wrap.appendChild(S.skeletonSection('openclaw-status-section-limits'));
  wrap.appendChild(S.skeletonSection('openclaw-status-section-subagents'));
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
  section.innerHTML = '';
  section.className = 'mb-4';

  if (DATA.error) {
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

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
  headerLabel.className = 'text-[10px] font-semibold tracking-wide uppercase '
    + 'text-gray-400 dark:text-gray-500';
  headerLabel.textContent = 'Context';
  headerRow.appendChild(headerLabel);
  if (S.identity.chatId && S.identity.messageId && S.identity.sessionId) {
    const compactBtn = document.createElement('button');
    compactBtn.type = 'button';
    compactBtn.textContent = 'Compact';
    compactBtn.className = 'text-[10px] font-medium px-2 py-0.5 rounded-full '
      + 'border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 '
      + 'hover:text-gray-800 dark:hover:text-gray-100 hover:border-gray-300 dark:hover:border-gray-600';
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

  if (DATA.error) {
    section.className = 'mb-4';
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

  if (!DATA.provider || DATA.provider === '?') {
    section.className = '';
    return;
  }

  section.className = 'mb-4';
  section.appendChild(S.sectionHeader('Rate Limits'));
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

# Deliberately hidden entirely (not even a header) when there are no tasks --
# "general tracking, no detail": a permanently-visible
# empty line for the common idle case would be more clutter than signal. Only
# appears when there's actually something to report. Each row is clickable --
# opens the per-subagent drawer (see _DRAWER_OPEN_JS_TEMPLATE) via
# S.openSubagentDrawer, passing the already-known TaskSummary fields along so
# the drawer can paint its header instantly, before any RPC resolves.
_SUBAGENTS_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-status-section-subagents');
  const S = window.__openclawStatus;
  if (!section || !S) return;
  section.innerHTML = '';

  if (DATA.error) {
    section.className = 'mb-4';
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

  if (!DATA.tasks || !DATA.tasks.length) {
    section.className = '';
    return;
  }

  section.className = 'mb-4';
  section.appendChild(S.sectionHeader('Subagents (' + DATA.tasks.length + ')'));

  const STATUS_COLOR = {
    queued: '#9ca3af', running: '#3b82f6', completed: '#10b981',
    failed: '#f43f5e', cancelled: '#f59e0b', timed_out: '#f59e0b',
  };

  DATA.tasks.forEach(function(t) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'w-full flex items-start gap-2 text-left py-1.5 px-1.5 -mx-1.5 '
      + 'rounded-lg hover:bg-gray-50 dark:hover:bg-gray-800/60';
    row.onclick = function() { S.openSubagentDrawer(t); };

    const dot = document.createElement('span');
    dot.className = 'mt-1.5 h-1.5 w-1.5 rounded-full shrink-0'
      + (t.status === 'running' ? ' animate-pulse' : '');
    dot.style.backgroundColor = STATUS_COLOR[t.status] || '#9ca3af';
    row.appendChild(dot);

    const body = document.createElement('div');
    body.className = 'min-w-0 flex-1';
    const titleLine = document.createElement('div');
    titleLine.className = 'text-sm text-gray-700 dark:text-gray-200 break-words';
    titleLine.textContent = t.title || t.id;
    body.appendChild(titleLine);

    const sub = t.status === 'running' ? t.progressSummary : (t.terminalSummary || t.error);
    if (sub) {
      const subLine = document.createElement('div');
      subLine.className = 'text-xs text-gray-400 dark:text-gray-500 break-words';
      subLine.textContent = sub;
      body.appendChild(subLine);
    }
    row.appendChild(body);

    const elapsed = document.createElement('span');
    elapsed.className = 'text-[10px] text-gray-400 dark:text-gray-500 shrink-0 mt-1.5';
    elapsed.textContent = S.elapsedTime(t.startedAt, t.endedAt);
    row.appendChild(elapsed);

    section.appendChild(row);
  });

  S.touchFooter(DATA.fetchedAt, DATA.source);
})();
"""


# Right-side slide-in panel, sibling to _MODAL_OPEN_JS_TEMPLATE -- deliberately
# does NOT close/replace the parent status modal (different root id,
# positioned to the side rather than centered) so the full subagent list
# stays visible and glanceable behind it. Opened by S.openSubagentDrawer()
# (defined in _MODAL_OPEN_JS_TEMPLATE) via a synthetic mode="subagent-detail"
# call -- same self-call convention triggerCompact() already uses. All three
# tab panes are created up front with skeletons; Action._run_subagent_detail
# fills all three on every poll tick regardless of which tab is visible
# (cheap: it's just a DOM update), so switching tabs is a pure visibility
# toggle with no extra fetch involved.
_DRAWER_OPEN_JS_TEMPLATE = r"""
(function() {
  const TASK = __OPENCLAW_TASK__;
  const existing = document.getElementById('openclaw-drawer-root');
  if (existing) existing.remove();

  const S = window.__openclawStatus;
  if (!S) return;

  const root = document.createElement('div');
  root.id = 'openclaw-drawer-root';
  root.className = 'fixed inset-0 z-[10000]';

  const backdrop = document.createElement('div');
  backdrop.className = 'absolute inset-0';
  backdrop.style.background = 'rgba(0,0,0,0.25)';
  backdrop.onclick = function() { root.remove(); };
  root.appendChild(backdrop);

  const panel = document.createElement('div');
  panel.className = 'absolute right-0 top-0 bottom-0 '
    + 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 '
    + 'shadow-2xl border-l border-gray-100 dark:border-gray-800 flex flex-col';
  // Inline, not a `w-[420px] max-w-[92vw]` utility class -- same reasoning
  // as the status modal's panel above. min() keeps this mobile-first: a
  // narrow viewport wins the 88vw side (leaves a visible margin so the
  // subagent list behind it stays glanceable, not edge-to-edge), a wide one
  // wins the 420px cap.
  panel.style.width = 'min(420px, 88vw)';
  panel.addEventListener('click', function(e) { e.stopPropagation(); });
  root.appendChild(panel);

  const header = document.createElement('div');
  header.className = 'flex items-start justify-between gap-2 p-4 border-b '
    + 'border-gray-100 dark:border-gray-800';
  const headerText = document.createElement('div');
  headerText.className = 'min-w-0';
  const titleEl = document.createElement('div');
  titleEl.className = 'text-sm font-semibold break-words';
  titleEl.textContent = TASK.title || TASK.id;
  headerText.appendChild(titleEl);
  const statusEl = document.createElement('div');
  statusEl.id = 'openclaw-drawer-status';
  statusEl.className = 'text-xs text-gray-400 dark:text-gray-500 mt-0.5';
  statusEl.textContent = TASK.status || '';
  headerText.appendChild(statusEl);
  header.appendChild(headerText);
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.textContent = '×';
  closeBtn.className = 'text-xl leading-none text-gray-400 hover:text-gray-700 '
    + 'dark:hover:text-gray-200 px-1 shrink-0';
  closeBtn.onclick = function() { root.remove(); };
  header.appendChild(closeBtn);
  panel.appendChild(header);

  const TABS = ['Overview', 'Transcript', 'Tools'];
  const tabBar = document.createElement('div');
  tabBar.className = 'flex gap-1 px-3 pt-2 border-b border-gray-100 dark:border-gray-800';
  const panes = {};
  function tabClass(active) {
    return 'text-xs font-medium px-2.5 py-1.5 rounded-t-lg border-b-2 '
      + (active ? 'border-blue-500 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-400 dark:text-gray-500 '
                  + 'hover:text-gray-600 dark:hover:text-gray-300');
  }
  TABS.forEach(function(name, i) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = name;
    btn.dataset.tab = name;
    btn.className = tabClass(i === 0);
    btn.onclick = function() {
      TABS.forEach(function(n) {
        const b = tabBar.querySelector('[data-tab="' + n + '"]');
        const active = n === name;
        b.className = tabClass(active);
        panes[n].style.display = active ? 'block' : 'none';
      });
    };
    tabBar.appendChild(btn);
  });
  panel.appendChild(tabBar);

  const content = document.createElement('div');
  content.className = 'flex-1 overflow-y-auto p-4';
  TABS.forEach(function(name, i) {
    const pane = document.createElement('div');
    pane.id = 'openclaw-drawer-pane-' + name.toLowerCase();
    pane.style.display = i === 0 ? 'block' : 'none';
    pane.appendChild(S.skeletonSection('openclaw-drawer-section-' + name.toLowerCase()));
    panes[name] = pane;
    content.appendChild(pane);
  });
  panel.appendChild(content);

  const footer = document.createElement('div');
  footer.id = 'openclaw-drawer-footer';
  footer.className = 'text-[11px] text-gray-400 dark:text-gray-600 px-4 py-2 '
    + 'border-t border-gray-100 dark:border-gray-800';
  footer.textContent = ' ';
  panel.appendChild(footer);

  const onKey = function(e) {
    if (e.key === 'Escape') {
      root.remove();
      document.removeEventListener('keydown', onKey);
    }
  };
  document.addEventListener('keydown', onKey);

  document.body.appendChild(root);
})();
"""

# Whole-drawer error (task not found, connect failure) -- replaces the
# content of all three panes at once, same reasoning as
# _SECTIONS_ERROR_JS_TEMPLATE for the outer modal.
_DRAWER_ERROR_JS_TEMPLATE = r"""
(function() {
  const MESSAGE = __OPENCLAW_ERROR_MESSAGE__;
  const S = window.__openclawStatus;
  ['overview', 'transcript', 'tools'].forEach(function(name) {
    const pane = document.getElementById('openclaw-drawer-pane-' + name);
    if (!pane) return;
    pane.innerHTML = '';
    pane.appendChild(S ? S.errorEl(MESSAGE) : document.createTextNode(MESSAGE));
  });
})();
"""

_DRAWER_OVERVIEW_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-drawer-section-overview');
  const S = window.__openclawStatus;
  if (!section || !S) return;

  const statusEl = document.getElementById('openclaw-drawer-status');
  if (statusEl && DATA.task) {
    statusEl.textContent = DATA.task.status
      + (DATA.task.terminalSummary ? ' · ' + DATA.task.terminalSummary : '')
      + (DATA.task.error ? ' · ' + DATA.task.error : '');
  }

  section.innerHTML = '';
  if (DATA.error) {
    section.appendChild(S.errorEl(DATA.error));
    return;
  }
  if (!DATA.task) {
    const empty = document.createElement('div');
    empty.className = 'text-gray-400 dark:text-gray-500 text-xs';
    empty.textContent = 'No task data.';
    section.appendChild(empty);
    return;
  }

  const meta = document.createElement('div');
  meta.className = 'text-xs text-gray-500 dark:text-gray-400 mb-3';
  meta.textContent = 'Started ' + (S.elapsedTime(DATA.task.startedAt, null) || '?') + ' ago'
    + (DATA.task.endedAt ? ' · ran ' + S.elapsedTime(DATA.task.startedAt, DATA.task.endedAt) : '');
  section.appendChild(meta);

  if (!DATA.childSessionKey) {
    const notStarted = document.createElement('div');
    notStarted.className = 'text-gray-400 dark:text-gray-500 text-xs';
    notStarted.textContent = 'Not started yet.';
    section.appendChild(notStarted);
    return;
  }

  const sub = document.createElement('div');
  sub.className = 'text-xs text-gray-500 dark:text-gray-400 mb-3';
  sub.textContent = (DATA.provider || '?') + (DATA.model ? (' · ' + DATA.model) : '');
  section.appendChild(sub);

  if (DATA.context) {
    section.appendChild(S.sectionHeader('Context'));
    section.appendChild(S.makeBar(DATA.context.pct));
    const label = document.createElement('div');
    label.className = 'text-xs text-gray-500 dark:text-gray-400 mt-1.5 mb-3';
    label.textContent = DATA.context.usedTokens + ' / ' + DATA.context.totalTokens
      + ' tokens · ' + DATA.context.pct + '%';
    section.appendChild(label);
  }

  if (DATA.goal) {
    section.appendChild(S.sectionHeader('Goal'));
    const line = document.createElement('div');
    line.className = 'text-xs text-gray-700 dark:text-gray-200';
    line.textContent = DATA.goal.line;
    section.appendChild(line);
  }

  const df = document.getElementById('openclaw-drawer-footer');
  if (df) df.textContent = 'Updated ' + DATA.fetchedAt + ' · via ' + DATA.source;
})();
"""

# Message shape assumed here ({role, content: [{type: "text"|"toolcall"|
# "tool_result"|"thinking", ...}]}) confirmed live against a real session via
# sessions_history (chat.history is a display-normalized projection of the
# same underlying transcript, not a separate schema, so expected to match --
# see _extract_transcript_and_tools). Only renders rows that actually have
# text after stripping tool-call/tool-result/thinking parts, so the
# scrollback doesn't show empty gaps for pure tool-call turns (those live in
# the Tools tab instead).
_DRAWER_TRANSCRIPT_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-drawer-section-transcript');
  const pane = document.getElementById('openclaw-drawer-pane-transcript');
  const S = window.__openclawStatus;
  if (!section || !pane || !S) return;

  if (DATA.error) {
    section.innerHTML = '';
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

  if (!DATA.childSessionKey) {
    section.innerHTML = '';
    const notStarted = document.createElement('div');
    notStarted.className = 'text-gray-400 dark:text-gray-500 text-xs';
    notStarted.textContent = 'Not started yet.';
    section.appendChild(notStarted);
    return;
  }

  // Preserve "was already scrolled to bottom" before rebuilding, so a live
  // poll tick doesn't yank the view away from wherever the user scrolled to
  // read older messages.
  const wasAtBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 40;

  section.innerHTML = '';
  const ROLE_STYLE = {
    user: 'text-gray-900 dark:text-gray-100',
    assistant: 'text-gray-700 dark:text-gray-300',
  };

  const rows = DATA.messages || [];
  if (!rows.length) {
    const empty = document.createElement('div');
    empty.className = 'text-gray-400 dark:text-gray-500 text-xs';
    empty.textContent = 'No transcript yet.';
    section.appendChild(empty);
    return;
  }

  rows.forEach(function(m) {
    if (!m.text) return;
    const row = document.createElement('div');
    row.className = 'text-xs mb-2.5 ' + (ROLE_STYLE[m.role] || ROLE_STYLE.assistant);
    const roleLabel = document.createElement('div');
    roleLabel.className = 'text-[10px] uppercase tracking-wide text-gray-400 dark:text-gray-500 mb-0.5';
    roleLabel.textContent = m.role;
    row.appendChild(roleLabel);
    const text = document.createElement('div');
    text.className = 'whitespace-pre-wrap break-words';
    text.textContent = m.text;
    row.appendChild(text);
    section.appendChild(row);
  });

  if (wasAtBottom) pane.scrollTop = pane.scrollHeight;
})();
"""

# Uses real <details>/<summary> (natively collapsible with zero JS) rather
# than OWUI's own "<details type=\"tool_calls\">" markup convention: that
# convention only renders as a card because OWUI's own markdown-message
# component parses it out of a message's markdown source -- it does nothing
# special for DOM nodes built directly via execute(), which is how this
# whole drawer (like the rest of the status dialog) is delivered. A plain
# native <details> gets the same "collapsible card" behavior for free
# without depending on that unrelated rendering path.
_DRAWER_TOOLS_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const section = document.getElementById('openclaw-drawer-section-tools');
  const S = window.__openclawStatus;
  if (!section || !S) return;

  if (DATA.error) {
    section.innerHTML = '';
    section.appendChild(S.errorEl(DATA.error));
    return;
  }

  if (!DATA.childSessionKey) {
    section.innerHTML = '';
    const notStarted = document.createElement('div');
    notStarted.className = 'text-gray-400 dark:text-gray-500 text-xs';
    notStarted.textContent = 'Not started yet.';
    section.appendChild(notStarted);
    return;
  }

  section.innerHTML = '';
  const calls = DATA.toolCalls || [];
  if (!calls.length) {
    const empty = document.createElement('div');
    empty.className = 'text-gray-400 dark:text-gray-500 text-xs';
    empty.textContent = 'No tool calls yet.';
    section.appendChild(empty);
    return;
  }

  calls.forEach(function(c) {
    const details = document.createElement('details');
    details.className = 'mb-2 rounded-lg border border-gray-100 dark:border-gray-800 px-2.5 py-1.5';
    const summary = document.createElement('summary');
    summary.className = 'text-xs font-medium cursor-pointer text-gray-700 dark:text-gray-200';
    // Only ❌ vs 🔧 here: every card comes from chat.history, which only ever
    // holds finished calls, so there is no in-flight state to distinguish.
    summary.textContent = (c.isError ? '❌' : '🔧') + ' ' + c.name;
    details.appendChild(summary);

    const argsEl = document.createElement('pre');
    argsEl.className = 'text-[11px] mt-1.5 whitespace-pre-wrap break-words '
      + 'text-gray-500 dark:text-gray-400';
    argsEl.textContent = c.arguments;
    details.appendChild(argsEl);

    if (c.result) {
      const resultLabel = document.createElement('div');
      resultLabel.className = 'text-[10px] uppercase tracking-wide text-gray-400 '
        + 'dark:text-gray-500 mt-1.5';
      resultLabel.textContent = 'Result';
      details.appendChild(resultLabel);
      const resultEl = document.createElement('pre');
      resultEl.className = 'text-[11px] whitespace-pre-wrap break-words '
        + 'text-gray-500 dark:text-gray-400';
      resultEl.textContent = c.result;
      details.appendChild(resultEl);
    }

    section.appendChild(details);
  });
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
    return _MODAL_OPEN_JS_TEMPLATE.replace("__OPENCLAW_IDENTITY__", _json_for_js(identity))


def _render_sections_error_js(message: str) -> str:
    return _SECTIONS_ERROR_JS_TEMPLATE.replace("__OPENCLAW_ERROR_MESSAGE__", _json_for_js(message))


def _render_context_fill_js(data: dict) -> str:
    return _CONTEXT_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_limits_fill_js(data: dict) -> str:
    return _LIMITS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_subagents_fill_js(data: dict) -> str:
    return _SUBAGENTS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_drawer_open_js(task: dict) -> str:
    return _DRAWER_OPEN_JS_TEMPLATE.replace("__OPENCLAW_TASK__", _json_for_js(task))


def _render_drawer_error_js(message: str) -> str:
    return _DRAWER_ERROR_JS_TEMPLATE.replace("__OPENCLAW_ERROR_MESSAGE__", _json_for_js(message))


def _render_drawer_overview_fill_js(data: dict) -> str:
    return _DRAWER_OVERVIEW_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_drawer_transcript_fill_js(data: dict) -> str:
    return _DRAWER_TRANSCRIPT_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


def _render_drawer_tools_fill_js(data: dict) -> str:
    return _DRAWER_TOOLS_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", _json_for_js(data))


# Bounds for Action._run_subagent_detail's hold-open poll loop -- same
# "hold the request, sleep, poll, keep emitting" shape _run_compact already
# proves works (there, bounded to 180s). A subagent can legitimately run much
# longer than a compact call, so this gets its own, longer ceiling; it exists
# purely so a forgotten-open drawer (or a subagent stuck non-terminal) can't
# pin the coroutine/connection open forever, not because 15 minutes is a
# meaningful product limit.
_SUBAGENT_POLL_MAX_S = 900
_SUBAGENT_POLL_INTERVAL_S = 2.5


def _extract_transcript_and_tools(messages: list) -> tuple[list, list]:
    """Splits chat.history's message rows into plain-text transcript rows and
    tool-call cards for the drawer's Transcript/Tools tabs.

    Assumes the same {role, content: [...]} shape confirmed live via
    sessions_history (content items typed "text" / "toolcall" /
    "tool_result" / "thinking") -- chat.history is a display-normalized
    projection of the same underlying transcript store, not a separate
    schema, so this is expected to match; a plain string `content` (some
    transports may still use that) is treated as a single text row so this
    degrades instead of silently dropping messages if the shape differs.
    """
    text_rows: list = []
    tool_calls: list = []
    pending_by_id: dict = {}
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                text_rows.append({"role": role, "text": content})
            continue
        if not isinstance(content, list):
            continue
        text_parts = []
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text" and item.get("text"):
                text_parts.append(item["text"])
            elif item_type == "toolcall":
                call = {
                    "id": item.get("id"),
                    "name": item.get("name") or "tool",
                    "arguments": (
                        json.dumps(item.get("arguments"), indent=2)
                        if item.get("arguments") is not None else ""
                    ),
                    "result": None,
                    "isError": False,
                }
                pending_by_id[call["id"]] = call
                tool_calls.append(call)
            elif item_type == "tool_result":
                call = pending_by_id.get(item.get("tool_use_id"))
                if call is not None:
                    result = item.get("content")
                    call["result"] = result if isinstance(result, str) else json.dumps(result)
                    # Real tool_result rows carry is_error; surfacing it here
                    # lets the card mark a failure the same way the inline
                    # chat tool cards already do.
                    call["isError"] = bool(item.get("is_error"))
        if text_parts:
            text_rows.append({"role": role, "text": "\n".join(text_parts)})
    return text_rows, tool_calls


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
            description="Copy this exactly from the Pipe's valves — do not leave "
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
            description="Copy this exactly from the Pipe's valves — the same "
                        "OpenClaw agent the Pipe routes chats to."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def action(self, body: dict, __user__=None, __event_emitter__=None):
        """Dispatches on body["mode"]: the default ("status", or absent --
        this is what OWUI sends for the actual message-toolbar click) opens
        the dialog and shows live usage; "compact" and "subagent-detail" are
        never sent by OWUI itself -- both are synthetic markers the dialog's
        own in-page buttons fetch back with (see triggerCompact() and
        openSubagentDrawer() in _MODAL_OPEN_JS_TEMPLATE), reusing this same
        endpoint rather than registering a second Action.

        A real toolbar click on a proactively-delivered sub-agent-finished
        message is also mode-less (OWUI itself never sends a "mode"), but
        carries a hidden `<!-- openclaw:taskId=... -->` marker in
        `body["content"]` (see `_deliver_subagent_proactive_owui_message` in
        gateway.py) -- there's deliberately no separate Action/button for
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
        if mode is None:
            marker = _SUBAGENT_TASK_ID_MARKER_RE.search(body.get("content") or "")
            if marker:
                return await self._run_subagent_detail(
                    {**body, "taskId": marker.group(1)}, __user__, __event_emitter__,
                )
        return await self._run_status(body, __user__, __event_emitter__)

    async def _run_status(self, body: dict, __user__, __event_emitter__, *, show_loading=True):
        """Fetches and renders the dialog's three sections -- Context, Rate
        Limits, Subagents -- each independently: its own skeleton at open,
        its own fill event as soon as its own data is ready, not one
        combined fetch-everything-then-render-once step. Subagents (tasks.list) has no dependency on the
        other two at all. Rate Limits genuinely needs the active provider's
        name, which only comes from sessions.describe -- rather than fake
        independence there, its fill awaits Context's own resolution of
        that RPC (the same Task object, not a second request) before adding
        usage.status on top; it still has its own skeleton and fills in on
        its own schedule, separate from Context's.
        """
        chat_id = body.get("chat_id")
        user_id = (__user__ or {}).get("id") or "unknown"

        if not chat_id:
            await __event_emitter__({
                "type": "notification",
                "data": {"type": "error", "content": "OpenClaw status: no chat_id on this message"},
            })
            return {"status": "error", "detail": "missing chat_id"}

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
            }
            await __event_emitter__({"type": "execute", "data": {"code": _render_modal_open_js(identity)}})
        else:
            # Post-compact refresh: the dialog is already open, but
            # _MODAL_COMPACTING_JS replaced the sections wrapper with its
            # spinner, destroying the three section ids -- recreate fresh
            # skeletons in their place before firing the refreshed RPCs.
            await __event_emitter__({"type": "execute", "data": {"code": _RESET_SECTIONS_JS}})

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            pipe_log(f"[status-action] connect failed: {ex}")
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_sections_error_js(f"Could not connect: {ex}")},
            })
            return {"status": "error", "detail": str(ex)}

        # Fired concurrently; each section's own coroutine below awaits
        # only what it actually needs and emits its own fill the moment
        # that's ready.
        desc_task = asyncio.ensure_future(
            conn.send_request("sessions.describe", dict(key=session_key), timeout=5)
        )
        usage_task = asyncio.ensure_future(conn.send_request("usage.status", {}, timeout=5))
        tasks_task = asyncio.ensure_future(conn.send_request(
            "tasks.list", dict(sessionKey=session_key, status=["running", "queued"], limit=50), timeout=5,
        ))

        async def fill_context():
            """Also resolves provider/session_row for fill_limits to reuse
            (returned, not re-fetched) -- still reuses emit.py's small
            formatting primitives (_fmt_tokens, _format_goal_line) so
            wording matches the status line exactly."""
            try:
                desc = await desc_task
            except Exception as ex:
                pipe_log(f"[status-action] sessions.describe failed: {ex}")
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_context_fill_js({"error": f"Could not fetch context: {ex}"})
                }})
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

            await __event_emitter__({"type": "execute", "data": {"code": _render_context_fill_js({
                "provider": provider,
                "model": session_row.get("model") or "",
                "context": context_data,
                "goal": goal_data,
                "source": source,
                "fetchedAt": time.strftime("%H:%M:%S"),
                "error": None,
            })}})
            return provider, session_row

        async def fill_limits():
            provider, session_row = await context_ready
            if provider is None:
                return  # fill_context already reported the fetch error
            try:
                usage = await usage_task
            except Exception as ex:
                pipe_log(f"[status-action] usage.status failed: {ex}")
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_limits_fill_js({"error": f"Could not fetch rate limits: {ex}"})
                }})
                return

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

            await __event_emitter__({"type": "execute", "data": {"code": _render_limits_fill_js({
                "provider": provider,
                "windows": windows_data,
                # Only used to word the empty-windows note -- a live run is
                # the one confirmed case (2026-07-11 live debugging) where
                # usage.status can transiently report zero windows for the
                # active provider, most likely because its rate-limit cache
                # refreshes from that run's own API response headers.
                "sessionActive": session_row.get("status") == "running",
                "source": source,
                "fetchedAt": time.strftime("%H:%M:%S"),
                "error": None,
            })}})

        async def fill_subagents():
            """No dependency on sessions.describe/usage.status at all --
            session_key is already known synchronously, so this is the
            most genuinely independent of the three sections.

            Passes the raw TaskSummary fields through (not just a count) so
            the fill template can render one clickable row per subagent --
            each row's own click handler carries these same fields into the
            detail drawer's opening request (see S.openSubagentDrawer /
            Action._run_subagent_detail), so the drawer can paint its header
            instantly before its own RPCs resolve."""
            try:
                tasks_resp = await tasks_task
            except Exception as ex:
                pipe_log(f"[status-action] tasks.list failed: {ex}")
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_subagents_fill_js({"error": f"Could not fetch subagents: {ex}"})
                }})
                return
            subagent_tasks = [
                {
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "status": t.get("status"),
                    "progressSummary": t.get("progressSummary"),
                    "terminalSummary": t.get("terminalSummary"),
                    "error": t.get("error"),
                    "childSessionKey": t.get("childSessionKey"),
                    "startedAt": t.get("startedAt"),
                    "endedAt": t.get("endedAt"),
                }
                for t in (tasks_resp or {}).get("tasks", []) if t.get("kind") == "subagent"
            ]
            await __event_emitter__({"type": "execute", "data": {"code": _render_subagents_fill_js({
                "tasks": subagent_tasks, "source": source, "fetchedAt": time.strftime("%H:%M:%S"), "error": None,
            })}})

        context_ready = asyncio.ensure_future(fill_context())
        limits_ready = asyncio.ensure_future(fill_limits())
        subagents_ready = asyncio.ensure_future(fill_subagents())

        provider, _session_row = await context_ready
        await limits_ready
        await subagents_ready

        pipe_log(f"[status-action] status dialog filled ({source}) provider={provider}")
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
                    "A response is currently in progress — wait for it to "
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
                idempotency_key=f"compact-{chat_id}-{time.time()}",
                owui_chat_id=chat_id,
                owui_user_id=user_id,
            )
            # Explicit, not relying on the gateway's own default for an
            # omitted field: this compact run should never trigger outbound
            # delivery to other configured channels (Slack/Discord/etc.) --
            # same reasoning gateway.py's existing send_stop already applies
            # to its own chat.send("/stop") call.
            compact_params["deliver"] = False
            send_resp = await conn.send_request("chat.send", compact_params, timeout=30)
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
                    "Compact timed out — it may still finish in the background."
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
        """Opens the per-subagent drawer and holds this same request open for
        the life of the subagent's run, polling and pushing fresh execute
        fills roughly every _SUBAGENT_POLL_INTERVAL_S seconds -- the same
        "hold the request open, sleep-poll, keep emitting" shape
        _run_compact already uses and has proven works (there, bounded to
        180s). Bounded here to _SUBAGENT_POLL_MAX_S so a forgotten-open
        drawer, or a subagent that never reaches a terminal status, can't
        pin this coroutine/connection open forever.

        Deliberately has no "close" round-trip to cancel the loop early: if
        the user closes the drawer (or reopens it for a different task,
        which removes and recreates '#openclaw-drawer-root'), every fill
        template's own `if (!section) return;` guard makes further
        execute() pushes targeting the now-missing ids silent no-ops. Same
        trade-off _run_compact already accepts (no cancel path there either)
        -- wasted polling until the next terminal status or the deadline,
        never a visible glitch.
        """
        task_id = body.get("taskId")
        if not task_id:
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js("Missing taskId")
            }})
            return {"status": "error", "detail": "missing taskId"}

        await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_open_js({
            "id": task_id, "title": body.get("taskTitle"), "status": body.get("taskStatus"),
        })}})

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            pipe_log(f"[status-action] subagent-detail connect failed: {ex}")
            await __event_emitter__({"type": "execute", "data": {
                "code": _render_drawer_error_js(f"Could not connect: {ex}")
            }})
            return {"status": "error", "detail": str(ex)}

        return await self._subagent_detail_poll_loop(
            conn, source, task_id, __event_emitter__,
        )

    async def _subagent_detail_poll_loop(self, conn, source, task_id,
                                         __event_emitter__):
        """The drawer's poll body: refresh task/session/history every
        _SUBAGENT_POLL_INTERVAL_S until the subagent reaches a terminal status
        or _SUBAGENT_POLL_MAX_S elapses.
        """
        deadline = time.time() + _SUBAGENT_POLL_MAX_S
        while True:
            try:
                task_resp = await conn.send_request("tasks.get", dict(taskId=task_id), timeout=8)
                task = (task_resp or {}).get("task") or {}
            except Exception as ex:
                pipe_log(f"[status-action] subagent tasks.get failed: {ex}")
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_drawer_error_js(f"Could not fetch task: {ex}")
                }})
                return {"status": "error", "detail": str(ex)}

            if not task:
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_drawer_error_js("Task not found.")
                }})
                return {"status": "error", "detail": "task not found"}

            child_key = task.get("childSessionKey")
            provider = model = None
            context_data = goal_data = None
            text_rows: list = []
            tool_calls: list = []

            if child_key:
                try:
                    desc_resp, history_resp = await asyncio.gather(
                        conn.send_request("sessions.describe", dict(key=child_key), timeout=8),
                        conn.send_request(
                            "chat.history", dict(sessionKey=child_key, limit=50, maxChars=4000), timeout=8,
                        ),
                    )
                except Exception as ex:
                    pipe_log(f"[status-action] subagent detail fetch failed: {ex}")
                    desc_resp, history_resp = None, None

                session_row = (desc_resp or {}).get("session") or {}
                provider = session_row.get("modelProvider")
                model = session_row.get("model")
                context_tokens = session_row.get("contextTokens")
                total_tokens = session_row.get("totalTokens")
                if context_tokens and total_tokens is not None:
                    context_data = {
                        "usedTokens": _fmt_tokens(total_tokens),
                        "totalTokens": _fmt_tokens(context_tokens),
                        "pct": round(total_tokens / context_tokens * 100, 1),
                    }
                goal = session_row.get("goal")
                if goal:
                    goal_line = _format_goal_line(goal)
                    if goal_line:
                        goal_data = {"line": goal_line}

                # KNOWN GAP (ELI-26): chat.history is blind to the in-flight
                # turn, so while the subagent is running this yields nothing and
                # every tool call appears at once when the turn flushes. There
                # is no pipe-side fix -- a running session's tool events reach
                # only the connection that started the run. See the note in
                # gateway.py's reader loop for the measurement.
                text_rows, tool_calls = _extract_transcript_and_tools(
                    (history_resp or {}).get("messages") or []
                )

            fetched_at = time.strftime("%H:%M:%S")
            await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_overview_fill_js({
                "task": task, "childSessionKey": child_key, "provider": provider, "model": model,
                "context": context_data, "goal": goal_data, "source": source,
                "fetchedAt": fetched_at, "error": None,
            })}})
            await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_transcript_fill_js({
                "childSessionKey": child_key, "messages": text_rows, "error": None,
            })}})
            await __event_emitter__({"type": "execute", "data": {"code": _render_drawer_tools_fill_js({
                "childSessionKey": child_key, "toolCalls": tool_calls, "error": None,
            })}})

            if task.get("status") not in ("queued", "running"):
                break
            if time.time() >= deadline:
                break
            await asyncio.sleep(_SUBAGENT_POLL_INTERVAL_S)

        pipe_log(f"[status-action] subagent-detail loop ended taskId={task_id}")
        return {"status": "ok"}
