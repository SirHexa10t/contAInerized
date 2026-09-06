"""Tests for launch.gui.picker_previews — the preview pane's text: the
child-process read that keeps a 155 MB transcript off the UI thread (and its
in-process fallback), the prompt's formatting, and the Cont-row composition.

Split out of test_menu_picker 2026-09-03 with the code. The LOADER's tests
stayed there: when to compute a preview and where to park the result is the
widget's business."""

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker, picker_previews, picker_widget
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, resolve_build, scan_all

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



def _plain_text(ansi: str) -> str:
    """`ansi` with the escape codes stripped — rich's YAML highlighting splits
    even `[loading…]` into separately-styled tokens, so substring assertions
    must run on the visible text, not the raw stream."""
    import re
    return re.sub(r"\x1b\[[0-9;]*m", "", ansi)


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

        with patch.object(picker_previews, "_PROMPT_POOL", BrokenPool()):
            found = picker_previews._read_last_prompt(self.state_dir)
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
        with patch.object(picker_previews, "_PROMPT_POOL", None):
            try:
                found = picker_previews._read_last_prompt(self.state_dir)
            finally:
                pool, picker_previews._PROMPT_POOL = picker_previews._PROMPT_POOL, None
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)
        self.assertEqual(found[0], "offloaded prompt")


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
        seam = patch.object(picker_previews, "_read_last_prompt",
                            picker_previews.last_prompt_in_state)
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
        self.assertIsNone(picker_previews._last_prompt_display(self.state_dir))

    def test_short_prompt_passes_through(self):
        self._write_prompt("fix the retry loop")
        self.assertEqual(picker_previews._last_prompt_display(self.state_dir),
                         "fix the retry loop")

    def test_a_long_prompt_is_cut_at_the_limit_with_an_ellipsis(self):
        self._write_prompt("x" * 400)
        shown = picker_previews._last_prompt_display(self.state_dir)
        self.assertEqual(len(shown), picker_previews.LAST_PROMPT_PREVIEW_CHARS + 1)
        self.assertTrue(shown.endswith("…"))

    def test_a_prompt_at_the_limit_is_not_touched(self):
        self._write_prompt("y" * picker_previews.LAST_PROMPT_PREVIEW_CHARS)
        self.assertEqual(picker_previews._last_prompt_display(self.state_dir),
                         "y" * picker_previews.LAST_PROMPT_PREVIEW_CHARS)

    def test_newlines_collapse_so_the_yaml_fence_survives(self):
        # A prompt containing a code fence must not close the preview's own.
        self._write_prompt("first line\n```\ncode\n```\nlast line")
        shown = picker_previews._last_prompt_display(self.state_dir)
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
        real = picker_previews.last_prompt_in_state

        def counting(state_dir):
            reads["count"] += 1
            return real(state_dir)

        with patch.object(picker_previews, "_read_last_prompt", side_effect=counting):
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
        real = picker_previews.last_prompt_in_state

        def counting(state_dir):
            reads["count"] += 1
            return real(state_dir)

        with patch.object(picker_previews, "_read_last_prompt", side_effect=counting):
            quick = _plain_text(entry.preview_quick)
        self.assertEqual(reads["count"], 0)
        self.assertIn(picker_widget.LAST_PROMPT_LOADING, quick)
        self.assertNotIn("the real prompt", quick)
        self.assertIn("Agent:", quick)          # the metadata is already there
        self.assertIn("Tags:", quick)           # and so is the tag list

    def test_quick_preview_shows_no_stand_in_for_a_fresh_instance(self):
        # No history → no Last prompt field ever → no flashing loading line.
        quick = _plain_text(self._entry().preview_quick)
        self.assertNotIn("Last prompt", quick)
        self.assertNotIn(picker_widget.LAST_PROMPT_LOADING, quick)

    def test_full_preview_replaces_the_stand_in_with_the_value(self):
        self._write_prompt("the real prompt")
        entry = self._entry()
        self.assertIn(picker_widget.LAST_PROMPT_LOADING, _plain_text(entry.preview_quick))
        self.assertIn("the real prompt", _plain_text(entry.preview))
        self.assertNotIn(picker_widget.LAST_PROMPT_LOADING, _plain_text(entry.preview))

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


if __name__ == "__main__":
    unittest.main()
