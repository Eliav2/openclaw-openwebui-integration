#!/usr/bin/env python3
"""Unit tests for channel threads (src/openclaw_pipe_pkg/channels.py): one
session per Open WebUI channel thread, the messages each turn carries, the
watcher that follows a thread, its judge, and the one-claim-per-message rule.

The Open WebUI database reads (`_channel_rows`, `_channel_message`,
`_channel_names`) are replaced by an in-memory channel; the query itself is
checked against a live Open WebUI in the PR's live test.
"""

import asyncio
import os
import tempfile
import types
import unittest
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe  # noqa: E402
from openclaw_pipe import (  # noqa: E402
    _channel_block,
    _channel_follow_up,
    _channel_id_of,
    _channel_mention_context,
    _channel_reply_text,
    _channel_run,
    _channel_session_key,
    _channel_state,
    _channel_trigger,
    _ChannelWatcher,
    _parse_channel_session_key,
    _sender_is_owner,
)

CID = "chan-1"
ROOT = "root-msg"
ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
MODEL = "openclaw_gateway.default"
S = 1_000_000_000  # one second in ns


def msg(mid, at, user=ALICE, text="hi", parent=None, bot=False, reply_to=None):
    return dict(id=mid, user_id=user, parent_id=parent, reply_to_id=reply_to, content=text,
                meta={"model_id": MODEL, "model_name": "OpenClaw"} if bot else {},
                created_at=at * S)


class FakeChannel:
    """The channel's messages, served the way `_channel_rows` filters them."""

    def __init__(self, messages):
        self.messages = list(messages)
        self.queries = []

    async def rows(self, channel_id, *, parent_id=None, top_level=False, after_ns=0, upto_ns=None,
                   parents=None):
        self.queries.append(dict(parent_id=parent_id, top_level=top_level, after_ns=after_ns,
                                 upto_ns=upto_ns, parents=None if parents is None else sorted(parents)))
        out = []
        for m in self.messages:
            if m["created_at"] <= after_ns or (upto_ns is not None and m["created_at"] > upto_ns):
                continue
            if parents is not None:
                ok = m["parent_id"] in parents
            elif top_level:
                ok = m["parent_id"] is None
            else:
                ok = m["parent_id"] == parent_id
            if ok:
                out.append(dict(m))
        return sorted(out, key=lambda m: m["created_at"])

    async def message(self, mid):
        for m in self.messages:
            if m["id"] == mid:
                return dict(m, channel_id=CID)
        return None

    async def names(self, ids):
        return {ALICE: "Alice", BOB: "Bob"}


class ChannelCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.chan = FakeChannel([])
        for name, fn in (("_channel_rows", self.chan.rows), ("_channel_message", self.chan.message),
                         ("_channel_names", self.chan.names)):
            p = mock.patch.object(openclaw_pipe, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def follow(self, root=ROOT, at=0, **extra):
        with _channel_state(self.tmp) as st:
            st["threads"][root] = dict(channel=CID, cursor=at * S, judged=at * S, active=at * S,
                                       model_id=MODEL, model_name="OpenClaw", **extra)

    def state(self):
        with _channel_state(self.tmp) as st:
            return st


class KeyTests(unittest.TestCase):
    def test_one_session_per_thread(self):
        key = _channel_session_key("main", CID, ROOT)
        self.assertEqual(key, f"agent:main:openwebui:channel:{CID}:thread:{ROOT}")
        self.assertEqual(_parse_channel_session_key(key), ("main", CID, ROOT))
        self.assertNotEqual(key, _channel_session_key("main", CID, "other-root"))

    def test_the_channel_id_comes_from_open_webuis_chat_id(self):
        self.assertEqual(_channel_id_of(f"channel:{CID}"), CID)
        self.assertIsNone(_channel_id_of("22222222-2222-2222-2222-222222222222"))
        self.assertIsNone(_channel_id_of(None))

    def test_a_chat_key_is_not_a_channel_key(self):
        self.assertIsNone(_parse_channel_session_key(f"agent:main:openwebui-{ALICE}-chat"))


class TriggerTests(unittest.TestCase):
    followed = {ROOT: {}}

    def trig(self, m, **kw):
        return _channel_trigger(m, followed=self.followed, **kw)

    def test_the_agents_own_message_triggers_nothing(self):
        self.assertIsNone(self.trig(msg("b", 1, parent=ROOT, bot=True)))

    def test_a_mention_is_open_webuis_to_send(self):
        m = msg("m", 1, parent=ROOT, text=f"<@M:{MODEL}|OpenClaw> what now?")
        self.assertEqual(self.trig(m), "answer")

    def test_a_reply_to_the_agent_is_open_webuis_to_send(self):
        m = msg("r", 1, parent=ROOT, reply_to="bot-msg")
        self.assertEqual(self.trig(m, bot_message_ids={"bot-msg"}), "answer")

    def test_a_message_in_a_followed_thread_goes_to_the_judge(self):
        self.assertEqual(self.trig(msg("t", 1, parent=ROOT)), "judge")

    def test_anything_else_is_context_for_the_next_turn(self):
        self.assertEqual(self.trig(msg("x", 1)), "context")
        self.assertEqual(self.trig(msg("y", 1, parent="unfollowed-thread")), "context")

    def test_a_user_mention_is_not_a_model_mention(self):
        m = msg("u", 1, parent=ROOT, text=f"<@U:{BOB}|Bob> look")
        self.assertEqual(self.trig(m), "judge")


class BlockTests(unittest.TestCase):
    def test_lines_carry_the_user_id(self):
        out = _channel_block([msg("a", 1, text="hello"), msg("b", 2, user=BOB, text="yo")],
                             {ALICE: "Alice", BOB: "Bob"}, 50, "Channel messages")
        self.assertIn(f"Alice ({ALICE}): hello", out)
        self.assertIn(f"Bob ({BOB}): yo", out)
        self.assertNotIn("not shown", out)

    def test_the_cap_keeps_the_newest_and_says_how_many_were_left_out(self):
        msgs = [msg(f"m{i}", i, text=f"n{i}") for i in range(60)]
        out = _channel_block(msgs, {ALICE: "Alice"}, 50, "Channel messages")
        self.assertIn("(10 earlier messages not shown)", out)
        self.assertNotIn(": n9\n", out)
        self.assertIn(": n10", out)
        self.assertIn(": n59", out)

    def test_mentions_read_as_their_label(self):
        out = _channel_block([msg("a", 1, text=f"<@M:{MODEL}|OpenClaw> hi")], {}, 50, "x")
        self.assertIn("@OpenClaw hi", out)
        self.assertNotIn("<@M:", out)


class ReplyTextTests(unittest.TestCase):
    def test_skip_posts_nothing(self):
        for t in ("[skip]", "  [skip]\n", "", None):
            self.assertIsNone(_channel_reply_text(t), t)

    def test_a_leading_skip_is_dropped_from_a_real_answer(self):
        self.assertEqual(_channel_reply_text("[skip] Sure, 3 pm."), "Sure, 3 pm.")

    def test_an_answer_is_posted_as_it_is(self):
        self.assertEqual(_channel_reply_text("Sure, 3 pm."), "Sure, 3 pm.")


class OwnerTests(unittest.IsolatedAsyncioTestCase):
    """Every channel turn goes out restricted when OWNER_USERS is set, the
    owner's included: one tool list per session."""

    def conn(self, owners):
        c = mock.MagicMock()
        c._valves = lambda: types.SimpleNamespace(OWNER_USERS=owners, AGENT_ID="main")
        return c

    async def test_a_channel_thread_is_restricted_even_for_the_owner(self):
        with mock.patch.object(openclaw_pipe, "_user_email", mock.AsyncMock(return_value="o@x.org")):
            self.assertFalse(await _sender_is_owner(self.conn(ALICE), _channel_session_key("main", CID, ROOT)))
            self.assertTrue(await _sender_is_owner(self.conn(ALICE), f"agent:main:openwebui-{ALICE}-chat"))

    async def test_no_owner_list_keeps_everyone_the_owner(self):
        self.assertTrue(await _sender_is_owner(self.conn(""), _channel_session_key("main", CID, ROOT)))


class MentionContextTests(ChannelCase):
    async def test_the_first_mention_sends_the_timeline_since_the_last_look(self):
        self.chan.messages = [
            msg("old", 1, text="before"),
            msg("talk", 5, user=BOB, text="lunch at 1?"),
            msg("bot-old", 6, bot=True, text="an old answer"),
            msg(ROOT, 7, text=f"<@M:{MODEL}|OpenClaw> book it"),
            msg("ph", 8, parent=ROOT, bot=True, text=""),
        ]
        with _channel_state(self.tmp) as st:
            st["channels"][CID] = {"cursor": 2 * S}
        ctx = await _channel_mention_context(self.tmp, CID, "ph", MODEL, 50)
        self.assertEqual(ctx["root"], ROOT)
        self.assertIn(f"Bob ({BOB}): lunch at 1?", ctx["text"])
        self.assertIn("@OpenClaw book it", ctx["text"])
        self.assertNotIn("before", ctx["text"])
        self.assertNotIn("an old answer", ctx["text"])  # the agent's own: its session has them
        self.assertEqual(self.state()["threads"], {})  # nothing is kept until the Gateway took it
        ctx["commit"]()
        st = self.state()
        self.assertEqual(st["channels"][CID]["cursor"], 8 * S)
        self.assertEqual(st["threads"][ROOT]["cursor"], 8 * S)
        self.assertEqual(st["threads"][ROOT]["model_id"], MODEL)

    async def test_a_timeline_message_after_a_mention_is_recalled_at_the_next_mention(self):
        self.chan.messages = [msg(ROOT, 1, text="@OpenClaw a"), msg("ph", 2, parent=ROOT, bot=True)]
        (await _channel_mention_context(self.tmp, CID, "ph", MODEL, 50))["commit"]()
        self.chan.messages += [msg("side", 3, user=BOB, text="unmentioned note"),
                               msg("root2", 4, text="@OpenClaw b"), msg("ph2", 5, parent="root2", bot=True)]
        ctx = await _channel_mention_context(self.tmp, CID, "ph2", MODEL, 50)
        self.assertEqual(ctx["root"], "root2")
        self.assertIn("unmentioned note", ctx["text"])
        self.assertNotIn(": @OpenClaw a", ctx["text"])

    async def test_a_second_call_for_the_same_reply_sends_the_same_text(self):
        self.chan.messages = [msg("talk", 1, user=BOB, text="dinner?"), msg(ROOT, 2, text="@OpenClaw book"),
                              msg("ph", 3, parent=ROOT, bot=True)]
        first = await _channel_mention_context(self.tmp, CID, "ph", MODEL, 50)
        first["commit"]()
        again = await _channel_mention_context(self.tmp, CID, "ph", MODEL, 50)
        self.assertEqual(again["text"], first["text"])
        self.assertEqual(again["root"], ROOT)

    async def test_a_mention_in_a_followed_thread_sends_the_thread_since_the_last_turn(self):
        self.follow(at=2)
        self.chan.messages = [msg(ROOT, 1), msg("t-old", 2, parent=ROOT, text="seen"),
                              msg("t-new", 3, parent=ROOT, user=BOB, text="new in thread"),
                              msg("timeline", 3, text="main timeline"),
                              msg("m", 4, parent=ROOT, text="@OpenClaw ?"),
                              msg("ph", 5, parent=ROOT, bot=True)]
        ctx = await _channel_mention_context(self.tmp, CID, "ph", MODEL, 50)
        self.assertIn("new in thread", ctx["text"])
        self.assertNotIn("seen", ctx["text"])
        self.assertNotIn("main timeline", ctx["text"])


class FakeRunConn:
    """A Gateway connection for one follow-up run: the chat.send answer, then
    the run's events into the registered queue."""

    def __init__(self, status="started", events=(), companion=None, active=()):
        self.status, self.events, self.companion = status, list(events), companion
        self.active = list(active)  # hasActiveRun answers, in order; then idle
        self.requests = []
        self._valves = lambda: types.SimpleNamespace(OWNER_USERS="", AGENT_ID="main")
        self.queues = {}
        self.changed = asyncio.Event()

    def send_section(self, key):
        return _AsyncNull()

    def session_change_event(self, key):
        self.changed = asyncio.Event()
        if self.active:  # the next read sees the next state
            self.changed.set()
        return self.changed

    def register_consumer(self, key, run_id):
        q = asyncio.Queue()
        self.queues[run_id] = q
        return q

    def unregister_consumer(self, key, run_id, queue=None):
        self.queues.pop(run_id, None)

    async def send_request(self, method, params, timeout=10):
        self.requests.append((method, params))
        if method == "sessions.companion.ask":
            if isinstance(self.companion, Exception):
                raise self.companion
            return {"answer": self.companion}
        if method == "sessions.list":
            if self.active and isinstance(self.active[0], Exception):
                raise self.active.pop(0)
            busy = self.active.pop(0) if self.active else False
            return {"sessions": [{"key": k, "hasActiveRun": busy}
                                 for k in ("k", _channel_session_key("main", CID, ROOT))]}
        if method == "chat.send":
            if self.status == "started":
                q = self.queues[params["idempotencyKey"]]
                for e in self.events:
                    q.put_nowait(e)
            return {"runId": params["idempotencyKey"], "status": self.status}
        return {}

    async def session_history(self, key, limit=10):
        return {"messages": []}

    def methods(self):
        """The requests, without the idle checks before a send."""
        return [m for m, _ in self.requests if m != "sessions.list"]


class _AsyncNull:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def delta(t):
    return {"payload": {"stream": "assistant", "data": {"delta": t}}}


def final(t):
    return {"payload": {"state": "final", "message": {"role": "assistant", "content": t}}}


class RunTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_the_sender_the_gateway_answers_started_gets_the_run(self):
        mine = FakeRunConn("started", [delta("Su"), delta("re"), final("Sure")])
        self.assertEqual(await _channel_run(mine, "k", "m", "chanfollow-x", owner=True), ("started", "Sure"))
        for other in ("in_flight", "ok", None):
            dup = FakeRunConn(other)
            got = await asyncio.wait_for(_channel_run(dup, "k", "m", "chanfollow-x", owner=True), 2)
            self.assertEqual(got, ("taken", None), other)

    async def test_a_busy_thread_is_waited_for_before_the_send(self):
        conn = FakeRunConn("started", [final("Sure")], active=[True, True, False])
        self.assertEqual(await asyncio.wait_for(
            _channel_run(conn, "k", "m", "chanfollow-x", owner=True), 2), ("started", "Sure"))
        methods = [m for m, _ in conn.requests]
        self.assertEqual(methods, ["sessions.list", "sessions.list", "sessions.list", "chat.send"])

    async def test_a_turn_that_meanwhile_carried_the_messages_means_no_send(self):
        conn = FakeRunConn("started", [final("Sure")], active=[True, False])
        got = await _channel_run(conn, "k", "m", "chanfollow-x", owner=True, still_needed=lambda: False)
        self.assertEqual(got, ("covered", None))
        self.assertNotIn("chat.send", [m for m, _ in conn.requests])

    async def test_no_send_when_the_gateway_cannot_say_whether_the_thread_is_busy(self):
        conn = FakeRunConn("started", [final("Sure")], active=[Exception("down")])
        self.assertEqual(await _channel_run(conn, "k", "m", "chanfollow-x", owner=True), ("failed", None))
        self.assertNotIn("chat.send", [m for m, _ in conn.requests])

    async def test_a_run_that_never_ends_gives_up_instead_of_hanging(self):
        conn = FakeRunConn("started", [delta("Half")])
        with mock.patch.object(openclaw_pipe, "_CHANNEL_RUN_TIMEOUT_S", 0.05):
            got = await asyncio.wait_for(_channel_run(conn, "k", "m", "chanfollow-x", owner=True), 2)
        self.assertEqual(got, ("failed", None))
        self.assertEqual(conn.queues, {})

    async def test_an_unreadable_transcript_at_the_end_keeps_the_streamed_reply(self):
        settle = {"type": openclaw_pipe._ROW_MARKER_TYPE, "row": {"hasActiveRun": False, "lastRunId": "chanfollow-x"}}
        conn = FakeRunConn("started", [delta("Boo"), delta("ked."), settle])
        conn.session_history = mock.AsyncMock(side_effect=openclaw_pipe.GatewayError("reconnecting"))
        got = await asyncio.wait_for(_channel_run(conn, "k", "m", "chanfollow-x", owner=True), 2)
        self.assertEqual(got, ("started", "Booked."))
        quiet = FakeRunConn("started", [settle])
        quiet.session_history = mock.AsyncMock(side_effect=openclaw_pipe.GatewayError("reconnecting"))
        got = await asyncio.wait_for(_channel_run(quiet, "k", "m", "chanfollow-x", owner=True), 2)
        self.assertEqual(got, ("failed", None))

    async def test_a_failed_run_gives_no_text(self):
        conn = FakeRunConn("started", [delta("[sk"), {"payload": {"state": "error"}}])
        self.assertEqual(await _channel_run(conn, "k", "m", "chanfollow-x", owner=True), ("failed", None))


class FollowUpTests(ChannelCase):
    def setUp(self):
        super().setUp()
        self.follow(at=2)
        self.chan.messages = [msg(ROOT, 1), msg("ph", 2, parent=ROOT, bot=True, text="Booked."),
                              msg("q", 10, parent=ROOT, user=BOB, text="and for 4 people?")]
        self.posted = []

    async def post(self, *args):
        self.posted.append(args)
        return "new-id"

    async def follow_up(self, conn, **kw):
        valves = types.SimpleNamespace(AGENT_ID="main", CHANNEL_CONTEXT_LIMIT=50)
        thread = self.state()["threads"][ROOT]
        return await _channel_follow_up(conn, valves, self.tmp, ROOT, thread, [self.chan.messages[-1]],
                                        post=self.post, **kw)

    async def test_judge_yes_runs_a_turn_and_posts_its_answer(self):
        conn = FakeRunConn("started", [final("Yes, 4 people.")], companion="YES")
        self.assertEqual(await self.follow_up(conn), "posted")
        self.assertEqual(conn.methods(), ["sessions.companion.ask", "chat.send"])
        self.assertEqual(len(self.posted), 1)
        channel_id, root, user, model_id, _name, text = self.posted[0]
        self.assertEqual((channel_id, root, user, model_id, text), (CID, ROOT, BOB, MODEL, "Yes, 4 people."))
        send = dict(conn.requests)["chat.send"]
        self.assertEqual(send["idempotencyKey"], "chanfollow-q")
        self.assertEqual(send["sessionKey"], _channel_session_key("main", CID, ROOT))
        self.assertIn(f"Bob ({BOB}): and for 4 people?", send["message"])
        self.assertEqual(self.state()["threads"][ROOT]["cursor"], 10 * S)

    async def test_judge_no_runs_nothing_and_keeps_the_message_for_the_next_turn(self):
        conn = FakeRunConn("started", [final("never")], companion="NO")
        self.assertEqual(await self.follow_up(conn), "judged-no")
        self.assertEqual(conn.methods(), ["sessions.companion.ask"])
        self.assertEqual(self.posted, [])
        t = self.state()["threads"][ROOT]
        self.assertEqual(t["judged"], 10 * S)
        self.assertEqual(t["cursor"], 2 * S)  # the next turn still carries it

    async def test_no_judge_falls_back_to_the_full_turn_and_never_asks_again(self):
        for err in (Exception("utility model unavailable"), Exception("question limit reached"),
                    Exception("UNAVAILABLE: Side chat is answering another question"),
                    asyncio.TimeoutError()):
            self.posted.clear()
            self.follow(at=2)  # a fresh thread each time
            conn = FakeRunConn("started", [final("[skip]")], companion=err)
            self.assertEqual(await self.follow_up(conn), "skip")
            self.assertEqual(conn.methods(), ["sessions.companion.ask", "chat.send"], err)

    async def test_an_answer_that_is_neither_yes_nor_no_falls_back_to_the_full_turn(self):
        conn = FakeRunConn("started", [final("Sure.")], companion="maybe")
        self.assertEqual(await self.follow_up(conn), "posted")

    async def test_skip_posts_nothing_even_after_partial_text(self):
        conn = FakeRunConn("started", [delta("[sk"), delta("ip]"), final("[skip]")], companion="YES")
        self.assertEqual(await self.follow_up(conn), "skip")
        self.assertEqual(self.posted, [])

    async def test_a_turn_cut_off_midway_posts_nothing(self):
        conn = FakeRunConn("started", [delta("Half an ans"), {"payload": {"state": "aborted"}}],
                           companion="YES")
        self.assertEqual(await self.follow_up(conn), "failed")
        self.assertEqual(self.posted, [])

    async def test_a_message_another_sender_claimed_is_not_posted_twice(self):
        conn = FakeRunConn("in_flight", companion="YES")
        self.assertEqual(await self.follow_up(conn), "taken")
        self.assertEqual(self.posted, [])

    async def test_the_judge_only_asks_and_never_starts_a_turn_in_the_thread(self):
        conn = FakeRunConn("started", companion="NO")
        await self.follow_up(conn)
        (method, params), = [r for r in conn.requests if r[0] != "sessions.list"]
        self.assertEqual(method, "sessions.companion.ask")
        self.assertLessEqual(len(params["question"]), 400)
        self.assertIn("the assistant OpenClaw reply", params["question"])  # the model's name, not ours
        long = openclaw_pipe._channel_judge_question("x" * 500)
        self.assertLessEqual(len(long), 400)
        self.assertIn("should the assistant reply", openclaw_pipe._channel_judge_question(""))
        self.assertIn("and for 4 people?", params["selectionContext"])
        self.assertLessEqual(len(params["selectionContext"]), 16000)

    async def test_a_restricted_thread_asks_the_judge_on_the_restricted_connection(self):
        restricted = FakeRunConn("started", [final("ok")], companion="YES")
        conn = FakeRunConn("started", companion="YES")
        conn._valves = lambda: types.SimpleNamespace(OWNER_USERS=ALICE, AGENT_ID="main")
        restricted.queues = conn.queues  # the run's events reach the main connection's listener
        with mock.patch.object(openclaw_pipe, "_get_restricted_connection",
                               mock.AsyncMock(return_value=restricted)):
            self.assertEqual(await self.follow_up(conn), "posted")
        self.assertEqual([m for m in conn.methods() if m != "chat.send"], [])
        self.assertIn("sessions.companion.ask", restricted.methods())


class WatcherTests(ChannelCase):
    def watcher(self, now, debounce=15, idle=24):
        valves = types.SimpleNamespace(STATE_DIR=self.tmp, CHANNEL_THREAD_DEBOUNCE_S=debounce,
                                       CHANNEL_THREAD_IDLE_HOURS=idle, AGENT_ID="main")
        w = _ChannelWatcher(lambda: valves, conn_getter=mock.AsyncMock(), now=lambda: now * S)
        w.handled = []

        async def handle(valves, state_dir, root, thread, pending):
            w.handled.append((root, [m["id"] for m in pending]))
            w.busy.discard(root)
        w._handle = handle
        return w

    async def tick(self, w):
        followed = await w.tick()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return followed

    async def test_no_followed_thread_means_no_query(self):
        w = self.watcher(100)
        self.assertIsNone(await self.tick(w))
        self.assertEqual(self.chan.queries, [])

    async def test_the_query_is_bounded_to_followed_threads_since_the_last_judged_message(self):
        self.follow(at=5)
        self.follow(root="root-2", at=8)
        await self.tick(self.watcher(100))
        (q,) = self.chan.queries
        self.assertEqual(q["parents"], sorted([ROOT, "root-2"]))
        self.assertEqual(q["after_ns"], 5 * S)

    async def test_a_burst_waits_for_the_debounce_then_gets_one_turn(self):
        self.follow(at=5)
        self.chan.messages = [msg("a", 10, parent=ROOT, text="one"), msg("b", 12, parent=ROOT, text="two")]
        w = self.watcher(20)
        await self.tick(w)
        self.assertEqual(w.handled, [])  # 8 s since the newest: wait
        w.now = lambda: 28 * S
        await self.tick(w)
        self.assertEqual(w.handled, [(ROOT, ["a", "b"])])

    async def test_zero_debounce_decides_at_once(self):
        self.follow(at=5)
        self.chan.messages = [msg("a", 10, parent=ROOT)]
        w = self.watcher(10, debounce=0)
        await self.tick(w)
        self.assertEqual(w.handled, [(ROOT, ["a"])])

    async def test_the_agents_own_messages_mentions_and_replies_to_it_are_not_judged(self):
        self.follow(at=5)
        self.chan.messages = [msg("bot", 6, parent=ROOT, bot=True, text="Booked."),
                              msg("men", 7, parent=ROOT, text=f"<@M:{MODEL}|OpenClaw> and?"),
                              msg("rep", 8, parent=ROOT, reply_to="bot", text="thanks"),
                              msg("talk", 9, parent=ROOT, user=BOB, text="ok then")]
        w = self.watcher(100)
        await self.tick(w)
        self.assertEqual(w.handled, [(ROOT, ["talk"])])

    async def test_a_thread_being_handled_is_not_handled_twice(self):
        self.follow(at=5)
        self.chan.messages = [msg("a", 10, parent=ROOT)]
        w = self.watcher(100)
        w.busy.add(ROOT)
        await self.tick(w)
        self.assertEqual(w.handled, [])

    async def test_another_copy_of_the_pipe_leaves_this_copys_threads_alone(self):
        self.follow(at=5, function="openclaw_gateway_private")
        self.chan.messages = [msg("a", 10, parent=ROOT)]
        w = self.watcher(100)
        w.function_id = "openclaw_gateway"
        self.assertIsNone(await self.tick(w))
        self.assertEqual(w.handled, [])
        w.function_id = "openclaw_gateway_private"
        await self.tick(w)
        self.assertEqual(w.handled, [(ROOT, ["a"])])

    async def test_an_idle_thread_is_no_longer_followed(self):
        self.follow(at=0)
        w = self.watcher(25 * 3600)
        self.assertIsNone(await self.tick(w))
        self.assertEqual(self.state()["threads"], {})

    async def test_the_watcher_stops_when_nothing_is_followed(self):
        w = self.watcher(100)
        w.function_state = mock.AsyncMock(return_value="active")
        await asyncio.wait_for(w.run(), 1)


class MentionTurnTests(ChannelCase):
    """Through the Pipe's real request path: what a channel mention sends."""

    async def asyncSetUp(self):
        self.conn = openclaw_pipe._GatewayConnection(lambda: None)
        self.conn._ws = object()
        self.sent = []
        self.events = []

        async def send_request(method, params, timeout=10):
            if method == "chat.send":
                self.sent.append(params)
                return {"runId": f"run-{len(self.sent)}"}
            if method == "sessions.describe":
                return {"session": {"status": "done", "agentRuntime": {"id": None}}}
            if method == "sessions.list":
                return {"sessions": [{"key": self.key, "hasActiveRun": False}]}
            return {}

        async def get_conn(_valves):
            return self.conn

        self.conn.send_request = send_request
        self.conn.model_patch_cached = lambda *a, **k: True
        self.watchers = []
        for p in (mock.patch.object(openclaw_pipe, "_get_gateway_connection", get_conn),
                  mock.patch.object(openclaw_pipe, "_build_usage_status_lines", mock.AsyncMock(return_value=[])),
                  mock.patch.object(openclaw_pipe, "_ensure_composer_watcher", lambda *a: None),
                  mock.patch.object(openclaw_pipe, "_ensure_channel_watcher",
                                    lambda *a: self.watchers.append(1))):
            p.start()
            self.addCleanup(p.stop)
        self.pipe = openclaw_pipe.Pipe()
        self.pipe.valves.AUTO_TITLE = False
        self.pipe.valves.STATE_DIR = self.tmp
        self.key = _channel_session_key(self.pipe.valves.AGENT_ID, CID, ROOT)
        self.chan.messages = [msg("talk", 1, user=BOB, text="dinner at 8?"),
                              msg(ROOT, 2, text=f"<@M:{MODEL}|OpenClaw> book it"),
                              msg("ph", 3, parent=ROOT, bot=True, text="")]

    async def turn(self, user=ALICE, reply="ph"):
        body = {"model": MODEL, "messages": [{"role": "user", "content": "Alice: @OpenClaw book it"}]}
        meta = {"chat_id": f"channel:{CID}", "session_id": f"channel:{CID}", "user_id": user,
                "message_id": reply}

        async def emitter(event):
            self.events.append(event)

        gen = self.pipe._pipe_impl(body, emitter, __metadata__=meta)
        first = asyncio.ensure_future(gen.__anext__())
        n = len(self.sent) + 1
        for _ in range(400):
            if f"{self.key}:run-{n}" in self.conn._consumers:
                break
            await asyncio.sleep(0.005)
        q = self.conn._consumers[f"{self.key}:run-{n}"].queues[0]
        q.put_nowait({"type": "event", "event": "agent", "payload": {
            "sessionKey": self.key, "runId": f"run-{n}", "stream": "assistant", "data": {"delta": "Booked."}}})
        q.put_nowait({"type": "event", "event": "chat", "payload": {
            "sessionKey": self.key, "runId": f"run-{n}", "state": "final"}})
        out = [await asyncio.wait_for(first, 30)]
        async for chunk in gen:
            out.append(chunk)
        return out

    async def test_a_mention_runs_in_the_threads_session_with_the_channel_so_far(self):
        await self.turn()
        (send,) = self.sent
        self.assertEqual(send["sessionKey"], self.key)
        self.assertIn(f"Bob ({BOB}): dinner at 8?", send["message"])
        self.assertIn(f"Alice ({ALICE}): @OpenClaw book it", send["message"])
        self.assertNotIn("Alice: @OpenClaw book it", send["message"])  # Open WebUI's own wrapping
        self.assertEqual(self.state()["threads"][ROOT]["cursor"], 3 * S)
        self.assertTrue(self.watchers)

    async def test_a_channel_gets_no_chat_page_hooks_and_no_composer_button(self):
        await self.turn()
        kinds = [e.get("type") for e in self.events]
        self.assertNotIn("execute", kinds)

    async def test_two_people_in_one_thread_share_its_session(self):
        await self.turn()
        self.chan.messages += [msg("m2", 4, parent=ROOT, user=BOB, text="@OpenClaw for 4"),
                               msg("ph2", 5, parent=ROOT, bot=True)]
        await self.turn(user=BOB, reply="ph2")
        self.assertEqual(self.sent[1]["sessionKey"], self.key)
        self.assertIn("for 4", self.sent[1]["message"])
        self.assertNotIn("dinner at 8?", self.sent[1]["message"])  # already seen


if __name__ == "__main__":
    unittest.main()
