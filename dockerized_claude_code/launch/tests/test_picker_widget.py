"""Tests for launch.gui.picker_widget — the row contract and the picker loop.

Split from test_menu_picker 2026-09-03 with the widget itself. The division
holds the same line the modules do: these cases hand the widget rows and
assert on what it RENDERS and which rows the cursor may land on, with no
agent, instance or cluster involved. What the launcher's actual menus put in
those rows is test_menu_picker's subject.

Nothing here opens a prompt_toolkit Application — the loop's key bindings are
covered where they are observable (test_picker_scroll for mouse/scroll,
test_menu_picker for the menus' end-to-end row assembly)."""

import dataclasses
import unittest
from pathlib import Path

from launch.gui import picker_widget
from launch.gui.picker_widget import (
    BREAK_CHAR, PANE_HIDDEN, PANE_LEGEND, PANE_PREVIEW, STYLE_WORKSPACE_HINT,
    PickerCwdHint, PickerEntry, PickerRowMarker, WorkspaceView, _accent_style,
    _cont_tags_column, _cursor_step, _focusable_indices, _pane_view, _tags_column,
    _visible_indices, break_row,
)
from launch.gui.styles import STYLE_TAG_INVALID, STYLE_TAG_SAFE, STYLE_TAG_WARN, UiClass
from launch.tags import AgentBuild, Instance, resolve_build
from launch.tags.base import SQUASH_AT
from launch.tests.fixtures import REGISTRY, make_inst


def _six_tags() -> list:
    """SQUASH_AT real tags — the smallest crowded row."""
    reg = REGISTRY
    tags = [reg.professions["code"], reg.professions["webdev"],
            reg.specialties["auto"], reg.specialties["cowork"],
            reg.specialties["manager"], reg.policies["no-sudo"]]
    assert len(tags) == SQUASH_AT
    return tags


class TestTagsColumn(unittest.TestCase):
    """_tags_column renders tag labels as warn-aware pt fragments — the one
    rendering source for both Create-row and Cont-row tag columns."""

    def test_empty_input(self):
        self.assertEqual(_tags_column([]), ([], 0))

    def test_safe_tag_renders_green(self):
        fragments, width = _tags_column([REGISTRY.professions["code"]])
        styles = {style for style, _ in fragments}
        self.assertIn(STYLE_TAG_SAFE, styles)
        self.assertIn("[code]", "".join(text for _, text in fragments))
        self.assertEqual(width, len("[code]") + 1)   # +1 trailing separator space

    def test_warn_specialty_renders_red(self):
        fragments, _ = _tags_column([REGISTRY.specialties["dood"]])
        self.assertIn(STYLE_TAG_WARN, {style for style, _ in fragments})

    def test_multiple_tags_space_separated(self):
        fragments, width = _tags_column([REGISTRY.professions["code"],
                                         REGISTRY.specialties["auto"]])
        text = "".join(t for _, t in fragments)
        self.assertEqual(text, "[code] {auto} ")
        self.assertEqual(width, len(text))

class TestTagsColumnSquashed(unittest.TestCase):
    """At SQUASH_AT tags a row's column stops showing labels: each tag becomes
    its one-char glyph on a chip of its usual color (black glyph, color
    background), one space between chips. The full names move to the preview
    pane."""

    def test_below_the_threshold_labels_survive(self):
        fragments, _ = _tags_column(_six_tags()[:SQUASH_AT - 1])
        self.assertIn("[code]", "".join(t for _, t in fragments))

    def test_at_the_threshold_each_tag_is_one_char(self):
        fragments, width = _tags_column(_six_tags())
        text = "".join(t for _, t in fragments)
        # code→c webdev→w auto→a cowork(cowrk)→c manager(mngr)→m no-sudo(-su)→s,
        # one space between chips so same-colored neighbours read as two tags.
        self.assertEqual(text, "c w a c m s ")
        self.assertEqual(width, 2 * SQUASH_AT)   # chips + separators + trailing space

    def test_chips_carry_the_color_as_background_with_a_black_glyph(self):
        fragments, _ = _tags_column(_six_tags())
        chip_styles = [style for style, text in fragments if text != " "]
        self.assertEqual(len(chip_styles), SQUASH_AT)
        for style in chip_styles:
            self.assertIn("fg:ansiblack", style)
            self.assertIn("bg:", style)

    def test_a_warn_tags_chip_keeps_its_warning_color(self):
        fragments, _ = _tags_column(_six_tags())
        auto_chip = next(style for style, text in fragments if text == "a")
        self.assertIn("bg:ansibrightred", auto_chip)

    def test_the_glyph_skips_punctuation_and_stance_symbols(self):
        # no-sudo's label is <-su>: the glyph must be 's', never '-' or '<'.
        chips = [t for _, t in _tags_column(_six_tags())[0] if t != " "]
        self.assertIn("s", chips)
        self.assertNotIn("-", chips)
        self.assertNotIn("<", chips)

