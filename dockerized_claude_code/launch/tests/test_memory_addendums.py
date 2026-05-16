"""Tests for launch.memory_addendums — wrapper format, addendum text content,
and the per-modifier getter.

Note: `Addendum.CREDENTIALS_NOTICE` is evaluated at module import (via
`installed_cred_clis()`), so its concrete value depends on the launcher
environment. Tests here assert structure and patterns, not the exact CLI
list."""

import unittest
from unittest.mock import patch

from launch.memory_addendums import (
    CREDENTIALS_NOTICE, FIREWALL_NOTICE, MEMORY_BLOCK_WRAPPER_BANNER,
    MODIFIER_ADDENDUMS, SEEK_SUMMARY, _wrap_block, _wrapper_end_line,
    _wrapper_start_line, addendum_text,
)
from launch.structs import InstanceModifiers


class TestWrapperBanner(unittest.TestCase):
    def test_banner_is_21_hashes(self):
        self.assertEqual(MEMORY_BLOCK_WRAPPER_BANNER, "#" * 21)


class TestWrapperLines(unittest.TestCase):
    def test_start_line_format(self):
        self.assertEqual(
            _wrapper_start_line("auto"),
            "##################### auto-instructions-start #####################",
        )

    def test_end_line_format(self):
        self.assertEqual(
            _wrapper_end_line("auto"),
            "##################### auto-instructions-end #####################",
        )

    def test_start_and_end_share_name_stem(self):
        self.assertIn("foo-instructions-start", _wrapper_start_line("foo"))
        self.assertIn("foo-instructions-end", _wrapper_end_line("foo"))

    def test_banner_on_both_sides(self):
        line = _wrapper_start_line("x")
        self.assertTrue(line.startswith(MEMORY_BLOCK_WRAPPER_BANNER))
        self.assertTrue(line.endswith(MEMORY_BLOCK_WRAPPER_BANNER))


class TestWrapBlock(unittest.TestCase):
    def test_wraps_content_with_start_end_lines(self):
        wrapped = _wrap_block("auto", "hello")
        self.assertEqual(
            wrapped,
            f"{_wrapper_start_line('auto')}\nhello\n{_wrapper_end_line('auto')}",
        )

    def test_strips_content_whitespace(self):
        wrapped = _wrap_block("auto", "\n\n  hello world  \n\n")
        # content.strip() removes leading/trailing whitespace
        self.assertIn("\nhello world\n", wrapped)

    def test_multiline_content_preserved(self):
        content = "line one\nline two\nline three"
        wrapped = _wrap_block("foo", content)
        self.assertIn(content, wrapped)


# ============================================================
# Addendum text constants — structural assertions
# ============================================================


class TestSeekSummary(unittest.TestCase):
    def test_references_summary_path(self):
        self.assertIn("/workspace/.claude_summary", SEEK_SUMMARY)

    def test_mentions_write_summary_command(self):
        self.assertIn("/write-summary", SEEK_SUMMARY)


class TestFirewallNotice(unittest.TestCase):
    def test_contains_auto_label_not_escape(self):
        # Should contain literal `{auto}` (rendered from InstanceModifiers.MODE_AUTO.label),
        # NOT `{{auto}}` (the f-string escape form).
        self.assertIn("{auto}", FIREWALL_NOTICE)
        self.assertNotIn("{{auto}}", FIREWALL_NOTICE)

    def test_references_status_file_in_container(self):
        # Path comes from state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)
        self.assertIn("/home/claude/.claude/domains_pending_resolve.yml", FIREWALL_NOTICE)

    def test_references_whitelist_file(self):
        # The host-side whitelist path is interpolated from FIREWALL_WHITELIST_FILE
        self.assertIn("firewall_whitelist.txt", FIREWALL_NOTICE)

    def test_mentions_econnrefused(self):
        self.assertIn("ECONNREFUSED", FIREWALL_NOTICE)

    def test_mentions_pending_and_failed_sections(self):
        self.assertIn("pending:", FIREWALL_NOTICE)
        self.assertIn("failed:", FIREWALL_NOTICE)


