"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
tag display), and the shared session prompt. The tag form's tests live in
test_tag_form.py; the prompt_toolkit Applications themselves are
interactive and stay out of unit scope."""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker
from launch.gui.menu_picker import (
    RUNNING_HINT, STYLE_RUNNING_NAME, PickerEntry, _agent_description,
    _cont_tags_column, _cursor_step, _focusable_indices, _tags_column,
    continuable_instances,
)
from launch.gui.tag_form import STYLE_TAG_SAFE, STYLE_TAG_WARN
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, resolve_build, scan_all
from launch.tags.base import SQUASH_AT
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


def _six_tags() -> list:
    """SQUASH_AT real tags — the smallest crowded row."""
    reg = REGISTRY
    tags = [reg.professions["code"], reg.professions["webdev"],
            reg.specialties["auto"], reg.specialties["cowork"],
            reg.specialties["manager"], reg.policies["no-sudo"]]
    assert len(tags) == SQUASH_AT
    return tags


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

    def test_preview_expands_the_tags(self):
        # The row column may show one-char chips; the preview is where a tag
        # can be READ — label plus description, not just the name.
        entries = self._entries([make_inst("golem", "a", self.ws, specialties=["auto"])])
        preview = entries[0].preview
        self.assertIn("{auto}", preview)
        self.assertIn(REGISTRY.specialties["auto"].short_description, preview)

    def test_preview_without_history_has_no_last_prompt_field(self):
        # The field drops out entirely rather than showing an empty label.
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertNotIn("Last prompt", entries[0].preview)


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


class TestInstanceFields(unittest.TestCase):
    """The instance form's fields — prompt_session's old behaviors, as a
    validator plus the auto-fill rule (no terminal prompt survives)."""

    def error(self, value, existing=(), current=None):
        with patch.object(menu_picker, "path_exists",
                          side_effect=lambda p: any(
                              str(p).endswith(f"golem__{e}") for e in existing)):
            return menu_picker._suffix_field_error("golem", value, current)

    def test_the_name_autofills_from_the_workspace_basename(self):
        # prompt_session's old default, now live: path first, name follows.
        workspace, session = menu_picker.instance_fields("golem")
        self.assertEqual(workspace.key, "workspace")   # path is the FIRST field
        self.assertIsNotNone(session.auto)
        self.assertEqual(session.auto({"workspace": "/some/workspace/myproj"}),
                         "myproj")

    def test_an_empty_path_falls_back_to_the_agent_name(self):
        # GUARDED before expanding: expand_user_path("") resolves to the CWD,
        # so an emptied field would otherwise derive from wherever the
        # launcher happens to run.
        _, session = menu_picker.instance_fields("golem")
        self.assertEqual(session.auto({"workspace": ""}), "golem")

    def test_the_cluster_name_derivation_has_the_same_empty_guard(self):
        fields = menu_picker._cluster_fields("", "devteam", derive="devteam")
        self.assertEqual(fields[1].auto({"project": ""}), "devteam")
        self.assertEqual(fields[1].auto({"project": "/code/thing"}),
                         "devteam__thing")

    def test_editing_pins_the_name(self):
        # A modify arrives with `current`: the name field sits still (renames
        # are deliberate) — no auto derivation.
        _, session = menu_picker.instance_fields(
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

    def test_empty_is_an_error(self):
        self.assertIsNotNone(self.error(""))


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


class TestPromptOffload(unittest.TestCase):
    """_read_last_prompt is the GIL escape hatch: the transcript parse runs in
    a child process (a CPU-bound thread convoys the render loop — measured at
    an 803 ms UI stall on a 155 MB state dir; see
    benchmark/bench_preview_gil.py), with an in-process fallback when a pool
    cannot serve, because degraded beats broken."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_dir = Path(self.tmpdir.name)
        transcript = self.state_dir / "projects" / "-workspace" / "s.jsonl"
        transcript.parent.mkdir(parents=True)
        import json
        transcript.write_text(json.dumps({
            "type": "user", "timestamp": "2026-08-07T10:00:00Z",
            "message": {"role": "user", "content": "offloaded prompt"}}) + "\n")

    def test_falls_back_in_process_when_the_pool_cannot_serve(self):
        class BrokenPool:
            def submit(self, *args, **kwargs):
                raise RuntimeError("no subprocesses here")

        with patch.object(menu_picker, "_PROMPT_POOL", BrokenPool()):
            found = menu_picker._read_last_prompt(self.state_dir)
        self.assertIsNotNone(found)
        self.assertEqual(found[0], "offloaded prompt")

    def test_the_child_process_reads_the_same_answer(self):
        # The one integration test of the real mechanism. Skipped where child
        # processes are forbidden — the fallback test above covers that world.
        try:
            import multiprocessing
            multiprocessing.get_context("spawn")
        except (ImportError, ValueError) as error:      # pragma: no cover
            self.skipTest(f"spawn unavailable: {error}")
        with patch.object(menu_picker, "_PROMPT_POOL", None):
            try:
                found = menu_picker._read_last_prompt(self.state_dir)
            finally:
                pool, menu_picker._PROMPT_POOL = menu_picker._PROMPT_POOL, None
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)
        self.assertEqual(found[0], "offloaded prompt")