class TestContTagsColumnSquashed(unittest.TestCase):
    """The Cont-row variant counts active AND invalid tags against the
    threshold, and squashes both — a half-squashed row would make the invalid
    alert look like a different feature."""

    def _inst_with_invalid(self, valid_count: int) -> Instance:
        from launch.tags.registry import TagProblem
        specialties = ["auto", "cowork", "manager", "dood", "firewall"][:valid_count]
        problem = TagProblem(name="typo", axis="specialties", kind="specialty",
                             parentheses=("{", "}"), reason="unknown",
                             actual_kind=None, options=())
        return dataclasses.replace(make_inst(specialties=specialties),
                                   invalid_tags=(problem,))

    def test_invalid_tags_count_toward_the_threshold(self):
        inst = self._inst_with_invalid(SQUASH_AT - 1)        # 5 valid + 1 invalid
        fragments, _ = _cont_tags_column(inst)
        self.assertNotIn("{auto}", "".join(t for _, t in fragments))

    def test_a_squashed_invalid_tag_keeps_the_alert_style(self):
        inst = self._inst_with_invalid(SQUASH_AT - 1)
        fragments, _ = _cont_tags_column(inst)
        self.assertIn((STYLE_TAG_INVALID, "t"), fragments)   # 'typo' → 't', black-on-red

    def test_below_the_threshold_the_full_alert_label_survives(self):
        inst = self._inst_with_invalid(1)                    # 1 valid + 1 invalid = 2
        fragments, _ = _cont_tags_column(inst)
        self.assertIn((STYLE_TAG_INVALID, "{typo}"), fragments)

class TestContTagsColumn(unittest.TestCase):
    """_cont_tags_column renders a Cont row's tags: resolved ones colored
    normally, then any invalid (stored-but-unresolvable) names in the
    red-background/black-foreground alert style, in the expected kind's
    punctuation."""

    def _inst_with_invalid(self, build):
        clean, problems = REGISTRY.resolve_store_build(build, scope="solo")
        return Instance(agent="refactorer", md_path=Path("/x.md"), session="s",
                        workspace="/tmp", is_brand_new=False, invalid_tags=tuple(problems),
                        **resolve_build(clean, "refactorer", REGISTRY))

    def test_valid_only_matches_plain_tags_column(self):
        inst = self._inst_with_invalid(AgentBuild(professions=("code",)))
        self.assertEqual(_cont_tags_column(inst), _tags_column(inst.active_tags))

    def test_invalid_tag_rendered_in_alert_style(self):
        inst = self._inst_with_invalid(AgentBuild(professions=("code", "web")))
        frags, _ = _cont_tags_column(inst)
        self.assertIn((STYLE_TAG_INVALID, "[web]"), frags)          # bad name, profession brackets, alert style
        self.assertIn("[code]", "".join(t for _, t in frags))       # the valid one still shown

    def test_width_counts_invalid_labels(self):
        inst = self._inst_with_invalid(AgentBuild(professions=("code", "web")))
        frags, width = _cont_tags_column(inst)
        self.assertEqual(width, sum(len(text) for _, text in frags))

