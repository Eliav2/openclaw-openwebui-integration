"""The in-chat map: the Gateway plugin `map` returns stops and routes, the Pipe turns the result
into one srcdoc embed that survives a reload."""
import asyncio
import hashlib
import json
import re
import types
import unittest
from pathlib import Path
from unittest import mock

import conftest  # noqa: F401  (imports real pydantic before the stubs can shadow it)

from _test_stubs import install_stubs  # noqa: E402

install_stubs()

import openclaw_pipe as ocp  # noqa: E402
import test_pipe_unit as T  # noqa: E402

MAP_ID = "0123456789abcdef"
STOPS = [
    {"n": 1, "label": "Fushimi Inari", "lat": 34.9671, "lon": 135.7727, "group": "Day 1", "place": "Fushimi Inari Taisha, Kyoto"},
    {"n": 2, "label": "Kiyomizu-dera", "lat": 34.9949, "lon": 135.785, "group": "Day 1", "note": "Go early"},
]


def geometry(**over):
    base = {"ok": True, "title": "Kyoto day 1", "stops": STOPS, "unresolved": [],
            "routes": [{"group": "Day 1", "mode": "walk", "distanceM": 5860.4, "durationS": 4689.9,
                        "polyline": ocp._encode_polyline([[34.9671, 135.7727], [34.98, 135.78], [34.9949, 135.785]])}]}
    return {**base, **over}


def card(name, call_id, text):
    # As the renderer builds it: the call id is the card's `id` (`_tool_call_started_event`).
    return [{"type": "function_call", "name": name, "id": call_id, "arguments": "{}", "status": "completed"},
            {"type": "function_call_output", "call_id": call_id, "output": [{"type": "output_text", "text": text}]}]


def result_text(map_id=MAP_ID):
    return json.dumps({"mapId": map_id, "stops": STOPS, "routes": [], "unresolved": [], "note": "n"})


class MapIdsTests(unittest.TestCase):
    def test_the_id_of_a_map_tool_result_is_found(self):
        self.assertEqual(ocp._map_ids(card("map", "c1", result_text())), [MAP_ID])

    def test_a_stored_card_with_call_id_counts_too(self):
        out = card("map", "c1", result_text())
        out[0] = {"type": "function_call", "name": "map", "call_id": "c1", "arguments": "{}"}
        self.assertEqual(ocp._map_ids(out), [MAP_ID])

    def test_other_tools_and_failed_calls_have_no_map(self):
        self.assertEqual(ocp._map_ids(card("exec", "c1", result_text())), [])
        self.assertEqual(ocp._map_ids(card("map", "c1", "A map needs at least one stop.")), [])

    def test_a_card_cut_at_the_limit_still_names_its_map(self):
        self.assertEqual(ocp._map_ids(card("map", "c1", result_text()[:60])), [MAP_ID])

    def test_the_mcp_prefixed_name_counts_and_a_repeat_is_one_map(self):
        out = card("mcp__openclaw__map", "c1", result_text()) + card("map", "c2", result_text())
        self.assertEqual(ocp._map_ids(out), [MAP_ID])

    def test_two_maps_keep_their_order(self):
        other = "fedcba9876543210"
        out = card("map", "c1", result_text()) + card("map", "c2", result_text(other))
        self.assertEqual(ocp._map_ids(out), [MAP_ID, other])


class MapScriptTests(unittest.TestCase):
    """The page script cannot run here (no browser): what it must do is pinned in its text, and
    the browser tests in e2e/test_browser_flows.py run it."""

    def js(self):
        h = ocp._map_html(geometry())
        return h[h.rindex("<script>"):]

    def test_a_map_that_does_not_load_in_time_is_disposed_and_the_drawing_stays(self):
        js = self.js()
        self.assertIn("map.remove()", js)
        self.assertNotIn("fb.style.display = 'none'", js[:js.index("'idle'")])

    def test_the_drawing_is_hidden_only_when_the_first_tiles_are_in(self):
        js = self.js()
        self.assertIn("map.once('idle'", js)
        self.assertIn("areTilesLoaded()", js)
        self.assertIn("tileErrors", js)

    def test_the_attribution_does_not_cover_the_foot_of_the_map(self):
        js = self.js()
        self.assertIn("maplibregl-compact-show", js)
        self.assertIn("bottom:", js)


