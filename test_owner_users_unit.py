"""Owner users (OWNER_USERS valve): a user not on the list reaches the Gateway
over a second connection without operator.admin, so the Gateway does not
treat that user's turns as the owner's and withholds owner-only tools.

Run: python3 -m unittest test_owner_users_unit
"""
import ast
import asyncio
import hashlib
import json
import sys
import tempfile
import types
import glob
import os
import unittest
from unittest import mock

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)

import openclaw_pipe
from openclaw_pipe import Pipe, GATEWAY_SCOPES, _sign_challenge

HERE = os.path.dirname(os.path.abspath(__file__))
RESTRICTED = ["operator.read", "operator.write"]
# Every request that starts, steers or ends a run. Sent on the admin
# connection, such a request makes the turn the owner's.
RUN_METHODS = {"chat.send", "chat.abort", "sessions.patch"}


class OwnerUsersValveTests(unittest.TestCase):
    def test_the_valve_exists_and_is_empty_by_default(self):
        self.assertEqual(Pipe.Valves().OWNER_USERS, "")

    def test_parse_ids_and_emails(self):
        owners = openclaw_pipe._parse_owner_users(" U-1 , Someone@Example.org,,u-2 ")
        self.assertEqual(owners, frozenset({"u-1", "u-2", "someone@example.org"}))

    def test_empty_means_everyone_is_an_owner(self):
        owners = openclaw_pipe._parse_owner_users("")
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "anyone", "x@y.z"))

    def test_match_by_id_or_email_case_insensitively(self):
        owners = openclaw_pipe._parse_owner_users("u-1, owner@example.org")
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "U-1", None))
        self.assertTrue(openclaw_pipe._is_owner_user(owners, "u-9", "Owner@Example.org"))
        self.assertFalse(openclaw_pipe._is_owner_user(owners, "u-9", "guest@example.org"))
        self.assertFalse(openclaw_pipe._is_owner_user(owners, None, None))


class SignedScopesTests(unittest.TestCase):
    """The Gateway checks the scope list inside the signed device payload, so
    a connection must sign exactly the scopes it requests."""

    IDENT = {"id": "dev-1", "publicKey": "pub", "privateKey": "pem"}

    def _signed_payload(self, **kw):
        seen = []

        class Key:
            def sign(self, data):
                seen.append(data.decode())
                return b"sig"
        with mock.patch.object(openclaw_pipe.serialization, "load_pem_private_key",
                               lambda *a, **k: Key()):
            _sign_challenge(self.IDENT, "n1", 7, token_str="tok", **kw)
        return seen[0]

    def _expected(self, scopes):
        return "|".join(["v2", "dev-1", "webchat", "cli", "operator", ",".join(scopes), "7", "tok", "n1"])

    def test_default_signs_todays_scopes(self):
        self.assertEqual(self._signed_payload(), self._expected(GATEWAY_SCOPES))

    def test_restricted_signs_read_write_only(self):
        self.assertEqual(self._signed_payload(scopes=RESTRICTED), self._expected(RESTRICTED))


