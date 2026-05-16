"""Tests for launch.file_access — parse_stem grammar, the JSON-map caching
layer, and the small helpers in this module.

Filesystem-touching tests use tmpdir + targeted patches so they don't depend
on the host's actual launcher state."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import file_access


# ============================================================
# parse_stem grammar
# ============================================================


class TestParseStem(unittest.TestCase):
    def test_name_only(self):
        self.assertEqual(file_access.parse_stem("poet"), ("poet", [], None))

    def test_name_with_tag(self):
        self.assertEqual(file_access.parse_stem("poet[prog]"), ("poet", ["prog"], None))

    def test_name_with_parent(self):
        self.assertEqual(file_access.parse_stem("poet(thinker)"), ("poet", [], "thinker"))

    def test_name_with_tag_then_parent(self):
        self.assertEqual(
            file_access.parse_stem("poet[prog](thinker)"),
            ("poet", ["prog"], "thinker"),
        )

    def test_name_with_parent_then_tag(self):
        # Order is free — same result either way
        self.assertEqual(
            file_access.parse_stem("poet(thinker)[prog]"),
            ("poet", ["prog"], "thinker"),
        )

    def test_multiple_tags_accumulate_in_order(self):
        self.assertEqual(
            file_access.parse_stem("poet[a][b][c]"),
            ("poet", ["a", "b", "c"], None),
        )

    def test_repeated_parent_last_wins(self):
        self.assertEqual(
            file_access.parse_stem("poet(a)(b)"),
            ("poet", [], "b"),
        )

    def test_empty_stem(self):
        # No name regex match → fallback returns the stem as-is
        self.assertEqual(file_access.parse_stem(""), ("", [], None))

    def test_complex_combo(self):
        self.assertEqual(
            file_access.parse_stem("name[a](parent)[b]"),
            ("name", ["a", "b"], "parent"),
        )


# ============================================================
# JSON-map cache (_cached_load_json_map / _cached_save_json_map)
# ============================================================


class TestJsonMapCache(unittest.TestCase):
    """The cache survives multiple loads and is refreshed on save. Tests use
    a tmp file as the cache key so they don't collide with the real workspace
    or modes maps."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "map.json"
        self.path.write_text(json.dumps({"a": 1}))
        # Ensure no stale entry from a prior test
        file_access._json_map_cache.pop(self.path, None)

    def tearDown(self):
        file_access._json_map_cache.pop(self.path, None)
        self.tmpdir.cleanup()

    def test_first_load_reads_from_disk(self):
        loaded = file_access._cached_load_json_map(self.path)
        self.assertEqual(loaded, {"a": 1})

    def test_second_load_returns_same_object(self):
        first = file_access._cached_load_json_map(self.path)
        second = file_access._cached_load_json_map(self.path)
        self.assertIs(first, second)   # same dict reference, not just equal

    def test_disk_change_not_seen_when_cached(self):
        # External edit after first load is invisible — no file-locking, by design.
        first = file_access._cached_load_json_map(self.path)
        self.path.write_text(json.dumps({"b": 2}))
        second = file_access._cached_load_json_map(self.path)
        self.assertEqual(second, {"a": 1})
        self.assertIs(first, second)

    def test_save_refreshes_cache(self):
        file_access._cached_load_json_map(self.path)   # prime
        file_access._cached_save_json_map(self.path, {"c": 3})
        loaded = file_access._cached_load_json_map(self.path)
        self.assertEqual(loaded, {"c": 3})

    def test_save_persists_to_disk(self):
        file_access._cached_save_json_map(self.path, {"persisted": True})
        on_disk = json.loads(self.path.read_text())
        self.assertEqual(on_disk, {"persisted": True})

    def test_mutation_visible_across_loads(self):
        # The "load-mutate-save" pattern relies on this: load returns a reference,
        # mutating it is visible to subsequent loads even before save.
        m = file_access._cached_load_json_map(self.path)
        m["new_key"] = "added"
        again = file_access._cached_load_json_map(self.path)
        self.assertEqual(again["new_key"], "added")


