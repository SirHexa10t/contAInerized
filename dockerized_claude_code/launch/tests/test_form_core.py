"""Tests for launch.gui.form_core — the generic form machinery: row ordering
and the requires cascade, the warning-zone logic, TextField auto-derivation,
the shared confirm/really-done? gate, and the interactive guarantees driven
through a real (headless) prompt_toolkit Application.

Split out of test_tag_form 2026-09-03 alongside the module it covers; what a
given form ASKS is tested in test_forms.py."""

import unittest
from unittest.mock import patch

from launch.gui import form_core
from launch.gui.form_core import (
    FormOption, active_warnings, ordered_form_options,
    requires_closure,
)
# The registry ADAPTERS live with the forms that need them (forms.py maps a
# Registry into the shapes below); the functions they feed are form_core's.
from launch.gui.forms import _combo_warnings, _form_labels, _form_requires
from launch.paths import AGENTS_DIR
from launch.tags import scan_all

REGISTRY = scan_all(AGENTS_DIR)


class TestRequiresClosure(unittest.TestCase):
    def test_no_requires_yields_empty(self):
        self.assertEqual(requires_closure("code", {}), set())

    def test_direct_requirement(self):
        self.assertEqual(requires_closure("web", {"web": frozenset({"code"})}), {"code"})

    def test_transitive_requirement(self):
        req = {"c": frozenset({"b"}), "b": frozenset({"a"})}
        self.assertEqual(requires_closure("c", req), {"a", "b"})

    def test_self_not_included(self):
        self.assertNotIn("web", requires_closure("web", {"web": frozenset({"code"})}))

    def test_cycle_terminates(self):
        req = {"a": frozenset({"b"}), "b": frozenset({"a"})}
        self.assertEqual(requires_closure("a", req), {"a", "b"})

    def test_real_tree_webdev_requires_code(self):
        self.assertEqual(requires_closure("webdev", _form_requires(REGISTRY)), {"code"})


class TestOrderedFormOptions(unittest.TestCase):
    """ordered_form_options — the attached_to proximity layout: attached
    options tuck directly beneath their anchor; no dependency semantics."""

    @staticmethod
    def _opt(key: str, attached_to: str | None = None) -> FormOption:
        return FormOption(key=key, label=key, attached_to=attached_to)

    def test_anchor_order_preserved_without_attachments(self):
        out = ordered_form_options([self._opt("a"), self._opt("b"), self._opt("c")])
        self.assertEqual([o.key for o in out], ["a", "b", "c"])

    def test_attached_tucks_directly_after_anchor(self):
        # The future firewall⇄auto shape: firewall declared last still
        # renders right beneath auto.
        out = ordered_form_options([self._opt("auto"), self._opt("dood"),
                                    self._opt("firewall", attached_to="auto")])
        self.assertEqual([o.key for o in out], ["auto", "firewall", "dood"])

    def test_multiple_attachments_keep_relative_order(self):
        out = ordered_form_options([self._opt("auto"),
                                    self._opt("f1", attached_to="auto"),
                                    self._opt("f2", attached_to="auto")])
        self.assertEqual([o.key for o in out], ["auto", "f1", "f2"])

    def test_unknown_anchor_appends_at_end(self):
        out = ordered_form_options([self._opt("a"), self._opt("x", attached_to="ghost")])
        self.assertEqual([o.key for o in out], ["a", "x"])


class TestActiveWarnings(unittest.TestCase):
    """active_warnings against the real combos.info copy (re-keyed by tag
    name via _combo_warnings) — the form's live warning zone."""

    def setUp(self):
        self.warnings = _combo_warnings(REGISTRY)

    def test_dood_plus_auto_combo_shipped(self):
        self.assertIn(frozenset({"dood", "auto"}), self.warnings)

    def test_auto_plus_dood_fires(self):
        self.assertEqual(len(active_warnings({"auto", "dood"}, self.warnings)), 1)

    def test_superset_still_fires(self):
        self.assertEqual(len(active_warnings({"auto", "dood", "web"}, self.warnings)), 1)

    def test_singles_dont_fire(self):
        self.assertEqual(active_warnings({"auto"}, self.warnings), [])
        self.assertEqual(active_warnings({"dood"}, self.warnings), [])

    def test_empty_selection_no_warnings(self):
        self.assertEqual(active_warnings(set(), self.warnings), [])


