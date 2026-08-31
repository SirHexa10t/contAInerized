"""Tests for launch.tags.ui_profile — the launcher-UI preferences file and its
TWO read paths, deliberately different: the FORM's read reconciles missing
keys to manifest defaults (the toolkit-profile behavior), while the LAUNCH
read (`muxer_backend`) follows the operator's spec — a missing FILE is
first-launch normal and generated from defaults, but a file that LOST the
field is a loud stop naming it, never a silent fallback."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.tags.base import TagError
from launch.tags.ui_profile import (
    MUXER_FIELD, UiEntry, load_ui_form, load_ui_profile, muxer_backend,
    save_ui_profile,
)

AN_ENTRY = UiEntry(key="some_pref", description="a preference",
                   default=True, body="the tradeoff")


class TestUiFormManifest(unittest.TestCase):
    def test_the_shipped_manifest_parses_and_defaults_to_herdr(self):
        # The real settings/ui.form — the picker's UI section and the launch
        # path both build on it; herdr-by-default is the operator's call
        # (2026-08-29), seeded into each user's ui_profile.toml from here.
        entries = load_ui_form()
        self.assertIn(MUXER_FIELD, entries)
        self.assertTrue(entries[MUXER_FIELD].default)
        self.assertIn("tmux", entries[MUXER_FIELD].body)   # the tradeoff is shown

    def test_a_manifest_entry_missing_a_field_fails_loud(self):
        # Same rule as the toolkit manifest parser: a typo surfaces as a
        # named error, never as a silently absent toggle.
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "ui.form"
            bad.write_text('[thing]\ndescription = "d"\ndefault = true\n')
            with self.assertRaisesRegex(TagError, "body"):
                load_ui_form(bad)


class TestProfileRoundTrip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "ui_profile.toml"
        self.entries = {AN_ENTRY.key: AN_ENTRY}

    def test_a_missing_file_yields_the_defaults(self):
        self.assertEqual(load_ui_profile(self.path, self.entries),
                         {"some_pref": True})

    def test_save_then_load_round_trips_and_comments_the_description(self):
        save_ui_profile(self.path, {"some_pref": False}, self.entries)
        self.assertEqual(load_ui_profile(self.path, self.entries),
                         {"some_pref": False})
        self.assertIn("# a preference", self.path.read_text())

    def test_reconciliation_matches_the_toolkit_rules(self):
        # A key the file misses falls back to its default; a key the manifest
        # dropped disappears on the next save — the file tracks the CURRENT
        # manifest, never stale cruft.
        self.path.write_text("retired_pref = false\n")
        self.assertEqual(load_ui_profile(self.path, self.entries),
                         {"some_pref": True})
        save_ui_profile(self.path, {"some_pref": True}, self.entries)
        self.assertNotIn("retired_pref", self.path.read_text())


class TestMuxerBackend(unittest.TestCase):
    """The strict launch read, against the REAL settings/ui.form manifest but
    a redirected AGENTS_STATE."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_first_launch_creates_the_profile_and_defaults_to_herdr(self):
        self.assertEqual(muxer_backend(), "herdr")
        text = paths.ui_profile_path().read_text()
        self.assertIn(f"{MUXER_FIELD} = true", text)
        self.assertIn("#", text)          # the hand-editor sees descriptions

    def test_the_persisted_preference_wins(self):
        paths.ui_profile_path().write_text(f"{MUXER_FIELD} = false\n")
        self.assertEqual(muxer_backend(), "tmux")
        paths.ui_profile_path().write_text(f"{MUXER_FIELD} = true\n")
        self.assertEqual(muxer_backend(), "herdr")

    def test_a_file_that_lost_the_field_is_a_loud_stop_naming_the_fix(self):
        # The operator's spec: never silently flip the muxer on a hand-edit
        # that dropped the field — say what is missing and how to regenerate.
        paths.ui_profile_path().write_text("something_else = true\n")
        with self.assertRaises(SystemExit) as caught:
            muxer_backend()
        self.assertIn(MUXER_FIELD, str(caught.exception))
        self.assertIn("Rename", str(caught.exception))

    def test_a_manifest_without_the_field_is_a_named_error(self):
        # Guards the guard: if settings/ui.form itself ever loses the field,
        # the failure names the manifest rather than KeyError-ing.
        with patch("launch.tags.ui_profile.load_ui_form", return_value={}):
            with self.assertRaisesRegex(TagError, MUXER_FIELD):
                muxer_backend()


if __name__ == "__main__":
    unittest.main()
