# ------------------------------------------------------------------
# BUILD FRAGMENT -- do not edit the built openclaw_pipe.py directly.
# Source of truth: src/openclaw_pipe_pkg/<module>.py + build.py
# ------------------------------------------------------------------


# Re-encoding a photo that is over the cap: libjpeg quality 85 keeps text on a
# photographed menu or sign readable at a fraction of a phone camera's size.
_FIT_JPEG_QUALITY = 85
# JPEG size grows with the pixel count, so each try scales both sides by the
# square root of (cap / size), a little under it so one try usually lands.
_FIT_SCALE_MARGIN = 0.95
# `chat.send` attachment type of an image (the same value gateway.py sends).
_IMAGE_ATTACHMENT_TYPE = "image"


class ImageTooLargeError(Exception):
    """An image over the Gateway's cap that could not be made to fit. `str()`
    is the sentence the user sees."""


def _base64_decoded_size(data: str) -> int:
    """Decoded byte count of a base64 string, without decoding it."""
    data = data.strip()
    return len(data) * 3 // 4 - (len(data) - len(data.rstrip("=")))


def _images_over_cap(attachments: list, max_bytes: int) -> bool:
    return any(a.get("type") == _IMAGE_ATTACHMENT_TYPE
               and _base64_decoded_size(a.get("content") or "") > max_bytes
               for a in attachments)


def _shrink_to_jpeg(raw: bytes, max_bytes: int, PIL) -> bytes | None:
    """`raw` as a JPEG of at most `max_bytes`, EXIF orientation applied; None
    when even a 1x1 image is too big."""
    Image, ImageOps = PIL
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if img.mode in ("RGBA", "LA", "P"):
        # JPEG has no alpha: put a transparent screenshot on white.
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, "white")
        img.paste(rgba, mask=rgba.getchannel("A"))
    elif img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "JPEG", quality=_FIT_JPEG_QUALITY, optimize=True)
    while out.tell() > max_bytes:
        scale = min(_FIT_SCALE_MARGIN, (max_bytes / out.tell()) ** 0.5 * _FIT_SCALE_MARGIN)
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        if size == (img.width, img.height):  # already 1x1: nothing left to shrink
            return None
        img = img.resize(size, Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, "JPEG", quality=_FIT_JPEG_QUALITY, optimize=True)
    return out.getvalue()


def _load_pil():
    """(Image, ImageOps) from Pillow, or None where it is not installed."""
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    return Image, ImageOps


def _fit_image_attachments(attachments: list, max_bytes: int) -> list:
    """`attachments` with every image over `max_bytes` (the Gateway's cap,
    decoded bytes) re-encoded as a JPEG that fits. Images under it are passed
    on untouched. Raises `ImageTooLargeError` when one cannot be made to fit."""
    out = []
    for a in attachments:
        content = a.get("content") or ""
        size = _base64_decoded_size(content)
        if not _images_over_cap([a], max_bytes):
            out.append(a)
            continue
        mb, cap_mb = size / 1048576, max_bytes / 1048576
        too_big = (f"**Couldn't send the image:** it is {mb:.1f} MB and the "
                   f"Gateway accepts images up to {cap_mb:.1f} MB")
        lib = _load_pil()
        if lib is None:
            raise ImageTooLargeError(
                f"{too_big}, and this server can't make it smaller (Pillow is "
                "not installed). Please send a smaller photo or a screenshot.")
        try:
            small = _shrink_to_jpeg(base64.b64decode(content), max_bytes, lib)
        except Exception as e:  # not an image Pillow can read
            pipe_log(f"image fit: {a.get('fileName')}: {e}")
            small = None
        if small is None:
            raise ImageTooLargeError(
                f"{too_big}, and it couldn't be made small enough. Please send "
                "a smaller photo or a screenshot.")
        pipe_log(f"image fit: {a.get('fileName')} {size} -> {len(small)} bytes "
                 f"(cap {max_bytes})")
        stem = (a.get("fileName") or "image").rsplit(".", 1)[0]
        out.append({**a, "mimeType": "image/jpeg", "fileName": f"{stem}.jpg",
                    "content": base64.b64encode(small).decode("ascii")})
    return out