class TestFocusableRows(unittest.TestCase):
    """`selectable=False` rows are rendered but never focusable, which is what
    blocks Enter / Del / F2 on a running instance. _cursor_step is the pure
    core of the picker's arrow-key movement."""

    def _rows(self, *selectable_flags):
        return [PickerEntry(value=i, selectable=s) for i, s in enumerate(selectable_flags)]

    def test_non_selectable_rows_excluded_from_focus(self):
        rows = self._rows(True, False, True)
        self.assertEqual(_focusable_indices(rows, [0, 1, 2]), [0, 2])

    def test_down_skips_over_non_selectable(self):
        rows = self._rows(True, False, True)
        self.assertEqual(_cursor_step(rows, [0, 1, 2], 0, 1), 2)     # 1 is skipped entirely

    def test_up_skips_over_non_selectable(self):
        rows = self._rows(True, False, True)
        self.assertEqual(_cursor_step(rows, [0, 1, 2], 2, -1), 0)

    def test_movement_wraps_across_focusable_only(self):
        rows = self._rows(True, False, True)
        self.assertEqual(_cursor_step(rows, [0, 1, 2], 2, 1), 0)     # wraps past the trailing skip

    def test_consecutive_non_selectable_all_skipped(self):
        rows = self._rows(True, False, False, False, True)
        self.assertEqual(_cursor_step(rows, [0, 1, 2, 3, 4], 0, 1), 4)

    def test_cursor_snaps_onto_a_focusable_row(self):
        # Cursor parked on an information-only row (e.g. it started running
        # while the menu was open) — any movement rescues it.
        rows = self._rows(False, True)
        self.assertEqual(_cursor_step(rows, [0, 1], 0, 1), 1)

    def test_nothing_focusable_leaves_cursor_put(self):
        # Every visible row is information-only: no crash, no move (Enter is
        # separately guarded, so the row still can't be picked).
        rows = self._rows(False, False)
        self.assertEqual(_cursor_step(rows, [0, 1], 0, 1), 0)

    def test_filtered_out_rows_are_not_focusable(self):
        rows = self._rows(True, True, True)
        self.assertEqual(_cursor_step(rows, [2], 2, 1), 2)           # only row 2 survived the filter

class TestPreviewLoader(unittest.TestCase):
    """The non-blocking contract: a slow preview yields a placeholder and the
    UI thread returns immediately; the resolution runs on the worker, pokes
    invalidate once, and the next render serves the real pane. Timing is
    controlled with events — no sleeps, no flakes."""

    def setUp(self):
        self.invalidations = []
        self.loader = picker_widget._PreviewLoader(
            invalidate=lambda: self.invalidations.append(True))
        self.addCleanup(self.loader.shutdown)

    def test_a_ready_preview_is_served_at_once_with_no_scheduling(self):
        entry = PickerEntry(preview="already rendered")
        self.assertEqual(self.loader.text(0, entry), "already rendered")
        self.assertEqual(self.invalidations, [])

    def test_a_slow_preview_yields_the_placeholder_without_blocking(self):
        import threading
        started, release, calls = threading.Event(), threading.Event(), []

        def slow() -> str:
            calls.append(True)
            started.set()
            release.wait(5)
            return "RESOLVED"

        entry = PickerEntry(preview=slow)
        # The UI thread gets the placeholder back IMMEDIATELY — this very
        # assertion runs while the resolution is still blocked on `release`.
        self.assertEqual(self.loader.text(3, entry), picker_widget.PREVIEW_LOADING_TEXT)
        self.assertTrue(started.wait(5))
        # Re-renders while it loads: still the placeholder, and NOT a second job.
        self.assertEqual(self.loader.text(3, entry), picker_widget.PREVIEW_LOADING_TEXT)
        release.set()
        self.loader._executor.shutdown(wait=True)      # deterministic: worker done
        self.assertEqual(self.loader.text(3, entry), "RESOLVED")
        self.assertEqual(len(calls), 1)                # one resolution, ever
        self.assertEqual(len(self.invalidations), 1)   # one repaint poke

    def test_the_quick_form_is_served_while_the_full_one_resolves(self):
        import threading
        release = threading.Event()

        def slow_full() -> str:
            release.wait(5)
            return "FULL"

        entry = PickerEntry(preview=slow_full, preview_quick=lambda: "QUICK")
        self.assertEqual(self.loader.text(0, entry), "QUICK")   # not the bare placeholder
        release.set()
        self.loader._executor.shutdown(wait=True)
        self.assertEqual(self.loader.text(0, entry), "FULL")

    def test_a_failing_preview_becomes_a_visible_error_not_eternal_loading(self):
        def broken() -> str:
            raise RuntimeError("transcript unreadable")

        entry = PickerEntry(preview=broken)
        self.assertEqual(self.loader.text(0, entry), picker_widget.PREVIEW_LOADING_TEXT)
        self.loader._executor.shutdown(wait=True)
        self.assertIn("preview failed", self.loader.text(0, entry))
        self.assertIn("transcript unreadable", self.loader.text(0, entry))
        self.assertEqual(len(self.invalidations), 1)   # the error pane still repaints

    def test_two_rows_resolve_independently(self):
        first = PickerEntry(preview=lambda: "ONE")
        second = PickerEntry(preview=lambda: "TWO")
        self.loader.text(0, first)
        self.loader.text(1, second)
        self.loader._executor.shutdown(wait=True)
        self.assertEqual(self.loader.text(0, first), "ONE")
        self.assertEqual(self.loader.text(1, second), "TWO")

