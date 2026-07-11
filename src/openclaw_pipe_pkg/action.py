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
# Each of Context, Rate Limits, and Subagents loads and fills independently
# (Eliav's explicit ask): three separate skeleton placeholders open
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

  window.__openclawStatus = {
    identity: IDENTITY, isDark: isDark, barColor: barColor, makeBar: makeBar,
    sectionHeader: sectionHeader, errorEl: errorEl,
    skeletonSection: skeletonSection, touchFooter: touchFooter,
    triggerCompact: triggerCompact,
  };

  const root = document.createElement('div');
  root.id = 'openclaw-status-modal-root';
  root.className = 'fixed inset-0 z-[9999] flex items-center justify-center';
  root.style.background = 'rgba(0,0,0,0.4)';

  const panel = document.createElement('div');
  panel.className = 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 '
    + 'rounded-2xl shadow-2xl border border-gray-100 dark:border-gray-800 '
    + 'p-5 w-[380px] max-w-[90vw]';
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
# feature rather than a temporary data gap (what Eliav asked about,
# 2026-07-11). Showing an explicit note instead turns that into an
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
    pctEl.textContent = Math.round(100 - w.usedPercent) + '% left';
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

# Deliberately hidden entirely (not even a header) when count is 0 --
# "general tracking, no detail" (Eliav's ask): a permanently-visible
# "0 running" line for the common idle case would be more clutter than
# signal. Only appears when there's actually something to report.
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

  if (!DATA.count) {
    section.className = '';
    return;
  }

  section.className = 'mb-4';
  section.appendChild(S.sectionHeader('Subagents'));
  const line = document.createElement('div');
  line.className = 'text-sm text-gray-700 dark:text-gray-200';
  line.textContent = '🤖 ' + DATA.count + ' running';
  section.appendChild(line);

  S.touchFooter(DATA.fetchedAt, DATA.source);
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


class Action:
    class Valves(BaseModel):
        GATEWAY_URL: str = Field(
            default="localhost:18789",
            description="OpenClaw Gateway address (host:port). Only used if "
                        "this Action ever has to open its own connection "
                        "instead of reusing the Pipe's (see module notes)."
        )
        GATEWAY_TOKEN: str = Field(
            default="",
            description="OpenClaw Gateway API token. Same fallback-only caveat as GATEWAY_URL."
        )
        DEVICE_IDENTITY: str = Field(
            default="",
            description="(Advanced) Fallback device identity JSON. Set this "
                        "to the exact same value as the Pipe's DEVICE_IDENTITY "
                        "valve so a fallback connection is recognized as the "
                        "same already-approved device, not a new pairing request."
        )
        STATE_DIR: str = Field(
            default="/data/openclaw-bridge",
            description="Must match the Pipe's STATE_DIR valve -- this is how "
                        "a fallback connection finds the Pipe's persisted "
                        "device token on disk without re-pairing."
        )
        AGENT_ID: str = Field(
            default="main",
            description="Target agent identifier -- must match the Pipe's AGENT_ID valve."
        )

    def __init__(self):
        self.valves = self.Valves()

    async def action(self, body: dict, __user__=None, __event_emitter__=None):
        """Dispatches on body["mode"]: the default ("status", or absent --
        this is what OWUI sends for the actual message-toolbar click) opens
        the dialog and shows live usage; "compact" is never sent by OWUI
        itself -- it's a synthetic marker the dialog's own in-page Compact
        button fetches back with (see triggerCompact() in
        _MODAL_OPEN_JS_TEMPLATE), reusing this same endpoint rather than
        registering a second Action."""
        if body.get("mode") == "compact":
            return await self._run_compact(body, __user__, __event_emitter__)
        return await self._run_status(body, __user__, __event_emitter__)

    async def _run_status(self, body: dict, __user__, __event_emitter__, *, show_loading=True):
        """Fetches and renders the dialog's three sections -- Context, Rate
        Limits, Subagents -- each independently: its own skeleton at open,
        its own fill event as soon as its own data is ready, not one
        combined fetch-everything-then-render-once step (Eliav's explicit
        ask, 2026-07-11). Subagents (tasks.list) has no dependency on the
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
            most genuinely independent of the three sections."""
            try:
                tasks_resp = await tasks_task
            except Exception as ex:
                pipe_log(f"[status-action] tasks.list failed: {ex}")
                await __event_emitter__({"type": "execute", "data": {
                    "code": _render_subagents_fill_js({"error": f"Could not fetch subagents: {ex}"})
                }})
                return
            count = sum(
                1 for t in (tasks_resp or {}).get("tasks", []) if t.get("kind") == "subagent"
            )
            await __event_emitter__({"type": "execute", "data": {"code": _render_subagents_fill_js({
                "count": count, "source": source, "fetchedAt": time.strftime("%H:%M:%S"), "error": None,
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
