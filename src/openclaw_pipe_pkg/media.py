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
    extension), so prose using "MEDIA:" as a term ("the MEDIA: fix") keeps
    its next word as text instead of a broken image link."""
    return bool(_MEDIA_FNAME_RE.search(fname))


def _advance_media_buffer(pending: str, delta: str, ctx: str = "") -> tuple[str, str]:
    """Feed a new assistant-delta chunk into the MEDIA: buffering state.

    Returns (text_to_yield_now, new_pending), mirroring
    `_advance_input_prompt_buffer`. A real streaming provider can deliver
    "MEDIA:filename.png" split across deltas at any point, mid-prefix or
    mid-filename, and resolving one delta alone drops the directive or
    truncates its filename. Hold back from the first "MEDIA:" (or a prefix
    of it that could still complete) until a whitespace char confirms the
    filename is done.

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

    A filename ends at whitespace OR at the next "MEDIA:" directive, so
    "X.pngMEDIA:Y.png" is two files, not one bogus name (ELI-76). A copy of
    the same directive glued on with no separator is dropped: nothing emits
    that on purpose, it is a re-delivered tail.
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


# A directive is code-aware, as the ask marker is (ELI-107, ELI-111). Code is
# a fenced block (CommonMark 4.5) or an inline code span (6.1): a backtick run
# closed by the next run of the same length within its paragraph. A run with
# no closer is a literal backtick, which is only known once its paragraph
# ends. Indented code blocks are not tracked: an indented line in a list is
# prose.
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
        # The line break after a directive depends on what follows it, so a
        # stream holds it until something does (ELI-113).
        complete = final or bool(text[end:].strip())
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
    `replacement(filename)`, and the text after it on a line of its own.
    Nothing else changes: a streamed chunk's own trailing line break is the
    next chunk's line start (ELI-113)."""
    out, pos = "", 0
    for start, end, fname in directives:
        rest = text[end:]
        shown = replacement(fname)
        # A directive taken out (ELI-328) leaves no line of its own.
        out += text[pos:start] + shown + ("\n" if rest.strip() and shown else "")
        pos = end + len(rest) - len(rest.lstrip())
    return out + text[pos:]


def _resolve_media(text, base_url=None, ctx=""):
    """Convert every MEDIA:filename directive in text to embedded media.

    A reply can carry several images, so every directive is converted, not
    just the first. Prose using "MEDIA:" as a term (no file extension) and a
    directive quoted in code (ELI-111) stay text. `ctx`: the text shown before `text` in its message
    item.
    """
    if base_url is None:
        base_url = MEDIA_BASE_URL
    directives, _hold = _media_scan(text, ctx)
    if not directives:
        return text, False
    # Prefer HTTPS URL over base64 data URI (base64 breaks OWUI streaming parser).
    # A directive that is a URL already is one.
    return _replace_media(text, directives,
                          lambda fname: f"![{os.path.basename(fname)}]({fname})" if _is_media_url(fname)
                          else f"![{fname}]({base_url.rstrip('/')}/{fname})"), True


def _is_media_url(fname: str) -> bool:
    return fname.startswith(("https://", "http://"))


def _is_gateway_media_name(fname: str) -> bool:
    """A directive naming a path, not a bare file name or a URL: the Gateway
    resolves it on its own host and stores the file as an `image` block of
    the run's message (ELI-328). The Pipe cannot read that path, and the
    file server never had it."""
    return "/" in fname and not _is_media_url(fname)


def _event_media_urls(data) -> list[str]:
    """The `mediaUrls` of an assistant event: on the Gateway's own runtime
    (DeepSeek, Grok, GPT...) the Gateway takes a reply's `MEDIA:` lines out
    of its text and sends their files here instead (measured 2026-10-26,
    ELI-328). claude-cli runs keep the lines in the text."""
    urls = data.get("mediaUrls") if isinstance(data, dict) else None
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()] if isinstance(urls, list) else []


def _take_gateway_media(text: str, ctx: str = "") -> tuple[str, list[str]]:
    """(`text` without its path `MEDIA:` directives, their paths). The image
    comes from the run's stored blocks once the run ends (`_gateway_images_text`);
    the directive line itself is not shown. `ctx`: as for `_media_scan`."""
    directives, _hold = _media_scan(text, ctx)
    paths = [d for d in directives if _is_gateway_media_name(d[2])]
    if not paths:
        return text, []
    return _replace_media(text, paths, lambda _fname: ""), [fname for _s, _e, fname in paths]


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

    A reply can carry several images, so every directive is uploaded, not
    just the first. If any single directive can't be resolved this way (file missing locally, bad name),
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