class TestPickerEntryPreviewState(unittest.TestCase):
    def test_a_plain_string_is_born_ready(self):
        self.assertTrue(PickerEntry(preview="x").preview_ready)

    def test_a_deferred_preview_is_not_ready_until_resolved(self):
        entry = PickerEntry(preview=lambda: "made")
        self.assertFalse(entry.preview_ready)
        self.assertEqual(entry.preview_ansi(), "made")
        self.assertTrue(entry.preview_ready)           # resolution is recorded on the entry

    def test_resolution_happens_once(self):
        calls = []
        entry = PickerEntry(preview=lambda: (calls.append(True), "made")[1])
        entry.preview_ansi()
        entry.preview_ansi()
        self.assertEqual(len(calls), 1)

class TestRowMarkers(unittest.TestCase):
    """PickerRowMarker — the bookmark lead-ins that contrast the picker's two
    row kinds: an agent row is a green TAB with a fading end, its instances
    nest beneath a dim grey indented ▸. Emoji stays on the menu rows only."""

    def test_the_agent_row_leads_with_a_fading_tab(self):
        # Tab body, then tip, in that order — the Starship-segment shape. Both
        # creation tabs lead with `+` (the verb), then name what gets created.
        (body_style, body), (tip_style, tip) = picker_widget.PickerRowMarker.NEW.lead
        self.assertIn("+ Agent", body)
        self.assertIn("+ Cluster", picker_widget.PickerRowMarker.CLUSTER.lead[0][1])
        self.assertEqual(tip, picker_widget.TAB_TIP)
        # THE invariant that makes it a tab: the tip's foreground is the tab's
        # background, so the ramp renders as the tab dissolving, not characters.
        self.assertIn(f"fg:{body_style.split('bg:', 1)[1].split()[0]}", tip_style)

    def test_the_tip_stays_inside_the_procedurally_drawn_set(self):
        # The kitty insight, transplanted: an application cannot rasterize
        # cells, but terminals (VTE/kitty/WezTerm/alacritty) rasterize the
        # Block Elements range THEMSELVES — full-cell, font never consulted.
        # The first tip, a triangle (▶ U+25B6, a Geometric Shape drawn from
        # the font at text size), rendered visibly SHORTER than its row on a
        # live launch; this fence keeps any such typographic glyph from
        # sneaking back into the tab's shape-work.
        for char in picker_widget.TAB_TIP:
            with self.subTest(char=hex(ord(char))):
                self.assertIn(ord(char), picker_widget.BLOCK_ELEMENTS)
        # A ramp needs at least two steps to read as a fade rather than a nub.
        self.assertGreaterEqual(len(picker_widget.TAB_TIP), 2)

    def test_instances_nest_under_the_tab_not_beside_it(self):
        ((style, text),) = picker_widget.PickerRowMarker.CONT.lead
        self.assertTrue(text.startswith("   "), "indent is the nesting cue")
        self.assertIn("▸", text)
        # Dim furniture, no tab: a second tab would read as a second agent.
        self.assertNotIn("bg:", style)

    def test_a_cluster_row_is_top_level_and_its_members_nest_beneath_it(self):
        # A cluster is not an instance OF its template (nothing is shared
        # after creation), so its row carries no indent — unlike an instance
        # row under its agent — while its members take the instance depth.
        ((_, cluster_text),) = picker_widget.PickerRowMarker.CLSTR.lead
        ((_, member_text),) = picker_widget.PickerRowMarker.MEMBER.lead
        ((_, instance_text),) = picker_widget.PickerRowMarker.CONT.lead
        self.assertFalse(cluster_text.startswith(" "))
        self.assertEqual(len(member_text) - len(member_text.lstrip(" ")),
                         len(instance_text) - len(instance_text.lstrip(" ")))

    def test_the_leads_are_universal_unicode_not_private_use(self):
        # The whole point of ▶/▸ over Nerd-Font wedges: stock fonts cover them.
        # PUA ranges: BMP E000–F8FF, planes 15/16 F0000–10FFFD. Emoji (1F3xx)
        # sit outside all three and stay legal for the menu rows.
        private_use = lambda cp: (0xE000 <= cp <= 0xF8FF or
                                  0xF0000 <= cp <= 0x10FFFD)
        for marker in picker_widget.PickerRowMarker:
            for _, text in marker.lead:
                for char in text:
                    with self.subTest(marker=marker.name, char=hex(ord(char))):
                        self.assertFalse(private_use(ord(char)))

    def test_the_alignment_suffix_never_wears_the_tab_background(self):
        # Glued onto the last lead fragment, the suffix would smear the tab's
        # background across the gap to the tag column.
        *_, last = picker_widget.PickerRowMarker.NEW.fragments("  ")
        self.assertEqual(last, ("", "  "))
        # And no suffix means no empty trailing fragment.
        self.assertEqual(picker_widget.PickerRowMarker.NEW.fragments(),
                         list(picker_widget.PickerRowMarker.NEW.lead))

    def test_the_kind_colours_agree_between_tab_and_accent_bar(self):
        # Each creation tab wears its kind colour (Create's green, iterated
        # from an all-grey first pass; the cluster tab's cyan) and the
        # preview's edge bar shows the same one — two different greens would
        # read as two different meanings. Cont keeps its yellow on the accent
        # bar only; its dim lead carries no colour.
        marker = picker_widget.PickerRowMarker
        tab_bg = picker_widget.STYLE_TAB.split("bg:", 1)[1].split()[0]
        cluster_tab_bg = picker_widget.STYLE_CLUSTER_TAB.split("bg:", 1)[1].split()[0]
        self.assertEqual(marker.NEW.accent, f"fg:{tab_bg}")
        self.assertEqual(marker.CLUSTER.accent, f"fg:{cluster_tab_bg}")
        self.assertEqual(marker.CONT.accent, "fg:ansiyellow")

    def test_everything_that_exists_shares_one_accent_and_cyan_is_the_new_clusters_alone(self):
        # The bar answers "does this row exist, or would picking it create
        # something": an existing cluster and its members read like an
        # instance (operator, 2026-09-17 — they were cyan, which said
        # "cluster" twice and "existing" never), and cyan is left to the
        # + Cluster tab.
        marker = picker_widget.PickerRowMarker
        existing = (marker.CONT, marker.CLSTR, marker.MEMBER)
        self.assertEqual({m.accent for m in existing}, {picker_widget.ACCENT_EXISTING})
        self.assertEqual([m.name for m in marker if m.accent == picker_widget.ACCENT_CREATE_CLUSTER],
                         ["CLUSTER"])
        # The lead still says "cluster" in cyan — the two colours answer
        # different questions, so a cluster row is never mistaken for an
        # instance row in the list itself.
        self.assertIn(picker_widget.STYLE_CLUSTER_NEST, [style for style, _ in marker.CLSTR.lead])
        self.assertEqual(picker_widget.STYLE_CLUSTER_NEST, "fg:ansicyan")

    def test_every_accent_comes_from_the_named_vocabulary(self):
        # A new row kind picks from the three named constants (or declares no
        # accent, like the menu openers) rather than inventing a colour.
        known = {"", picker_widget.ACCENT_CREATE_AGENT, picker_widget.ACCENT_CREATE_CLUSTER,
                 picker_widget.ACCENT_EXISTING}
        for row_marker in picker_widget.PickerRowMarker:
            with self.subTest(marker=row_marker.name):
                self.assertIn(row_marker.accent, known)


