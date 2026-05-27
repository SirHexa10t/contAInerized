"""Tests for launch.memory_addendums — Addendum shape, the addendum bodies,
and the composed_addendum renderer.

`CREDENTIALS_NOTICE.body` is evaluated at module import (via
`installed_cred_clis()`), so its concrete value depends on the launcher
environment. Tests here assert structure and patterns, not the exact CLI list."""

import unittest
from unittest.mock import patch

from launch.memory_addendums import (
    ADDENDUM_SECTION_TITLE, Addendum, CREDENTIALS_NOTICE, FIREWALL_NOTICE,
    MAINTAIN_PRIVACY, MODIFIER_ADDENDUMS, SEEK_SUMMARY, composed_addendum,
)
from launch.structs import InstanceModifiers


# ============================================================
# Addendum NamedTuple — shape + accessors
# ============================================================


class TestAddendumShape(unittest.TestCase):
    def test_each_constant_is_an_addendum(self):
        for addendum in (SEEK_SUMMARY, FIREWALL_NOTICE, CREDENTIALS_NOTICE, MAINTAIN_PRIVACY):
            with self.subTest(addendum=addendum):
                self.assertIsInstance(addendum, Addendum)

    def test_title_and_body_attributes(self):
        # NamedTuple style — `.title` / `.body` access, plus tuple indexing.
        self.assertEqual(SEEK_SUMMARY.title, SEEK_SUMMARY[0])
        self.assertEqual(SEEK_SUMMARY.body, SEEK_SUMMARY[1])

    def test_titles_are_human_readable(self):
        # Sub-headings render verbatim as `### <title>`; assert each title is
        # set to its user-facing form.
        self.assertEqual(SEEK_SUMMARY.title, "Project summary")
        self.assertEqual(FIREWALL_NOTICE.title, "Firewall")
        self.assertEqual(CREDENTIALS_NOTICE.title, "Credentials")
        self.assertEqual(MAINTAIN_PRIVACY.title, "Privacy")


# ============================================================
# Addendum body contents — structural assertions
# ============================================================


class TestSeekSummaryBody(unittest.TestCase):
    def test_references_summary_path(self):
        self.assertIn("/workspace/.claude_summary", SEEK_SUMMARY.body)

    def test_mentions_write_summary_command(self):
        self.assertIn("/write-summary", SEEK_SUMMARY.body)


class TestFirewallNoticeBody(unittest.TestCase):
    def test_contains_auto_label_not_escape(self):
        # Should contain literal `{auto}` (rendered from InstanceModifiers.MODE_WARN_AUTO.label),
        # NOT `{{auto}}` (the f-string escape form).
        self.assertIn("{auto}", FIREWALL_NOTICE.body)
        self.assertNotIn("{{auto}}", FIREWALL_NOTICE.body)

    def test_references_status_file_in_container(self):
        # Path comes from state_domain_resolve_status_path(CLAUDE_CONFIG_IN_CONTAINER)
        self.assertIn("/home/claude/.claude/domains_pending_resolve.yml", FIREWALL_NOTICE.body)

    def test_references_whitelist_file(self):
        # The host-side whitelist path is interpolated from FIREWALL_WHITELIST_FILE
        self.assertIn("firewall_whitelist.txt", FIREWALL_NOTICE.body)

    def test_mentions_econnrefused(self):
        self.assertIn("ECONNREFUSED", FIREWALL_NOTICE.body)

    def test_mentions_pending_and_failed_sections(self):
        self.assertIn("pending:", FIREWALL_NOTICE.body)
        self.assertIn("failed:", FIREWALL_NOTICE.body)


class TestCredentialsNoticeBody(unittest.TestCase):
    """CREDENTIALS_NOTICE.body is dynamic: text+CLI-list when creds present, '' otherwise.
    The value is locked at import time, so we test the two shapes it can have."""

    def test_either_empty_or_describes_clis(self):
        if CREDENTIALS_NOTICE.body:
            self.assertIn("credentials", CREDENTIALS_NOTICE.body.lower())
            self.assertIn("installed", CREDENTIALS_NOTICE.body)
        else:
            self.assertEqual(CREDENTIALS_NOTICE.body, "")