class TestPreviewLoader(unittest.TestCase):
    """The non-blocking contract: a slow preview yields a placeholder and the
    UI thread returns immediately; the resolution runs on the worker, pokes
    invalidate once, and the next render serves the real pane. Timing is
    controlled with events — no sleeps, no flakes."""

    def setUp(self):
        self.invalidations = []
        self.loader = menu_picker._PreviewLoader(
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
        self.assertEqual(self.loader.text(3, entry), menu_picker.PREVIEW_LOADING_TEXT)
        self.assertTrue(started.wait(5))
        # Re-renders while it loads: still the placeholder, and NOT a second job.
        self.assertEqual(self.loader.text(3, entry), menu_picker.PREVIEW_LOADING_TEXT)
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
        self.assertEqual(self.loader.text(0, entry), menu_picker.PREVIEW_LOADING_TEXT)
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


def _plain_text(ansi: str) -> str:
    """`ansi` with the escape codes stripped — rich's YAML highlighting splits
    even `[loading…]` into separately-styled tokens, so substring assertions
    must run on the visible text, not the raw stream."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", ansi)


class TestLastPromptDisplay(unittest.TestCase):
    """The preview's `Last prompt` value: the transcript's newest human prompt,
    condensed for a metadata pane — collapsed whitespace (it sits inside the
    preview's YAML fence, which a raw ``` line would close early) and an
    ellipsis past 250 chars (the field recognises a conversation, it does not
    replay one)."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.state_dir = Path(self.tmpdir.name)
        # In-process read: these tests exercise the DISPLAY logic, and the
        # offload pool (tested in TestPromptOffload) must not spawn children
        # inside unit tests — a sandboxed CI may forbid subprocesses entirely.
        seam = patch.object(menu_picker, "_read_last_prompt",
                            menu_picker.last_prompt_in_state)
        seam.start()
        self.addCleanup(seam.stop)

    def _write_prompt(self, text: str) -> None:
        import json
        transcript = self.state_dir / "projects" / "-workspace" / "s.jsonl"
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps({
            "type": "user", "timestamp": "2026-08-07T10:00:00Z",
            "message": {"role": "user", "content": text}}) + "\n")

    def test_no_transcript_yields_none(self):
        self.assertIsNone(menu_picker._last_prompt_display(self.state_dir))

    def test_short_prompt_passes_through(self):
        self._write_prompt("fix the retry loop")
        self.assertEqual(menu_picker._last_prompt_display(self.state_dir),
                         "fix the retry loop")

    def test_a_long_prompt_is_cut_at_the_limit_with_an_ellipsis(self):
        self._write_prompt("x" * 400)
        shown = menu_picker._last_prompt_display(self.state_dir)
        self.assertEqual(len(shown), menu_picker.LAST_PROMPT_PREVIEW_CHARS + 1)
        self.assertTrue(shown.endswith("…"))

    def test_a_prompt_at_the_limit_is_not_touched(self):
        self._write_prompt("y" * menu_picker.LAST_PROMPT_PREVIEW_CHARS)
        self.assertEqual(menu_picker._last_prompt_display(self.state_dir),
                         "y" * menu_picker.LAST_PROMPT_PREVIEW_CHARS)

    def test_newlines_collapse_so_the_yaml_fence_survives(self):
        # A prompt containing a code fence must not close the preview's own.
        self._write_prompt("first line\n```\ncode\n```\nlast line")
        shown = menu_picker._last_prompt_display(self.state_dir)
        self.assertNotIn("\n", shown)
        self.assertEqual(shown, "first line ``` code ``` last line")

    def test_the_transcript_is_read_once_per_screen_session(self):
        # The buffering contract: nothing is read at menu build, one read on
        # first highlight, and every later render of the row reuses it — a
        # preview cannot change mid-session, and holding an arrow key cannot
        # re-walk a multi-megabyte transcript per keystroke. The cache resets
        # only when select_agent's loop rebuilds the entries (after a pick,
        # a delete, or a toolkits edit), which is when disk state may have
        # legitimately changed.
        self._write_prompt("the prompt")
        entry = menu_picker.ContEntry(
            identity=dataclasses.replace(make_inst(), state_dir_override=self.state_dir),
            workspace_display="/w", is_current_dir=False, is_default_dir=False,
            is_invalid_dir=False, last_used_display="now")
        row = menu_picker.PickerEntry(preview=menu_picker._deferred_preview(entry))
        reads = {"count": 0}
        real = menu_picker.last_prompt_in_state

        def counting(state_dir):
            reads["count"] += 1
            return real(state_dir)

        with patch.object(menu_picker, "_read_last_prompt", side_effect=counting):
            self.assertEqual(reads["count"], 0)      # deferred: menu build reads nothing
            first = row.preview_ansi()
            for _ in range(50):                      # re-renders while browsing
                self.assertEqual(row.preview_ansi(), first)
        self.assertEqual(reads["count"], 1)

    def test_quick_preview_stands_in_for_the_prompt_without_reading(self):
        # The benchmark's conclusion, enforced: 99.9% of a heavy preview is the
        # transcript read, so the quick form must carry everything EXCEPT that
        # — metadata, tags, and a [loading…] stand-in — and never touch a file.
        self._write_prompt("the real prompt")
        entry = self._entry()
        reads = {"count": 0}
        real = menu_picker.last_prompt_in_state

        def counting(state_dir):
            reads["count"] += 1
            return real(state_dir)

        with patch.object(menu_picker, "_read_last_prompt", side_effect=counting):
            quick = _plain_text(entry.preview_quick)
        self.assertEqual(reads["count"], 0)
        self.assertIn(menu_picker.LAST_PROMPT_LOADING, quick)
        self.assertNotIn("the real prompt", quick)
        self.assertIn("Agent:", quick)          # the metadata is already there
        self.assertIn("Tags:", quick)           # and so is the tag list

    def test_quick_preview_shows_no_stand_in_for_a_fresh_instance(self):
        # No history → no Last prompt field ever → no flashing loading line.
        quick = _plain_text(self._entry().preview_quick)
        self.assertNotIn("Last prompt", quick)
        self.assertNotIn(menu_picker.LAST_PROMPT_LOADING, quick)

    def test_full_preview_replaces_the_stand_in_with_the_value(self):
        self._write_prompt("the real prompt")
        entry = self._entry()
        self.assertIn(menu_picker.LAST_PROMPT_LOADING, _plain_text(entry.preview_quick))
        self.assertIn("the real prompt", _plain_text(entry.preview))
        self.assertNotIn(menu_picker.LAST_PROMPT_LOADING, _plain_text(entry.preview))

    def _entry(self) -> menu_picker.ContEntry:
        return menu_picker.ContEntry(
            identity=dataclasses.replace(make_inst(), state_dir_override=self.state_dir),
            workspace_display="/w", is_current_dir=False, is_default_dir=False,
            is_invalid_dir=False, last_used_display="now")

    def test_the_preview_field_appears_when_a_prompt_exists(self):
        self._write_prompt("the question I asked")
        inst = make_inst("golem", "a", "/tmp")
        entry = menu_picker.ContEntry(
            identity=dataclasses.replace(inst, state_dir_override=self.state_dir),
            workspace_display="/tmp", is_current_dir=False, is_default_dir=False,
            is_invalid_dir=False, last_used_display="(never)")
        self.assertIn("Last prompt", entry.preview)
        self.assertIn("the question I asked", entry.preview)


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


class TestRowMarkers(unittest.TestCase):
    """PickerRowMarker — the bookmark lead-ins that contrast the picker's two
    row kinds: an agent row is a green TAB with a fading end, its instances
    nest beneath a dim grey indented ▸. Emoji stays on the menu rows only."""

    def test_the_agent_row_leads_with_a_fading_tab(self):
        # Tab body, then tip, in that order — the Starship-segment shape. Both
        # creation tabs lead with `+` (the verb), then name what gets created.
        (body_style, body), (tip_style, tip) = menu_picker.PickerRowMarker.NEW.lead
        self.assertIn("+ Agent", body)
        self.assertIn("+ Cluster", menu_picker.PickerRowMarker.CLUSTER.lead[0][1])
        self.assertEqual(tip, menu_picker.TAB_TIP)
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
        for char in menu_picker.TAB_TIP:
            with self.subTest(char=hex(ord(char))):
                self.assertIn(ord(char), menu_picker.BLOCK_ELEMENTS)
        # A ramp needs at least two steps to read as a fade rather than a nub.
        self.assertGreaterEqual(len(menu_picker.TAB_TIP), 2)

    def test_instances_nest_under_the_tab_not_beside_it(self):
        ((style, text),) = menu_picker.PickerRowMarker.CONT.lead
        self.assertTrue(text.startswith("   "), "indent is the nesting cue")
        self.assertIn("▸", text)
        # Dim furniture, no tab: a second tab would read as a second agent.
        self.assertNotIn("bg:", style)

    def test_the_leads_are_universal_unicode_not_private_use(self):
        # The whole point of ▶/▸ over Nerd-Font wedges: stock fonts cover them.
        # PUA ranges: BMP E000–F8FF, planes 15/16 F0000–10FFFD. Emoji (1F3xx)
        # sit outside all three and stay legal for the menu rows.
        private_use = lambda cp: (0xE000 <= cp <= 0xF8FF or
                                  0xF0000 <= cp <= 0x10FFFD)
        for marker in menu_picker.PickerRowMarker:
            for _, text in marker.lead:
                for char in text:
                    with self.subTest(marker=marker.name, char=hex(ord(char))):
                        self.assertFalse(private_use(ord(char)))

    def test_the_alignment_suffix_never_wears_the_tab_background(self):
        # Glued onto the last lead fragment, the suffix would smear the tab's
        # background across the gap to the tag column.
        *_, last = menu_picker.PickerRowMarker.NEW.fragments("  ")
        self.assertEqual(last, ("", "  "))
        # And no suffix means no empty trailing fragment.
        self.assertEqual(menu_picker.PickerRowMarker.NEW.fragments(),
                         list(menu_picker.PickerRowMarker.NEW.lead))

    def test_the_kind_colours_agree_between_tab_and_accent_bar(self):
        # The tab wears Create's kind colour (green, iterated from an all-grey
        # first pass) and the preview's edge bar shows the same one — two
        # different greens would read as two different meanings. Cont keeps
        # its yellow on the accent bar only; its dim lead carries no colour.
        tab_bg = menu_picker.STYLE_TAB.split("bg:", 1)[1].split()[0]
        self.assertEqual(menu_picker.PickerRowMarker.NEW.accent, f"fg:{tab_bg}")
        self.assertEqual(menu_picker.PickerRowMarker.CONT.accent, "fg:ansiyellow")


class TestRowAssembly(unittest.TestCase):
    """select_agent's entry building, run for real with the TUI stubbed out.
    The markers are multi-fragment now and SPLATTED into each display list —
    a call site still treating one as a single fragment would only blow up
    when the picker opens interactively, which no other test does."""

    def entries(self):
        captured = {}

        def fake_pick(title, entries, **kw):
            captured["entries"] = entries
            return (None, None)

        # One real store-backed instance rides along, so the instance-row
        # assertions below can never pass vacuously — without this, the test
        # environment has no instances and `inst_rows` would be empty (the
        # exact silent-guard failure a mutation run caught once already).
        inst = make_inst("golem", "assembly", "/tmp")
        with patch.object(menu_picker, "pick_with_preview", fake_pick), \
             patch.object(menu_picker, "list_all_instances",
                          return_value=[inst.instance]), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: inst), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=None), \
             patch("launch.tags.identity.last_history_mtime", return_value=None):
            self.assertIsNone(menu_picker.select_agent(REGISTRY))
        return captured["entries"]

    def test_every_display_fragment_is_a_style_text_pair(self):
        for entry in self.entries():
            for fragment in entry.display:
                with self.subTest(fragment=fragment):
                    style, text = fragment          # unpacking IS the assertion
                    self.assertIsInstance(style, str)
                    self.assertIsInstance(text, str)

    def test_agent_rows_open_with_the_tab_and_its_tip(self):
        agent_rows = [e for e in self.entries()
                      if isinstance(e.value, menu_picker.Agent)]
        self.assertTrue(agent_rows)
        for row in agent_rows:
            with self.subTest(agent=row.value.name):
                self.assertEqual(tuple(row.display[:2]),
                                 menu_picker.PickerRowMarker.NEW.lead)

    def test_instance_rows_open_with_the_nested_marker(self):
        inst_rows = [e for e in self.entries()
                     if isinstance(e.value, Instance)]
        self.assertTrue(inst_rows, "fixture must yield at least one Cont row")
        for row in inst_rows:
            with self.subTest(instance=row.value.instance):
                self.assertEqual((row.display[0],),
                                 menu_picker.PickerRowMarker.CONT.lead)

    def test_each_shipped_template_gets_a_cluster_row(self):
        # The real tree ships devteam.legoset; its row opens the creation flow
        # (the value carries the template path for the dispatcher).
        rows = [e for e in self.entries()
                if isinstance(e.value, menu_picker._ClusterTemplateRow)]
        self.assertEqual([r.value.name for r in rows], ["devteam"])
        (row,) = rows
        self.assertEqual(tuple(row.display[:2]),
                         menu_picker.PickerRowMarker.CLUSTER.lead)
        # Agent-row anatomy: member COUNT (in creation-green) where agents
        # show tags, then the name, then " — description" — which describes
        # what the TEAM does (the .legoset's description key), never a member
        # list; the enumeration lives in the preview.
        text = "".join(t for _, t in row.display)
        self.assertIn("(5 members)", text)
        count_style = next(style for style, t in row.display if "members)" in t)
        self.assertEqual(count_style, menu_picker.STYLE_MEMBER_COUNT)
        self.assertIn("green", menu_picker.STYLE_MEMBER_COUNT)
        self.assertIn(" — Builds features end to end", text)
        self.assertNotIn("researcher__primary", text)

    def test_the_template_name_sits_in_the_agents_name_column(self):
        # "indented the same distance as agents' entries": the cluster tab is
        # wider than the agent tab, so the count column is PADDED to land the
        # template name exactly where agent names start — measured per row
        # text, not trusted from the arithmetic that produced it.
        entries = self.entries()
        agent_row = next(e for e in entries
                         if isinstance(e.value, menu_picker.Agent))
        cluster_row = next(e for e in entries
                           if isinstance(e.value, menu_picker._ClusterTemplateRow))
        agent_text = "".join(t for _, t in agent_row.display)
        cluster_text = "".join(t for _, t in cluster_row.display)
        self.assertEqual(cluster_text.index(cluster_row.value.name),
                         agent_text.index(agent_row.value.name))

    def test_a_broken_template_renders_unselectable_not_a_crash(self):
        # Templates are hand-authored; the picker is where the author IS, so a
        # parse error must become a red info row naming the fault.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "oops.legoset"
            bad.write_text("members = []")
            with patch.object(menu_picker, "discover_templates",
                              return_value={"oops": bad}):
                rows = [e for e in self.entries() if e.selectable is False
                        and "broken template" in "".join(t for _, t in e.display)]
        self.assertEqual(len(rows), 1)


