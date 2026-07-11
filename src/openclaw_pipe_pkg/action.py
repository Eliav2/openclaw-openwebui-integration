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
# Milestone 2: a real dialog instead of a toast, rendered via the "execute"
# event (runs unsandboxed in the actual OWUI page, not a sandboxed Rich UI
# iframe) so it can use OWUI's own live Tailwind classes and CSS custom
# properties directly -- exact design-system match for free, no hand-tuned
# color palette, no guessing which of OWUI's several themes is active. The
# trade-off (unsandboxed JS) is acceptable here because the code is 100%
# admin-authored, not influenced by any untrusted input; the only dynamic
# values are our own gateway's numbers, passed in as a single JSON blob
# (never string-interpolated) so nothing can break out of the data payload.
#
# Split into two separate `execute` emits rather than one, for latency: a
# click should get instant visual feedback (the overlay + a loading
# skeleton), not nothing happening for however long the gateway round trip
# takes. `_MODAL_OPEN_JS` (static, no data, fired immediately) builds the
# overlay/panel/header/close-button and a placeholder body with a loading
# skeleton; `_render_modal_fill_js(data)` (fired once the RPCs resolve)
# finds that same body by id and replaces its contents in place -- no
# flicker, no second overlay animation. If the user already dismissed the
# dialog before the data arrived, the body element is gone and the fill
# call is a safe no-op (checked explicitly below).
# ---------------------------------------------------------------------------