class TestEnterGate(unittest.TestCase):
    """Enter's per-row gate, `pickable`: a focusable row Enter must leave
    ALONE — no exit, no result, no redraw — while Del / F2 keep working (a
    cluster member launches with its cluster; the pause that used to explain
    so told the operator nothing new). Driven through the picker's REAL
    binding against a captured Application, like the form tests."""

    def _press_enter(self, entries):
        from unittest.mock import MagicMock, patch
        from prompt_toolkit.keys import Keys
        captured = {}

        class FakeApp:
            def __init__(self, **kw: object) -> None:
                captured.update(kw)

            def invalidate(self) -> None: ...

            def run(self) -> None: ...

        with patch.object(picker_widget, "Application", FakeApp):
            picker_widget.pick_with_preview("t", entries)
        binding = next(b for b in captured["key_bindings"].bindings
                       if tuple(b.keys) == (Keys.Enter,))
        event = MagicMock()
        binding.handler(event)
        return event

    def test_enter_on_an_unpickable_row_does_nothing(self):
        event = self._press_enter([PickerEntry(display=[("", "member")], value="m",
                                               pickable=False)])
        event.app.exit.assert_not_called()

    def test_enter_on_a_pickable_row_selects_it(self):
        event = self._press_enter([PickerEntry(display=[("", "row")], value="r")])
        event.app.exit.assert_called_once()

    def test_an_unpickable_row_stays_focusable_for_the_other_keys(self):
        rows = [PickerEntry(value=0, pickable=False), PickerEntry(value=1)]
        self.assertEqual(_focusable_indices(rows, [0, 1]), [0, 1])


