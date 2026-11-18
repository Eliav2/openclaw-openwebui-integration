"""ELI-512 #417 re-check (report d2de3011).

1. In 1 of 4 Hebrew stop runs (chat cb7b779f) the open tab never showed "נעצר", though a reload did.
   A stop after text spawns `show_stopped` and `_close_unsent_reply` side by side
   (`_PipeTurn.on_parked_stop`). Open WebUI's emitter sends a status line to the tab, then writes it
   to the DB; the close read the message in between and pushed it whole (`chat:messages`), and Open
   WebUI merges a pushed node over the tab's (`{...local, ...message}`, Chat.svelte:1438): the
   stale `statusHistory` replaced the tab's, "נעצר" included. The push now carries only what it
   changed.
2. Coverage of the `<details>` shaper: multi-line and nested blocks, fenced code, attributes."""
import unittest

import test_pipe_unit  # noqa: F401  (installs the module stubs openclaw_pipe needs)
from test_eli512_p2_unit import _WriterCase

import openclaw_pipe as ocp


def _tab_after(local: dict, pushed: dict) -> dict:
    """What an open tab holds after Open WebUI merges a pushed node (Chat.svelte:1438)."""
    return {**local, **pushed}


class StoppedTabTests(_WriterCase):
    async def _stop(self, content):
        self.owui.msgs()["m4"] = {
            "id": "m4", "role": "assistant", "parentId": "q4", "content": content, "done": True,
            # Saved before the status line's own DB write landed.
            "statusHistory": [{"description": "חושב…", "done": False}, {"description": "", "done": True}],
            "output": [{"type": "function_call", "id": "t1", "name": "web_search", "arguments": "{}",
                        "status": "incomplete"}]}
        ref = {"chat_id": self.CHAT, "message_id": "m4", "user_id": self.USER, "lang": "he", "shown": content}
        await ocp._close_unsent_reply(self.conn, ref, ocp.ui_text("stopped_reason", "he"), only_if_empty=True)
        return self.owui.events("chat:messages")[-1]["data"]["messages"]["m4"]

    async def test_the_push_keeps_the_tabs_stopped_line(self):
        """The ordering of cb7b779f: the tab has "נעצר", the saved row not yet."""
        tab = {"id": "m4", "role": "assistant", "content": "חצי תשובה", "done": True,
               "statusHistory": [{"description": "חושב…", "done": False}, {"description": "", "done": True},
                                 {"description": "נעצר", "done": True}]}
        pushed = await self._stop("חצי תשובה")
        merged = _tab_after(tab, pushed)
        self.assertEqual(merged["statusHistory"][-1]["description"], "נעצר")
        self.assertEqual([i["status"] for i in merged["output"] if i["type"] == "function_call"], ["completed"])

    async def test_the_push_names_its_message(self):
        """A tab without the message still gets a whole node to show."""
        pushed = await self._stop("חצי תשובה")
        self.assertEqual({k: pushed.get(k) for k in ("id", "role", "parentId")},
                         {"id": "m4", "role": "assistant", "parentId": "q4"})

    async def test_a_reply_stopped_before_text_still_pushes_its_status_lines(self):
        """There the push sets the status lines on purpose: its text says "Stopped" now."""
        pushed = await self._stop("")
        self.assertEqual(pushed["content"], "**נעצר.**")
        self.assertEqual([h["description"] for h in pushed["statusHistory"]], ["חושב…", ""])


def _shaped(text):
    r = ocp._TurnRenderer("en")
    out = [e for e in r.feed({"stream": "assistant", "data": {"delta": text}}) if isinstance(e, str)]
    out.append(r.flush())
    return "".join(out)


class DetailsCoverageTests(unittest.TestCase):
    def test_a_multi_line_block_stays(self):
        text = "<details>\n<summary>Sources</summary>\n\n- one\n- two\n\n</details>\nafter"
        self.assertEqual(_shaped(text), text)

    def test_nested_blocks_are_each_shaped(self):
        self.assertEqual(
            _shaped("<details><summary>A</summary>\n<details><summary>B</summary>\ninner\n</details>\n</details>"),
            "<details>\n<summary>A</summary>\n<details>\n<summary>B</summary>\ninner\n</details>\n</details>")

    def test_a_block_in_fenced_code_stays_literal(self):
        for fence in ("```", "~~~", "````"):
            text = f"{fence}html\n<details><summary>S</summary>x</details>\n<summary>T</summary> y\n{fence}\n"
            self.assertEqual(_shaped(text), text, fence)

    def test_open_and_other_attributes_are_kept(self):
        self.assertEqual(_shaped('<details open><summary>S</summary>body\n</details>'),
                         '<details open>\n<summary>S</summary>\nbody\n</details>')
        self.assertEqual(_shaped('<details class="x" open><summary>S</summary>\n</details>'),
                         '<details class="x" open>\n<summary>S</summary>\n</details>')

    def test_a_summary_with_attributes_becomes_a_plain_one(self):
        """Open WebUI reads only `<summary>` (extension.ts:35); its attributes do nothing there."""
        self.assertEqual(_shaped('<details><summary class="s">S</summary>\nbody\n</details>'),
                         '<details>\n<summary>S</summary>\nbody\n</details>')

    def test_an_indented_block_comes_to_the_line_start(self):
        """Open WebUI matches `<details` only at a line's start; up to three spaces are no code block."""
        self.assertEqual(_shaped("  <details><summary>S</summary>\nbody\n</details>"),
                         "<details>\n<summary>S</summary>\nbody\n</details>")

    def test_a_quoted_attribute_is_one_unit(self):
        """CodeRabbit on #418: a `>` or `<summary` inside a quoted value neither ends the tag nor
        becomes the summary. Its `<` and `>` are escaped: Open WebUI's own tag match stops at the
        first `>` (extension.ts:34)."""
        self.assertEqual(_shaped('<details data-x="a>b"><summary>S</summary>\nbody\n</details>'),
                         '<details data-x="a&gt;b">\n<summary>S</summary>\nbody\n</details>')
        self.assertEqual(
            _shaped("<details title='<summary>X</summary>'><summary>S</summary>\nbody\n</details>"),
            "<details title='&lt;summary&gt;X&lt;/summary&gt;'>\n<summary>S</summary>\nbody\n</details>")
        self.assertEqual(_shaped('<details>\n<summary title="a>b">S</summary> x\n</details>'),
                         '<details>\n<summary>S</summary>\nx\n</details>')

    def test_four_spaces_are_code_and_stay(self):
        text = "    <details><summary>S</summary>\n"
        self.assertEqual(_shaped(text), text)


if __name__ == "__main__":
    unittest.main()
