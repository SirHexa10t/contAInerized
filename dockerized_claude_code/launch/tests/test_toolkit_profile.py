"""Tests for launch.tags.toolkit_profile — default/load/save for a
profession's `~/.claude-agents/<profession>_profile.toml`. Every load/save
here uses a tmp path (never paths.toolkit_profile_path / AGENTS_STATE) —
this module must never touch a real host's toolkit profile."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from launch.tags.profession import ToolkitEntry
from launch.tags.toolkit_profile import default_profile, load_profile, save_profile

ENTRIES = {
    "rust":  ToolkitEntry(key="rust",  description="Rust toolchain", run_command="cargo", language="compiled", approx_size_mb=613, default=True,  build_arg="INSTALL_RUST"),
    "cmake": ToolkitEntry(key="cmake", description="CMake", run_command="cmake", language="build-system", approx_size_mb=66,  default=False, build_arg="INSTALL_CMAKE"),
}


class TestDefaultProfile(unittest.TestCase):
    def test_seeds_from_manifest_defaults(self):
        self.assertEqual(default_profile(ENTRIES), {"rust": True, "cmake": False})

    def test_empty_entries_yields_empty(self):
        self.assertEqual(default_profile({}), {})


class TestLoadProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "code_profile.toml"

    def test_missing_file_yields_manifest_defaults(self):
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": True, "cmake": False})

    def test_no_entries_yields_empty_regardless_of_file(self):
        self.path.write_text("rust = false\n")
        self.assertEqual(load_profile(self.path, {}), {})

    def test_explicit_value_overrides_default(self):
        self.path.write_text("rust = false\ncmake = true\n")
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": False, "cmake": True})

    def test_key_missing_from_file_falls_back_to_default(self):
        # A tool added to the manifest after the file was first written.
        self.path.write_text("rust = false\n")
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": False, "cmake": False})

    def test_stale_key_no_longer_in_manifest_is_dropped(self):
        self.path.write_text("rust = true\ncmake = true\nremoved_tool = true\n")
        self.assertEqual(set(load_profile(self.path, ENTRIES)), {"rust", "cmake"})

    def test_empty_file_yields_manifest_defaults(self):
        self.path.write_text("")
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": True, "cmake": False})


class TestSaveProfile(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "code_profile.toml"

    def test_round_trips_through_load(self):
        save_profile(self.path, {"rust": False, "cmake": True}, ENTRIES)
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": False, "cmake": True})

    def test_missing_value_falls_back_to_entry_default(self):
        save_profile(self.path, {"rust": False}, ENTRIES)   # "cmake" absent from values
        self.assertEqual(load_profile(self.path, ENTRIES), {"rust": False, "cmake": False})

    def test_comments_carry_description_and_size(self):
        save_profile(self.path, {"rust": True, "cmake": False}, ENTRIES)
        text = self.path.read_text()
        self.assertIn("Rust toolchain", text)
        self.assertIn("~613MB", text)
        self.assertIn("CMake", text)

    def test_stale_key_dropped_on_rewrite(self):
        self.path.write_text("rust = true\nremoved_tool = true\n")
        save_profile(self.path, {"rust": True, "cmake": False}, ENTRIES)
        self.assertNotIn("removed_tool", self.path.read_text())