class TestMaintainPrivacyBody(unittest.TestCase):
    """MAINTAIN_PRIVACY warns the agent off persisting personal / runtime-environment
    details (emails, usernames, installed CLI inventories, etc.) into project text.
    Structural asserts — the body must carry the load-bearing phrases that a
    future agent reading its CLAUDE.md needs to pattern-match against its own
    proposed writes."""

    def test_mentions_persistence(self):
        # The directive is about WRITING TO PERSISTED FILES, not about chat output.
        self.assertIn("persist", MAINTAIN_PRIVACY.body.lower())

    def test_lists_personal_identifier_categories(self):
        # The categories the user explicitly named in the incident that motivated this.
        for term in ("email", "name", "username", "credential"):
            with self.subTest(term=term):
                self.assertIn(term, MAINTAIN_PRIVACY.body.lower())

    def test_lists_environment_categories(self):
        # "What's installed in this dev shell" — the smoking-gun heading from the incident.
        self.assertIn("CLI", MAINTAIN_PRIVACY.body)

    def test_specifies_exception_requires_confirmation(self):
        # When the user explicitly asks, a confirmation prompt is still required.
        self.assertIn("confirm", MAINTAIN_PRIVACY.body.lower())

    def test_confirmation_required_even_under_bypass_mode(self):
        # `{auto}` mode bypasses routine permission prompts — but NOT this one.
        self.assertIn("{auto}", MAINTAIN_PRIVACY.body)
        self.assertIn("bypass", MAINTAIN_PRIVACY.body.lower())


# ============================================================
# MODIFIER_ADDENDUMS dict structure
# ============================================================


class TestModifierAddendumsDict(unittest.TestCase):
    def test_keys_are_instance_modifiers(self):
        for k in MODIFIER_ADDENDUMS:
            with self.subTest(modifier=k):
                self.assertIsInstance(k, InstanceModifiers)

    def test_values_are_lists_of_addendums(self):
        for k, v in MODIFIER_ADDENDUMS.items():
            with self.subTest(modifier=k):
                self.assertIsInstance(v, list)
                for item in v:
                    self.assertIsInstance(item, Addendum)

    def test_base_maps_to_seek_summary(self):
        self.assertIn(SEEK_SUMMARY, MODIFIER_ADDENDUMS[InstanceModifiers.BASE])

    def test_base_maps_to_maintain_privacy(self):
        # Privacy guidance is project-wide, not mode-conditional — sits under BASE
        # so every agent's CLAUDE.md carries it.
        self.assertIn(MAINTAIN_PRIVACY, MODIFIER_ADDENDUMS[InstanceModifiers.BASE])

    def test_tag_code_maps_to_credentials_notice(self):
        self.assertIn(CREDENTIALS_NOTICE, MODIFIER_ADDENDUMS[InstanceModifiers.TAG_CODE])

    def test_mode_auto_maps_to_firewall_notice(self):
        self.assertIn(FIREWALL_NOTICE, MODIFIER_ADDENDUMS[InstanceModifiers.MODE_WARN_AUTO])

    def test_mode_dood_has_no_addendum(self):
        # MODE_WARN_DOOD doesn't currently advertise anything in CLAUDE.md.
        self.assertNotIn(InstanceModifiers.MODE_WARN_DOOD, MODIFIER_ADDENDUMS)


# ============================================================
# composed_addendum — the chain-to-markdown renderer
# ============================================================


