"""Photos over the Gateway's image cap are shrunk before chat.send (S8)."""
import base64
import io
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)
from _test_stubs import install_stubs  # noqa: E402

install_stubs()

from PIL import Image  # noqa: E402

import openclaw_pipe  # noqa: E402
from openclaw_pipe import (  # noqa: E402
    GATEWAY_IMAGE_BYTES_FALLBACK,
    ImageTooLargeError,
    _fit_image_attachments,
    _hello_max_image_bytes,
)


def noise_image(size, fmt="PNG", exif=None) -> bytes:
    """A photo-like worst case: noise barely compresses."""
    img = Image.effect_noise(size, 60).convert("RGB")
    out = io.BytesIO()
    img.save(out, fmt, **({"exif": exif} if exif else {}))
    return out.getvalue()


def attachment(raw: bytes, name="image-1.png", mime="image/png") -> dict:
    return {"type": "image", "mimeType": mime, "fileName": name,
            "content": base64.b64encode(raw).decode("ascii")}


class FitImageAttachmentsTests(unittest.TestCase):
    CAP = 300_000

    def test_an_image_over_the_cap_is_sent_as_a_jpeg_under_it(self):
        raw = noise_image((1600, 1200))
        self.assertGreater(len(raw), self.CAP)
        [out] = _fit_image_attachments([attachment(raw)], self.CAP)
        small = base64.b64decode(out["content"])
        self.assertLessEqual(len(small), self.CAP)
        self.assertEqual((out["mimeType"], out["fileName"]), ("image/jpeg", "image-1.jpg"))
        img = Image.open(io.BytesIO(small))
        self.assertEqual(img.format, "JPEG")
        self.assertAlmostEqual(img.width / img.height, 1600 / 1200, places=2)

    def test_an_image_under_the_cap_is_passed_on_byte_for_byte(self):
        a = attachment(noise_image((200, 150)))
        out = _fit_image_attachments([a], self.CAP)
        self.assertIs(out[0], a)

    def test_the_size_counted_is_the_decoded_size_the_gateway_counts(self):
        """Base64 is a third bigger: an image just under the cap in bytes is not touched."""
        raw = noise_image((300, 200))
        a = attachment(raw)
        self.assertGreater(len(a["content"]), len(raw))
        self.assertIs(_fit_image_attachments([a], len(raw))[0], a)

    def test_exif_orientation_is_applied_before_the_exif_is_dropped(self):
        """A phone shot stored landscape with orientation 6 arrives upright."""
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 CW to display
        raw = noise_image((1600, 1200), "JPEG", exif=exif.tobytes())
        [out] = _fit_image_attachments([attachment(raw, "image-1.jpg", "image/jpeg")],
                                       len(raw) // 2)
        img = Image.open(io.BytesIO(base64.b64decode(out["content"])))
        self.assertLess(img.width, img.height)
        self.assertNotEqual(img.getexif().get(0x0112), 6)

    def test_a_transparent_screenshot_is_flattened_on_white(self):
        img = Image.effect_noise((1600, 1200), 60).convert("RGBA")
        img.putalpha(128)
        out = io.BytesIO()
        img.save(out, "PNG")
        self.assertGreater(out.tell(), self.CAP)
        [fit] = _fit_image_attachments([attachment(out.getvalue())], self.CAP)
        self.assertEqual(Image.open(io.BytesIO(base64.b64decode(fit["content"]))).mode, "RGB")

    def test_without_pillow_the_user_gets_one_plain_sentence(self):
        raw = noise_image((1600, 1200))
        with mock.patch.object(openclaw_pipe, "_load_pil", lambda: None):
            with self.assertRaises(ImageTooLargeError) as caught:
                _fit_image_attachments([attachment(raw)], self.CAP)
        msg = str(caught.exception)
        self.assertIn("Couldn't send the image", msg)
        self.assertIn("Pillow is not installed", msg)
        self.assertNotIn("exceeds size limit", msg)

    def test_an_image_that_cannot_fit_readably_is_refused_plainly(self):
        raw = noise_image((1600, 1200))
        with self.assertRaises(ImageTooLargeError) as caught:
            _fit_image_attachments([attachment(raw)], 2_000)
        self.assertIn("couldn't be made small enough", str(caught.exception))

    def test_bytes_pillow_cannot_read_are_refused_plainly(self):
        junk = attachment(b"\x00" * (self.CAP + 1))
        with self.assertRaises(ImageTooLargeError):
            _fit_image_attachments([junk], self.CAP)


class HelloImageCapTests(unittest.TestCase):
    def test_the_cap_comes_from_hello_ok(self):
        hello = {"policy": {"attachments": {"maxBytes": 20971520, "maxImageBytes": 1234567}}}
        self.assertEqual(_hello_max_image_bytes(hello), 1234567)

    def test_a_gateway_that_does_not_advertise_it_gets_the_built_in_cap(self):
        for hello in ({}, {"policy": {}}, {"policy": {"attachments": {"maxImageBytes": 0}}}):
            self.assertEqual(_hello_max_image_bytes(hello), GATEWAY_IMAGE_BYTES_FALLBACK)
        self.assertEqual(GATEWAY_IMAGE_BYTES_FALLBACK, 6291456)


if __name__ == "__main__":
    unittest.main()
