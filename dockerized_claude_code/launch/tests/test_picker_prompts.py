"""Tests for launch.gui.picker_prompts — the field validators the instance
and cluster forms are handed (collision rules, the auto-derived name) and the
agent one-liner every membership form shows. Split out of test_menu_picker
2026-09-03 with the code."""

import unittest
from unittest.mock import patch

from launch.gui import picker_prompts
from launch.gui.menu_picker import (
    _agent_description,
)



class TestInstanceFields(unittest.TestCase):
    """The instance form's fields — prompt_session's old behaviors, as a
    validator plus the auto-fill rule (no terminal prompt survives)."""

    def error(self, value, existing=(), current=None):
        with patch.object(picker_prompts, "path_exists",
                          side_effect=lambda p: any(
                              str(p).endswith(f"golem__{e}") for e in existing)):
            return picker_prompts._suffix_field_error("golem", value, current)

    def test_the_name_autofills_from_the_workspace_basename(self):
        # prompt_session's old default, now live: path first, name follows.
        workspace, session = picker_prompts.instance_fields("golem")
        self.assertEqual(workspace.key, "workspace")   # path is the FIRST field
        self.assertIsNotNone(session.auto)
        self.assertEqual(session.auto({"workspace": "/some/workspace/myproj"}),
                         "myproj")

    def test_the_auto_filled_name_is_bent_legal(self):
        # A dotted project dir used to pre-fill `my.app` — a name the field
        # itself refuses (tmux addresses `session:window.pane`). The DEFAULT is
        # bent (`my-app`); a name the user types never is.
        _, session = picker_prompts.instance_fields("golem")
        self.assertEqual(session.auto({"workspace": "/some/workspace/my.app"}), "my-app")
        self.assertIsNone(session.validate("my-app"))
        self.assertIsNotNone(session.validate("my.app"))
        fields = picker_prompts._cluster_fields("", "devteam", derive="devteam")
        self.assertEqual(fields[1].auto({"project": "/code/my.app"}), "devteam__my-app")
        # Nothing legal left in the basename → the plain fallback, as for an empty path.
        self.assertEqual(session.auto({"workspace": "/x/..."}), "golem")
        self.assertEqual(fields[1].auto({"project": "/x/..."}), "devteam")

    def test_an_empty_path_falls_back_to_the_agent_name(self):
        # GUARDED before expanding: expand_user_path("") resolves to the CWD,
        # so an emptied field would otherwise derive from wherever the
        # launcher happens to run.
        _, session = picker_prompts.instance_fields("golem")
        self.assertEqual(session.auto({"workspace": ""}), "golem")

    def test_the_cluster_name_derivation_has_the_same_empty_guard(self):
        fields = picker_prompts._cluster_fields("", "devteam", derive="devteam")
        self.assertEqual(fields[1].auto({"project": ""}), "devteam")
        self.assertEqual(fields[1].auto({"project": "/code/thing"}),
                         "devteam__thing")

    def test_editing_pins_the_name(self):
        # A modify arrives with `current`: the name field sits still (renames
        # are deliberate) — no auto derivation.
        _, session = picker_prompts.instance_fields(
            "golem", workspace="/w", suffix="mysess", current="mysess")
        self.assertIsNone(session.auto)
        self.assertEqual(session.value, "mysess")

    def test_collision_is_an_error(self):
        self.assertIsNotNone(self.error("taken", existing=["taken"]))

    def test_keeping_your_own_name_is_not_a_collision(self):
        self.assertIsNone(self.error("mysess", existing=["mysess"],
                                     current="mysess"))

    def test_renaming_onto_another_existing_name_is_an_error(self):
        self.assertIsNotNone(self.error("other", existing=["mysess", "other"],
                                        current="mysess"))

    def test_a_fresh_rename_is_fine(self):
        self.assertIsNone(self.error("newname", existing=["mysess"],
                                     current="mysess"))

    def test_illegal_characters_are_errors_with_the_shared_rules_reason(self):
        # The gap cluster/solo.py documented — a dotted project dir naming an
        # instance that reaches tmux as an address — closes at the field: the
        # instance name is held to the same rule clusters and cowork use.
        self.assertIn("tmux", self.error("my.app"))
        self.assertIn("ref name", self.error("a~b"))
        self.assertIn("path component", self.error("a/b"))
        self.assertIsNone(self.error("my-app_2"))

    def test_the_cluster_name_field_applies_the_same_rule(self):
        with patch.object(picker_prompts.cluster_state, "exists", return_value=False):
            self.assertIn("ref name", picker_prompts._session_field_error("a~b"))
            self.assertEqual(picker_prompts._session_field_error(""), "cannot be empty")
            self.assertIsNone(picker_prompts._session_field_error("team"))

    def test_empty_is_an_error(self):
        self.assertIsNotNone(self.error(""))


class TestAgentDescription(unittest.TestCase):
    def test_plain_first_line(self):
        self.assertEqual(_agent_description("A fast simpleton.\nMore text."), "A fast simpleton.")

    def test_heading_marker_stripped(self):
        self.assertEqual(_agent_description("# Poet\nbody"), "Poet")

    def test_deep_heading_marker_stripped(self):
        self.assertEqual(_agent_description("### Deep heading"), "Deep heading")

    def test_empty_md_yields_empty_string(self):
        # Regression: splitlines()[0] raised IndexError on a zero-byte agent
        # .md, crashing the picker before it could even render.
        self.assertEqual(_agent_description(""), "")

    def test_whitespace_only_md_yields_empty_string(self):
        self.assertEqual(_agent_description("   \n\n"), "")


if __name__ == "__main__":
    unittest.main()
