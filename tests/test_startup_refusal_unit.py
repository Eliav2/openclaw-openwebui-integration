"""A Gateway that has just started refuses runs while it inspects and
prepares the agent's database (OpenClaw 2026.10.5+): the run request is sent
again, same key, after the wait the refusal names, and never shown as a reply.

Run: uv run --with-requirements requirements-dev.txt pytest -q tests/test_startup_refusal_unit.py
"""
import asyncio
import json
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402

KEY = "agent:main:openwebui-user-1-chat-1"
REASON = ("Agent main has not completed startup inspection and preparation. "
          "Background inspection is still running.")


def refusal(code="agent-database-inspection-pending", retryable=True, retry_after_ms=1):
    """The error shape of createAgentDatabaseAdmissionErrorShape (2026.10.5,
    agent-database-admission-BjKNx4ry.mjs:91)."""
    err = {"code": "UNAVAILABLE", "message": REASON, "retryable": retryable,
           "details": {"agentId": "main", "code": code, "reason": REASON}}
    if retryable:
        err["retryAfterMs"] = retry_after_ms
    return ocp.GatewayError(REASON, error=err)


class FakeConn:
    def __init__(self, answers):
        self.answers = list(answers)
        self.sent = []

    async def send_request(self, method, params, timeout=10):
        self.sent.append((method, dict(params)))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class StartupRefusalTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.logged = []
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: self.logged.append(a[0]))
        patch.start()
        self.addCleanup(patch.stop)

    PARAMS = {"sessionKey": KEY, "message": "hi", "idempotencyKey": "msg-1"}

    async def test_a_run_refused_while_the_gateway_prepares_is_sent_again(self):
        conn = FakeConn([refusal(), refusal(), {"runId": "run-1"}])
        got = await ocp._run_request(conn, ocp.RUN_SEND, dict(self.PARAMS), owner=True, timeout=30)
        self.assertEqual(got, {"runId": "run-1"})
        self.assertEqual([p for _m, p in conn.sent], [self.PARAMS] * 3, "the same request, same key")
        self.assertEqual(len([line for line in self.logged if "still preparing the agent" in line]), 1)

    async def test_the_wait_is_the_one_the_refusal_names(self):
        conn = FakeConn([refusal(retry_after_ms=250), {"runId": "run-1"}])
        slept = []

        async def sleep(s):
            slept.append(s)
        with mock.patch.object(ocp.asyncio, "sleep", sleep):
            await ocp._run_request(conn, ocp.RUN_SEND, dict(self.PARAMS), owner=True)
        self.assertEqual(slept, [0.25])

    async def test_any_other_refusal_is_raised_at_once(self):
        for err in (refusal(code="agent-database-inspection-failed", retryable=False),
                    refusal(retryable=False),
                    ocp.GatewayError("gateway connection not ready (reconnecting)")):
            with self.subTest(err=str(err.error)):
                conn = FakeConn([err, {"runId": "never"}])
                with self.assertRaises(ocp.GatewayError):
                    await ocp._run_request(conn, ocp.RUN_SEND, dict(self.PARAMS), owner=True)
                self.assertEqual(len(conn.sent), 1)

    def patch_failed(self):
        """2026.10.5's sessions.patch refusal while its session storage gets
        ready, just after preparation (sessions-mutations-6pl7o9sA.mjs:142)."""
        return ocp.GatewayError("Session patch failed unexpectedly. Retry the request.", error={
            "code": "UNAVAILABLE", "message": "Session patch failed unexpectedly. Retry the request.",
            "retryable": True})

    async def test_a_request_refused_as_retryable_after_preparation_is_sent_again(self):
        conn = FakeConn([refusal(), self.patch_failed(), {"ok": True}])
        slept = []

        async def sleep(s):
            slept.append(s)
        with mock.patch.object(ocp.asyncio, "sleep", sleep):
            got = await ocp._send_when_prepared(conn, "sessions.patch", {"key": KEY})
        self.assertEqual((got, len(conn.sent)), ({"ok": True}, 3))
        self.assertEqual(slept, [0.001, 0.25], "the named wait, then the startup refusal's 250 ms")
        self.assertEqual(len([line for line in self.logged if "refused as retryable" in line]), 1)

    async def test_a_refusal_that_stays_retryable_fails_the_request_sooner(self):
        conn = FakeConn([self.patch_failed()] * 1000)
        with mock.patch.object(ocp, "RETRYABLE_REFUSAL_WAIT_S", 0.05):
            with self.assertRaisesRegex(ocp.GatewayError, "Session patch failed"):
                await asyncio.wait_for(ocp._send_when_prepared(conn, "sessions.patch", {"key": KEY}), 5)

    async def test_a_gateway_that_never_finishes_preparing_fails_the_send(self):
        conn = FakeConn([refusal()] * 1000)
        with mock.patch.object(ocp, "STARTUP_REFUSAL_WAIT_S", 0.05):
            with self.assertRaises(ocp.GatewayError) as caught:
                await asyncio.wait_for(
                    ocp._run_request(conn, ocp.RUN_SEND, dict(self.PARAMS), owner=True), 5)
        self.assertIn("startup inspection", str(caught.exception))

    async def test_a_refused_response_frame_keeps_its_error_shape(self):
        """The `res` frame's error shape reaches the caller, not only its
        message: the retry reads `retryable`, `retryAfterMs` and details.code."""
        conn = ocp._GatewayConnection(lambda: None)
        fut = asyncio.get_running_loop().create_future()
        conn._pending_reqs["7"] = fut
        frames = [{"type": "res", "id": "7", "ok": False, "error": refusal(retry_after_ms=250).error}]

        class ScriptedWS:
            async def recv(self_):
                if frames:
                    return json.dumps(frames.pop(0))
                conn._stopped = True
                raise asyncio.CancelledError
        conn._ws = ScriptedWS()
        await conn._event_loop()
        with self.assertRaises(ocp.GatewayError) as caught:
            fut.result()
        self.assertEqual(str(caught.exception), REASON)
        self.assertEqual(ocp._startup_refusal_retry_ms(caught.exception), 250)
        self.assertIsNone(ocp._startup_refusal_retry_ms(ocp.GatewayError(REASON)))



    async def test_a_transcript_subscription_refused_while_preparing_is_renewed(self):
        """A renewal refused after a restart would leave the session without
        tool events until the next reconnect (the key stays recorded)."""
        conn = ocp._GatewayConnection(lambda: None)
        fake = FakeConn([refusal(), {}])
        conn.send_request = fake.send_request
        self.assertTrue(await conn._subscribe_messages(KEY))
        self.assertEqual([m for m, _p in fake.sent], ["sessions.messages.subscribe"] * 2)