class TestComposedAddendum(unittest.TestCase):
    """`composed_addendum(chain)` is the only consumer of MODIFIER_ADDENDUMS.
    Tests both the live (un-patched) production data and a patched dict where
    we control titles/bodies so structural assertions don't depend on the
    body text of real addendums."""

    # --- Production-data assertions ---

    def test_empty_chain_returns_empty_string(self):
        self.assertEqual(composed_addendum(()), "")

    def test_chain_without_any_known_modifier_returns_empty(self):
        # 'unknown' isn't in MODIFIER_ADDENDUMS → no sub-sections → ''
        self.assertEqual(composed_addendum(("unknown",)), "")

    def test_base_chain_contains_section_title(self):
        result = composed_addendum(("base",))
        self.assertIn(f"## {ADDENDUM_SECTION_TITLE}", result)

    def test_base_chain_contains_seek_summary_body(self):
        # The integration assertion the user asked for: a known-BASE substring
        # is wholly present in the rendered addendum text.
        result = composed_addendum(("base",))
        self.assertIn(SEEK_SUMMARY.body, result)

    def test_base_chain_contains_maintain_privacy_body(self):
        # MAINTAIN_PRIVACY also sits under BASE — both should render.
        result = composed_addendum(("base",))
        self.assertIn(MAINTAIN_PRIVACY.body, result)

    def test_base_chain_renders_each_title_as_h3(self):
        result = composed_addendum(("base",))
        self.assertIn(f"### {SEEK_SUMMARY.title}", result)
        self.assertIn(f"### {MAINTAIN_PRIVACY.title}", result)

    def test_auto_chain_includes_firewall_body(self):
        result = composed_addendum(("base", "auto"))
        self.assertIn(FIREWALL_NOTICE.body, result)
        self.assertIn(f"### {FIREWALL_NOTICE.title}", result)

    def test_section_title_appears_once_even_with_multiple_modifiers(self):
        result = composed_addendum(("base", "auto"))
        self.assertEqual(result.count(f"## {ADDENDUM_SECTION_TITLE}"), 1)

    def test_modifier_order_follows_enum_declaration(self):
        # InstanceModifiers declaration order: BASE → TAG_CODE → MODE_WARN_AUTO → MODE_WARN_DOOD.
        # Even when chain is passed with 'auto' before 'base', the output must
        # follow enum order (composed_addendum iterates InstanceModifiers, not chain).
        result = composed_addendum(("auto", "base"))
        i_seek = result.find(SEEK_SUMMARY.title)
        i_fire = result.find(FIREWALL_NOTICE.title)
        self.assertGreater(i_seek, -1)
        self.assertGreater(i_fire, -1)
        self.assertLess(i_seek, i_fire)

    # --- Patched-data assertions (control title/body for structure-only tests) ---

    def test_empty_body_addendum_is_filtered(self):
        # An addendum with body='' must NOT render — its sub-heading would be
        # an empty section, which is exactly what we don't want when
        # CREDENTIALS_NOTICE collapses to empty under no-creds.
        custom = {
            InstanceModifiers.BASE: [
                Addendum("Real", "real body"),
                Addendum("Phantom", ""),
            ],
        }
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            result = composed_addendum(("base",))
            self.assertIn("### Real", result)
            self.assertIn("real body", result)
            self.assertNotIn("### Phantom", result)

    def test_all_empty_bodies_return_empty_string(self):
        custom = {InstanceModifiers.BASE: [Addendum("A", ""), Addendum("B", "")]}
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            self.assertEqual(composed_addendum(("base",)), "")

    def test_join_separator_between_sub_sections(self):
        # Two non-empty addendums under BASE — sub-sections joined with '\n\n'
        # (one blank line between them).
        custom = {
            InstanceModifiers.BASE: [
                Addendum("First", "alpha"),
                Addendum("Second", "beta"),
            ],
        }
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            result = composed_addendum(("base",))
            self.assertEqual(
                result,
                f"## {ADDENDUM_SECTION_TITLE}\n\n"
                "### First\n\nalpha\n\n"
                "### Second\n\nbeta",
            )

    def test_modifier_with_no_entry_is_skipped(self):
        # A chain value with no MODIFIER_ADDENDUMS entry contributes nothing.
        custom = {InstanceModifiers.BASE: [Addendum("Base", "base body")]}
        with patch.dict(MODIFIER_ADDENDUMS, custom, clear=True):
            # 'code' has no entry in our patched dict → only base contributes.
            result = composed_addendum(("base", "code"))
            self.assertIn("base body", result)
            self.assertEqual(result.count("###"), 1)


if __name__ == "__main__":
    unittest.main()