# Where the Gateway serves media a reply carries as `image` blocks (an
# agent's own image tool, e.g. Codex image generation). It needs the Gateway
# token, which the browser does not have, so the Pipe fetches it (ELI-322).
_GATEWAY_MEDIA_PREFIX = "/api/chat/media/outgoing/"
_GATEWAY_MEDIA_MAX_BYTES = 50 * 1024 * 1024
_SAFE_FNAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _gateway_image_blocks(message) -> list[dict]:
    """The `image` blocks of a final assistant message that point at the
    Gateway's media route, in order."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "image"
        and isinstance(block.get("url"), str) and block["url"].startswith(_GATEWAY_MEDIA_PREFIX)
    ]


def _history_image_blocks_for_run(history: dict, run_id: str) -> list[dict]:
    """The Gateway image blocks of one run's assistant messages in
    `chat.history`, in order. The Gateway attaches a run's managed images to
    its stored message; the live `chat` final and `session.message` carry
    only the text (measured 2026-10-25, ELI-322)."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list) or not run_id:
        return []
    blocks = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        if run_id in (meta.get("runId"), message.get("runId")):
            blocks += _gateway_image_blocks(message)
    return blocks


def _history_media_errors_for_run(history: dict, run_id: str) -> list[str]:
    """The labels (file names) of the `attachment_error` blocks of one run's
    assistant messages: a reply's media file the Gateway could not store,
    e.g. a path that does not exist (seen live: code `file-not-found`)."""
    labels = []
    for message in (history or {}).get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
        if run_id not in (meta.get("runId"), message.get("runId")):
            continue
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            attachment = block.get("attachment") if isinstance(block, dict) else None
            if block.get("type") == "attachment_error" and isinstance(attachment, dict):
                labels.append(str(attachment.get("label") or ""))
    return labels


def _unstored_gateway_media(paths, blocks: int, errors: list[str]) -> list[str]:
    """The names of the path directives in `paths` that have no stored image
    yet: each one the Gateway reported it could not store (`errors`, by file
    name), then, while fewer than `blocks` arrived for the rest, the last of
    those. Empty once every path is accounted for."""
    names = [os.path.basename(p) for p in paths]
    failed = [n for n in names if n in {os.path.basename(e) for e in errors if e}]
    rest = [n for n in names if n not in failed]
    return failed + rest[max(0, blocks):]


def _gateway_image_fname(block: dict) -> str:
    """A unique bare file name for an image block, with its type's extension."""
    stem = os.path.splitext(os.path.basename(str(block.get("alt") or "")))[0]
    stem = _SAFE_FNAME_RE.sub("-", stem).strip("-.")[:60] or "image"
    ext = mimetypes.guess_extension(block.get("mimeType") or "") or ".png"
    return f"{stem}-{uuid.uuid4().hex[:8]}{ext}"


def _fetch_gateway_media(gateway_url: str, token: str, path: str) -> bytes:
    """GET one media path from the Gateway's HTTP server, with its token."""
    host, port = _parse_gateway_url(gateway_url)
    req = urllib.request.Request(
        f"http://{host}:{port}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(_GATEWAY_MEDIA_MAX_BYTES + 1)
    if len(raw) > _GATEWAY_MEDIA_MAX_BYTES:
        raise RuntimeError(f"media over {_GATEWAY_MEDIA_MAX_BYTES} bytes")
    return raw


async def _gateway_images_text(blocks, *, valves, __request__=None, __event_emitter__=None,
                               missing=()) -> str:
    """Show a final message's Gateway image blocks the way a MEDIA: directive
    is shown: each image is fetched into the media dir, then resolved by
    `_resolve_media_text` (Open WebUI's files, else the file server). An
    image that cannot be fetched is named in the text, never dropped
    silently, and so is each of `missing`, a file the reply named that the
    Gateway never stored."""
    lines = [f"_(an image could not be loaded: {name})_" for name in missing]
    for block in blocks:
        fname = _gateway_image_fname(block)
        try:
            raw = await asyncio.to_thread(
                _fetch_gateway_media, valves.GATEWAY_URL, valves.GATEWAY_TOKEN, block["url"])
            os.makedirs(MEDIA_DIR, exist_ok=True)
            with open(os.path.join(MEDIA_DIR, fname), "wb") as f:
                f.write(raw)
        except Exception as ex:
            pipe_log(f"  gateway image fetch failed for {block['url']}: {ex!r}")
            lines.append(f"_(an image could not be loaded: {block.get('alt') or 'image'})_")
            continue
        lines.append(f"MEDIA:{fname}")
    if not lines:
        return ""
    text = "\n\n" + "\n\n".join(lines) + "\n"
    resolved, _handled = await _resolve_media_text(
        text, valves=valves, __request__=__request__, __event_emitter__=__event_emitter__)
    return resolved


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
            self._handle_upload()


    try:
        server = HTTPServer(("0.0.0.0", port), _Handler)
        _remember_file_server(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        _file_server_started = True
    except OSError:
        pass