def starting(retry_after_ms=500):
    """The connect refusal of a Gateway still starting (2026.10.5,
    message-handler-e9vaqREf.mjs:566 rejectGatewayStartupConnect)."""
    return ocp.GatewayError("gateway starting; retry shortly", error={
        "code": "UNAVAILABLE", "message": "gateway starting; retry shortly", "retryable": True,
        "retryAfterMs": retry_after_ms, "details": {"reason": "startup-sidecars"}})


class GatewayStartingTests(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.logged = []
        patch = mock.patch.object(ocp, "pipe_log", lambda *a, **k: self.logged.append(a[0]))
        patch.start()
        self.addCleanup(patch.stop)
        self.conn = ocp._GatewayConnection(lambda: None)
        self.slept = []

        async def sleep(s):
            self.slept.append(s)
        patch = mock.patch.object(ocp.asyncio, "sleep", sleep)
        patch.start()
        self.addCleanup(patch.stop)

    def connects(self, *outcomes):
        outcomes = list(outcomes)
        tries = []

        async def connect():
            tries.append(1)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.conn._connect_and_start = connect
        return tries

    async def test_a_starting_gateway_is_connected_to_again_after_the_wait_it_names(self):
        tries = self.connects(starting(), starting(), None)
        await self.conn._connect_once_started()
        self.assertEqual((len(tries), self.slept), (3, [0.5, 0.5]))
        self.assertEqual(len([line for line in self.logged if "Gateway is starting" in line]), 1)

    async def test_the_wait_is_bounded_as_openclaws_own_clients_bound_it(self):
        for asked, waited in ((5, 0.1), (60_000, 2.0), ("soon", 0.5)):
            with self.subTest(asked=asked):
                self.slept.clear()
                self.connects(starting(asked), None)
                await self.conn._connect_once_started()
                self.assertEqual(self.slept, [waited])

    async def test_any_other_connect_failure_is_raised_at_once(self):
        for err in (ocp.GatewayError("pairing required", error={"code": "NOT_PAIRED"}),
                    ocp.GatewayError("gateway starting; retry shortly",
                                     error={"code": "UNAVAILABLE", "retryable": False,
                                            "details": {"reason": "startup-sidecars"}})):
            with self.subTest(err=str(err)):
                tries = self.connects(err, None)
                with self.assertRaises(ocp.GatewayError):
                    await self.conn._connect_once_started()
                self.assertEqual(len(tries), 1)

    async def test_a_gateway_that_never_finishes_starting_fails_the_connect(self):
        self.connects(*[starting()] * 1000)
        with mock.patch.object(ocp, "STARTUP_REFUSAL_WAIT_S", 0):
            with self.assertRaises(ocp.GatewayError):
                await self.conn._connect_once_started()

if __name__ == "__main__":
    unittest.main()
