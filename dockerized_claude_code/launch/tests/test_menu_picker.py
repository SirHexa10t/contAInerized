"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
tag display), and the shared session prompt. The tag form's tests live in
test_tag_form.py; the prompt_toolkit Applications themselves are
interactive and stay out of unit scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker
from launch.gui.menu_picker import (
    RUNNING_HINT, STYLE_RUNNING_NAME, PickerEntry, _agent_description,
    _cont_tags_column, _cursor_step, _focusable_indices, _tags_column,
    continuable_instances, prompt_session,
)
from launch.gui.tag_form import STYLE_TAG_SAFE, STYLE_TAG_WARN
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, resolve_build, scan_all
from launch.gui.tag_form import STYLE_TAG_INVALID

REGISTRY = scan_all(AGENTS_DIR)


def make_inst(agent="poet", session="s", workspace="/tmp", *,
              professions=(), specialties=(), policies=()):
    """A real Instance resolved against the real registry (engine falls back
    agent-name → default, exactly like a launch)."""
    build = AgentBuild(engine=None, professions=tuple(professions),
                       specialties=tuple(specialties), policies=tuple(policies))
    return Instance(agent=agent, md_path=Path(f"/fake/{agent}.md"), session=session,
                    workspace=workspace, is_brand_new=False,
                    **resolve_build(build, agent, REGISTRY))


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


class TestInstanceBuild(unittest.TestCase):
    def test_round_trips_axis_names(self):
        inst = make_inst(professions=["code", "webdev"], specialties=["auto"],
                         policies=["no-sudo"])
        build = inst.build
        self.assertEqual(build.professions, ("code", "webdev"))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("no-sudo",))

    def test_engine_name_captured(self):
        self.assertEqual(make_inst(agent="poet").build.engine, "poet")


class TestContinuableInstances(unittest.TestCase):
    """continuable_instances turns store-backed Instances into sorted,
    flagged Cont rows. Real repo tags/engines provide the resolution side
    (poet — sonnet, golem — haiku); the store factory + listing + cwd are
    patched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ws = self.tmpdir.name
        # No instance has a history.jsonl in these fixtures.
        patcher = patch("launch.tags.identity.last_history_mtime", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entries(self, insts, cwd=None, running=None):
        # `running` mirrors docker_running_instances_subprocess: a frozenset of
        # running instance ids, or None for "couldn't determine". Default None
        # keeps every other test docker-free.
        by_id = {i.instance: i for i in insts}
        with patch.object(menu_picker, "list_all_instances", return_value=list(by_id) + ["ghost__x"]), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: by_id.get(name)), \
             patch.object(menu_picker, "docker_running_instances_subprocess", return_value=running), \
             patch.object(menu_picker, "resolved_cwd", return_value=cwd or Path("/nowhere")):
            return continuable_instances(REGISTRY)

    def test_orphan_instances_skipped(self):
        # instance_from_store returns None for ghost__x (no .md) — row dropped.
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertEqual([e.identity.instance for e in entries], ["golem__a"])

    def test_tags_display_names(self):
        entries = self._entries([make_inst("golem", "a", self.ws, specialties=["auto"])])
        self.assertEqual(entries[0].tags_display, "auto")

    def test_tagless_sorts_before_tagged_and_family_orders_within(self):
        # poet (sonnet) outranks golem (haiku); golem__b carries a specialty
        # so it sinks below both tag-less rows regardless of family.
        entries = self._entries([
            make_inst("golem", "a", self.ws),
            make_inst("golem", "b", self.ws, specialties=["auto"]),
            make_inst("poet", "p", self.ws),
        ])
        self.assertEqual([e.identity.instance for e in entries],
                         ["poet__p", "golem__a", "golem__b"])

    def test_current_dir_flagged(self):
        entries = self._entries([make_inst("golem", "a", self.ws)],
                                cwd=Path(self.ws).resolve())
        self.assertTrue(entries[0].is_current_dir)
        self.assertFalse(entries[0].is_invalid_dir)

    def test_invalid_workspace_flagged_but_shown(self):
        entries = self._entries([make_inst("golem", "a", "/no/such/dir")])
        self.assertTrue(entries[0].is_invalid_dir)
        self.assertFalse(entries[0].is_current_dir)
        self.assertEqual(entries[0].workspace_display, "/no/such/dir")   # stored value still shown

    def test_missing_workspace_shows_placeholder(self):
        entries = self._entries([make_inst("golem", "a", None)])
        self.assertEqual(entries[0].workspace_display, "?")
        self.assertIsNone(entries[0].identity.workspace)

    def test_never_used_renders_never(self):
        entries = self._entries([make_inst("golem", "a", self.ws)])
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


class TestContTagsColumn(unittest.TestCase):
    """_cont_tags_column renders a Cont row's tags: resolved ones colored
    normally, then any invalid (stored-but-unresolvable) names in the
    red-background/black-foreground alert style, in the expected kind's
    punctuation."""

    def _inst_with_invalid(self, build):
        clean, problems = REGISTRY.resolve_store_build(build)
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


class TestRunningFlag(TestContinuableInstances):
    """continuable_instances marks which instances have a live container, and
    the picker renders those rows greyed + tagged instead of blue."""

    def test_running_instance_flagged(self):
        entries = self._entries([make_inst("golem", "a", self.ws), make_inst("poet", "b", self.ws)],
                                running=frozenset({"golem__a"}))
        flags = {e.identity.instance: e.is_running for e in entries}
        self.assertTrue(flags["golem__a"])
        self.assertFalse(flags["poet__b"])

    def test_nothing_running_flags_nothing(self):
        entries = self._entries([make_inst("golem", "a", self.ws)], running=frozenset())
        self.assertFalse(entries[0].is_running)

    def test_undeterminable_docker_state_flags_nothing(self):
        # None = `docker ps` failed / docker absent. Marking every row RUNNING
        # would wrongly lock instances the user can actually launch.
        entries = self._entries([make_inst("golem", "a", self.ws)], running=None)
        self.assertFalse(entries[0].is_running)

    def test_running_row_greyed_and_tagged_and_locked(self):
        (entry,) = self._entries([make_inst("golem", "a", self.ws)], running=frozenset({"golem__a"}))
        self.assertTrue(entry.is_running)
        self.assertEqual(STYLE_RUNNING_NAME, "fg:ansibrightblack")   # grey, not STYLE_AGENT_NAME blue
        self.assertEqual(RUNNING_HINT[1].strip(), "(RUNNING)")
        self.assertIn("red", RUNNING_HINT[0])                        # the tag itself is the red part


if __name__ == "__main__":
    unittest.main()