class TestCreateClusterFlow(unittest.TestCase):
    """_create_cluster_flow — prompts and form stubbed, persistence real (into
    a redirected AGENTS_STATE). What must hold: a confirm SAVES exactly the
    picked members with template roles and auto-numbering applied, and a
    cancel saves nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The whole cluster feature composes its paths from AGENTS_STATE at
        # call time, so one patch on launch.paths moves it (the design
        # test_cluster_state relies on too).
        from launch import paths as launch_paths
        patcher = patch.object(launch_paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def flow(self, picks):
        # The form now carries name + path as TEXT FIELDS; its return is
        # (field values, picks) — None still means cancel.
        template_path = AGENTS_DIR / "devteam.legoset"
        answer = None if picks is None else (
            {"session": "myteam", "project": "/tmp/project"}, picks)
        with patch.object(menu_picker, "prompt_members",
                          return_value=answer) as form, \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            menu_picker._create_cluster_flow(REGISTRY, template_path)
        return form

    def test_confirming_creates_the_cluster_with_previewed_ids(self):
        self.flow([("golem", None), ("golem", None), ("researcher", "primary")])
        from launch.cluster import state
        cluster = state.load("myteam")
        self.assertEqual(cluster.ids, ("golem__1", "golem__2",
                                       "researcher__primary"))
        self.assertEqual(str(cluster.project), "/tmp/project")
        # Members carry their agents' .lego defaults, not empty builds.
        self.assertEqual(cluster.member("researcher__primary").build.engine,
                         "researcher")

    def test_the_form_opens_prefilled_with_the_template(self):
        form = self.flow([("golem", None)])
        prefill = form.call_args.args[1]
        self.assertEqual(prefill[2:4], [("researcher", "primary"),
                                        ("researcher", "adversarial")])
        # Unroled template entries arrive as None so duplicates can renumber.
        self.assertEqual(prefill[0], ("project-starter", None))
        # And the fields ride in prefilled: template name, default workspace.
        # Project FIRST — it feeds the name's auto-fill; the name derives
        # <template>__<basename> live once the form opens.
        fields = form.call_args.kwargs["fields"]
        self.assertEqual([(f.key, f.value) for f in fields],
                         [("project", menu_picker.DEFAULT_WORKSPACE),
                          ("session", "devteam")])
        self.assertIsNotNone(fields[1].auto)
        self.assertEqual(fields[1].auto({"project": "/code/thing"}),
                         "devteam__thing")

    def test_cancelling_the_form_saves_nothing(self):
        self.flow(None)
        from launch.cluster import state
        self.assertEqual(state.discover(), [])


class TestClusterRowsAndEditing(unittest.TestCase):
    """Existing clusters in the picker: the rows, and the three verbs on them —
    F2 re-tags a member, Del removes one (guarding the last), Del on the
    cluster destroys it. Flows run against real persistence in a redirected
    AGENTS_STATE; only the interactive pieces (form, prompts, confirm) are
    stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from launch import paths as launch_paths
        patcher = patch.object(launch_paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        from launch.cluster import state
        from launch.cluster.member import Member
        self.state = state
        self.cluster = state.save(state.from_template(
            "team", Path("/tmp/project"),
            (Member.of("golem"), Member.of("researcher", "primary")),
            template="devteam"))

    def entries(self, running=None):
        captured = {}
        with patch.object(menu_picker, "pick_with_preview",
                          lambda t, entries, **kw:
                          captured.update(entries=entries) or (None, None)), \
             patch.object(menu_picker, "list_all_instances", return_value=[]), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=running):
            menu_picker.select_agent(REGISTRY)
        return captured["entries"]

    def test_a_cluster_row_then_its_members_nested_beneath(self):
        entries = self.entries()
        kinds = [type(e.value).__name__ for e in entries
                 if isinstance(e.value, (menu_picker._ClusterRow,
                                         menu_picker._MemberRow))]
        self.assertEqual(kinds, ["_ClusterRow", "_MemberRow", "_MemberRow"])
        cluster_row = next(e for e in entries
                           if isinstance(e.value, menu_picker._ClusterRow))
        self.assertEqual(tuple(cluster_row.display[:1]),
                         menu_picker.PickerRowMarker.CLSTR.lead)
        self.assertTrue(cluster_row.modifiable)   # F2: rename / repoint / membership
        self.assertTrue(cluster_row.deletable)

    def test_member_rows_are_the_editing_unit(self):
        member_rows = [e for e in self.entries()
                       if isinstance(e.value, menu_picker._MemberRow)]
        self.assertEqual([r.value.member_id for r in member_rows],
                         ["golem", "researcher__primary"])
        for row in member_rows:
            with self.subTest(member=row.value.member_id):
                self.assertTrue(row.modifiable)
                self.assertTrue(row.deletable)
                # The row shows the member's tags — {muxer}{cluster} forced at
                # creation are the visible proof it is a cluster member.
                text = "".join(t for _, t in row.display)
                self.assertIn("{clstr}", text)

    def test_a_running_cluster_locks_its_row_and_its_members(self):
        # The live container is named claude-code_cluster-team, so the probe's
        # stripped set carries "cluster-team" — the same detection instances
        # get. All three keys go dark on the cluster row (Enter → docker name
        # conflict; F2 rename / Del would move or delete the state dir the
        # container has mounted) and on every member row (their edits write
        # into that same dir), and the row wears the red RUNNING tag.
        rows = [e for e in self.entries(running=frozenset({"cluster-team"}))
                if isinstance(e.value, (menu_picker._ClusterRow,
                                        menu_picker._MemberRow))]
        self.assertEqual(len(rows), 3)   # the cluster and both members
        for row in rows:
            with self.subTest(value=row.value):
                self.assertFalse(row.selectable)
                self.assertFalse(row.deletable)
                self.assertFalse(row.modifiable)
        self.assertIn(menu_picker.RUNNING_HINT, rows[0].display)

    def test_a_different_running_cluster_locks_nothing_here(self):
        cluster_row = next(e for e in self.entries(
            running=frozenset({"cluster-other"}))
            if isinstance(e.value, menu_picker._ClusterRow))
        self.assertTrue(cluster_row.selectable)
        self.assertNotIn(menu_picker.RUNNING_HINT, cluster_row.display)

    def test_f2_persists_the_new_build_with_forced_tags_reapplied(self):
        with patch.object(menu_picker, "prompt_tags",
                          return_value=AgentBuild(professions=("code",))):
            menu_picker._edit_member_flow(REGISTRY, "team", "golem")
        edited = self.state.load("team").member("golem")
        self.assertEqual(edited.build.professions, ("code",))
        # The form returned a build WITHOUT them; persistence must not.
        self.assertEqual(edited.build.specialties, ("muxer", "cluster"))

    def test_cancelling_the_tag_form_changes_nothing(self):
        with patch.object(menu_picker, "prompt_tags", return_value=None):
            menu_picker._edit_member_flow(REGISTRY, "team", "golem")
        self.assertEqual(self.state.load("team"), self.cluster)

    def test_del_removes_the_member_after_confirmation(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=True):
            menu_picker._remove_member_flow("team", "researcher__primary")
        self.assertEqual(self.state.load("team").ids, ("golem",))

    def test_an_unconfirmed_removal_changes_nothing(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=False):
            menu_picker._remove_member_flow("team", "golem")
        self.assertEqual(self.state.load("team").ids,
                         ("golem", "researcher__primary"))

    def test_the_last_member_cannot_be_removed(self):
        # An empty cluster is unrepresentable; the honest gesture is destroying
        # the cluster, which the guard message points at.
        with patch.object(menu_picker, "confirm_dialog", return_value=True), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print") as told:
            menu_picker._remove_member_flow("team", "researcher__primary")
            menu_picker._remove_member_flow("team", "golem")
        self.assertEqual(self.state.load("team").ids, ("golem",))
        self.assertIn("only member", str(told.call_args_list))

    def test_del_on_the_cluster_row_destroys_it(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=True):
            menu_picker._destroy_cluster_flow("team")
        self.assertFalse(self.state.exists("team"))

    def test_an_unconfirmed_destroy_keeps_everything(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=False):
            menu_picker._destroy_cluster_flow("team")
        self.assertTrue(self.state.exists("team"))

    def dispatch(self, action, value):
        """Drive select_agent's dispatch once: the stubbed picker returns the
        given (action, value), then cancels on the next loop iteration."""
        answers = iter([(action, value), (None, None)])
        with patch.object(menu_picker, "pick_with_preview",
                          lambda *a, **kw: next(answers)), \
             patch.object(menu_picker, "list_all_instances", return_value=[]), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=None), \
             patch.object(menu_picker, "confirm_dialog", return_value=True):
            self.assertIsNone(menu_picker.select_agent(REGISTRY))

    def test_the_delete_key_routes_a_member_row_to_member_removal(self):
        # The DELETE branch used to assume Instance (`value.instance`) — a
        # member row reaching it must shrink the cluster, not crash or, worse,
        # destroy the whole cluster.
        self.dispatch(menu_picker.PickerAction.DELETE,
                      menu_picker._MemberRow("team", "golem"))
        self.assertEqual(self.state.load("team").ids, ("researcher__primary",))

    def test_the_modify_key_routes_a_member_row_to_the_tag_form(self):
        with patch.object(menu_picker, "prompt_tags",
                          return_value=AgentBuild(engine="thinker")):
            self.dispatch(menu_picker.PickerAction.MODIFY,
                          menu_picker._MemberRow("team", "golem"))
        self.assertEqual(self.state.load("team").member("golem").build.engine,
                         "thinker")

    def test_the_delete_key_routes_a_cluster_row_to_destruction(self):
        self.dispatch(menu_picker.PickerAction.DELETE,
                      menu_picker._ClusterRow("team"))
        self.assertFalse(self.state.exists("team"))