class TestAccentStyle(unittest.TestCase):
    """The preview's accent bar reads the highlighted row's KIND off its
    marker — the widget no longer needs to know what an Instance or Agent is
    to colour it (the old isinstance dispatch could not reach the cluster
    rows at all)."""

    def test_the_marker_decides_the_colour_not_the_value(self):
        cont = PickerEntry(value=make_inst(), marker=PickerRowMarker.CONT)
        self.assertEqual(_accent_style(cont), PickerRowMarker.CONT.accent)
        cluster = PickerEntry(value="an opaque cluster row", marker=PickerRowMarker.CLSTR)
        self.assertEqual(_accent_style(cluster), "fg:ansiyellow")   # it exists — the same bar an instance shows
        template = PickerEntry(value="a template row", marker=PickerRowMarker.CLUSTER)
        self.assertEqual(_accent_style(template), "fg:ansicyan")    # creating one — the reserved colour

    def test_no_row_no_marker_or_no_accent_falls_back_to_the_divider(self):
        self.assertEqual(_accent_style(None), UiClass.DIVIDER.css)
        self.assertEqual(_accent_style(PickerEntry(value=1)), UiClass.DIVIDER.css)
        self.assertEqual(_accent_style(PickerEntry(marker=PickerRowMarker.TOOLS)),
                         UiClass.DIVIDER.css)


class TestWorkspaceView(unittest.TestCase):
    """A row's workspace as one fact: the path, and at most one cwd hint in
    front of it."""

    def test_the_hint_precedes_the_path(self):
        view = WorkspaceView("/w", PickerCwdHint.DEFAULT)
        self.assertEqual(view.fragments,
                         [PickerCwdHint.DEFAULT.fragment, (STYLE_WORKSPACE_HINT, "/w")])

    def test_no_hint_is_just_the_path(self):
        self.assertEqual(WorkspaceView("/w", None).fragments,
                         [(STYLE_WORKSPACE_HINT, "/w")])


class TestTagsColumnProblems(unittest.TestCase):
    """`_tags_column` takes the unresolvable names directly, so a cluster's
    or a member's OWN stale tag renders with the same alert chip an
    instance's does — `_cont_tags_column` is now just the Instance-fed form."""

    def _problem(self, name="typo"):
        from launch.tags.registry import TagProblem
        return TagProblem(name=name, axis="specialties", kind="specialty",
                          parentheses=("{", "}"), reason="unknown",
                          actual_kind=None, options=())

    def test_problems_follow_the_tags_in_the_alert_style(self):
        fragments, width = _tags_column([REGISTRY.professions["code"]],
                                        problems=[self._problem()])
        self.assertEqual("".join(t for _, t in fragments), "[code] {typo} ")
        self.assertIn((STYLE_TAG_INVALID, "{typo}"), fragments)
        self.assertEqual(width, len("[code] {typo} "))

    def test_problems_alone_still_render(self):
        fragments, _ = _tags_column([], problems=[self._problem()])
        self.assertEqual(fragments, [(STYLE_TAG_INVALID, "{typo}"), ("", " ")])


