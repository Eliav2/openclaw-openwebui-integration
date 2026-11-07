"""ELI-369: a dropped Gateway socket is logged with both close frames. websockets
reports code 1006 whenever no frame came back, also when the Pipe failed the
connection itself; the frame it sent says why (1009 too big, 1007 not UTF-8)."""
import unittest
from types import SimpleNamespace

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402


def closed(code, sent=None, rcvd=None):
    """A ConnectionClosed as websockets builds one: `code`, and the `sent` and
    `rcvd` Close frames (code, reason) or None."""
    frame = lambda f: SimpleNamespace(code=f[0], reason=f[1]) if f else None
    return SimpleNamespace(code=code, sent=frame(sent), rcvd=frame(rcvd))


class CloseDetailTests(unittest.TestCase):

    def test_a_connection_this_side_failed_names_the_frame_it_sent(self):
        e = closed(1006, sent=(1009, "frame with 70000000 bytes exceeds limit"))
        self.assertEqual(ocp._close_detail(e),
                         "code 1006, sent 1009 'frame with 70000000 bytes exceeds limit', received none")

    def test_a_gateway_restart_names_the_frame_it_received(self):
        e = closed(1012, rcvd=(1012, "service restart"))
        self.assertEqual(ocp._close_detail(e), "code 1012, sent none, received 1012 'service restart'")

    def test_a_drop_with_no_frames_says_so(self):
        self.assertEqual(ocp._close_detail(closed(1006)), "code 1006, sent none, received none")


if __name__ == "__main__":
    unittest.main()