class NoRunRequestBypassesTheRouterTests(unittest.TestCase):
    """A run request sent on the admin connection for a non-owner chat would
    make that turn the owner's. Every such request goes through
    `_run_request`, which picks the connection; none is sent anywhere else."""

    RUN_NAMES = {"RUN_SEND", "RUN_ABORT", "RUN_PATCH"}

    def _trees(self):
        for path in sorted(glob.glob(os.path.join(HERE, "src", "openclaw_pipe_pkg", "*.py"))):
            yield os.path.basename(path), ast.parse(open(path).read())

    def test_run_method_names_are_written_once(self):
        """The method strings exist only in the RUN_* constants' definition."""
        offenders, defined = [], []
        for name, tree in self._trees():
            definition = {id(x) for n in ast.walk(tree) if isinstance(n, ast.Assign)
                          and any(isinstance(t, ast.Tuple) and {getattr(e, "id", None) for e in t.elts} == self.RUN_NAMES
                                  for t in n.targets)
                          for x in ast.walk(n)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and node.value in RUN_METHODS:
                    (defined if id(node) in definition else offenders).append(f"{name}:{node.lineno} {node.value}")
        self.assertEqual(offenders, [], "run method strings outside the RUN_* definition")
        self.assertEqual(len(defined), 3)

    def test_run_constants_are_only_passed_to_the_router(self):
        """Every use of RUN_SEND/RUN_ABORT/RUN_PATCH is an argument of a
        `_run_request(...)` call (or the router's own comparison)."""
        offenders = []
        for name, tree in self._trees():
            ok = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_run_request":
                    ok.update(id(a) for a in n.args)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_run_request":
                    ok.update(id(x) for x in ast.walk(n))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Name) and node.id in self.RUN_NAMES
                        and isinstance(node.ctx, ast.Load) and id(node) not in ok):
                    offenders.append(f"{name}:{node.lineno} {node.id}")
        self.assertEqual(offenders, [], "run requests that bypass _run_request")

    def test_every_run_request_call_site_is_routed(self):
        """The 11 places that start, steer or end a run (PHASE-B-PLAN.md)."""
        calls = [f"{name}:{n.lineno}" for name, tree in self._trees() for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_run_request"]
        self.assertEqual(len(calls), 11, calls)



# ── What reaches the Gateway, byte for byte ─────────────────────────────────
# One harness for the code before and after OWNER_USERS: a fixed identity, a
# signer that hashes the payload, a socket that records every frame. The
# frames recorded on f7faa5f are `test_fixture_owner_users_empty_valve.json`
# (`python3 -c "import test_owner_users_unit as t; t.print_frames()"` there).

FIXED_IDENT = {"id": "dev-fixed", "publicKey": "pub-fixed", "privateKey": "pem-fixed"}
KEY = "agent:main:openwebui-11111111-1111-1111-1111-111111111111-22222222-2222-2222-2222-222222222222"


class _Key:
    def sign(self, data):
        return hashlib.sha256(data).digest()


class _FakeWS:
    def __init__(self, conn_ref):
        self.sent, self.conn_ref = [], conn_ref
        self.incoming = [json.dumps({"type": "event", "event": "connect.challenge",
                                     "payload": {"nonce": "n1", "ts": 7}}),
                         json.dumps({"type": "res", "id": "1", "ok": True, "payload": {}})]

    async def send(self, data):
        self.sent.append(data)
        req = json.loads(data)
        fut = self.conn_ref[0]._pending_reqs.get(req.get("id"))
        if fut is not None and not fut.done():
            fut.set_result({"ok": True})

    async def recv(self):
        return self.incoming.pop(0)


def capture_frames(make_conn, send, owner_users=""):
    """[(label, frame)] of a connect, a chat.send, a sessions.patch and an
    abort, with `send(conn, method, params)` doing the sending."""
    async def run():
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "identity.json"), "w") as fh:
                json.dump(FIXED_IDENT, fh)
            valves = types.SimpleNamespace(GATEWAY_URL="localhost:18789", GATEWAY_TOKEN="tok",
                                           STATE_DIR=tmp, DEVICE_IDENTITY="", AGENT_ID="main",
                                           OWNER_USERS=owner_users)
            ref = [None]
            ws = _FakeWS(ref)

            async def connect(*a, **k):
                return ws
            with mock.patch.object(openclaw_pipe.websockets, "connect", connect, create=True), \
                    mock.patch.object(openclaw_pipe.serialization, "load_pem_private_key",
                                      lambda *a, **k: _Key()):
                conn = make_conn(lambda: valves)
                ref[0] = conn
                await conn._connect_and_start()
                await send(conn, "chat.send", dict(sessionKey=KEY, message="hi", idempotencyKey="msg-1"))
                await send(conn, "sessions.patch", dict(key=KEY, model="m"))
                await conn.abort(KEY, "run-1")
            return [json.loads(f) for f in ws.sent]
    return asyncio.run(run())


