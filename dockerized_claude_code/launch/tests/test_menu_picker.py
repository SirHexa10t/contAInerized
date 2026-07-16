"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
modes conversion), the shared session prompt, and the checkbox form's pure
parts (option assembly, attached_to ordering, live-warning computation).
The prompt_toolkit Applications themselves (pick_with_preview,
checkbox_form) are interactive and stay out of unit scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import menu_picker
from launch.menu_picker import (
    FormOption, _agent_description, _mode_form_options, _modifier_display,
    _normalize, _notice_warnings_by_value, _plain, active_warnings,
    continuable_instances, ordered_form_options, prompt_session,
)
from launch.structs import ANSI_TO_PT_STYLE, InstanceModifiers
from launch.template_code.modifier_prompts import MODIFIER_YN_PROMPTS


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


# ============================================================
# Checkbox form — pure assembly / ordering / warning logic
# ============================================================


class TestModeFormOptions(unittest.TestCase):
    """_mode_form_options — the pure assembly behind the mode form: which
    rows appear (applies_to gating), their order (declaration order), their
    keys (canonical values), and which arrive pre-checked (current_modes)."""

    def test_tagless_agent_gets_auto_only(self):
        # DooD / web declare a [code] prerequisite; auto has none.
        keys = [o.key for o in _mode_form_options(())]
        self.assertEqual(keys, [InstanceModifiers.MODE_WARN_AUTO.value])

    def test_code_agent_gets_all_modes_in_declaration_order(self):
        keys = [o.key for o in _mode_form_options((InstanceModifiers.TAG_CODE,))]
        self.assertEqual(keys, ["auto", "DooD", "web"])

    def test_current_modes_precheck(self):
        opts = _mode_form_options((InstanceModifiers.TAG_CODE,),
                                  (InstanceModifiers.MODE_WARN_DOOD,))
        self.assertEqual({o.key for o in opts if o.checked}, {"DooD"})

    def test_nothing_prechecked_for_new_instance(self):
        self.assertFalse(any(o.checked for o in _mode_form_options((InstanceModifiers.TAG_CODE,))))

    def test_labels_state_rather_than_ask(self):
        # The YN-prompt headers are questions; the form drops the trailing '?'.
        for o in _mode_form_options((InstanceModifiers.TAG_CODE,)):
            self.assertFalse(_plain(o.label).rstrip().endswith("?"),
                             f"{o.key} label still reads as a question")

    def test_every_mode_has_prompt_copy(self):
        # A mode absent from MODIFIER_YN_PROMPTS silently never appears in
        # the form — guard the pairing, like test_essential_files does for
        # the _apply_* handlers.
        self.assertEqual(set(InstanceModifiers.modes()), set(MODIFIER_YN_PROMPTS))


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
    """active_warnings against the real MODIFIER_NOTICE_PROMPTS copy (re-keyed
    by value via _notice_warnings_by_value) — the form's live warning zone.
    Same subset semantics the old post-persist warning gate enforced."""

    def setUp(self):
        self.warnings = _notice_warnings_by_value()

    def test_auto_plus_dood_fires(self):
        self.assertEqual(len(active_warnings({"auto", "DooD"}, self.warnings)), 1)

    def test_superset_still_fires(self):
        self.assertEqual(len(active_warnings({"auto", "DooD", "web"}, self.warnings)), 1)

    def test_singles_dont_fire(self):
        self.assertEqual(active_warnings({"auto"}, self.warnings), [])
        self.assertEqual(active_warnings({"DooD"}, self.warnings), [])

    def test_empty_selection_no_warnings(self):
        self.assertEqual(active_warnings(set(), self.warnings), [])


if __name__ == "__main__":
    unittest.main()
