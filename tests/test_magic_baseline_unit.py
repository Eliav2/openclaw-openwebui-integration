"""The no-magic-values baseline (scripts/magic_baseline.py) fails only on a hit it does not hold."""
import sys
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from magic_baseline import new_hits  # noqa: E402

LINE = 'if kind == "final":'


class NewHitsTests(unittest.TestCase):
    def test_a_known_line_is_not_new(self):
        self.assertEqual(new_hits({"a.py": Counter({LINE: 1})}, {"a.py": {LINE: 1}}), [])

    def test_fewer_than_the_baseline_is_not_new(self):
        self.assertEqual(new_hits({"a.py": Counter({LINE: 1})}, {"a.py": {LINE: 3}}), [])

    def test_a_new_line_in_a_known_file_is_new(self):
        self.assertEqual(new_hits({"a.py": Counter({LINE: 1, "if n > 7:": 1})}, {"a.py": {LINE: 1}}),
                         [("a.py", "if n > 7:", 1)])

    def test_a_second_copy_of_a_known_line_is_new(self):
        self.assertEqual(new_hits({"a.py": Counter({LINE: 2})}, {"a.py": {LINE: 1}}), [("a.py", LINE, 1)])

    def test_a_known_line_in_another_file_is_new(self):
        self.assertEqual(new_hits({"b.py": Counter({LINE: 1})}, {"a.py": {LINE: 1}}), [("b.py", LINE, 1)])


if __name__ == "__main__":
    unittest.main()