class TestCredentialsNotice(unittest.TestCase):
    """CREDENTIALS_NOTICE is dynamic: text+CLI-list when creds present, '' otherwise.
    The value is locked at import time, so we test the two shapes it can have."""

    def test_either_empty_or_describes_clis(self):
        if CREDENTIALS_NOTICE:
            self.assertIn("credentials", CREDENTIALS_NOTICE.lower())
            self.assertIn("installed", CREDENTIALS_NOTICE)
        else:
            self.assertEqual(CREDENTIALS_NOTICE, "")


# ============================================================
# MODIFIER_ADDENDUMS dict structure
# ============================================================


class TestModifierAddendumsDict(unittest.TestCase):
    def test_keys_are_instance_modifiers(self):
        for k in MODIFIER_ADDENDUMS:
            with self.subTest(modifier=k):
                self.assertIsInstance(k, InstanceModifiers)

    def test_base_maps_to_seek_summary(self):
        self.assertIn(SEEK_SUMMARY, MODIFIER_ADDENDUMS[InstanceModifiers.BASE])

    def test_tag_prog_maps_to_credentials_notice(self):
        self.assertIn(CREDENTIALS_NOTICE, MODIFIER_ADDENDUMS[InstanceModifiers.TAG_PROG])

    def test_mode_auto_maps_to_firewall_notice(self):
        self.assertIn(FIREWALL_NOTICE, MODIFIER_ADDENDUMS[InstanceModifiers.MODE_AUTO])

    def test_mode_dood_has_no_addendum(self):
        # MODE_DOOD doesn't currently advertise anything in MEMORY.md.
        self.assertNotIn(InstanceModifiers.MODE_DOOD, MODIFIER_ADDENDUMS)


# ============================================================
# addendum_text getter
# ============================================================


class TestAddendumText(unittest.TestCase):
    def test_returns_empty_for_unmapped_modifier(self):
        # MODE_DOOD has no entry in MODIFIER_ADDENDUMS → MODIFIER_ADDENDUMS.get(...)
        # falls back to (); join of () is "".
        self.assertEqual(addendum_text(InstanceModifiers.MODE_DOOD), "")

    def test_returns_seek_summary_for_base(self):
        # BASE → [SEEK_SUMMARY]; addendum_text joins (single entry, no separator added).
        self.assertEqual(addendum_text(InstanceModifiers.BASE), SEEK_SUMMARY)

    def test_returns_firewall_notice_for_auto(self):
        self.assertEqual(addendum_text(InstanceModifiers.MODE_AUTO), FIREWALL_NOTICE)

    def test_credentials_addendum_matches_credentials_notice(self):
        # Whether the notice is "" or "...CLIs...", addendum_text returns the same value.
        self.assertEqual(addendum_text(InstanceModifiers.TAG_PROG), CREDENTIALS_NOTICE)

    def test_join_separator_is_triple_newline(self):
        # Patch the dict to add a second entry for BASE and confirm the join shape.
        custom = {
            InstanceModifiers.BASE: ["alpha-content", "beta-content"],
        }
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            result = addendum_text(InstanceModifiers.BASE)
            self.assertEqual(result, "alpha-content\n\n\nbeta-content")

    def test_empty_entries_filtered_before_join(self):
        # Mixed empty + non-empty addendums — empties get dropped, no orphan separators.
        custom = {
            InstanceModifiers.BASE: ["", "alpha", "", "beta", ""],
        }
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            result = addendum_text(InstanceModifiers.BASE)
            self.assertEqual(result, "alpha\n\n\nbeta")

    def test_all_empty_returns_empty(self):
        custom = {
            InstanceModifiers.BASE: ["", ""],
        }
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            self.assertEqual(addendum_text(InstanceModifiers.BASE), "")


if __name__ == "__main__":
    unittest.main()
