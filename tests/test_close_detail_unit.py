"""ELI-369: a dropped Gateway socket is logged with both close frames. websockets
reports code 1006 whenever no frame came back, also when the Pipe failed the
connection itself; the frame it sent says why (1009 too big, 1007 not UTF-8)."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

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


class _DroppingWS:
    """A Gateway socket whose next read raises `error`, as websockets does on a drop."""

    def __init__(self, error):
        self.error = error

    async def recv(self):
        raise self.error

    async def close(self):
        pass


class DisconnectHandlerTests(unittest.IsolatedAsyncioTestCase):
    """The read loop's own disconnect handler logs both frames, in the Pipe and in the
    Action (each built file carries its own copy of the loop)."""

    async def dropped(self, module, sent=None, rcvd=None):
        error = module.websockets.exceptions.ConnectionClosed()
        frames = closed(1006 if rcvd is None else rcvd[0], sent, rcvd)
        error.code, error.sent, error.rcvd = frames.code, frames.sent, frames.rcvd
        logs = []
        reconnected = asyncio.Event()

        async def reconnect(*a, **k):
            reconnected.set()
            await asyncio.Event().wait()   # stays down: the test only needs the drop's log
        conn = module._GatewayConnection(lambda: None)
        conn._ws = _DroppingWS(error)
        with mock.patch.object(module, "pipe_log", logs.append), \
                mock.patch.object(module._GatewayConnection, "_reconnect", reconnect):
            task = asyncio.ensure_future(conn._event_loop())
            try:
                for _ in range(500):
                    if any("WS disconnected" in line for line in logs):
                        break
                    await asyncio.sleep(0)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return [line for line in logs if "WS disconnected" in line]

    async def test_the_pipe_logs_the_frame_it_sent(self):
        self.assertEqual(await self.dropped(ocp, sent=(1009, "too big")),
                         ["WS disconnected -- reconnecting... (code 1006, sent 1009 'too big', received none)"])

    async def test_the_action_logs_the_frame_it_received(self):
        import openclaw_status_action as act
        self.assertEqual(await self.dropped(act, rcvd=(1012, "service restart")),
                         ["WS disconnected -- reconnecting... (code 1012, sent none, received 1012 'service restart')"])
