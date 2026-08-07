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
    continuable_instances, prompt_session,
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


if __name__ == "__main__":
    unittest.main()