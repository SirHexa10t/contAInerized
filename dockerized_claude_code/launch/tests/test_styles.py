"""Tests for launch.gui.styles — the display coercers every surface's filter
matching runs on, and the tag-colour functions: `tag_style`'s per-kind
dispatch (per MEMBER for AIs), the chip form, and the prompt_toolkit → rich
conversion that keeps one tag the same colour in a row and in a pane."""

import unittest

from launch.gui.styles import (
    _STYLE_BY_STANCE, RICH_BY_STYLE, STYLE_TAG_ENGINE, STYLE_TAG_SAFE, STYLE_TAG_WARN, _normalize,
    _plain, rich_style, squashed_tag_style, tag_style,
)
from launch.paths import AGENTS_DIR
from launch.tags import scan_all

REGISTRY = scan_all(AGENTS_DIR)


class TestTagStyle(unittest.TestCase):
    """tag_style — one colour per kind, except the AI, whose tag.info paints it."""

    def test_an_ai_wears_its_own_logo_colours(self):
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertEqual(tag_style(ai), f"fg:{ai.fg} bg:{ai.bg}")

    def test_engines_are_the_engine_colour(self):
        # `budget` is the engine's marker field (it was `conf` until 2026-09-13,
        # and the dispatch kept asking for `conf` — every engine fell to the
        # safe colour without a test noticing).
        for engine in REGISTRY.engines.values():
            with self.subTest(engine=engine.name):
                self.assertEqual(tag_style(engine), STYLE_TAG_ENGINE)

    def test_specialties_split_on_warn(self):
        for specialty in REGISTRY.specialties.values():
            with self.subTest(specialty=specialty.name):
                self.assertEqual(tag_style(specialty), STYLE_TAG_WARN if specialty.warn else STYLE_TAG_SAFE)

    def test_policies_colour_by_stance(self):
        for policy in REGISTRY.policies.values():
            with self.subTest(policy=policy.name):
                self.assertEqual(tag_style(policy), _STYLE_BY_STANCE[policy.stance])


class TestSquashedTagStyle(unittest.TestCase):
    def test_a_foreground_colour_becomes_the_chips_background(self):
        self.assertEqual(squashed_tag_style("fg:ansiyellow"), "fg:ansiblack bg:ansiyellow")
        self.assertEqual(squashed_tag_style("bold fg:ansired"), "fg:ansiblack bg:ansired")

    def test_a_style_with_its_own_background_is_already_a_chip(self):
        # An AI paints both colours itself (Claude: orange on dark grey); the
        # usual squash would turn the foreground into the block and lose the
        # background the logo needs.
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertEqual(squashed_tag_style(tag_style(ai)), tag_style(ai))


class TestRichStyle(unittest.TestCase):
    """rich_style — the fixed tag styles keep their curated rich twins; any
    other style converts token by token."""

    def test_the_curated_table_wins_for_the_fixed_styles(self):
        for pt_style, rich in RICH_BY_STYLE.items():
            with self.subTest(style=pt_style):
                self.assertEqual(rich_style(pt_style), rich)

    def test_hex_colours_convert_as_fg_on_bg(self):
        self.assertEqual(rich_style("fg:#ff8700 bg:#3a3a3a"), "#ff8700 on #3a3a3a")

    def test_ansi_names_drop_the_prefix_and_bright_takes_an_underscore(self):
        self.assertEqual(rich_style("fg:ansired"), "red")
        self.assertEqual(rich_style("bold fg:ansibrightred"), "bold bright_red")
        self.assertEqual(rich_style("fg:ansiblack bg:ansibrightblue"), "black on bright_blue")

    def test_every_ai_renders_in_rich_as_it_does_in_prompt_toolkit(self):
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertEqual(rich_style(tag_style(ai)), f"{ai.fg} on {ai.bg}")


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