class TestRefreshAuto(unittest.TestCase):
    """The auto-fill contract: a derived field FOLLOWS its source until the
    user touches it, then never again — 'the name follows the path until you
    type your own'."""

    def fields(self):
        path = form_core.TextField(key="path", label="path", value="/code/thing")
        name = form_core.TextField(
            key="name", label="name", value="",
            auto=lambda values: f"golem__{values['path'].rsplit('/', 1)[-1]}")
        return path, name

    def test_untouched_fields_follow_their_source(self):
        path, name = self.fields()
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing")
        path.insert("2")
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing2")

    def test_one_keystroke_in_the_field_stops_the_following(self):
        path, name = self.fields()
        form_core.refresh_auto([path, name])
        name.insert("!")                      # the user typed their own
        path.insert("2")
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing!")

    def test_backspace_counts_as_touching_too(self):
        # Deleting part of the suggestion IS choosing a name.
        path, name = self.fields()
        form_core.refresh_auto([path, name])
        name.backspace()
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thin")

    def test_cursor_motion_does_not_count_as_touching(self):
        # Arrowing around a suggestion is LOOKING, not choosing — the field
        # keeps following its source, and the rewrite snaps the cursor back
        # to the end (there was no user cursor position worth preserving).
        path, name = self.fields()
        form_core.refresh_auto([path, name])
        name.home()
        name.word_right()
        path.insert("2")
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing2")
        self.assertEqual(name.cursor, len("golem__thing2"))

    def test_fields_without_auto_never_move(self):
        path, name = self.fields()
        name.auto = None
        name.value = "pinned"
        form_core.refresh_auto([path, name])
        self.assertEqual(name.value, "pinned")


