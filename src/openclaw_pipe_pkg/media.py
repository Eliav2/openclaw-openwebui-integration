# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------



MEDIA_DIR = "/tmp/openclaw-pipe-media"
MEDIA_BASE_URL = "http://localhost:18791"

_file_server_started = False

_MEDIA_TRIGGER = "MEDIA:"
_MEDIA_FNAME_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def _looks_like_media_filename(fname: str) -> bool:
    """Whether fname has an extension, so prose like "the MEDIA: fix" stays text."""
    return bool(_MEDIA_FNAME_RE.search(fname))


def _advance_media_buffer(pending: str, delta: str, ctx: str = "") -> tuple[str, str]:
    """Feed a delta; return (text_to_yield_now, new_pending).

    A directive can split across deltas, so hold from "MEDIA:" (or a partial
    prefix) until whitespace ends the filename, or until an open backtick
    run is decided (ELI-111). `ctx`: text already shown in this item.
    """
    candidate = pending + delta
    _directives, hold = _media_scan(candidate, ctx, final=False)
    if hold is None:
        return candidate, ""
    return candidate[:hold], candidate[hold:]


def _take_media_fname(after_prefix: str) -> tuple[str, str]:
    """Split text after "MEDIA:" into (filename, rest).

    A filename also ends at the next "MEDIA:" (ELI-76). A glued copy of the
    same directive is a re-delivered tail and is dropped.
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


# Directives in code stay text (ELI-107, ELI-111): fenced blocks (CommonMark
# 4.5) and code spans (6.1). An unclosed backtick run is literal, known only
# when its paragraph ends. Indented code is not tracked (list lines indent).
_BLOCK_START_RE = re.compile(r"^ {0,3}(#{1,6}(\s|$)|[-+*](\s|$)|\d{1,9}[.)](\s|$))")


def _media_code_ranges(text: str, ctx: str = "", final: bool = True):
    """(code ranges of `text`, offset where code state is undecided or None).

    `ctx` precedes `text`; `final`: nothing follows `text`."""
    full, base = ctx + text, len(ctx)
    ranges, open_at = [], None
    fence_step = globals().get("_fence_step")

    def spans(start, end, last):
        """Code spans of full[start:end]; `last`: the paragraph may go on."""
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
    """(directives as (start, end, filename) outside code, hold offset or None).

    `ctx` precedes `text`; `final`: nothing follows `text`."""
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
        # The line break after it depends on what follows (ELI-113).
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
    """Replace each directive with `replacement(filename)`, text after it on
    a new line. A chunk's trailing line break is left alone (ELI-113)."""
    out, pos = "", 0
    for start, end, fname in directives:
        rest = text[end:]
        shown = replacement(fname)
        # A directive taken out (ELI-328) leaves no line of its own.
        out += text[pos:start] + shown + ("\n" if rest.strip() and shown else "")
        pos = end + len(rest) - len(rest.lstrip())
    return out + text[pos:]


def _resolve_media(text, base_url=None, ctx="", file_server=True):
    """Turn every MEDIA: directive outside code (ELI-111) into an image link.

    A bare filename links to the file server at `base_url`. With the file
    server off (`file_server` False) nothing serves that link, so the line
    becomes a plain error instead of an image that never loads; a URL
    directive needs no server and stays an image either way."""
    if base_url is None:
        base_url = MEDIA_BASE_URL
    directives, _hold = _media_scan(text, ctx)
    if not directives:
        return text, False

    def shown(fname):
        if _is_media_url(fname):
            return f"![{os.path.basename(fname)}]({fname})"
        if file_server:
            # A URL, not base64: base64 breaks OWUI's streaming parser.
            return f"![{fname}]({base_url.rstrip('/')}/{fname})"
        return f"_(a file could not be uploaded: {fname})_"
    return _replace_media(text, directives, shown), True


def _is_media_url(fname: str) -> bool:
    return fname.startswith(("https://", "http://"))


def _is_gateway_media_name(fname: str) -> bool:
    """A path directive: the Gateway stores that file as an `image` block
    (ELI-328); the Pipe cannot read the path."""
    return "/" in fname and not _is_media_url(fname)


def _event_media_urls(data) -> list[str]:
    """An assistant event's `mediaUrls`. The Gateway's own runtime moves
    `MEDIA:` lines here; claude-cli keeps them in the text (ELI-328)."""
    urls = data.get("mediaUrls") if isinstance(data, dict) else None
    return [u.strip() for u in urls if isinstance(u, str) and u.strip()] if isinstance(urls, list) else []


def _take_gateway_media(text: str, ctx: str = "") -> tuple[str, list[str]]:
    """(`text` without path directives, their paths). The images come later
    from the run's stored blocks (`_gateway_images_text`)."""
    directives, _hold = _media_scan(text, ctx)
    paths = [d for d in directives if _is_gateway_media_name(d[2])]
    if not paths:
        return text, []
    return _replace_media(text, paths, lambda _fname: ""), [fname for _s, _e, fname in paths]


def _extract_request_bearer(__request__):
    """The OWUI request's Bearer token, or None."""
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
    """Upload one file to the OWUI Files API; return the file object."""
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
    """Upload every MEDIA: file to OWUI and attach it to the message.

    If any one cannot be uploaded, return handled=False so the fallback
    resolves all of them the same way.
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
        # Off the event loop: this blocking call goes to OWUI's own API, which
        # cannot answer while its loop is blocked (self-deadlock).
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
    """Resolve MEDIA: directives via OWUI Files, else the file server.

    Pass only text whose filenames are complete (see `_advance_media_buffer`).
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
        resolved, handled = _resolve_media(
            text, base_url=valves.FILE_SERVER_BASE_URL, ctx=ctx, file_server=valves.ENABLE_FILE_SERVER)
        if handled and not valves.ENABLE_FILE_SERVER and resolved != text:
            pipe_log("  MEDIA: file not uploaded to OWUI and the file server is off; the reply says so")
    return resolved, handled