def print_frames(path="test_fixture_owner_users_empty_valve.json"):
    """Run on the code before OWNER_USERS to record the fixture (the pipe logs
    to stdout, so the frames go to a file)."""
    frames = capture_frames(openclaw_pipe._GatewayConnection,
                            lambda conn, m, p: conn.send_request(m, p))
    with open(path, "w") as fh:
        json.dump(frames, fh, indent=1, sort_keys=True)
        fh.write("\n")


class EmptyValveParityTests(unittest.TestCase):
    """OWNER_USERS empty: the Gateway gets exactly what it got before."""

    def test_frames_match_the_code_before_the_valve(self):
        want = json.load(open(os.path.join(HERE, "test_fixture_owner_users_empty_valve.json")))
        got = capture_frames(openclaw_pipe._GatewayConnection,
                             lambda conn, m, p: openclaw_pipe._run_request(
                                 conn, {"chat.send": openclaw_pipe.RUN_SEND,
                                        "sessions.patch": openclaw_pipe.RUN_PATCH}[m], p))
        self.assertEqual(got, want)

    def test_no_second_connection_and_no_owui_lookup(self):
        made = []
        with mock.patch.object(openclaw_pipe, "_get_restricted_connection",
                               side_effect=lambda *a: made.append(1)), \
                mock.patch.object(openclaw_pipe, "_user_email", side_effect=AssertionError("looked up")):
            capture_frames(openclaw_pipe._GatewayConnection,
                           lambda conn, m, p: openclaw_pipe._run_request(conn, openclaw_pipe.RUN_SEND, p))
        self.assertEqual(made, [])


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """Which connection a run request goes out on."""

    OWNER = "11111111-1111-1111-1111-111111111111"
    GUEST = "33333333-3333-3333-3333-333333333333"

    def setUp(self):
        self.restricted = mock.AsyncMock()
        self.restricted.send_request.return_value = {"ok": True}
        p = mock.patch.object(openclaw_pipe, "_get_restricted_connection",
                              mock.AsyncMock(return_value=self.restricted))
        p.start()
        self.addCleanup(p.stop)
        self.emails = {self.OWNER: "owner@example.org", self.GUEST: "guest@example.org"}
        p2 = mock.patch.object(openclaw_pipe, "_user_email",
                               mock.AsyncMock(side_effect=lambda uid: self.emails.get(uid)))
        p2.start()
        self.addCleanup(p2.stop)

    def conn(self, owner_users):
        c = mock.MagicMock()
        c._valves = lambda: types.SimpleNamespace(OWNER_USERS=owner_users, AGENT_ID="main")
        c.send_request = mock.AsyncMock(return_value={"ok": True})
        return c

    def key(self, user, chat="22222222-2222-2222-2222-222222222222"):
        return f"agent:main:openwebui-{user}-{chat}"

    async def send(self, conn, key, **kw):
        await openclaw_pipe._run_request(conn, openclaw_pipe.RUN_SEND, {"sessionKey": key}, **kw)

    async def test_owner_by_id_uses_the_main_connection(self):
        c = self.conn(self.OWNER)
        await self.send(c, self.key(self.OWNER))
        c.send_request.assert_awaited_once()
        self.restricted.send_request.assert_not_awaited()

    async def test_owner_by_email_uses_the_main_connection(self):
        c = self.conn("Owner@Example.org")
        await self.send(c, self.key(self.OWNER))
        c.send_request.assert_awaited_once()

    async def test_a_guest_uses_the_restricted_connection(self):
        c = self.conn(f"{self.OWNER}, owner@example.org")
        await self.send(c, self.key(self.GUEST))
        c.send_request.assert_not_awaited()
        self.restricted.send_request.assert_awaited_once()

    async def test_a_guest_resume_abort_and_patch_are_restricted(self):
        """The writer's resume (`writer.py`), Stop and the model override
        carry the chat's session key; a guest chat's never leave on main."""
        c = self.conn(self.OWNER)
        k = self.key(self.GUEST)
        await openclaw_pipe._run_request(c, openclaw_pipe.RUN_SEND,
                                         {"sessionKey": k, "message": "continue", "idempotencyKey": "msg-x-resume"})
        await openclaw_pipe._run_request(c, openclaw_pipe.RUN_ABORT, {"sessionKey": k, "runId": "r"})
        await openclaw_pipe._run_request(c, openclaw_pipe.RUN_PATCH, {"key": k, "model": "m"})
        c.send_request.assert_not_awaited()
        self.assertEqual(self.restricted.send_request.await_count, 3)

    async def test_a_guest_send_leaves_out_the_admin_only_fields(self):
        """The Gateway refuses a chat.send carrying system provenance or
        originating route fields from a connection without operator.admin
        ("system provenance fields require admin scope"). An owner's send keeps
        the conversation metadata block; a guest's goes without it."""
        k_owner, k_guest = self.key(self.OWNER), self.key(self.GUEST)
        c = self.conn(self.OWNER)
        owner_params = openclaw_pipe._owui_chat_send_params(k_owner, "hi", "i1", "chat-1", self.OWNER)
        guest_params = openclaw_pipe._owui_chat_send_params(k_guest, "hi", "i2", "chat-2", self.GUEST)
        guest_params.update({f: "x" for f in openclaw_pipe.ADMIN_ONLY_SEND_FIELDS})
        await openclaw_pipe._run_request(c, openclaw_pipe.RUN_SEND, owner_params)
        await openclaw_pipe._run_request(c, openclaw_pipe.RUN_SEND, guest_params)
        sent_owner = c.send_request.await_args.args[1]
        sent_guest = self.restricted.send_request.await_args.args[1]
        self.assertIn("systemProvenanceReceipt", sent_owner)
        for f in openclaw_pipe.ADMIN_ONLY_SEND_FIELDS:
            self.assertNotIn(f, sent_guest)
        self.assertEqual({k: sent_guest[k] for k in ("sessionKey", "message", "idempotencyKey")},
                         {"sessionKey": k_guest, "message": "hi", "idempotencyKey": "i2"})
        self.assertIn("systemProvenanceReceipt", guest_params)  # caller's dict untouched

    async def test_a_key_without_a_user_is_not_the_owners(self):
        c = self.conn(self.OWNER)
        await self.send(c, "agent:main:somewhere-else")
        c.send_request.assert_not_awaited()
        self.restricted.send_request.assert_awaited_once()

    async def test_an_explicit_owner_flag_decides(self):
        """The title run's session is not the chat's; the caller passes the
        chat's owner status."""
        c = self.conn(self.OWNER)
        await self.send(c, "agent:title-gen:title-gen-abc", owner=False)
        await self.send(c, "agent:title-gen:title-gen-abc", owner=True)
        self.assertEqual((c.send_request.await_count, self.restricted.send_request.await_count), (1, 1))

    async def test_the_status_actions_connection_reads_the_pipes_valves(self):
        """The Status Action's own connection has no OWNER_USERS valve: the
        Pipe function's saved valves decide, and an unreadable one fails closed."""
        c = self.conn(None)
        c._valves = lambda: types.SimpleNamespace(AGENT_ID="main")
        fns = types.SimpleNamespace(
            get_functions_by_type=lambda t: [types.SimpleNamespace(id="openclaw_gateway")],
            get_function_valves_by_id=lambda fid: {"OWNER_USERS": self.OWNER})
        mod = types.ModuleType("open_webui.models.functions")
        mod.Functions = fns
        with mock.patch.dict(sys.modules, {"open_webui.models.functions": mod}):
            await self.send(c, self.key(self.GUEST))
            await self.send(c, self.key(self.OWNER))
        self.assertEqual((c.send_request.await_count, self.restricted.send_request.await_count), (1, 1))
        broken = types.SimpleNamespace(get_functions_by_type=lambda t: 1 / 0)
        mod.Functions = broken
        with mock.patch.dict(sys.modules, {"open_webui.models.functions": mod}):
            await self.send(c, self.key(self.OWNER))
        self.assertEqual(self.restricted.send_request.await_count, 2, "unreadable setting: not the owner's")