# ============================================================
# installed_cred_clis — derived from present_optional_cred_services
# ============================================================


class TestInstalledCredClis(unittest.TestCase):
    """installed_cred_clis returns space-joined CLI names for services that
    are (a) present on host AND (b) have a non-None CLI in OPTIONAL_CREDS_MOUNTS."""

    def setUp(self):
        # Clear the lru_cache before each test so we control what
        # present_optional_cred_services returns via patching.
        file_access.present_optional_cred_services.cache_clear()

    def tearDown(self):
        file_access.present_optional_cred_services.cache_clear()

    def _with_present(self, present):
        return patch.object(
            file_access, "present_optional_cred_services",
            return_value=frozenset(present),
        )

    def test_no_creds_empty_string(self):
        with self._with_present(set()):
            self.assertEqual(file_access.installed_cred_clis(), "")

    def test_single_cred_with_cli(self):
        with self._with_present({"gh"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")

    def test_multiple_creds_space_joined(self):
        with self._with_present({"gh", "aws"}):
            # Order follows OPTIONAL_CREDS_MOUNTS declaration (aws appears before gh)
            self.assertEqual(file_access.installed_cred_clis(), "aws gh")

    def test_kube_renders_as_kubectl(self):
        # service name `kube` → CLI binary `kubectl`
        with self._with_present({"kube"}):
            self.assertEqual(file_access.installed_cred_clis(), "kubectl")

    def test_cli_less_services_excluded(self):
        # ssh/npmrc/pypirc have cli=None — they don't appear in the output even
        # when present on host.
        with self._with_present({"ssh", "npmrc", "pypirc"}):
            self.assertEqual(file_access.installed_cred_clis(), "")

    def test_mix_with_and_without_clis(self):
        with self._with_present({"ssh", "gh", "npmrc"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")

    def test_unknown_service_silently_skipped(self):
        # A service that's "present" but not in OPTIONAL_CREDS_MOUNTS at all
        # is just ignored — iteration is keyed off OPTIONAL_CREDS_MOUNTS.items().
        with self._with_present({"bogus", "gh"}):
            self.assertEqual(file_access.installed_cred_clis(), "gh")


# ============================================================
# Small helpers
# ============================================================


class TestParseLines(unittest.TestCase):
    """parse_lines yields non-blank, non-comment lines."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "list.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_skips_blank_lines(self):
        self.path.write_text("a\n\nb\n\n\nc\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["a", "b", "c"])

    def test_skips_comment_lines(self):
        self.path.write_text("a\n# comment\nb\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["a", "b"])

    def test_strips_each_line(self):
        self.path.write_text("  spacy  \n\ttab\t\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["spacy", "tab"])

    def test_missing_file_raises(self):
        # Documented behavior: parse_lines requires the file to exist —
        # callers must ensure it first (typically via a template plant).
        missing = Path(self.tmpdir.name) / "nope.txt"
        with self.assertRaises(FileNotFoundError):
            list(file_access.parse_lines(missing))

    def test_inline_comment_stripped(self):
        self.path.write_text("value  # trailing comment\n")
        self.assertEqual(list(file_access.parse_lines(self.path)), ["value"])


class TestIsFileRecent(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "file.txt"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_missing_file_is_not_recent(self):
        self.assertFalse(file_access.is_file_recent(self.path, 60))

    def test_just_written_file_is_recent(self):
        self.path.write_text("x")
        self.assertTrue(file_access.is_file_recent(self.path, 60))

    def test_stale_file_is_not_recent(self):
        import os
        import time
        self.path.write_text("x")
        # Backdate mtime to 2 hours ago
        old = time.time() - 7200
        os.utime(self.path, (old, old))
        self.assertFalse(file_access.is_file_recent(self.path, 60))


if __name__ == "__main__":
    unittest.main()
