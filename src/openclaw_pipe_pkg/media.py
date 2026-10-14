# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



# ---------------------------------------------------------------------------
# File Server (media delivery)
# ---------------------------------------------------------------------------

MEDIA_DIR = "/tmp/openclaw-pipe-media"
MEDIA_BASE_URL = "http://localhost:18791"

_file_server_started = False

_MEDIA_TRIGGER = "MEDIA:"
_MEDIA_FNAME_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _looks_like_media_filename(fname: str) -> bool:
    """Return True only if fname plausibly names a real file (has an
    extension). Without this, ordinary prose that happens to use "MEDIA:"
    as a documentation term (e.g. "the MEDIA: fix is proven") gets its next
    word treated as a filename and mangled into a broken image link --
    confirmed live 2026-07-10 when explaining this very feature corrupted
    "the MEDIA: fix" into `![fix](.../fix)`.
    """
    return bool(_MEDIA_FNAME_RE.search(fname))


def _advance_media_buffer(pending: str, delta: str, ctx: str = "") -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the MEDIA: buffering state.

    Returns (text_to_yield_now, new_pending), mirroring
    `_advance_input_prompt_buffer`. A real streaming provider can deliver
    "MEDIA:filename.png" split across multiple deltas at any point,
    including mid-filename or mid-prefix -- resolving `_resolve_media`
    eagerly against a single truncated delta either drops the directive
    entirely (if "MEDIA:" itself is split, so neither chunk contains the
    full prefix) or resolves it against a truncated filename (confirmed
    live 2026-07-10: sending several images in one turn hit both failure
    modes in the same message). Hold back from the first "MEDIA:" (or a
    prefix of it that could still complete) until a whitespace char
    confirms the filename is done.

    A directive in code is text and never held; one after a backtick run
    that may still open a code span is held until that is decided (ELI-111).
    `ctx`: the text already shown in this message item.
    """
    candidate = pending + delta
    _directives, hold = _media_scan(candidate, ctx, final=False)
    if hold is None:
        return candidate, ""
    return candidate[:hold], candidate[hold:]


def _take_media_fname(after_prefix: str) -> tuple[str, str]:
    """Split the text after a "MEDIA:" prefix into (filename, rest).

    A filename ends at whitespace OR at the next "MEDIA:" directive. Splitting
    on whitespace alone glued "X.pngMEDIA:X.png" into one bogus filename, and
    the reply persisted as `![X.pngMEDIA:X.png](.../X.pngMEDIA:X.png)`, a link
    that 404s (ELI-76). A copy of the same directive glued on with no separator
    is dropped: nothing emits that on purpose, it is a re-delivered tail.
    """
    token = after_prefix.split()[0] if after_prefix else ""
    fname = token.split(_MEDIA_TRIGGER, 1)[0]
    rest = after_prefix[len(fname):]
    glued = _MEDIA_TRIGGER + fname
    while fname and rest.startswith(glued):
        after = rest[len(glued):]
        if after and not after[0].isspace() and not after.startswith(_MEDIA_TRIGGER):
            break  # a different, longer filename that merely starts the same
        rest = after
    return fname, rest


# ELI-111: a directive is code-aware, as the ask marker is (ELI-107). Code is
# a fenced block (CommonMark 4.5) or an inline code span (6.1): a backtick run
# closed by the next run of the same length within its paragraph. A run with
# no closer is a literal backtick, which is only known once its paragraph
# ends. Indented code blocks are not tracked: an indented line in a list is
# prose. A report quoting a directive in a double-backtick span had it
# rendered as an image (message 442ecfa3, 2026-10-14).
_BLOCK_START_RE = re.compile(r"^ {0,3}(#{1,6}(\s|$)|[-+*](\s|$)|\d{1,9}[.)](\s|$))")


def _media_code_ranges(text: str, ctx: str = "", final: bool = True):
    """(ranges, open_at) for `text` as it continues `ctx`: the [start, end)
    spans of `text` that are code, and the offset in `text` from which that
    is not decided yet (a backtick run that may still be closed in the open
    paragraph `text` ends in), else None. `final`: nothing follows `text`."""
    full, base = ctx + text, len(ctx)
    ranges, open_at = [], None
    fence_step = globals().get("_fence_step")

    def spans(start, end, last):
        """Code spans of the paragraph full[start:end]; `last`: it may go on."""
        nonlocal open_at
        i = start
        while i < end:
            c = full[i]
            if c == "\\" and i + 1 < end and full[i + 1] == "`":
                i += 2
                continue
            if c != "`":
                i += 1
                continue
            j = i
            while j < end and full[j] == "`":
                j += 1
            k, close = j, None
            while k < end and close is None:
                if full[k] != "`":
                    k += 1
                    continue
                r = k
                while r < end and full[r] == "`":
                    r += 1
                if r - k == j - i and not (last and r == end):
                    close = r
                k = r
            if close is not None:
                ranges.append((i, close))
                i = close
                continue
            if last and open_at is None:
                open_at = i
            i = j

    fence, para, pos = "", None, 0
    lines = full.split("\n")
    for n, line in enumerate(lines):
        end = pos + len(line)
        complete = final or n < len(lines) - 1
        if fence or (complete and fence_step and fence_step(line, "")):
            if para is not None:
                spans(para, pos, False)
                para = None
            ranges.append((pos, min(end + 1, len(full))))
            if complete and fence_step:
                fence = fence_step(line, fence)
        elif complete and not line.strip():
            if para is not None:
                spans(para, pos, False)
                para = None
        else:
            if para is not None and _BLOCK_START_RE.match(line):
                spans(para, pos, False)
                para = None
            if para is None:
                para = pos
        pos = end + 1
    if para is not None:
        spans(para, len(full), not final)
    shifted = [(max(a, base) - base, b - base) for a, b in ranges if b > base]
    if open_at is not None:
        open_at = max(open_at, base) - base
    return shifted, open_at


def _media_scan(text: str, ctx: str = "", final: bool = True):
    """(directives, hold_at): each complete `MEDIA:<file>` directive of `text`
    outside code, as (start, end, filename) by `_take_media_fname`, and the
    offset a stream holds `text` back from (None when nothing): the first
    occurrence, or prefix of one at the end, whose filename may still be
    arriving or whose code state is not decided. `ctx`: the text shown
    before `text` in its message item. `final`: nothing follows `text`."""
    ranges, open_at = _media_code_ranges(text, ctx, final)

    def in_code(i):
        return any(a <= i < b for a, b in ranges)
    directives, hold, i = [], None, 0
    while True:
        idx = text.find(_MEDIA_TRIGGER, i)
        if idx < 0:
            break
        i = idx + len(_MEDIA_TRIGGER)
        if in_code(idx):
            continue
        after = text[i:]
        stripped = after.strip()
        fname, rest = _take_media_fname(stripped)
        end = i + (len(after) - len(after.lstrip())) + len(stripped) - len(rest)
        complete = final or end < len(text)
        if not complete or (open_at is not None and idx >= open_at
                            and _looks_like_media_filename(fname)):
            hold = idx
            break
        if fname and _looks_like_media_filename(fname):
            directives.append((idx, end, fname))
            i = end
    if hold is None and not final:
        for n in range(min(len(_MEDIA_TRIGGER) - 1, len(text)), 0, -1):
            if text.endswith(_MEDIA_TRIGGER[:n]) and not in_code(len(text) - n):
                hold = len(text) - n
                break
    return directives, hold


def _replace_media(text: str, directives, replacement) -> str:
    """`text` with each directive (from `_media_scan`) replaced by
    `replacement(filename)`, and the text after it on a line of its own."""
    out, pos = "", 0
    for start, end, fname in directives:
        rest = text[end:]
        out += text[pos:start] + replacement(fname) + ("\n" if rest.strip() else "")
        pos = end + len(rest) - len(rest.lstrip())
    return out + text[pos:].rstrip()


def _resolve_media(text, base_url=None, ctx=""):
    """Convert every MEDIA:filename directive in text to embedded media.

    A single reply can legitimately carry several images (confirmed live
    2026-07-10 sending a 4-screenshot bug repro) -- this must loop over every
    occurrence, not just the first, or later directives in the same chunk
    silently pass through as literal "MEDIA:filename" text. Prose using
    "MEDIA:" as a term (no file extension) and a directive quoted in code
    (ELI-111) stay text. `ctx`: the text shown before `text` in its message
    item.
    """
    if base_url is None:
        base_url = MEDIA_BASE_URL
    directives, _hold = _media_scan(text, ctx)
    if not directives:
        return text, False
    # Prefer HTTPS URL over base64 data URI (base64 breaks OWUI streaming parser).
    return _replace_media(text, directives,
                          lambda fname: f"![{fname}]({base_url.rstrip('/')}/{fname})"), True


def _extract_request_bearer(__request__):
    """Return a Bearer token from the current OWUI request, when available."""
    if not __request__:
        return None
    headers = getattr(__request__, "headers", None)
    if not headers:
        return None
    auth = headers.get("authorization") or headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth.split(" ", 1)[1].strip()
    return None


def _multipart_body(field_name, file_path, filename, mime):
    boundary = "----openclawowui" + uuid.uuid4().hex
    with open(file_path, "rb") as f:
        raw = f.read()
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; '
        f'filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    return boundary, head + raw + tail


def _upload_owui_file(file_path, base_url, token):
    """Upload one file to OWUI Files API and return the file object."""
    if not token:
        raise RuntimeError("missing OWUI bearer token")
    filename = os.path.basename(file_path)
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    boundary, body = _multipart_body("file", file_path, filename, mime)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/files/?process=false",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise RuntimeError(f"OWUI file upload failed: {exc.code} {detail}") from exc


async def _resolve_media_via_owui(
    text,
    *,
    base_url,
    token,
    __event_emitter__,
    ctx="",
):
    """Upload every MEDIA: file in text to OWUI, in order, and attach each to
    the current message.

    A single reply can legitimately carry several images (confirmed live
    2026-07-10 sending a 4-screenshot bug repro) -- this must loop over every
    occurrence, not just the first, or later directives in the same chunk
    silently pass through as literal "MEDIA:filename" text. If any single
    directive can't be resolved this way (file missing locally, bad name),
    bail out entirely with handled=False so the caller's fallback
    (`_resolve_media`) handles ALL directives uniformly via the external
    URL instead of leaving a mix of native-upload and unresolved directives
    in one message.
    """
    directives, _hold = _media_scan(text, ctx)
    if not directives:
        return text, False
    for _start, _end, fname in directives:
        if ".." in fname or "/" in fname or not os.path.isfile(os.path.join(MEDIA_DIR, fname)):
            return text, False

    shown = {}
    for _start, _end, fname in directives:
        if fname in shown:
            continue
        # _upload_owui_file uses blocking urllib -- must run off the event loop.
        # This call goes to OWUI's own API (often 127.0.0.1:8080, i.e. OWUI
        # calling itself) from *inside* the async handler for the very request
        # that's driving this pipe run. Calling it directly would block the
        # single asyncio event loop thread, and OWUI can't service its own
        # incoming HTTP request while its own loop is blocked waiting on it --
        # a guaranteed self-deadlock that only resolves via timeout. Running it
        # in a thread lets the event loop keep serving requests concurrently.
        fpath = os.path.join(MEDIA_DIR, fname)
        file_obj = await asyncio.to_thread(_upload_owui_file, fpath, base_url, token)
        if __event_emitter__:
            await __event_emitter__(
                {
                    "type": "files",
                    "data": {"files": [file_obj]},
                }
            )

        file_id = file_obj.get("id")
        mime = file_obj.get("meta", {}).get("content_type") or mimetypes.guess_type(fname)[0] or ""
        content_url = f"/api/v1/files/{file_id}/content" if file_id else ""
        if content_url and mime.startswith("image/"):
            shown[fname] = f"![{fname}]({content_url})"
        elif content_url:
            shown[fname] = f"[{fname}]({content_url})"
        else:
            shown[fname] = f"`{fname}`"
    return _replace_media(text, directives, shown.__getitem__), True


async def _resolve_media_text(text, *, valves, __request__=None, __event_emitter__=None, ctx=""):
    """Resolve a MEDIA: directive, preferring the native OWUI Files API upload
    and falling back to the external file-server URL. Shared by the main
    per-delta resolution site and the end-of-stream final-flush site so both
    stay in sync (see `_advance_media_buffer` for why callers must only pass
    in text whose MEDIA:filename is already known-complete).
    """
    handled = False
    resolved = text
    if valves.USE_OWUI_FILES:
        token = _extract_request_bearer(__request__) or valves.OWUI_API_KEY
        try:
            resolved, handled = await _resolve_media_via_owui(
                text,
                base_url=valves.OWUI_BASE_URL,
                token=token,
                __event_emitter__=__event_emitter__,
                ctx=ctx,
            )
        except Exception as ex:
            pipe_log(f"  OWUI file upload failed; falling back: {ex}")
    if not handled:
        resolved, handled = _resolve_media(text, base_url=valves.FILE_SERVER_BASE_URL, ctx=ctx)
    return resolved, handled


# Anchor for finding a file server left behind by a *previous* deploy of this
# same function (same class of issue as P33/P36/gateway._reap_stale_gateway_
# connection: OWUI's function loader execs each redeploy into a brand-new
# module object with no teardown hook on the old one, so a module-level
# _file_server_started flag alone just resets and a second HTTPServer tries
# to bind the same port -- silently failing since the old one is still
# listening, per _start_file_server's `except OSError: pass`). Stashing the
# server object on `open_webui.socket.main` -- OWUI's own stable module,
# never reloaded by our function -- lets the next deploy find and shut down
# the previous one first, so redeploys self-heal without a container
# restart. Falls back to a no-op when not running inside OWUI (unit tests).
_STALE_FILE_SERVER_ATTR = "_openclaw_file_server_v1"


def _reap_stale_file_server() -> None:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return
    stale = getattr(_owui_socket_main, _STALE_FILE_SERVER_ATTR, None)
    if stale is None:
        return
    try:
        stale.shutdown()
        stale.server_close()
        pipe_log("  reaped stale file server from a previous deploy")
    except Exception as ex:
        pipe_log(f"  failed to reap stale file server (non-fatal): {ex}")


def _remember_file_server(server) -> None:
    try:
        import open_webui.socket.main as _owui_socket_main
    except Exception:
        return
    setattr(_owui_socket_main, _STALE_FILE_SERVER_ATTR, server)


def _start_file_server(port=18791):
    """Start a minimal HTTP server for media files. Starts once per process."""
    global _file_server_started
    if _file_server_started:
        return
    _reap_stale_file_server()
    directory = "/tmp/openclaw-pipe-media"
    os.makedirs(directory, exist_ok=True)
    Path(directory, "health").write_text("ok")

    class _Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=directory, **kwargs)

        def end_headers(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def _handle_upload(self):
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"empty")
                return
            body = self.rfile.read(length)
            path = self.path.strip("/").split("?")[0]
            if not path or ".." in path or "/" in path:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"bad path")
                return
            dest = os.path.join(directory, os.path.basename(path))
            with open(dest, "wb") as f:
                f.write(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"ok:{path}".encode())

        def do_PUT(self):
            self._handle_upload()

        def do_POST(self):
            # DEV-ONLY-START
            # Cross-agent deploy coordination (ELI-24): install.py has no
            # filesystem access to this container's STATE_DIR, so it signals
            # a pending deploy over this same unauthenticated port instead.
            # Dev/internal-tooling only -- stripped from openclaw_pipe.py by
            # build.py, never present in the artifact end users install.
            if self.path.rstrip("/") == "/__devcoord__/deploy-pending":
                _write_json_file(_devcoord_pending_path(), {"requested": time.time()})
                self.send_response(200)
                self.end_headers()
                return
            if self.path.rstrip("/") == "/__devcoord__/drop-gateway-ws":
                # Live test of a mid-run socket drop (repro_socket_drop.py).
                # Close the Gateway socket from this thread, exactly as a
                # network drop ends it: the event loop sees ConnectionClosed,
                # marks the gap and reconnects.
                conn = _gateway_connection
                loop = getattr(conn, "_loop", None) if conn else None
                ok = bool(loop and conn._ws)
                if ok:
                    asyncio.run_coroutine_threadsafe(conn._ws.close(), loop)
                pipe_log(f"[devcoord] test socket drop requested: {'closing' if ok else 'no socket'}")
                self.send_response(200 if ok else 409)
                self.end_headers()
                return
            # DEV-ONLY-END
            self._handle_upload()

        # DEV-ONLY-START
        def do_GET(self):
            if self.path.rstrip("/") == "/__devcoord__/status":
                try:
                    inflight = _devcoord_inflight_count()
                except Exception:
                    inflight = 0
                body = json.dumps({"inflight": inflight}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            super().do_GET()

        def do_DELETE(self):
            if self.path.rstrip("/") == "/__devcoord__/deploy-pending":
                try:
                    os.remove(_devcoord_pending_path())
                except FileNotFoundError:
                    pass
                self.send_response(200)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()
        # DEV-ONLY-END

    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
        _remember_file_server(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _file_server_started = True
    except OSError:
        pass