if __name__ == "__main__":
    unittest.main()


class TestBreakRows(unittest.TestCase):
    """A BREAK row separates two blocks of rows (one cluster's from the
    next's). Two properties make it work: the cursor never lands on it, and
    the typed-word filter never drops it — a break is a boundary, not content,
    so two clusters' members matching one word stay visibly apart (operator,
    2026-09-17). Kept honest by collapsing: a boundary that separates nothing
    is not shown."""

    @staticmethod
    def _rows(*texts):
        """A row per text; `None` makes a break."""
        return [break_row(3) if text is None else PickerEntry(display=[("", text)], value=text)
                for text in texts]

    def test_a_break_is_information_only_and_wears_the_rule(self):
        row = break_row(7)
        self.assertEqual("".join(t for _, t in row.display), BREAK_CHAR * 7)
        self.assertTrue(row.separator)
        self.assertFalse(row.selectable or row.pickable or row.deletable or row.modifiable)

    def test_the_cursor_steps_over_a_break(self):
        rows = self._rows("golem", None, "poet")
        shown = list(range(3))
        self.assertEqual(_focusable_indices(rows, shown), [0, 2])
        self.assertEqual(_cursor_step(rows, shown, 0, 1), 2)      # never lands on index 1
        self.assertEqual(_cursor_step(rows, shown, 2, 1), 0)      # and wraps past it

    def test_a_filter_keeps_the_break_between_what_survives(self):
        # THE point: "researcher" matches a member in each cluster, and the
        # break between them says they are not neighbours.
        rows = self._rows("team-a", "researcher__primary", None, "team-b", "researcher__other")
        self.assertEqual(_visible_indices(rows, "researcher"), [1, 2, 4])

    def test_a_break_that_separates_nothing_is_dropped(self):
        rows = self._rows("team-a", "golem", None, "team-b", "poet")
        self.assertEqual(_visible_indices(rows, "golem"), [1])        # nothing below it — no trailing rule
        self.assertEqual(_visible_indices(rows, "poet"), [4])         # nothing above it — no leading rule
        self.assertEqual(_visible_indices(rows, "team"), [0, 2, 3])   # a real boundary survives

    def test_consecutive_breaks_collapse_to_one(self):
        rows = self._rows("a", None, "b", None, "c")
        self.assertEqual(_visible_indices(rows, "b"), [2])            # both boundaries lose their sides
        self.assertEqual(_visible_indices(rows, ""), [0, 1, 2, 3, 4])
        self.assertEqual(_visible_indices(rows, "zzz"), [])           # no rows, so no rules

    def test_an_unmatched_row_still_goes_while_its_break_may_stay(self):
        rows = self._rows("alpha", None, "beta", "alpha-two")
        self.assertEqual(_visible_indices(rows, "alpha"), [0, 1, 3])


class TestPaneView(unittest.TestCase):
    """`_pane_view` — the one answer the layout (is the pane there) and the
    content function (what to compute) both read, so they cannot disagree."""

    def test_the_three_states(self):
        self.assertEqual(_pane_view(hidden=False, legend_open=False, has_legend=True), PANE_PREVIEW)
        self.assertEqual(_pane_view(hidden=False, legend_open=True, has_legend=True), PANE_LEGEND)
        self.assertEqual(_pane_view(hidden=True, legend_open=False, has_legend=True), PANE_HIDDEN)

    def test_hidden_wins_over_an_open_legend(self):
        # F12 closes the legend on its way out, but the table must not depend
        # on that housekeeping having happened.
        self.assertEqual(_pane_view(hidden=True, legend_open=True, has_legend=True), PANE_HIDDEN)

    def test_a_legend_no_caller_supplied_is_never_shown(self):
        self.assertEqual(_pane_view(hidden=False, legend_open=True, has_legend=False), PANE_PREVIEW)