class GeneratedTypesTests(unittest.TestCase):
    def test_the_types_load_on_python_3_10(self):
        text = (Path(__file__).resolve().parent.parent / "src" / "openclaw_pipe_pkg" / "mapspec.py").read_text()
        self.assertNotIn("from typing import Literal, NotRequired", text)
        self.assertIn("from typing_extensions import", text)

    def test_the_types_are_generated_from_the_schema_copy_in_the_repo(self):
        root = Path(__file__).resolve().parent.parent
        digest = hashlib.sha256((root / "schemas" / "map-spec.schema.json").read_bytes()).hexdigest()
        self.assertIn(f"# schema-sha256: {digest}\n", (root / "src" / "openclaw_pipe_pkg" / "mapspec.py").read_text())

    def test_the_artifact_has_the_types(self):
        self.assertTrue(hasattr(ocp, "MapGeometry"))


class PolylineTests(unittest.TestCase):
    def test_the_reference_vector(self):
        pts = [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]]
        self.assertEqual(ocp._decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"), pts)
        self.assertEqual(ocp._encode_polyline(pts), "_p~iF~ps|U_ulLnnqC_mqNvxq`@")


class MapHtmlTests(unittest.TestCase):
    def html(self, **over):
        return ocp._map_html(geometry(**over))

    def test_every_stop_is_listed_in_order_with_its_number_and_label(self):
        h = self.html()
        self.assertLess(h.index("Fushimi Inari"), h.index("Kiyomizu-dera"))
        self.assertEqual(len(re.findall(r'class="stop"', h)), 2)

    def test_each_stop_has_a_copy_button_with_its_coordinates_and_no_outbound_link(self):
        h = self.html()
        self.assertIn('data-coords="34.9671,135.7727"', h)
        self.assertNotIn("<a ", h)
        self.assertNotIn("window.open", h)
        self.assertNotIn("target=", h)

    def test_the_route_summary_reads_in_words(self):
        h = self.html()
        self.assertIn("5.9 km", h)
        self.assertIn("1 h 18 min", h)
        self.assertIn("walk", h)

    def test_the_library_is_pinned_with_an_integrity_hash(self):
        h = self.html()
        self.assertRegex(h, r'maplibre-gl/\d+\.\d+\.\d+/maplibre-gl\.js"[^>]*integrity="sha512-')
        self.assertIn("tiles.openfreemap.org", h)

    def test_two_finger_gestures_on_a_phone(self):
        self.assertIn("cooperativeGestures", self.html())

    def test_a_static_fallback_is_in_the_page_before_any_script_runs(self):
        h = self.html()
        svg = h[h.index('<svg id="fallback"'):h.index("</svg>")]
        self.assertEqual(len(re.findall(r"<circle", svg)), 2)
        self.assertIn("<polyline", svg)

    def test_the_fallback_shows_stops_alone_without_a_route(self):
        h = self.html(routes=[])
        self.assertNotIn("<polyline", h[h.index('<svg id="fallback"'):h.index("</svg>")])
        self.assertEqual(len(re.findall(r'class="stop"', h)), 2)

    def test_markup_in_a_label_is_inert(self):
        evil = [{**STOPS[0], "label": "</script><img src=x onerror=alert(1)>", "note": "<b>x</b>"}]
        h = self.html(stops=evil, title="<i>t</i>")
        self.assertNotIn("<img", h)
        self.assertNotIn("<b>x", h)
        self.assertNotIn("<i>t", h)
        self.assertEqual(h.count("</script>"), h.count("<script"))

    def test_unresolved_stops_are_said(self):
        h = self.html(unresolved=[{"n": 3, "label": "Atlantis", "reason": "not found"}])
        self.assertIn("Atlantis", h)
        self.assertIn("not found", h)

    def test_the_page_tells_its_height_to_the_chat(self):
        self.assertIn("iframe:height", self.html())

    def test_one_stop_is_a_map_too(self):
        h = self.html(stops=STOPS[:1], routes=[])
        self.assertEqual(len(re.findall(r'class="stop"', h)), 1)

    def test_a_group_has_a_legend_colour(self):
        two = [STOPS[0], {**STOPS[1], "group": "Day 2"}]
        h = self.html(stops=two, routes=[])
        self.assertIn("Day 1", h)
        self.assertIn("Day 2", h)

    def test_no_script_tag_inside_the_data_can_close_the_page(self):
        h = self.html(title="a </script> b")
        self.assertNotIn("a </script>", h)


class EmbedListTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_embeds_stay_and_new_pages_follow_without_a_repeat(self):
        self.assertEqual(ocp._merge_embeds(["https://live", "<old>"], ["<old>", "<new>"]),
                         ["https://live", "<old>", "<new>"])
        self.assertEqual(ocp._merge_embeds(None, ["<new>"]), ["<new>"])

    async def test_fields_for_a_rebuilt_reply_carry_its_map_and_what_was_saved(self):
        conn = FakeConn({MAP_ID: geometry()})
        fields = await ocp._embed_fields(conn, card("map", "c1", result_text()), ["https://live"])
        self.assertEqual(fields["embeds"][0], "https://live")
        self.assertEqual(len(fields["embeds"]), 2)

    async def test_no_map_no_fields(self):
        self.assertEqual(await ocp._embed_fields(FakeConn({}), card("exec", "c1", "x"), ["https://live"]), {})


class FakeConn:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    async def send_request(self, method, params, timeout=10):
        self.calls.append((method, params))
        reply = self.replies[params["mapId"]]
        if isinstance(reply, Exception):
            raise reply
        return reply


class MapEmbedsTests(unittest.TestCase):
    def run_embeds(self, conn, output, seen=None):
        return asyncio.run(ocp._map_embeds(conn, output, seen if seen is not None else {}))

    def test_a_map_is_fetched_once_and_built_once(self):
        conn = FakeConn({MAP_ID: geometry()})
        seen = {}
        out = card("map", "c1", result_text())
        first = self.run_embeds(conn, out, seen)
        again = self.run_embeds(conn, out, seen)
        self.assertEqual(len(first), 1)
        self.assertEqual(first, again)
        self.assertEqual(conn.calls, [("map.get", {"mapId": MAP_ID})])

    def test_an_expired_map_gives_no_embed(self):
        conn = FakeConn({MAP_ID: {"ok": False, "error": "unknown or expired map id"}})
        self.assertEqual(self.run_embeds(conn, card("map", "c1", result_text())), [])

    def test_a_failed_request_is_tried_again_on_the_next_call(self):
        conn = FakeConn({MAP_ID: RuntimeError("not ready")})
        seen = {}
        self.assertEqual(self.run_embeds(conn, card("map", "c1", result_text()), seen), [])
        conn.replies[MAP_ID] = geometry()
        self.assertEqual(len(self.run_embeds(conn, card("map", "c1", result_text()), seen)), 1)

    def test_no_map_no_request(self):
        conn = FakeConn({})
        self.assertEqual(self.run_embeds(conn, card("exec", "c1", "hi")), [])
        self.assertEqual(conn.calls, [])


class WriterTests(unittest.IsolatedAsyncioTestCase):
    """The turn writer owns the message's DB writes (update_db=False), so Open WebUI never saves
    an `embeds` event it emits: the embed has to ride in the writer's own writes to survive a reload."""

    def writer(self, output, seen=None):
        conn = ocp._GatewayConnection(lambda: types.SimpleNamespace(AGENT_ID="main"))
        w = ocp._TurnWriter(conn, session_key="agent:main:owui-u-c", chat_id="c1", user_id="u1",
                            run_id="run-1", message_id="m1")
        w.renderer.output = output
        w._map_seen = {MAP_ID: "<html>map</html>"} if seen is None else seen
        w._saved_embeds = mock.AsyncMock(return_value=[])
        return w

    async def test_every_write_of_the_message_carries_the_embed(self):
        saved = []

        async def upsert(chat_id, message_id, fields):
            saved.append(fields)
            return {"id": message_id}
        w = self.writer(card("map", "c1", result_text()))
        with mock.patch.object(ocp, "_owui_upsert_message", upsert):
            await w._persist(done=False)
            await w._persist(done=True)
        self.assertEqual([f["embeds"] for f in saved], [["<html>map</html>"]] * 2)

    async def test_a_reply_without_a_map_writes_no_embeds_field(self):
        saved = []

        async def upsert(chat_id, message_id, fields):
            saved.append(fields)
            return {"id": message_id}
        w = self.writer(card("exec", "c1", "hi"))
        with mock.patch.object(ocp, "_owui_upsert_message", upsert):
            await w._persist(done=True)
        self.assertNotIn("embeds", saved[0])

    async def test_the_final_push_to_open_tabs_keeps_the_embed(self):
        pushed = []

        async def introduce(chat_id, user_id, patch, *rest):
            pushed.append(patch)
        w = self.writer(card("map", "c1", result_text()))
        w._claim = {"messages": {"m1": {"id": "m1"}}, "parent": "p1", "how": "new"}
        with mock.patch.object(ocp, "_introduce_turn_message", introduce):
            await w._push_final("text", w.renderer.output, [])
        self.assertIn("<html>map</html>", json.dumps(pushed))

    @mock.patch.object(ocp, "_owui_upsert_message", mock.AsyncMock(return_value={"id": "m1"}))
    async def test_a_map_is_fetched_before_the_final_write_and_shown_to_the_open_tab(self):
        w = self.writer(card("map", "c1", result_text()), seen={})
        w.conn.send_request = mock.AsyncMock(return_value=geometry())
        emitted = []

        async def emit(event):
            emitted.append(event)
        w._emit = emit
        await w._show_maps()
        await w._show_maps()
        self.assertEqual([e["type"] for e in emitted], ["embeds"])
        self.assertEqual(len(emitted[0]["data"]["embeds"]), 1)
        self.assertEqual(w.conn.send_request.await_count, 1)

    async def test_saved_embeds_are_kept_when_the_writer_writes_the_list(self):
        saved = []

        async def upsert(chat_id, message_id, fields):
            saved.append(fields)
            return {"id": message_id}
        w = self.writer(card("map", "c1", result_text()))
        w._saved_embeds = mock.AsyncMock(return_value=["https://live", "<old map>"])
        with mock.patch.object(ocp, "_owui_upsert_message", upsert):
            await w._persist(done=True)
        self.assertEqual(saved[0]["embeds"], ["https://live", "<old map>", "<html>map</html>"])

    async def test_a_failed_embed_emission_is_logged_and_tried_again_not_raised(self):
        w = self.writer(card("map", "c1", result_text()), seen={})
        w.conn.send_request = mock.AsyncMock(return_value=geometry())
        w._saved_embeds = mock.AsyncMock(return_value=[])
        calls = []

        async def emit(event):
            calls.append(event)
            if len(calls) == 1:
                raise RuntimeError("socket gone")
        w._emit = emit
        with mock.patch.object(ocp, "_owui_upsert_message", mock.AsyncMock(return_value={"id": "m1"})):
            await w._show_maps()
            await w._show_maps()
        self.assertEqual(len(calls), 2)

    async def test_a_map_in_a_running_reply_is_saved_at_once_so_a_reload_shows_it(self):
        w = self.writer(card("map", "c1", result_text()), seen={})
        w.conn.send_request = mock.AsyncMock(return_value=geometry())
        w._saved_embeds = mock.AsyncMock(return_value=[])
        w._emit = mock.AsyncMock()
        upsert = mock.AsyncMock(return_value={"id": "m1"})
        with mock.patch.object(ocp, "_owui_upsert_message", upsert):
            await w._show_maps()
        (chat_id, message_id, fields), _ = upsert.call_args
        self.assertEqual((chat_id, message_id, list(fields)), ("c1", "m1", ["embeds"]))
        self.assertEqual(len(fields["embeds"]), 1)

    @mock.patch.object(ocp, "_owui_upsert_message", mock.AsyncMock(return_value={"id": "m1"}))
    async def test_a_second_map_sends_the_whole_list_because_a_tab_replaces_its_embeds(self):
        other = "fedcba9876543210"
        w = self.writer(card("map", "c1", result_text()), seen={})
        w.conn.send_request = mock.AsyncMock(side_effect=lambda m, p, timeout=0: geometry(title=p["mapId"]))
        emitted = []

        async def emit(event):
            emitted.append(event)
        w._emit = emit
        await w._show_maps()
        w.renderer.output += card("map", "c2", result_text(other))
        await w._show_maps()
        self.assertEqual([len(e["data"]["embeds"]) for e in emitted], [1, 2])
        self.assertTrue(all(e["data"]["replace"] for e in emitted))


class InlineReplyTests(unittest.IsolatedAsyncioTestCase):
    """The inline reply shows the map as the tool result arrives, to the whole list a tab replaces."""

    asyncTearDown = T.InlineOwnerRuleTests.asyncTearDown
    _queue = T.InlineOwnerRuleTests._queue
    _delta = T.InlineOwnerRuleTests._delta
    _final = T.NoDroppedMessagesTests._final
    _until_registered = T.InlineOwnerRuleTests._until_registered
    _event = T.InlineOwnerRuleTests._event
    _turn = T.InlineOwnerRuleTests._turn
    USER, CHAT = T.InlineOwnerRuleTests.USER, T.InlineOwnerRuleTests.CHAT

    async def asyncSetUp(self):
        self.events = []
        await T.InlineOwnerRuleTests.asyncSetUp(self)
        self.requests = []

        harness_request = self.harness_request = self.conn.send_request

        async def send_request(method, params, timeout=10):
            if method != "map.get":
                return await harness_request(method, params, timeout)
            self.requests.append((method, params))
            return geometry()
        self.conn.send_request = send_request

    def _reply(self, text, msg_id):
        async def emitter(event):
            self.events.append(event)
        body = {"model": "openclaw_gateway.default", "messages": [{"role": "user", "content": text}]}
        meta = {"chat_id": self.CHAT, "user_id": self.USER, "message_id": msg_id}
        return self.pipe._pipe_impl(body, emitter, __user__={"id": self.USER}, __metadata__=meta)

    def _tool(self, phase, name, tcid, text=""):
        data = {"phase": phase, "name": name, "toolCallId": tcid, "args": {}}
        if phase == "result":
            data["result"] = {"content": [{"type": "text", "text": text}]}
        return self._event("agent", stream="tool", data=data)

    def embeds(self):
        return [e["data"] for e in self.events if e.get("type") == "embeds"]

    async def test_a_map_result_adds_the_page_as_the_whole_list(self):
        await self._turn(self._tool("start", "map", "t1"), self._tool("result", "map", "t1", result_text()),
                         self._final("run-1"))
        (data,) = self.embeds()
        self.assertTrue(data["replace"])
        self.assertEqual(len(data["embeds"]), 1)
        self.assertIn("Fushimi Inari", data["embeds"][0])
        self.assertEqual(self.requests, [("map.get", {"mapId": MAP_ID})])

    async def test_a_map_the_gateway_could_not_give_at_first_is_added_when_the_reply_ends(self):
        answers = [RuntimeError("busy"), geometry()]

        async def flaky(method, params, timeout=10):
            if method != "map.get":
                return await self.harness_request(method, params, timeout)
            reply = answers.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        self.conn.send_request = flaky
        await self._turn(self._tool("start", "map", "t1"), self._tool("result", "map", "t1", result_text()),
                         self._final("run-1"))
        (data,) = self.embeds()
        self.assertEqual(len(data["embeds"]), 1)

    async def test_the_live_view_stays_in_the_list_when_a_map_follows(self):
        self.pipe.valves.LIVE_VIEW_URL = "https://replay.example/embed?chat={chat_id}&message={message_id}"
        await self._turn(self._tool("start", "browser", "t1"), self._tool("result", "browser", "t1"),
                         self._tool("start", "map", "t2"), self._tool("result", "map", "t2", result_text()),
                         self._final("run-1"))
        last = self.embeds()[-1]["embeds"]
        self.assertTrue(last[0].startswith("https://replay.example/embed"))
        self.assertEqual(len(last), 2)
        self.assertTrue(self.embeds()[0]["replace"])

    async def test_other_tools_add_no_embed_and_make_no_request(self):
        await self._turn(self._tool("start", "exec", "t1"), self._tool("result", "exec", "t1", "ok"),
                         self._final("run-1"))
        self.assertEqual(self.embeds(), [])
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
