"""Tests for launch.gui.styles — the display coercers every surface's filter
matching runs on. (The colour tables are data; `tag_style`'s field dispatch
is exercised through the form and picker tests that render real tags.)"""

import unittest

from launch.gui.styles import _normalize, _plain


class TestDisplayCoercion(unittest.TestCase):
    """_normalize/_plain back the picker's filter matching — every accepted
    display shape must round-trip to comparable plain text."""

    def test_normalize_plain_string(self):
        self.assertEqual(_normalize("hello"), [("", "hello")])

    def test_normalize_fragment_list_passthrough(self):
        frags = [("bold", "a"), ("", "b")]
        self.assertEqual(_normalize(frags), frags)

    def test_plain_joins_fragment_text(self):
        self.assertEqual(_plain([("bold", "a"), ("", "b")]), "ab")

    def test_plain_of_string(self):
        self.assertEqual(_plain("hello"), "hello")




# ============================================================
# Checkbox form — pure assembly / ordering / cascade / warning logic
# ============================================================