class TestEditClusterFlow(TestClusterRowsAndEditing):
    """F2 on the cluster row — one form edits name, project, and membership.
    Inherits the fixture (cluster 'team': golem + researcher__primary in a
    redirected AGENTS_STATE); the form itself is stubbed, everything it
    returns is applied for real."""

    def edit(self, session="team", values=None, picks=None):
        answer = None if values is None else (values, picks)
        with patch.object(menu_picker, "prompt_members",
                          return_value=answer) as form, \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            menu_picker._edit_cluster_flow(REGISTRY, session)
        return form

    def keep_picks(self):
        return [("golem", None), ("researcher", "primary")]

    def test_the_form_opens_prefilled_with_the_cluster(self):
        form = self.edit(values={"session": "team", "project": "/tmp/project"},
                         picks=self.keep_picks())
        self.assertEqual(form.call_args.args[1],
                         [("golem", None), ("researcher", "primary")])
        fields = form.call_args.kwargs["fields"]
        self.assertEqual([(f.key, f.value) for f in fields],
                         [("project", "/tmp/project"), ("session", "team")])
        # Editing pins the name — no auto derivation on a rename form.
        self.assertIsNone(fields[1].auto)
        # Keeping your own name must not read as a collision.
        self.assertIsNone(fields[1].validate("team"))
        self.assertIsNotNone(menu_picker._session_field_error("team", None))

    def test_renaming_moves_the_whole_directory(self):
        # Member state dirs ride the move — a rename must not orphan them.
        from launch import paths as launch_paths
        member_file = (launch_paths.cluster_member_dir("team", "golem")
                       / "CLAUDE.md")
        member_file.parent.mkdir(parents=True)
        member_file.write_text("persona")
        self.edit(values={"session": "crew", "project": "/tmp/project"},
                  picks=self.keep_picks())
        self.assertFalse(self.state.exists("team"))
        renamed = self.state.load("crew")
        self.assertEqual(renamed.ids, ("golem", "researcher__primary"))
        self.assertEqual((launch_paths.cluster_member_dir("crew", "golem")
                          / "CLAUDE.md").read_text(), "persona")

    def test_repointing_the_project(self):
        self.edit(values={"session": "team", "project": "/tmp"},
                  picks=self.keep_picks())
        self.assertEqual(str(self.state.load("team").project), "/tmp")

    def test_surviving_members_keep_their_edited_builds(self):
        # THE reassemble guarantee: an unrelated edit (rename, membership
        # change) must not wipe a member's F2-edited tags back to .lego.
        self.state.save(self.cluster.with_build(
            "golem", AgentBuild(engine="thinker")))
        self.edit(values={"session": "team", "project": "/tmp/project"},
                  picks=self.keep_picks() + [("poet", None)])
        edited = self.state.load("team")
        self.assertEqual(edited.member("golem").build.engine, "thinker")
        # The newcomer starts from its .lego plus the forced tags.
        self.assertEqual(edited.member("poet").build.specialties,
                         ("muxer", "cluster"))

    def test_membership_shrinks_when_a_pick_is_dropped(self):
        self.edit(values={"session": "team", "project": "/tmp/project"},
                  picks=[("golem", None)])
        self.assertEqual(self.state.load("team").ids, ("golem",))

    def test_cancel_changes_nothing(self):
        self.edit(values=None)
        self.assertEqual(self.state.load("team"), self.cluster)

    def test_the_modify_key_routes_a_cluster_row_here(self):
        with patch.object(menu_picker, "prompt_members",
                          return_value=({"session": "crew",
                                         "project": "/tmp/project"},
                                        self.keep_picks())), \
             patch("builtins.input", return_value=""):
            self.dispatch(menu_picker.PickerAction.MODIFY,
                          menu_picker._ClusterRow("team"))
        self.assertTrue(self.state.exists("crew"))