_MODAL_OPEN_JS = r"""
(function() {
  const existing = document.getElementById('openclaw-status-modal-root');
  if (existing) existing.remove();

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

  const body = document.createElement('div');
  body.id = 'openclaw-status-modal-body';
  body.innerHTML = '<div class="animate-pulse space-y-2">'
    + '<div class="h-3 w-2/3 bg-gray-200 dark:bg-gray-700 rounded"></div>'
    + '<div class="h-3 w-full bg-gray-200 dark:bg-gray-700 rounded"></div>'
    + '<div class="h-3 w-1/2 bg-gray-200 dark:bg-gray-700 rounded"></div>'
    + '</div>';
  panel.appendChild(body);

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

# Static (no data), fired the moment a compact is triggered -- see
# Action._run_compact. Deliberately a distinct, prominent "Compacting..."
# state rather than reusing the generic loading skeleton: compaction is an
# LLM call that can take a while, so the dialog should look like it's
# doing something specific and ongoing, not like a normal fetch that's
# about to finish in a second.
_MODAL_COMPACTING_JS = r"""
(function() {
  const body = document.getElementById('openclaw-status-modal-body');
  if (!body) return;
  body.innerHTML = '';

  const isDark = document.documentElement.classList.contains('dark');

  const wrap = document.createElement('div');
  wrap.className = 'flex flex-col items-center justify-center py-6 text-center';

  const spinner = document.createElement('div');
  spinner.className = 'animate-spin rounded-full h-6 w-6 border-2 mb-3';
  spinner.style.borderColor = isDark ? '#4b5563' : '#d1d5db';
  spinner.style.borderTopColor = 'transparent';
  wrap.appendChild(spinner);

  const title = document.createElement('div');
  title.className = 'text-sm font-medium text-gray-700 dark:text-gray-200';
  title.textContent = 'Compacting…';
  wrap.appendChild(title);

  const sub = document.createElement('div');
  sub.className = 'text-xs text-gray-400 dark:text-gray-500 mt-1';
  sub.textContent = 'This can take a moment for long conversations.';
  wrap.appendChild(sub);

  body.appendChild(wrap);
})();
"""

_MODAL_FILL_JS_TEMPLATE = r"""
(function() {
  const DATA = __OPENCLAW_STATUS_DATA__;
  const body = document.getElementById('openclaw-status-modal-body');
  if (!body) return;
  body.innerHTML = '';

  const isDark = document.documentElement.classList.contains('dark');

  if (DATA.error) {
    const err = document.createElement('div');
    err.className = 'text-sm';
    err.style.color = isDark ? '#fb7185' : '#e11d48';
    err.textContent = DATA.error;
    body.appendChild(err);
    return;
  }

  // Used-percent -> color: green while healthy, amber approaching the
  // limit, red once it's mostly consumed. Same thresholds for every bar
  // (context and each rate-limit window) so the color language is
  // consistent across sections.
  //
  // Inline hex, not Tailwind utility classes: Tailwind only ships the
  // utility classes it finds referenced somewhere in ITS OWN build's
  // source, so a class this injected code invents (rather than one OWUI's
  // own frontend already uses) can silently have zero CSS behind it --
  // exactly what happened here. 'bg-rose-500' is never referenced
  // anywhere in open-webui/open-webui's own source (verified against the
  // real repo), so that utility class was never generated in OWUI's
  // compiled stylesheet at all; the fill div picked up the class but no
  // styling, rendering as an invisible 0-color bar even at 86% width.
  // 'bg-emerald-500' happened to render fine only because OWUI's own UI
  // elsewhere happens to use emerald -- relying on that coincidence for
  // every color is fragile (a future OWUI redesign could drop it too).
  // Inline styles have no dependency on the host page's Tailwind content
  // scan at all, so this can't recur regardless of what OWUI's frontend
  // does or doesn't use.
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

  // Fetches back to this same Action (same-origin, unsandboxed execute
  // context -- no iframe/postMessage plumbing needed) with a synthetic
  // mode="compact" marker. Reads OWUI's own stored auth token directly
  // (confirmed against OWUI's frontend source: it keeps its bearer token
  // at localStorage['token']) rather than assuming cookie auth. The
  // chat/message/session identity is the *original* click's, threaded
  // through in DATA, so the Python side's __event_emitter__ calls for
  // this follow-up route back to this same open tab. The fetch response
  // itself is ignored -- all UI updates arrive as separate execute events
  // pushed from the Python side as the compact operation progresses,
  // exactly like the initial open/fill sequence.
  function triggerCompact() {
    const token = localStorage.getItem('token');
    fetch('/api/chat/actions/openclaw_status_action', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        model: DATA.owuiModel,
        chat_id: DATA.chatId,
        id: DATA.messageId,
        session_id: DATA.sessionId,
        mode: 'compact',
      }),
    }).catch(function(err) {
      const target = document.getElementById('openclaw-status-modal-body');
      if (!target) return;
      target.innerHTML = '';
      const errEl = document.createElement('div');
      errEl.className = 'text-sm';
      errEl.style.color = isDark ? '#fb7185' : '#e11d48';
      errEl.textContent = 'Could not start compact: ' + err;
      target.appendChild(errEl);
    });
  }

  const sub = document.createElement('div');
  sub.className = 'text-xs text-gray-500 dark:text-gray-400 mb-4';
  sub.textContent = DATA.provider + (DATA.model ? (' · ' + DATA.model) : '');
  body.appendChild(sub);

  let sectionCount = 0;

  if (DATA.context) {
    sectionCount++;
    const section = document.createElement('div');
    section.className = 'mb-4';

    const headerRow = document.createElement('div');
    headerRow.className = 'flex items-center justify-between mb-1.5';
    const headerLabel = document.createElement('div');
    headerLabel.className = 'text-[10px] font-semibold tracking-wide uppercase '
      + 'text-gray-400 dark:text-gray-500';
    headerLabel.textContent = 'Context';
    headerRow.appendChild(headerLabel);
    if (DATA.chatId && DATA.messageId && DATA.sessionId) {
      const compactBtn = document.createElement('button');
      compactBtn.type = 'button';
      compactBtn.textContent = 'Compact';
      compactBtn.className = 'text-[10px] font-medium px-2 py-0.5 rounded-full '
        + 'border border-gray-200 dark:border-gray-700 text-gray-500 dark:text-gray-400 '
        + 'hover:text-gray-800 dark:hover:text-gray-100 hover:border-gray-300 dark:hover:border-gray-600';
      compactBtn.onclick = triggerCompact;
      headerRow.appendChild(compactBtn);
    }
    section.appendChild(headerRow);

    section.appendChild(makeBar(DATA.context.pct));
    const label = document.createElement('div');
    label.className = 'text-xs text-gray-500 dark:text-gray-400 mt-1.5';
    label.textContent = DATA.context.usedTokens + ' / ' + DATA.context.totalTokens
      + ' tokens · ' + DATA.context.pct + '%';
    section.appendChild(label);
    body.appendChild(section);
  }

  if (DATA.windows.length > 0) {
    sectionCount++;
    const section = document.createElement('div');
    section.className = 'mb-4';
    section.appendChild(sectionHeader('Rate Limits'));
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
      row.appendChild(makeBar(w.usedPercent));
      if (w.resetIn) {
        const reset = document.createElement('div');
        reset.className = 'text-[11px] text-gray-400 dark:text-gray-500 mt-1';
        let text = 'resets in ' + w.resetIn;
        if (w.resetAtMs) {
          // Formatted in the browser's own local timezone, not computed
          // server-side -- the gateway/OWUI container's system timezone
          // and the person actually looking at this dialog aren't
          // guaranteed to be the same, so doing this client-side is the
          // only way to get it right regardless of where either runs.
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
    body.appendChild(section);
  }

  if (DATA.goal) {
    sectionCount++;
    const section = document.createElement('div');
    section.className = 'mb-4';
    section.appendChild(sectionHeader('Goal'));
    const line = document.createElement('div');
    line.className = 'text-sm text-gray-700 dark:text-gray-200';
    line.textContent = DATA.goal.line;
    section.appendChild(line);
    if (DATA.goal.pct !== null) {
      const barWrap = document.createElement('div');
      barWrap.className = 'mt-1.5';
      barWrap.appendChild(makeBar(DATA.goal.pct));
      section.appendChild(barWrap);
    }
    body.appendChild(section);
  }

  if (sectionCount === 0) {
    const empty = document.createElement('div');
    empty.className = 'text-gray-400 dark:text-gray-500 text-xs mb-4';
    empty.textContent = 'No usage data available for this session yet.';
    body.appendChild(empty);
  }

  const footer = document.createElement('div');
  footer.className = 'text-[11px] text-gray-400 dark:text-gray-600 pt-3 '
    + 'border-t border-gray-100 dark:border-gray-800';
  footer.textContent = 'Fetched ' + DATA.fetchedAt + ' · via ' + DATA.source;
  body.appendChild(footer);
})();
"""


def _render_modal_fill_js(data: dict) -> str:
    """Fill the modal template with a single JSON blob -- never string-
    interpolate individual fields directly into the JS source. json.dumps
    always produces a syntactically valid JS object-literal expression, so
    this is the one substitution point that needs to be safe against
    whatever the gateway's numbers/text happen to contain (e.g. a goal
    description with quotes or backslashes).

    json.dumps does NOT escape "<" by default, so a value containing a
    literal "</script>" would terminate an enclosing <script> tag early if
    this code ever ends up placed into HTML rather than passed straight to
    a JS execution call -- escaping "<" to the equivalent \\u003c unicode
    escape is a no-op for JSON.parse but closes that off regardless of how
    the execute event happens to deliver this string."""
    payload = json.dumps(data).replace("<", "\\u003c")
    return _MODAL_FILL_JS_TEMPLATE.replace("__OPENCLAW_STATUS_DATA__", payload)


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
        button fetches back with (see the Compact button's onclick in
        _MODAL_FILL_JS_TEMPLATE), reusing this same endpoint rather than
        registering a second Action."""
        if body.get("mode") == "compact":
            return await self._run_compact(body, __user__, __event_emitter__)
        return await self._run_status(body, __user__, __event_emitter__)

    async def _run_status(self, body: dict, __user__, __event_emitter__, *, show_loading=True):
        chat_id = body.get("chat_id")
        user_id = (__user__ or {}).get("id") or "unknown"

        if not chat_id:
            await __event_emitter__({
                "type": "notification",
                "data": {"type": "error", "content": "OpenClaw status: no chat_id on this message"},
            })
            return {"status": "error", "detail": "missing chat_id"}

        # Open the dialog immediately -- before touching the network -- so
        # the click gets instant feedback (overlay + loading skeleton)
        # instead of nothing visibly happening for however long the
        # gateway round trip takes. Skipped when refreshing after a compact
        # completes (_run_compact already has the dialog open on its own
        # "Compacting..." state) -- re-showing the generic skeleton there
        # would be an unnecessary extra flash between two loading states.
        if show_loading:
            await __event_emitter__({"type": "execute", "data": {"code": _MODAL_OPEN_JS}})

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
            # Raw sessions.describe/usage.status rather than emit.py's
            # _build_usage_status_lines: that helper joins context+primary
            # window into one thin combined line to fit the status UI's
            # single-line clamp (see its docstring), which is exactly the
            # constraint the dialog doesn't have -- Context and Rate Limits
            # get their own sections with individual progress bars here.
            # Still reuses emit.py's small formatting primitives
            # (_fmt_tokens, _relative_time, _format_goal_line) so text like
            # "resets in 2h05m" matches the status line's wording exactly.
            desc_task = asyncio.ensure_future(
                conn.send_request("sessions.describe", dict(key=session_key), timeout=5)
            )
            usage_task = asyncio.ensure_future(
                conn.send_request("usage.status", {}, timeout=5)
            )
            desc = await desc_task
            usage = await usage_task
        except Exception as ex:
            pipe_log(f"[status-action] fetch failed: {ex}")
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_modal_fill_js(
                    {"error": f"Could not fetch status: {ex}"}
                )},
            })
            return {"status": "error", "detail": str(ex)}

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
                    # Raw ms timestamp, not a server-formatted clock time --
                    # the absolute time is rendered client-side (see
                    # _MODAL_FILL_JS_TEMPLATE) so it shows in the browser's
                    # own local timezone rather than whatever timezone this
                    # container happens to be running in.
                    "resetAtMs": w.get("resetAt"),
                })
            break

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

        modal_data = {
            "provider": provider,
            "model": session_row.get("model") or "",
            "context": context_data,
            "windows": windows_data,
            "goal": goal_data,
            "source": source,
            "fetchedAt": time.strftime("%H:%M:%S"),
            "error": None,
            # Threaded through so the dialog's own Compact button can POST
            # back to this same Action (mode="compact") with the exact same
            # chat/message/session identity -- required so the follow-up
            # call's __event_emitter__ routes to this same open browser tab.
            # __user__ is NOT included here: OWUI resolves that itself from
            # the fetch call's own Authorization header, same as the
            # original click, so there's nothing for the client to send.
            "chatId": chat_id,
            "messageId": body.get("id"),
            "sessionId": body.get("session_id"),
            "owuiModel": body.get("model"),
        }
        pipe_log(f"[status-action] opened modal ({source}): "
                 f"context={context_data} windows={len(windows_data)} goal={bool(goal_data)}")

        await __event_emitter__({
            "type": "execute",
            "data": {"code": _render_modal_fill_js(modal_data)},
        })
        return {"status": "ok", "context": context_data, "windows": windows_data}

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
                "data": {"code": _render_modal_fill_js({"error": "Missing chat_id"})},
            })
            return {"status": "error", "detail": "missing chat_id"}

        session_key = _owui_session_key(self.valves.AGENT_ID, user_id, chat_id)

        try:
            conn, source = await _get_action_connection(lambda: self.valves)
        except Exception as ex:
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_modal_fill_js({"error": f"Could not connect: {ex}"})},
            })
            return {"status": "error", "detail": str(ex)}

        if conn.active_run_id_for_session(session_key):
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_modal_fill_js({
                    "error": "A response is currently in progress — wait for "
                             "it to finish before compacting."
                })},
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
                "data": {"code": _render_modal_fill_js({"error": f"Could not start compact: {ex}"})},
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
                "data": {"code": _render_modal_fill_js(
                    {"error": "Compact timed out — it may still finish in the background."}
                )},
            })
            return {"status": "error", "detail": "timeout"}

        if status != "done":
            await __event_emitter__({
                "type": "execute",
                "data": {"code": _render_modal_fill_js({"error": f"Compact {status}."})},
            })
            return {"status": "error", "detail": status}

        pipe_log("[status-action] compact done, refreshing status")
        return await self._run_status(body, __user__, __event_emitter__, show_loading=False)
