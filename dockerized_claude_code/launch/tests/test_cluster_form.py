"""Tests for launch.gui.cluster_form — the membership form's pure half.

The interactive Application stays out of unit scope (the house rule from
test_menu_picker's docstring); everything with rules — prefill, the
accumulate/remove semantics, the live id preview — is a pure helper tested
here. The rules encode two decisions: picking an agent ADDS AN ENTRY (that is
what makes a cluster hold two researchers), and there is NO per-member editing
mid-flow (roles auto-derive; the preview is what keeps them from surprising).
"""

import unittest

from launch.cluster.legoset import ClusterTemplate
from launch.cluster.member import Member
from launch.gui.cluster_form import (
    TextField, add_pick, field_errors, prefill_picks, preview_ids,
    prompt_members, remove_last,
)


def a_template(*members: Member) -> ClusterTemplate:
    return ClusterTemplate(name="team", members=members)


class TestPrefill(unittest.TestCase):
    def test_template_roles_ride_in_verbatim(self):
        picks = prefill_picks(a_template(Member.of("researcher", "primary"),
                                         Member.of("researcher", "adversarial")))
        self.assertEqual(picks, [("researcher", "primary"),
                                 ("researcher", "adversarial")])

    def test_a_defaulted_role_comes_back_as_none(self):
        # `Member.of("golem")` stores role="golem" (the collapse default). The
        # form must treat that as UNROLED: if the user then adds a second
        # golem, both renumber to golem__1/golem__2 — a kept literal "golem"
        # role would pin the first id while its twin got a number.
        picks = prefill_picks(a_template(Member.of("golem")))
        self.assertEqual(picks, [("golem", None)])

    def test_order_is_kept(self):
        picks = prefill_picks(a_template(Member.of("b"), Member.of("a")))
        self.assertEqual([agent for agent, _ in picks], ["b", "a"])


class TestAddRemove(unittest.TestCase):
    def test_picking_appends_an_entry(self):
        picks = [("golem", None)]
        add_pick(picks, "golem")
        add_pick(picks, "poet")
        self.assertEqual(picks, [("golem", None), ("golem", None), ("poet", None)])

    def test_remove_takes_that_agents_last_entry_only(self):
        picks = [("golem", None), ("poet", None), ("golem", None)]
        remove_last(picks, "golem")
        self.assertEqual(picks, [("golem", None), ("poet", None)])

    def test_remove_at_zero_is_a_no_op(self):
        picks = [("poet", None)]
        remove_last(picks, "golem")
        self.assertEqual(picks, [("poet", None)])

    def test_template_entries_are_not_protected(self):
        # Prefills are a starting point, never a lock: shrinking devteam's two
        # researchers drops the most recently listed one first.
        picks = [("researcher", "primary"), ("researcher", "adversarial")]
        remove_last(picks, "researcher")
        self.assertEqual(picks, [("researcher", "primary")])


class TestPreview(unittest.TestCase):
    """The live id panel — what makes invisible auto-roles acceptable: every
    id the confirm will create is on screen before it happens."""

    def test_shows_the_exact_ids_confirm_would_create(self):
        picks = [("researcher", "primary"), ("researcher", None),
                 ("golem", None)]
        self.assertEqual(preview_ids(picks),
                         ["researcher__primary", "researcher__1", "golem"])

    def test_duplicates_render_numbered_the_moment_the_second_is_picked(self):
        picks = [("golem", None)]
        self.assertEqual(preview_ids(picks), ["golem"])
        add_pick(picks, "golem")
        self.assertEqual(preview_ids(picks), ["golem__1", "golem__2"])