class TestPreviewPaneToggle(unittest.TestCase):
    """F12 hides the side pane, and while it is hidden NOTHING is computed for
    it — the rule the legend already followed: it never asks the loader, so no
    transcript is read for a row nobody is looking at (operator, 2026-09-17).
    Driven through the real key bindings and the real layout, with the
    Application faked, because "the pane is gone" and "the loader was not
    asked" are properties of those two objects."""

    def _picker(self, legend_text="LEGEND", preview="THE PREVIEW"):
        from unittest.mock import patch
        captured: dict = {}

        class FakeApp:
            def __init__(self, **kw: object) -> None:
                captured.update(kw)

            def invalidate(self) -> None: ...

            def run(self) -> None: ...

        entries = [PickerEntry(display=[("", "row")], value="v", preview=preview)]
        with patch.object(picker_widget, "Application", FakeApp):
            picker_widget.pick_with_preview("t", entries, legend_text=legend_text)
        return captured

    @staticmethod
    def _press(captured, key):
        from unittest.mock import MagicMock
        binding = next(b for b in captured["key_bindings"].bindings if tuple(b.keys) == (key,))
        binding.handler(MagicMock())

    @staticmethod
    def _windows(container):
        from prompt_toolkit.layout import Window, walk
        return [w for w in walk(container) if isinstance(w, Window)
                and callable(getattr(w.content, "text", None))]

    @classmethod
    def _pane(cls, captured):
        from prompt_toolkit.layout import ConditionalContainer, walk
        (pane,) = [c for c in walk(captured["layout"].container)
                   if isinstance(c, ConditionalContainer)]
        return pane

    @classmethod
    def _pane_text(cls, captured):
        (window,) = cls._windows(cls._pane(captured))
        return str(window.content.text())

    @classmethod
    def _hint(cls, captured):
        *_, status = cls._windows(captured["layout"].container)
        return "".join(text for _, text in status.content.text())

    def test_f12_takes_the_pane_away_and_brings_it_back(self):
        from prompt_toolkit.keys import Keys
        captured = self._picker()
        pane = self._pane(captured)
        self.assertTrue(pane.filter())
        self._press(captured, Keys.F12)
        self.assertFalse(pane.filter())      # divider, accent bar and preview go together
        self._press(captured, Keys.F12)
        self.assertTrue(pane.filter())

    def test_a_hidden_pane_asks_the_loader_for_nothing(self):
        # THE point: no transcript read, no worker scheduled, for a pane
        # nobody can see.
        from unittest.mock import patch
        from prompt_toolkit.keys import Keys
        captured = self._picker()
        with patch.object(picker_widget._PreviewLoader, "text", return_value="RESOLVED") as asked:
            self.assertIn("RESOLVED", self._pane_text(captured))
            self.assertTrue(asked.called)
            asked.reset_mock()
            self._press(captured, Keys.F12)
            self.assertEqual(self._pane_text(captured), "")
            asked.assert_not_called()
            self._press(captured, Keys.F12)
            self.assertIn("RESOLVED", self._pane_text(captured))

    def test_the_legend_brings_the_pane_back_and_f12_takes_the_legend_with_it(self):
        from prompt_toolkit.keys import Keys
        captured = self._picker()
        pane = self._pane(captured)
        self._press(captured, Keys.F12)
        self.assertFalse(pane.filter())
        self._press(captured, Keys.F8)                     # the pane is the legend's only home
        self.assertTrue(pane.filter())
        self.assertIn("LEGEND", self._pane_text(captured))
        self._press(captured, Keys.F12)                    # hiding closes the legend with it
        self.assertFalse(pane.filter())
        self._press(captured, Keys.F12)
        self.assertIn("THE PREVIEW", self._pane_text(captured))

    def test_the_hint_says_which_way_the_key_goes(self):
        from prompt_toolkit.keys import Keys
        captured = self._picker()
        self.assertIn(picker_widget.HINT_PREVIEW_SUFFIX.strip(), self._hint(captured))
        self._press(captured, Keys.F12)
        self.assertIn(picker_widget.HINT_PREVIEW_HIDDEN.strip(), self._hint(captured))
        self.assertNotIn("hide preview", self._hint(captured))