# Gateway route for `image` blocks. It needs the Gateway token, which the
# browser lacks, so the Pipe fetches it (ELI-322).
_GATEWAY_MEDIA_PREFIX = "/api/chat/media/outgoing/"
_GATEWAY_MEDIA_MAX_BYTES = 50 * 1024 * 1024
_SAFE_FNAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _gateway_image_blocks(message) -> list[dict]:
    """A message's `image` blocks on the Gateway media route, in order."""
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [
        block for block in content
        if isinstance(block, dict) and block.get("type") == "image"
        and isinstance(block.get("url"), str) and block["url"].startswith(_GATEWAY_MEDIA_PREFIX)
    ]


def _is_run_assistant_message(message, run_id: str) -> bool:
    """Whether `message` is a stored assistant message of run `run_id`.

    Includes the media message the Gateway appends, keyed
    `<run id>:assistant-media[:n]` with no `runId` (2026.10.10,
    appendInjectedAssistantMessageToTranscript)."""
    if not isinstance(message, dict) or message.get("role") != "assistant" or not run_id:
        return False
    meta = message.get("__openclaw") if isinstance(message.get("__openclaw"), dict) else {}
    if run_id in (meta.get("runId"), message.get("runId")):
        return True
    media_key = f"{run_id}:assistant-media"
    return any(isinstance(key, str) and (key == media_key or key.startswith(media_key + ":"))
               for key in (meta.get("idempotencyKey"), message.get("idempotencyKey")))


def _history_image_blocks_for_run(history: dict, run_id: str) -> list[dict]:
    """One run's Gateway image blocks in `chat.history`. The live final has
    only text; images are stored after it (ELI-322)."""
    messages = (history or {}).get("messages")
    if not isinstance(messages, list):
        return []
    blocks = []
    for message in messages:
        if _is_run_assistant_message(message, run_id):
            blocks += _gateway_image_blocks(message)
    return blocks


def _history_media_errors_for_run(history: dict, run_id: str) -> list[str]:
    """File names from one run's `attachment_error` blocks (e.g. `file-not-found`)."""
    labels = []
    for message in (history or {}).get("messages") or []:
        if not _is_run_assistant_message(message, run_id):
            continue
        for block in message.get("content") if isinstance(message.get("content"), list) else []:
            attachment = block.get("attachment") if isinstance(block, dict) else None
            if block.get("type") == "attachment_error" and isinstance(attachment, dict):
                labels.append(str(attachment.get("label") or ""))
    return labels


def _unstored_gateway_media(paths, blocks: int, errors: list[str]) -> list[str]:
    """Names in `paths` with no stored image: those in `errors`, then the
    rest beyond the first `blocks`."""
    names = [os.path.basename(p) for p in paths]
    failed = [n for n in names if n in {os.path.basename(e) for e in errors if e}]
    rest = [n for n in names if n not in failed]
    return failed + rest[max(0, blocks):]


def _gateway_image_fname(block: dict) -> str:
    """A unique safe file name for an image block."""
    stem = os.path.splitext(os.path.basename(str(block.get("alt") or "")))[0]
    stem = _SAFE_FNAME_RE.sub("-", stem).strip("-.")[:60] or "image"
    ext = mimetypes.guess_extension(block.get("mimeType") or "") or ".png"
    return f"{stem}-{uuid.uuid4().hex[:8]}{ext}"


def _fetch_gateway_media(gateway_url: str, token: str, path: str) -> bytes:
    """GET one media path from the Gateway with its token."""
    host, port, tls = _parse_gateway_url(gateway_url)
    req = urllib.request.Request(
        f"{'https' if tls else 'http'}://{host}:{port}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read(_GATEWAY_MEDIA_MAX_BYTES + 1)
    if len(raw) > _GATEWAY_MEDIA_MAX_BYTES:
        raise RuntimeError(f"media over {_GATEWAY_MEDIA_MAX_BYTES} bytes")
    return raw


async def _gateway_images_text(blocks, *, valves, __request__=None, __event_emitter__=None,
                               missing=()) -> str:
    """Fetch Gateway image blocks and show them as MEDIA: directives.

    Failed fetches and `missing` files are named, never dropped silently."""
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


# OWUI reloads a function into a new module with no teardown, so the old
# file server keeps the port. It is stashed on `open_webui.socket.main`, which
# is never reloaded, so the next deploy can shut it down first.
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


def _upload_dest(directory: str, raw_path: str) -> str | None:
    """Where an upload to `raw_path` (the request path) is written: a file
    directly inside `directory`, after symlinks resolve. None to refuse it."""
    name = raw_path.strip("/").split("?")[0]
    root = os.path.realpath(directory)
    dest = os.path.realpath(os.path.join(root, name))
    # Resolved, then prefixed (the check CodeQL's py/path-injection
    # recognises); a direct child only, so "", ".", ".." and "a/b" fail too.
    if not dest.startswith(root + os.sep) or os.path.dirname(dest) != root:
        return None
    return dest


def _start_file_server(port=18791):
    """Start the media file server, once per process."""
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
            dest = _upload_dest(directory, self.path)
            if dest is None:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"bad path")
                return
            with open(dest, "wb") as f:
                f.write(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(f"ok:{os.path.basename(dest)}".encode())

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