class RestrictedConnectionTests(unittest.TestCase):
    """The restricted connection's own identity, scopes and connect frame."""

    def test_connect_frame_requests_and_signs_read_write_only(self):
        with mock.patch.object(openclaw_pipe, "_generate_device_identity",
                               return_value={"id": "dev-restricted", "publicKey": "p", "privateKey": "k"}):
            frames = self._frames()
        self._check(frames)

    def _frames(self):
        return capture_frames(
            lambda v: openclaw_pipe._GatewayConnection(
                v, writes_turns=False, scopes=openclaw_pipe.RESTRICTED_SCOPES,
                identity_name="identity-restricted", requests_only=True),
            lambda conn, m, p: conn.send_request(m, p), owner_users="u")

    def _check(self, frames):
        connect = frames[0]["params"]
        self.assertEqual(connect["scopes"], RESTRICTED)
        self.assertNotIn("operator.admin", json.dumps(frames))
        payload = "|".join(["v2", connect["device"]["id"], "webchat", "cli", "operator",
                            ",".join(RESTRICTED), "7", "tok", "n1"]).encode()
        import base64
        self.assertEqual(base64.urlsafe_b64decode(connect["device"]["signature"] + "=="),
                         hashlib.sha256(payload).digest())
        self.assertNotEqual(connect["device"]["id"], FIXED_IDENT["id"], "its own identity, not the main one")

    def test_its_identity_files_are_separate_and_the_valve_identity_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            valves = types.SimpleNamespace(STATE_DIR=tmp, DEVICE_IDENTITY=json.dumps(FIXED_IDENT))
            c = openclaw_pipe._GatewayConnection(lambda: valves, writes_turns=False,
                                                 scopes=openclaw_pipe.RESTRICTED_SCOPES,
                                                 identity_name="identity-restricted", requests_only=True)
            self.assertEqual(os.path.basename(c._identity_path(valves)), "identity-restricted.json")
            self.assertEqual(os.path.basename(c._device_token_path(valves)), "device-token-restricted.json")
            with mock.patch.object(openclaw_pipe, "_generate_device_identity",
                                   return_value={"id": "dev-r", "publicKey": "p", "privateKey": "k"}):
                c._ensure_identity(valves)
            self.assertEqual(c._ident["id"], "dev-r")
            self.assertFalse(os.path.exists(os.path.join(tmp, "identity.json")))

    def test_the_factory_builds_a_read_write_requests_only_connection(self):
        async def run():
            with mock.patch.object(openclaw_pipe, "_restricted_connection", None), \
                    mock.patch.object(openclaw_pipe._GatewayConnection, "ensure_connected",
                                      mock.AsyncMock()):
                return await openclaw_pipe._get_restricted_connection(lambda: None)
        c = asyncio.run(run())
        self.assertEqual(c.scopes, RESTRICTED)
        self.assertNotIn("operator.admin", c.scopes)
        self.assertEqual((c._identity_name, c.requests_only, c.writes_turns),
                         ("identity-restricted", True, False))

    def test_the_main_connection_keeps_its_file_names(self):
        c = openclaw_pipe._GatewayConnection(lambda: None)
        v = types.SimpleNamespace(STATE_DIR="/x")
        self.assertEqual((c._identity_path(v), c._device_token_path(v)),
                         ("/x/identity.json", "/x/device-token.json"))
        self.assertEqual((c.scopes, c.requests_only), (GATEWAY_SCOPES, False))


if __name__ == "__main__":
    unittest.main()