class TestFormDrivenHeadless(unittest.TestCase):
    """checkbox_form driven for real — keystrokes through a pipe input, no
    terminal. This is where the interactive-only guarantees live: the confirm
    gate on invalid fields is unreachable from pure helpers, and a mutation
    run proved it was unpinned until these."""

    def drive(self, keys, fields=None, options=None):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                # Trailing double ctrl-C is a TRIPWIRE, not part of any test's
                # script: a form still open after its keys (e.g. the really-
                # done? question arming when it shouldn't) cancels to None and
                # FAILS loudly — without it such a regression hangs the suite.
                # Two, because the first may be consumed as the question's
                # answer. A correctly-exited form never reads them.
                pipe.send_text(keys + "\x03\x03")
                return form_core.checkbox_form(
                    "t", options or [form_core.FormOption(key="o", label="opt")],
                    fields=fields)

    def test_confirm_refuses_while_a_field_is_invalid(self):
        # First Enter must be REFUSED (field empty + validator says so); the
        # typed x then makes it valid and the second Enter lands. If the gate
        # dies, the first Enter exits with the empty value instead.
        result = self.drive("\rx\r", fields=[
            form_core.TextField(key="f", label="name", value="",
                               validate=lambda v: "empty" if not v else None)])
        self.assertEqual(result, ({"f": "x"}, []))

    def test_space_is_a_literal_in_a_field(self):
        result = self.drive("a b\r", fields=[
            form_core.TextField(key="f", label="name", value="")])
        self.assertEqual(result, ({"f": "a b"}, []))

    def test_backspace_edits_the_field(self):
        result = self.drive("ab\x7f\r", fields=[
            form_core.TextField(key="f", label="name", value="")])
        self.assertEqual(result, ({"f": "a"}, []))

    def test_options_below_the_fields_still_toggle(self):
        # Down-arrow onto the option row, Space toggles it, Enter confirms —
        # the field rows must not have broken the row offset arithmetic.
        result = self.drive("\x1b[B \r", fields=[
            form_core.TextField(key="f", label="name", value="ok")])
        self.assertEqual(result, ({"f": "ok"}, ["o"]))

    def test_arrows_never_edit_a_field(self):
        # ← once ate a character in a live form (a remove handler fell through
        # to backspace). Left and right on a focused field must change nothing
        # — which makes this form UNCHANGED, so Enter asks and `y` closes it.
        result = self.drive("\x1b[D\x1b[C\ry", fields=[
            form_core.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abc"}, []))

    def test_left_arrow_moves_the_cursor_so_typing_lands_mid_string(self):
        result = self.drive("\x1b[Dx\r", fields=[
            form_core.TextField(key="f", label="name", value="ab")])
        self.assertEqual(result, ({"f": "axb"}, []))

    def test_ctrl_left_jumps_a_word_in_a_path(self):
        # ctrl+← from the end of /tmp/proj lands before `proj` (path
        # separators end a word), so the x goes in front of the basename.
        result = self.drive("\x1b[1;5Dx\r", fields=[
            form_core.TextField(key="f", label="path", value="/tmp/proj")])
        self.assertEqual(result, ({"f": "/tmp/xproj"}, []))

    def test_home_and_the_delete_key_erase_at_the_cursor(self):
        # Home to column 0, Delete eats the char AT the cursor (not before).
        result = self.drive("\x1b[H\x1b[3~\r", fields=[
            form_core.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "bc"}, []))

    def test_an_unchanged_confirm_asks_and_any_other_key_stays(self):
        # Enter on an untouched form arms the really-done? question. The next
        # key ANSWERS it and is consumed — the `n` here must not land in the
        # field as text; the x afterwards proves the form is still live.
        result = self.drive("\rnx\r", fields=[
            form_core.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abcx"}, []))

    def test_a_changed_confirm_never_asks(self):
        # One real edit and Enter closes directly — no `y` is queued, so if
        # the question wrongly armed, the tripwire would cancel to None.
        result = self.drive("x\r", fields=[
            form_core.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abcx"}, []))


class TestWantsWarnings(unittest.TestCase):
    """wants_warnings — the advisory zone. Keys stay manifest NAMES (they must
    match the checked set); `labels` is display-only, and it exists because a
    header naming 'cowork' and 'free-bash' points the user at two strings that
    appear nowhere on screen — the rows say {cowork} and <+bash>."""

    WANTS = {"cowork": (("free-bash", "coworkers get auto-denied without it"),)}

    def test_fires_only_while_the_wanted_tag_is_unchecked(self):
        self.assertEqual(len(form_core.wants_warnings({"cowork"}, self.WANTS)), 1)
        self.assertEqual(form_core.wants_warnings({"cowork", "free-bash"},
                                                 self.WANTS), [])
        self.assertEqual(form_core.wants_warnings(set(), self.WANTS), [])

    def test_the_header_shows_labels_when_given(self):
        labels = {"cowork": "{cowrk}", "free-bash": "<+bash>"}
        (header, _), = form_core.wants_warnings({"cowork"}, self.WANTS, labels)
        self.assertEqual(header, "'{cowrk}' wants '<+bash>':")

    def test_a_key_missing_from_the_map_falls_back_to_itself(self):
        # Non-tag callers of checkbox_form lose nothing by omitting labels.
        (header, _), = form_core.wants_warnings({"cowork"}, self.WANTS,
                                               {"cowork": "{cowrk}"})
        self.assertEqual(header, "'{cowrk}' wants 'free-bash':")

    def test_the_forms_label_map_is_punctuated_for_every_kind(self):
        # The real registry, all four kinds — a want may point at any tag.
        labels = _form_labels(REGISTRY)
        self.assertEqual(labels["cowork"], "{cowrk}")
        self.assertEqual(labels["free-bash"], "<+bash>")
        self.assertEqual(labels["code"], "[code]")
        # Engines included (a want may point at one) — asserted via the tag's
        # own label because engine shortnames are expressive (thinker is 🧠).
        self.assertEqual(labels["thinker"], REGISTRY.engines["thinker"].label)


class TestCascadeInForm(unittest.TestCase):
    """The check-cascade wiring: simulate what the form's Space handler does
    (toggle + cascade) using the pure pieces, against the real tree's
    webdev→code edge."""

    def test_checking_dependent_checks_requirement(self):
        req = _form_requires(REGISTRY)
        checked = {"webdev"} | requires_closure("webdev", req)
        self.assertIn("code", checked)

    def test_unchecking_requirement_identifies_dependents(self):
        req = _form_requires(REGISTRY)
        dependents = {k for k in ("webdev",) if "code" in requires_closure(k, req)}
        self.assertEqual(dependents, {"webdev"})


class TestSharedFormScaffold(unittest.TestCase):
    """The pieces BOTH full-screen forms run on (form_core.confirm_gate and
    friends), extracted 2026-09-02 after the copies drifted: the same
    really-done? message rendered at the BOTTOM of the tag form and at the
    TOP of the cluster form, because each host had embedded it in a
    different window."""

    def a_gate(self, *, changed=False, ready=True, asks=True):
        value = {"v": 0}
        gate = form_core.confirm_gate(
            snapshot=lambda: value["v"], ready=lambda: ready,
            asks_when_unchanged=asks)
        if changed:
            value["v"] = 1
        return gate

    def press(self, gate, handler, data=""):
        from unittest.mock import MagicMock
        event = MagicMock()
        event.data = data
        handler(event)
        return event

    def test_an_unchanged_confirm_asks_before_closing(self):
        gate = self.a_gate()
        event = self.press(gate, gate.confirm)
        event.app.exit.assert_not_called()
        self.assertFalse(gate.confirmed())
        self.assertIn(form_core.UNCHANGED_QUESTION,
                      "".join(text for _, text in gate.question()))

    def test_y_answers_the_question_and_any_other_key_stays(self):
        gate = self.a_gate()
        self.press(gate, gate.confirm)
        self.assertTrue(gate.answers(self.press(gate, lambda e: None, "n")))
        self.assertFalse(gate.confirmed())          # stayed
        self.assertEqual(gate.question(), [])       # question cleared
        self.press(gate, gate.confirm)              # ask again
        self.assertTrue(gate.answers(self.press(gate, lambda e: None, "y")))
        self.assertTrue(gate.confirmed())

    def test_a_changed_confirm_never_asks(self):
        gate = self.a_gate(changed=True)
        event = self.press(gate, gate.confirm)
        event.app.exit.assert_called_once()
        self.assertTrue(gate.confirmed())

    def test_a_form_with_nothing_to_type_in_never_asks(self):
        gate = self.a_gate(asks=False)
        self.press(gate, gate.confirm)
        self.assertTrue(gate.confirmed())

    def test_a_refused_confirm_does_nothing_at_all(self):
        # ready() False = the host's warning zone is already explaining.
        gate = self.a_gate(ready=False)
        event = self.press(gate, gate.confirm)
        event.app.exit.assert_not_called()
        self.assertFalse(gate.confirmed())
        self.assertEqual(gate.question(), [])

    def test_the_question_is_only_a_fragment_never_a_render_decision(self):
        # question() returns fragments and nothing else, so WHERE it lands is
        # the host's window choice — which is what the two hosts now agree on
        # (see TestFormTailsMatch).
        gate = self.a_gate()
        self.press(gate, gate.confirm)
        (fragment,) = gate.question()
        self.assertEqual(fragment[0], form_core.UiClass.TITLE.css)


class TestFormTailsMatch(unittest.TestCase):
    """THE regression the 2026-09-02 extraction was for: both forms must end
    with the same window stack, so the really-done? question and the field
    complaints hug the confirm button in each. They differed — the cluster
    form put them in its FLEXIBLE members panel, which top-aligns.

    Since 2026-09-09 both forms are built by ONE scaffold (`form_core.run_form`),
    so the shapes agree by construction; this test now guards the construction
    itself — the membership form builds no Application of its own — and keeps
    the tail comparison as the cheap end-to-end check of it. WHERE a given
    complaint lands inside that tail is `TestFormPlacementParity`'s job."""

    @staticmethod
    def _tail(build):
        from unittest.mock import MagicMock
        captured = {}

        def fake_app(**kwargs):
            captured["layout"] = kwargs["layout"]
            return MagicMock()

        with patch("launch.gui.form_core.Application", side_effect=fake_app):
            build()
        children = captured["layout"].container.children
        # (height, hugs-the-bottom) per window — enough to compare shapes
        # without asserting on content.
        def hugs(window):
            # dont_extend_height is a prompt_toolkit Filter — call it.
            flag = getattr(window, "dont_extend_height", False)
            return bool(flag()) if callable(flag) else bool(flag)

        return [(str(getattr(w, "height", None)), hugs(w))
                for w in children[-4:]]

    def test_only_the_scaffold_builds_a_form_application(self):
        from launch.gui import cluster_form
        self.assertFalse(hasattr(cluster_form, "Application"))

    def test_both_forms_end_with_the_same_window_stack(self):
        from launch.gui import cluster_form
        field = form_core.TextField(key="name", label="name", value="x")
        tags_tail = self._tail(lambda: form_core.checkbox_form(
            "t", [FormOption(key="a", label="a")], fields=[field]))
        members_tail = self._tail(lambda: cluster_form.prompt_members(
            [("golem", "d")], [("golem", None)], title="t",
            fields=[form_core.TextField(key="name", label="name", value="x")]))
        self.assertEqual(tags_tail, members_tail)



class TestFormPlacementParity(unittest.TestCase):
    """WHERE things render — the drift `TestFormTailsMatch` could not see,
    because it compares window SHAPES, not which window carries which text.
    Two regressions found 2026-09-09, both in the cluster form only:

    - its 'no members yet' complaint rode the FLEXIBLE members panel (which
      top-aligns under the options) while its field complaints hugged the
      confirm button — the operator saw the same kind of warning at two
      heights across the two forms;
    - its `cursor_pos` ignored the blank separator line rendered after the
      text fields, so with fields present the reported cursor sat one line
      above the highlighted agent row (only visible once the list overflows
      and prompt_toolkit scrolls to the wrong line).

    Both are pinned by driving the REAL bindings against a captured layout,
    and both are asserted for both forms, so the two can only agree."""

    @staticmethod
    def _capture(build):
        from unittest.mock import MagicMock
        captured = {}

        def fake_app(**kwargs):
            captured["layout"] = kwargs["layout"]
            captured["key_bindings"] = kwargs["key_bindings"]
            return MagicMock()

        with patch("launch.gui.form_core.Application", side_effect=fake_app):
            build()
        return captured

    @staticmethod
    def _text(window) -> str:
        return "".join(text for _, text in window.content.text())

    @staticmethod
    def _press(captured, key) -> None:
        """Fire the form's own handler for `key` — the real binding, not a
        re-implementation of what the key is supposed to do."""
        from unittest.mock import MagicMock
        from prompt_toolkit.keys import Keys
        wanted = Keys(key)
        binding = next(b for b in captured["key_bindings"].bindings
                       if tuple(b.keys) == (wanted,))
        event = MagicMock()
        event.data = ""
        binding.handler(event)

    def _forms(self):
        """Both forms, each with ONE text field above ONE row."""
        from launch.gui import cluster_form
        field = lambda: form_core.TextField(key="name", label="name", value="x")   # noqa: E731
        return {
            "tag form": lambda: form_core.checkbox_form(
                "t", [FormOption(key="a", label="a")], fields=[field()]),
            "membership form": lambda: cluster_form.prompt_members(
                [("golem", "d")], [("golem", None)], title="t", fields=[field()]),
        }

    def test_the_empty_membership_complaint_sits_in_the_warning_window(self):
        from launch.gui import cluster_form
        captured = self._capture(lambda: cluster_form.prompt_members(
            [("golem", "d")], [], title="t",
            fields=[form_core.TextField(key="name", label="name", value="x")]))
        children = captured["layout"].container.children
        # The tail every form ends with: filler · warnings · blank · confirm · hint
        filler, warnings = children[-5], children[-4]
        complaint = cluster_form.EMPTY_WARNING.strip()
        self.assertIn(complaint, self._text(warnings))
        self.assertNotIn(complaint, self._text(filler))

    def test_cursor_pos_names_the_line_the_highlighted_row_renders_on(self):
        for name, build in self._forms().items():
            with self.subTest(form=name):
                captured = self._capture(build)
                self._press(captured, "down")     # field → the one row beneath it
                options = captured["layout"].container.children[2]   # title · blank · options
                fragments = options.content.text()
                highlighted = next(i for i, (style, _) in enumerate(fragments)
                                   if form_core.UiClass.CURSOR.css in style)
                rendered_line = "".join(t for _, t in fragments[:highlighted]).count("\n")
                self.assertEqual(options.content.get_cursor_position().y,
                                 rendered_line)


if __name__ == "__main__":
    unittest.main()