class TestTextField(unittest.TestCase):
    """The form's text rows — a real cursor (insert/erase AT it, arrows move
    it, ctrl+arrows jump words), validation live, and the confirm gate reads
    the same errors the warning zone shows."""

    def test_insert_and_backspace_work_at_the_cursor(self):
        field = TextField(key="k", label="name", value="tea")
        field.insert("m")                    # cursor starts at the end
        self.assertEqual(field.value, "team")
        field.left()
        field.left()
        field.insert("x")                    # te|am → tex|am
        self.assertEqual(field.value, "texam")
        field.backspace()                    # erases the x it just typed
        self.assertEqual(field.value, "team")
        self.assertEqual(field.cursor, 2)

    def test_delete_erases_at_the_cursor_not_before(self):
        field = TextField(key="k", label="name", value="abc")
        field.home()
        field.delete()
        self.assertEqual(field.value, "bc")
        field.end()
        field.delete()                       # nothing AT the end — no-op
        self.assertEqual(field.value, "bc")

    def test_backspace_on_empty_is_a_no_op(self):
        field = TextField(key="k", label="name", value="")
        field.backspace()
        self.assertEqual(field.value, "")

    def test_motion_clamps_at_both_ends(self):
        field = TextField(key="k", label="name", value="ab")
        field.right()                        # already at the end
        self.assertEqual(field.cursor, 2)
        field.home()
        field.left()
        self.assertEqual(field.cursor, 0)

    def test_word_jumps_stop_at_path_separators(self):
        field = TextField(key="k", label="path", value="/code/my-thing")
        field.word_left()                    # from the end: before `thing`
        self.assertEqual(field.cursor, len("/code/my-"))
        field.word_left()                    # before `my`
        self.assertEqual(field.cursor, len("/code/"))
        field.home()
        field.word_right()                   # past `code`
        self.assertEqual(field.cursor, len("/code"))

    def test_error_validates_the_stripped_value(self):
        field = TextField(key="k", label="name", value="  ok  ",
                          validate=lambda v: None if v == "ok" else "bad")
        self.assertIsNone(field.error)
        field.insert("x")
        self.assertEqual(field.error, "bad")

    def test_field_errors_labels_every_complaint(self):
        fields = [
            TextField(key="a", label="name", value="",
                      validate=lambda v: "cannot be empty" if not v else None),
            TextField(key="b", label="path", value="/fine"),
        ]
        self.assertEqual(field_errors(fields), ["name: cannot be empty"])


class TestPromptMembersGuards(unittest.TestCase):
    def test_no_agents_is_a_programming_error(self):
        # An empty pick list is a user state the form handles; an empty AGENT
        # list means the caller scanned nothing — fail loud, not a blank form.
        with self.assertRaises(ValueError):
            prompt_members([], [], title="t")


class TestFormDrivenHeadless(unittest.TestCase):
    """prompt_members driven for real through a pipe input — the regressions
    that live only in key handling, keystroke by keystroke."""

    def drive(self, keys, fields=None, initial=None):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                # Trailing double ctrl-C: tripwire only (see test_tag_form's
                # drive) — a form unexpectedly still open cancels to None
                # instead of hanging the suite.
                pipe.send_text(keys + "\x03\x03")
                return prompt_members(
                    [("golem", "a simpleton")],
                    initial if initial is not None else [("golem", None)],
                    title="t", fields=fields)

    def test_left_arrow_never_eats_field_characters(self):
        # THE reported bug: ← while a field was focused deleted a character
        # (the remove handler's field branch fell through to backspace).
        # Motion-only now — so the form is unchanged, Enter asks, y closes.
        result = self.drive("\x1b[D\x1b[D\ry", fields=[
            TextField(key="name", label="name", value="abc")])
        self.assertEqual(result, ({"name": "abc"}, [("golem", None)]))

    def test_left_arrow_positions_the_cursor_for_a_mid_string_edit(self):
        result = self.drive("\x1b[Dx\r", fields=[
            TextField(key="name", label="name", value="ab")])
        values, _ = result
        self.assertEqual(values["name"], "axb")

    def test_minus_and_space_are_literals_in_a_field(self):
        # The same keys REMOVE and ADD on agent rows — on a field they type.
        result = self.drive("a-b c\r", fields=[
            TextField(key="name", label="name", value="")])
        values, picks = result
        self.assertEqual(values["name"], "a-b c")
        self.assertEqual(picks, [("golem", None)])   # no pick was added or removed

    def test_space_on_an_agent_row_still_adds(self):
        # Down past the field onto the agent row: Space accumulates there —
        # a membership change, so Enter confirms without asking.
        result = self.drive("\x1b[B \r", fields=[
            TextField(key="name", label="name", value="ok")])
        values, picks = result
        self.assertEqual(picks, [("golem", None), ("golem", None)])

    def test_unchanged_confirm_asks_and_n_is_swallowed(self):
        # Enter on the untouched form asks really-done?; the n answers (stays,
        # NOT typed into the field); the x proves the form is still live; the
        # final Enter then closes without asking — the form changed.
        result = self.drive("\rnx\r", fields=[
            TextField(key="name", label="name", value="abc")])
        values, _ = result
        self.assertEqual(values["name"], "abcx")

    def test_an_undone_membership_change_counts_as_unchanged(self):
        # Space adds a golem, ← takes it back — the picks equal the baseline
        # again, so Enter asks really-done? and y closes. Honest by value,
        # not by keystroke count.
        result = self.drive("\x1b[B \x1b[D\ry", fields=[
            TextField(key="name", label="name", value="ok")])
        values, picks = result
        self.assertEqual(picks, [("golem", None)])


if __name__ == "__main__":
    unittest.main()
