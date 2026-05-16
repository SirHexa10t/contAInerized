"""Tests for launch.agents_crud — sort keys + model parsing.

Other agents_crud functions (delete_instance, install_latest_md, modify_instance,
the picker-entry factories) touch disk + the cached JSON maps + sometimes
print/prompt — covered by file_access tests for the cache layer plus
manually for the integration path."""

import unittest

from launch.agents_crud import (
    ORDERED_MODEL_FAMILIES, _write_modes_entry, mode_sort_key, parse_model_id,
    tag_sort_key,
)


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
        # "prog" is at index 0 of InstanceModifiers.tag_values()
        self.assertEqual(tag_sort_key(["prog"]), (0,))

    def test_unknown_tag_sinks_to_end(self):
        # tag_values() == ("prog",) → unknown gets sentinel index 1
        key = tag_sort_key(["typo"])
        self.assertEqual(key, (1,))

    def test_sorted_internally(self):
        # tag_sort_key sorts its members so the key is order-stable regardless
        # of input order. With only one known tag the result is a 1-tuple
        # whichever way it's passed.
        self.assertEqual(tag_sort_key(["prog"]), tag_sort_key(["prog"]))

    def test_untagged_sorts_before_tagged(self):
        # Empty tuple < any non-empty tuple lexicographically
        self.assertLess(tag_sort_key([]), tag_sort_key(["prog"]))


class TestModeSortKey(unittest.TestCase):
    def test_empty_modes(self):
        self.assertEqual(mode_sort_key([]), ())

    def test_auto_only(self):
        # mode_values() == ("auto", "DooD") → "auto" is index 0
        self.assertEqual(mode_sort_key(["auto"]), (0,))

    def test_dood_only(self):
        self.assertEqual(mode_sort_key(["DooD"]), (1,))

    def test_both_sorted_by_declaration_order(self):
        # Input order doesn't matter — output sorts by InstanceModifiers position.
        self.assertEqual(mode_sort_key(["auto", "DooD"]), (0, 1))
        self.assertEqual(mode_sort_key(["DooD", "auto"]), (0, 1))

    def test_unknown_mode_sinks(self):
        # mode_values() has 2 items → sentinel index is 2.
        self.assertEqual(mode_sort_key(["bogus"]), (2,))

    def test_modeless_sorts_before_any(self):
        self.assertLess(mode_sort_key([]), mode_sort_key(["auto"]))


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
        _write_modes_entry(m, _StubSess("poet__draft", ["auto"]))
        self.assertEqual(m, {"poet__draft": ["auto"]})

    def test_replaces_existing(self):
        m = {"poet__draft": ["DooD"]}
        _write_modes_entry(m, _StubSess("poet__draft", ["auto"]))
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


if __name__ == "__main__":
    unittest.main()
