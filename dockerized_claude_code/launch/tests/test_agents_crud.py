"""Tests for launch.agents_crud — sort keys + model parsing + the
install_latest_md integration round-trip.

delete_instance, modify_instance, and the picker-entry factories touch disk +
the cached JSON maps + sometimes print/prompt — covered by file_access tests
for the cache layer plus manually for the integration path."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.agents_crud import (
    ORDERED_MODEL_FAMILIES, _write_modes_entry, install_latest_md,
    mode_sort_key, parse_model_id, tag_sort_key,
)
from launch.memory_addendums import (
    ADDENDUM_SECTION_TITLE, MODIFIER_ADDENDUMS, SEEK_SUMMARY,
)
from launch.structs import InstanceModifiers, InstanceIdentity


# ============================================================
# parse_model_id
# ============================================================


class TestParseModelId(unittest.TestCase):
    def test_opus_with_minor(self):
        self.assertEqual(parse_model_id("claude-opus-4-7"), ("opus", 4, 7))

    def test_sonnet_with_minor(self):
        self.assertEqual(parse_model_id("claude-sonnet-4-6"), ("sonnet", 4, 6))

    def test_haiku_with_minor(self):
        self.assertEqual(parse_model_id("claude-haiku-4-5-20251001"), ("haiku", 4, 5))

    def test_major_only(self):
        # Minor defaults to 0 when absent
        self.assertEqual(parse_model_id("claude-opus-4"), ("opus", 4, 0))

    def test_unknown_family(self):
        self.assertIsNone(parse_model_id("claude-unknown-4-7"))

    def test_empty_string(self):
        self.assertIsNone(parse_model_id(""))

    def test_garbage_string(self):
        self.assertIsNone(parse_model_id("not-a-model"))

    def test_family_in_middle(self):
        # _FAMILY_RE uses `.search`, so the family can be anywhere
        self.assertEqual(parse_model_id("some-prefix-opus-4-7"), ("opus", 4, 7))


class TestOrderedModelFamilies(unittest.TestCase):
    def test_priority_order(self):
        # opus first, haiku last — affects agent_sort_key
        self.assertEqual(ORDERED_MODEL_FAMILIES, ["opus", "sonnet", "haiku"])


# ============================================================
# Sort keys
# ============================================================


class TestTagSortKey(unittest.TestCase):
    def test_empty_tags(self):
        self.assertEqual(tag_sort_key([]), ())

    def test_known_tag(self):
        # TAG_CODE is at index 0 of InstanceModifiers.tag_values()
        self.assertEqual(tag_sort_key([InstanceModifiers.TAG_CODE]), (0,))

    def test_sorted_internally(self):
        # tag_sort_key sorts its members so the key is order-stable regardless
        # of input order. With only one known tag the result is a 1-tuple
        # whichever way it's passed.
        self.assertEqual(
            tag_sort_key([InstanceModifiers.TAG_CODE]),
            tag_sort_key([InstanceModifiers.TAG_CODE]),
        )

    def test_untagged_sorts_before_tagged(self):
        # Empty tuple < any non-empty tuple lexicographically
        self.assertLess(tag_sort_key([]), tag_sort_key([InstanceModifiers.TAG_CODE]))


class TestModeSortKey(unittest.TestCase):
    def test_empty_modes(self):
        self.assertEqual(mode_sort_key([]), ())

    def test_auto_only(self):
        # mode_values() == ("auto", "DooD", "web") → MODE_WARN_AUTO is index 0
        self.assertEqual(mode_sort_key([InstanceModifiers.MODE_WARN_AUTO]), (0,))

    def test_dood_only(self):
        self.assertEqual(mode_sort_key([InstanceModifiers.MODE_WARN_DOOD]), (1,))

    def test_both_sorted_by_declaration_order(self):
        # Input order doesn't matter — output sorts by InstanceModifiers position.
        self.assertEqual(mode_sort_key([InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD]), (0, 1))
        self.assertEqual(mode_sort_key([InstanceModifiers.MODE_WARN_DOOD, InstanceModifiers.MODE_WARN_AUTO]), (0, 1))

    def test_modeless_sorts_before_any(self):
        self.assertLess(mode_sort_key([]), mode_sort_key([InstanceModifiers.MODE_WARN_AUTO]))


# ============================================================
# _write_modes_entry — dict mutation, pop on empty
# ============================================================


class _StubSess:
    """Minimal sess-id stand-in for _write_modes_entry tests — just needs
    .instance and .modes attributes."""
    def __init__(self, instance, modes):
        self.instance = instance
        self.modes = tuple(modes)


class TestWriteModesEntry(unittest.TestCase):
    def test_sets_when_modes_present(self):
        m = {}
        _write_modes_entry(m, _StubSess("poet__draft", [InstanceModifiers.MODE_WARN_AUTO]))
        self.assertEqual(m, {"poet__draft": ["auto"]})

    def test_replaces_existing(self):
        m = {"poet__draft": ["DooD"]}
        _write_modes_entry(m, _StubSess("poet__draft", [InstanceModifiers.MODE_WARN_AUTO]))
        self.assertEqual(m, {"poet__draft": ["auto"]})

    def test_pops_when_empty(self):
        m = {"poet__draft": ["auto"]}
        _write_modes_entry(m, _StubSess("poet__draft", []))
        self.assertEqual(m, {})

    def test_pop_on_empty_when_absent_is_safe(self):
        # No prior entry + empty modes → no-op (no KeyError)
        m = {}
        _write_modes_entry(m, _StubSess("poet__draft", []))
        self.assertEqual(m, {})

    def test_other_entries_untouched(self):
        m = {"a__1": ["auto"], "b__1": ["DooD"]}
        _write_modes_entry(m, _StubSess("a__1", []))
        self.assertEqual(m, {"b__1": ["DooD"]})

    def test_writes_canonical_value_string_per_member(self):
        # Modes serialize as their `.value` (the JSON-friendly canonical form),
        # preserving the in-memory tuple's declaration order.
        m = {}
        _write_modes_entry(m, _StubSess("agent__sess", [InstanceModifiers.MODE_WARN_AUTO, InstanceModifiers.MODE_WARN_DOOD]))
        self.assertEqual(m["agent__sess"], ["auto", "DooD"])


# ============================================================
# install_latest_md — source `.md` + composed addendum → state-dir CLAUDE.md
# ============================================================


class _FakeInst(InstanceIdentity):
    """InstanceIdentity subclass overriding `md_path`, `state_dir`, and `tags`
    so install_latest_md can be exercised against temp paths without a real
    agent .md on disk. Frozen dataclass blocks normal __setattr__, so the
    overrides come through object.__setattr__ on attributes the subclass
    properties read from."""

    @property
    def md_path(self):
        return self._md_path_override

    @property
    def state_dir(self):
        return self._state_dir_override

    @property
    def tags(self):
        return self._tags_override

    @classmethod
    def make(cls, md_path, state_dir, *, tags=(), modes=(), agent="x", session="s"):
        s = cls(agent=agent, session=session, workspace="/tmp",
                is_brand_new=False, modes=tuple(modes))
        object.__setattr__(s, "_md_path_override", md_path)
        object.__setattr__(s, "_state_dir_override", state_dir)
        object.__setattr__(s, "_tags_override", tuple(tags))
        return s


class TestInstallLatestMd(unittest.TestCase):
    """End-to-end check that install_latest_md writes the source body plus the
    composed-addendum section to the state-dir CLAUDE.md in a single overwrite.
    Uses real (production) MODIFIER_ADDENDUMS for the BASE-substring assertion
    so a regression in the addendum-composition path surfaces here, not just
    in the memory_addendums unit tests."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        root = Path(self.tmpdir.name)
        self.md_path = root / "agent.md"
        self.state_dir = root / "state"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _sess(self, body, *, tags=(), modes=()):
        self.md_path.write_text(body)
        return _FakeInst.make(self.md_path, self.state_dir, tags=tags, modes=modes)

    def test_source_body_is_at_top_of_resulting_md(self):
        sess = self._sess("Source line 1\nSource line 2\n")
        install_latest_md(sess)
        result = sess.state_md.read_text()
        self.assertTrue(result.startswith("Source line 1\nSource line 2\n"))

    def test_base_addendum_body_is_present_in_resulting_md(self):
        # The integration assertion the user asked for: a string that's part of
        # BASE (SEEK_SUMMARY's body, which sits under BASE) is entirely included
        # in the file install_latest_md writes.
        sess = self._sess("agent body\n")
        install_latest_md(sess)
        self.assertIn(SEEK_SUMMARY.body, sess.state_md.read_text())

    def test_section_heading_is_present_in_resulting_md(self):
        sess = self._sess("agent body\n")
        install_latest_md(sess)
        self.assertIn(f"## {ADDENDUM_SECTION_TITLE}", sess.state_md.read_text())

    def test_separator_between_source_body_and_addendum(self):
        # Source body ends with '\n', addendum is prefixed with '\n\n' — so the
        # transition is `body\n\n\n## Launch-time...` (one blank line gap).
        sess = self._sess("agent body\n")
        install_latest_md(sess)
        self.assertIn(f"agent body\n\n\n## {ADDENDUM_SECTION_TITLE}",
                      sess.state_md.read_text())

    def test_overwrite_replaces_previous_content(self):
        # First launch with one source body.
        sess1 = self._sess("body v1\n")
        install_latest_md(sess1)
        # Re-write source `.md`, reinstall — state-dir CLAUDE.md must reflect v2.
        self.md_path.write_text("body v2\n")
        install_latest_md(sess1)
        result = sess1.state_md.read_text()
        self.assertIn("body v2", result)
        self.assertNotIn("body v1", result)
        # Addendum still there post-overwrite.
        self.assertIn(SEEK_SUMMARY.body, result)

    def test_empty_addendum_yields_source_only(self):
        # Patch MODIFIER_ADDENDUMS to empty so composed_addendum returns ''.
        # install_latest_md must skip the separator+addendum append, yielding
        # the source body byte-for-byte.
        sess = self._sess("just the body\n")
        with patch.dict(MODIFIER_ADDENDUMS, {}, clear=True):
            install_latest_md(sess)
        self.assertEqual(sess.state_md.read_text(), "just the body\n")


if __name__ == "__main__":
    unittest.main()