class TestPromptStop(unittest.TestCase):
    """prompt_stop — the `--stop` flag's selector. Running rows only, wearing
    the picker's Cont-row anatomy WITHOUT the (RUNNING) hint (everything here
    runs by definition), `{muxer}` emphasized wherever present, clusters and
    stray container ids included, and the checked keys returned verbatim in
    the running-snapshot's prefix-stripped spelling."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ws = self.tmpdir.name
        patcher = patch("launch.tags.identity.last_history_mtime", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, insts, running, clusters=(), picked=None):
        captured = {}

        def fake_form(title, options, **kwargs):
            captured["title"] = title
            captured["options"] = options
            return picked

        by_id = {i.instance: i for i in insts}
        with patch.object(menu_picker, "list_all_instances",
                          return_value=list(by_id)), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: by_id.get(name)), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=frozenset(running)), \
             patch.object(menu_picker, "resolved_cwd",
                          return_value=Path("/nowhere")), \
             patch.object(menu_picker.cluster_state, "discover",
                          return_value=list(clusters)), \
             patch.object(menu_picker, "checkbox_form",
                          side_effect=fake_form), \
             patch("builtins.print"):
            result = menu_picker.prompt_stop(REGISTRY)
        return result, captured

    @staticmethod
    def _row_text(option):
        return "".join(text for _, text in option.label)

    def test_only_running_instances_are_offered(self):
        insts = [make_inst("golem", "up", self.ws),
                 make_inst("golem", "down", self.ws)]
        _, captured = self._run(insts, running={"golem__up"})
        self.assertEqual([o.key for o in captured["options"]], ["golem__up"])

    def test_no_running_hint_and_the_row_keeps_the_picker_anatomy(self):
        # tags · name · workspace — but never "(RUNNING)": in this list it
        # would say nothing.
        insts = [make_inst("golem", "up", self.ws, specialties=["auto"])]
        _, captured = self._run(insts, running={"golem__up"})
        row = self._row_text(captured["options"][0])
        self.assertIn("golem__up", row)
        self.assertIn(self.ws, row)
        self.assertNotIn("(RUNNING)", row)

    def test_muxer_is_emphasized_other_tags_are_not(self):
        insts = [make_inst("golem", "up", self.ws,
                           specialties=["auto", "muxer"])]
        _, captured = self._run(insts, running={"golem__up"})
        styles = {text: style for style, text in captured["options"][0].label}
        self.assertIn(menu_picker.TAG_EMPHASIS, styles["{mux}"])
        self.assertNotIn(menu_picker.TAG_EMPHASIS, styles["{auto}"])

    def test_running_clusters_get_a_row_keyed_by_container_id(self):
        from types import SimpleNamespace
        cluster = SimpleNamespace(session="team", members=[1, 2],
                                  project=Path("/proj"), ids=("a", "b"))
        _, captured = self._run([], running={"cluster-team"},
                                clusters=[cluster])
        (row,) = captured["options"]
        self.assertEqual(row.key, "cluster-team")
        self.assertIn("team", self._row_text(row))
        self.assertIn("/proj", self._row_text(row))

    def test_a_stray_running_id_still_gets_a_stoppable_row(self):
        # A container with no store entry and no cluster is exactly what
        # someone reaching for --stop most needs to be able to stop.
        _, captured = self._run([], running={"mystery__leftover"})
        (row,) = captured["options"]
        self.assertEqual(row.key, "mystery__leftover")

    def test_esc_stops_nothing(self):
        insts = [make_inst("golem", "up", self.ws)]
        result, _ = self._run(insts, running={"golem__up"}, picked=None)
        self.assertEqual(result, [])

    def test_picked_keys_come_back_verbatim(self):
        insts = [make_inst("golem", "up", self.ws)]
        result, _ = self._run(insts, running={"golem__up"},
                              picked=["golem__up"])
        self.assertEqual(result, ["golem__up"])

    def test_nothing_running_skips_the_form_entirely(self):
        result, captured = self._run([make_inst("golem", "s", self.ws)],
                                     running=set())
        self.assertEqual(result, [])
        self.assertNotIn("options", captured)   # checkbox_form never opened


if __name__ == "__main__":
    unittest.main()
