"""Tests for launch.audit — the launcher's state-integrity report.

Three pure helpers cover the audit's per-data-source logic:
  `_check_json_file`        — classifies a JSON state file's status
  `_stale_memory_wrappers`  — scans MEMORY.md for orphaned banner wrappers
  `_modes_map_issues`       — validates modes-map entries (ghost/empty/bad)
All are easily-testable in isolation. `main()` is the I/O orchestrator that
loads the maps and walks state dirs — left out of unit tests since it
exercises the same primitives plus a great deal of file system access."""

import json
import tempfile
import unittest
from pathlib import Path

from launch.audit import (
    _check_json_file, _modes_map_issues, _stale_memory_wrappers,
)
from launch.paths import state_memory_path


class TestCheckJsonFile(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file(self):
        self.assertEqual(_check_json_file(self.path), "file is missing")

    def test_empty_file(self):
        self.path.write_text("")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_whitespace_only_is_empty(self):
        # _check_json_file strips before checking
        self.path.write_text("   \n\t  \n")
        self.assertEqual(_check_json_file(self.path), "file is empty")

    def test_invalid_json(self):
        self.path.write_text("not json {")
        result = _check_json_file(self.path)
        self.assertIsNotNone(result)
        self.assertIn("invalid JSON", result)

    def test_empty_object(self):
        self.path.write_text("{}")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_empty_array(self):
        # The check is `if not data` — falsy for both {} and []
        self.path.write_text("[]")
        self.assertEqual(_check_json_file(self.path), "contents are an empty object")

    def test_populated_object_is_clean(self):
        self.path.write_text(json.dumps({"key": "value"}))
        self.assertIsNone(_check_json_file(self.path))

    def test_populated_array_is_clean(self):
        self.path.write_text(json.dumps(["entry"]))
        self.assertIsNone(_check_json_file(self.path))

    def test_nested_structure_is_clean(self):
        self.path.write_text(json.dumps({"a": {"b": [1, 2]}}))
        self.assertIsNone(_check_json_file(self.path))


class TestStaleMemoryWrappers(unittest.TestCase):
    """Builds a fake state_dir + MEMORY.md and asserts on the helper's report.
    Current MODIFIER_ADDENDUMS keys provide the live valid-marker set; the
    tests use slugs known to be absent from it ('legacy', 'oldmode', etc.)
    so they remain stale regardless of future modifier additions."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmpdir.name)
        self.memory_path = state_memory_path(self.state_dir)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_memory(self, content: str) -> None:
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.memory_path.write_text(content)

    def test_no_memory_file_returns_empty(self):
        # state_dir exists but MEMORY.md doesn't — nothing to scan, no findings.
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ())

    def test_empty_memory_returns_empty(self):
        self._write_memory("")
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ())

    def test_no_banners_returns_empty(self):
        self._write_memory("Just some plain MEMORY.md prose\nwith no banner lines.\n")
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ())

    def test_only_valid_wrappers_returns_empty(self):
        # `base-instructions-{start,end}` is always-on (BASE is in MODIFIER_ADDENDUMS).
        self._write_memory(
            "##################### base-instructions-start #####################\n"
            "hello\n"
            "##################### base-instructions-end #####################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ())

    def test_unknown_slug_flagged_and_start_end_collapse(self):
        # `oldmode` isn't in MODIFIER_ADDENDUMS → start+end of the same slug
        # collapse to one reported entry via the `-instructions-{start|end}` strip.
        self._write_memory(
            "##################### oldmode-instructions-start #####################\n"
            "stale block\n"
            "##################### oldmode-instructions-end #####################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ("oldmode",))

    def test_pre_refactor_banner_grammar_flagged_as_is(self):
        # Old-format banner whose middle text doesn't follow `-instructions-{start|end}`
        # surfaces under its raw middle text (no suffix to strip).
        self._write_memory(
            "############# auto-addendum #############\n"
            "old auto block\n"
            "############# end auto-addendum #############\n"
        )
        self.assertEqual(
            _stale_memory_wrappers(self.state_dir),
            ("auto-addendum", "end auto-addendum"),
        )

    def test_mix_of_valid_and_stale(self):
        self._write_memory(
            "##################### base-instructions-start #####################\n"
            "valid\n"
            "##################### base-instructions-end #####################\n"
            "\n"
            "##################### legacy-instructions-start #####################\n"
            "stale\n"
            "##################### legacy-instructions-end #####################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ("legacy",))

    def test_markdown_headers_not_flagged(self):
        # Markdown headers max at 6 hashes and never have trailing hashes —
        # the 10-hash floor and `\s+#{10,}$` anchor reject them on both counts.
        self._write_memory(
            "# Top\n"
            "## Sub\n"
            "###### Six-hash header\n"
            "###### Closed atx header ######\n"   # max h6, still under the floor
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ())

    def test_distinct_stale_slugs_deduped(self):
        # Same stale slug appearing in multiple start/end occurrences → one entry.
        self._write_memory(
            "##################### foo-instructions-start #####################\n"
            "##################### foo-instructions-end #####################\n"
            "##################### foo-instructions-start #####################\n"
            "##################### foo-instructions-end #####################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ("foo",))

    def test_multiple_distinct_stale_slugs_sorted(self):
        self._write_memory(
            "##################### zebra-instructions-start #####################\n"
            "##################### zebra-instructions-end #####################\n"
            "##################### apple-instructions-start #####################\n"
            "##################### apple-instructions-end #####################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ("apple", "zebra"))

    def test_varying_banner_widths_all_caught(self):
        # The regex accepts any `#{10,}` run, not just the current 21-wide banner —
        # critical for catching pre-refactor wrappers with different widths.
        self._write_memory(
            "########## old-instructions-start ##########\n"
            "############################# old-instructions-end #############################\n"
        )
        self.assertEqual(_stale_memory_wrappers(self.state_dir), ("old",))


class TestModesMapIssues(unittest.TestCase):
    """Each finding kind is exercised in isolation, then a multi-entry mix to
    confirm the helper accumulates rather than short-circuits. Mode values use
    'auto' (a real InstanceModifiers.mode_values() entry) for valid cases and
    'fox'/'badmode' for unknown ones — picking strings unlikely to collide with
    future mode additions."""

    def test_empty_map_returns_empty_list(self):
        self.assertEqual(_modes_map_issues({}, set()), [])

    def test_valid_entry_returns_empty_list(self):
        # Instance present in `actual`, mode list non-empty, all modes recognized.
        result = _modes_map_issues({"agent__sess1": ["auto"]}, {"agent__sess1"})
        self.assertEqual(result, [])

    def test_ghost_mode_when_instance_missing(self):
        result = _modes_map_issues({"agent__gone": ["auto"]}, set())
        self.assertEqual(
            result,
            [("ghost_mode", "agent__gone", "modes-map entry has no state dir")],
        )

    def test_empty_list_value_flagged(self):
        # `_write_modes_entry` pops empties — so an empty list in the map is
        # a violated invariant, not a legitimate "no modes" state.
        result = _modes_map_issues({"agent__sess1": []}, {"agent__sess1"})
        self.assertEqual(
            result,
            [("empty_modes_entry", "agent__sess1", "modes-map entry has an empty list")],
        )

    def test_non_list_value_flagged_as_bad_mode(self):
        # Manually-corrupted value (string instead of list).
        result = _modes_map_issues({"agent__sess1": "auto"}, {"agent__sess1"})
        self.assertEqual(len(result), 1)
        kind, target, msg = result[0]
        self.assertEqual(kind, "bad_mode")
        self.assertEqual(target, "agent__sess1")
        self.assertIn("not a list", msg)

    def test_unknown_mode_flagged(self):
        result = _modes_map_issues({"agent__sess1": ["fox"]}, {"agent__sess1"})
        self.assertEqual(len(result), 1)
        kind, target, msg = result[0]
        self.assertEqual(kind, "bad_mode")
        self.assertEqual(target, "agent__sess1")
        self.assertIn("'fox'", msg)

    def test_multiple_unknown_modes_each_flagged(self):
        # Each unknown mode in the same entry surfaces as its own finding so
        # the user sees the full list of typos rather than just the first.
        result = _modes_map_issues(
            {"agent__sess1": ["fox", "badmode"]}, {"agent__sess1"}
        )
        self.assertEqual(len(result), 2)
        msgs = [m for _, _, m in result]
        self.assertTrue(any("'fox'" in m for m in msgs))
        self.assertTrue(any("'badmode'" in m for m in msgs))

    def test_mixed_valid_and_invalid_modes_only_invalid_flagged(self):
        # 'auto' is valid (in InstanceModifiers.mode_values()), 'fox' is not.
        result = _modes_map_issues(
            {"agent__sess1": ["auto", "fox"]}, {"agent__sess1"}
        )
        self.assertEqual(len(result), 1)
        self.assertIn("'fox'", result[0][2])

    def test_ghost_precludes_other_checks(self):
        # A ghost entry is only reported once — the value-shape checks are
        # skipped via the `continue`, since they apply only to live instances.
        result = _modes_map_issues({"agent__gone": []}, set())
        self.assertEqual(
            result,
            [("ghost_mode", "agent__gone", "modes-map entry has no state dir")],
        )

    def test_non_list_precludes_empty_and_mode_checks(self):
        # Once the value isn't a list, neither the empty-check nor the per-mode
        # iteration runs — single bad_mode finding describes the shape problem.
        result = _modes_map_issues({"agent__sess1": None}, {"agent__sess1"})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0], "bad_mode")

    def test_multi_entry_accumulation(self):
        # Multiple entries with different issues all surface; the helper doesn't
        # stop at the first finding.
        modes = {
            "agent__valid":   ["auto"],            # clean
            "agent__ghost":   ["auto"],            # ghost_mode
            "agent__empty":   [],                  # empty_modes_entry
            "agent__bad":     ["typo"],            # bad_mode (unknown)
            "agent__shape":   {"not": "a list"},   # bad_mode (non-list)
        }
        actual = {"agent__valid", "agent__empty", "agent__bad", "agent__shape"}
        result = _modes_map_issues(modes, actual)
        kinds_by_target = {target: kind for kind, target, _ in result}
        self.assertEqual(
            kinds_by_target,
            {
                "agent__ghost": "ghost_mode",
                "agent__empty": "empty_modes_entry",
                "agent__bad":   "bad_mode",
                "agent__shape": "bad_mode",
            },
        )
        self.assertNotIn("agent__valid", kinds_by_target)


if __name__ == "__main__":
    unittest.main()
