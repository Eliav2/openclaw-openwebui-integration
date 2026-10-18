#!/usr/bin/env python3
"""The Status Action's dialog and drawer only use CSS classes Open WebUI
actually ships (ELI-118).

Open WebUI's stylesheet is prebuilt: a Tailwind class its own source never
uses (`z-[10000]`, `text-[10px]`, `shadow-2xl`) has no rule, and the element
silently renders without it. The drawer had no z-index that way and opened
behind the Status dialog.

The shipped class list is vendored in test_fixture_owui_css_classes.txt
(scripts/owui_css.py dump); the e2e suite checks it against the pinned image.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import owui_css  # noqa: E402


class ActionClassesShipped(unittest.TestCase):
    def test_every_action_class_is_in_open_webuis_css(self):
        _, shipped = owui_css.read_fixture()
        missing = sorted(owui_css.action_classes() - shipped)
        self.assertEqual(missing, [], "classes Open WebUI's CSS does not define; use a class "
                         "from test_fixture_owui_css_classes.txt or an inline style")

    def test_the_vendored_list_is_for_the_pinned_open_webui(self):
        ref, shipped = owui_css.read_fixture()
        self.assertEqual(ref, owui_css.pinned_owui_ref(),
                         "e2e/versions.env moved: regenerate with scripts/owui_css.py dump")
        self.assertGreater(len(shipped), 1000)

    def test_the_action_uses_a_plausible_number_of_classes(self):
        # Guards the extractor itself: if it stopped finding classes, the
        # check above would pass on an empty set.
        used = owui_css.action_classes()
        self.assertGreater(len(used), 80)
        self.assertIn("fixed", used)
        self.assertIn("dark:bg-gray-900", used)


class Extractor(unittest.TestCase):
    def test_classname_concatenation_and_conditionals(self):
        js = ("row.className = 'w-full flex ' + 'py-1.5 -mx-1.5 '\n"
              "  + (t.status === 'running' ? ' animate-pulse' : '');\n")
        self.assertEqual(owui_css.used_classes(js),
                         {"w-full", "flex", "py-1.5", "-mx-1.5", "animate-pulse"})

    def test_class_attributes_in_html_strings_and_classlist(self):
        js = ("el.innerHTML = '<div class=\"h-3 w-2/3 dark:bg-gray-700\"></div>';\n"
              "b.classList.add('z-[9999]');\n")
        self.assertEqual(owui_css.used_classes(js), {"h-3", "w-2/3", "dark:bg-gray-700", "z-[9999]"})

    def test_css_names_are_unescaped(self):
        css = (".z-\\[9999\\]{z-index:9999}.dark\\:hover\\:bg-gray-800\\/60:hover{}"
               ".py-1\\.5{}.text-\\[0\\.625rem\\]{}@media (min-width:40rem){.sm\\:flex{}}")
        self.assertEqual(owui_css.css_class_names(css),
                         {"z-[9999]", "dark:hover:bg-gray-800/60", "py-1.5", "text-[0.625rem]", "sm:flex"})


if __name__ == "__main__":
    unittest.main()
