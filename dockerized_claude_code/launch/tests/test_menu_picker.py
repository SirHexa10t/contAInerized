"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
modes conversion), and the shared session prompt. The prompt_toolkit
Application itself (pick_with_preview) is interactive and stays out of unit
scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import menu_picker
from launch.menu_picker import (
    _agent_description, _modifier_display, _normalize, _plain,
    continuable_instances, prompt_session,
)
from launch.structs import ANSI_TO_PT_STYLE, InstanceModifiers


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


class TestModifierDisplay(unittest.TestCase):
    """_modifier_display parses colored_chain's ANSI output back into
    prompt_toolkit fragments via ANSI_TO_PT_STYLE — the round-trip that lets
    one rendering source feed the status line AND the picker."""

    def test_empty_input(self):
        self.assertEqual(_modifier_display([]), ([], 0))

    def test_code_tag_renders_green_fragment(self):
        fragments, width = _modifier_display([InstanceModifiers.TAG_CODE])
        styles = {style for style, _ in fragments}
        self.assertIn("fg:ansibrightgreen", styles)                      # safe modifier → green
        self.assertIn("[code]", "".join(text for _, text in fragments))
        self.assertEqual(width, len("[code]") + 1)                        # +1 trailing separator space

    def test_warn_mode_renders_red_fragment(self):
        fragments, _ = _modifier_display([InstanceModifiers.MODE_WARN_DOOD])
        self.assertIn("bold fg:ansibrightred", {style for style, _ in fragments})

    def test_every_fragment_style_known_to_mapping(self):
        fragments, _ = _modifier_display([InstanceModifiers.TAG_CODE, InstanceModifiers.MODE_WARN_AUTO])
        known = set(ANSI_TO_PT_STYLE.values()) | {""}
        self.assertTrue(all(style in known for style, _ in fragments))


class TestContinuableInstances(unittest.TestCase):
    """continuable_instances turns raw disk/map state into sorted, flagged
    Cont rows. Real repo agents (golem — haiku, poet — sonnet) provide the
    md/conf side; instance listings + maps + cwd are patched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)
        # No instance has a history.jsonl in these fixtures.
        self._mtime = patch("launch.structs.last_history_mtime", return_value=None)
        self._mtime.start()

    def tearDown(self):
        self._mtime.stop()
        self.tmpdir.cleanup()

    def _entries(self, instances, workspaces, modes, cwd=None):
        with patch.object(menu_picker, "list_all_instances", return_value=instances), \
             patch.object(menu_picker, "load_workspace_map", return_value=workspaces), \
             patch.object(menu_picker, "load_modes_map", return_value=modes), \
             patch.object(menu_picker, "resolved_cwd", return_value=cwd or Path("/nowhere")):
            return continuable_instances()

    def test_orphan_instances_skipped(self):
        entries = self._entries(["ghost__x", "golem__a"], {"golem__a": str(self.ws)}, {})
        self.assertEqual([e.identity.instance for e in entries], ["golem__a"])

    def test_modes_converted_to_typed_members(self):
        entries = self._entries(["golem__a"], {"golem__a": str(self.ws)}, {"golem__a": ["auto"]})
        self.assertEqual(entries[0].identity.modes, (InstanceModifiers.MODE_WARN_AUTO,))
        self.assertEqual(entries[0].modes_display, "auto")

    def test_modeless_sorts_before_moded_and_family_orders_within(self):
        # poet (sonnet) outranks golem (haiku); golem__b carries a mode so it
        # sinks below both modeless rows regardless of family.
        entries = self._entries(
            ["golem__a", "golem__b", "poet__p"],
            {n: str(self.ws) for n in ("golem__a", "golem__b", "poet__p")},
            {"golem__b": ["auto"]},
        )
        self.assertEqual([e.identity.instance for e in entries],
                         ["poet__p", "golem__a", "golem__b"])

    def test_current_dir_flagged(self):
        entries = self._entries(["golem__a"], {"golem__a": str(self.ws)}, {}, cwd=self.ws.resolve())
        self.assertTrue(entries[0].is_current_dir)
        self.assertFalse(entries[0].is_invalid_dir)

    def test_invalid_workspace_flagged_but_shown(self):
        entries = self._entries(["golem__a"], {"golem__a": "/no/such/dir"}, {})
        self.assertTrue(entries[0].is_invalid_dir)
        self.assertFalse(entries[0].is_current_dir)
        self.assertEqual(entries[0].workspace_display, "/no/such/dir")   # stored value still shown

    def test_missing_map_entry_shows_placeholder(self):
        entries = self._entries(["golem__a"], {}, {})
        self.assertEqual(entries[0].workspace_display, "?")
        self.assertIsNone(entries[0].identity.workspace)

    def test_never_used_renders_never(self):
        entries = self._entries(["golem__a"], {"golem__a": str(self.ws)}, {})
        self.assertEqual(entries[0].last_used_display, "(never)")


class TestPromptSession(unittest.TestCase):
    """One shared collision loop for both flows: create (default = workspace
    basename) and modify (default = current name, which is always accepted)."""

    def _run(self, answers, existing=(), **kwargs):
        answer_iter = iter(answers)
        with patch("builtins.input", side_effect=lambda _prompt: next(answer_iter)), \
             patch.object(menu_picker, "path_exists",
                          side_effect=lambda p: any(str(p).endswith(f"golem__{e}") for e in existing)), \
             patch("builtins.print"):
            return prompt_session("golem", "/some/workspace/myproj", **kwargs)

    def test_enter_accepts_workspace_basename_default(self):
        self.assertEqual(self._run([""]), "myproj")

    def test_collision_reprompts_until_free(self):
        self.assertEqual(self._run(["taken", "free"], existing=["taken"]), "free")

    def test_modify_flow_defaults_to_current_name(self):
        # Enter keeps the existing session even though its state dir exists.
        self.assertEqual(self._run([""], existing=["mysess"], current="mysess"), "mysess")

    def test_modify_flow_rejects_other_existing_names(self):
        self.assertEqual(self._run(["other", "free"], existing=["mysess", "other"], current="mysess"), "free")

    def test_rename_to_fresh_name_accepted(self):
        self.assertEqual(self._run(["newname"], existing=["mysess"], current="mysess"), "newname")


if __name__ == "__main__":
    unittest.main()
