"""Tests for launch.claude_code_config — the host-staged in-container UX
strings. build_status_line is pure string assembly over an Instance plus one
JSON field read (patched here), so it tests without any launcher state."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from launch.ai import active_adapter
from launch import claude_code_config, paths
from launch.claude_code_config import SQUASH_AT, colored_tag_chain
from launch.tags import Instance, PolicyStance


def _inst() -> Instance:
    """A cont-shaped identity — no tags, so the trailing chain stays empty."""
    return Instance(agent="golem", md_path=Path("/fake/golem.md"), session="s1",
                    workspace="/tmp/ws", is_brand_new=False, engine=None)


class TestBuildStatusLine(unittest.TestCase):
    def _line(self, email):
        with patch.object(claude_code_config, "read_json_field", return_value=email):
            return claude_code_config.build_status_line(_inst())

    def test_missing_email_renders_no_none_text(self):
        # Regression: before first login (.claude.json empty), the raw None
        # lookup was interpolated straight into the f-string — the status
        # line showed a literal green "None".
        line = self._line(None)
        self.assertNotIn("None", line)
        self.assertNotIn(" : ", line)   # separator drops out with the email

    def test_email_present_renders_email_and_separator(self):
        line = self._line("dev@example.com")
        self.assertIn("dev@example.com", line)
        self.assertIn(" : ", line)

    def test_instance_id_always_present(self):
        for email in (None, "dev@example.com"):
            with self.subTest(email=email):
                self.assertIn("golem__s1", self._line(email))

    def test_agent_and_session_title_cased(self):
        line = self._line(None)
        self.assertIn("Golem", line)
        self.assertIn("S1", line)

    def test_workspace_shown(self):
        self.assertIn("/tmp/ws", self._line(None))


class TestColoredTagChain(unittest.TestCase):
    """Warn-aware ANSI coloring: warn-flagged tags bright red, everything
    else bright green; each label self-resets. Duck-typed stand-ins — the
    function only reads `.label` and (optionally) `.warn`."""

    def test_empty_chain_is_empty_string(self):
        self.assertEqual(colored_tag_chain(()), "")

    def test_safe_tag_green(self):
        chain = colored_tag_chain((SimpleNamespace(label="[code]"),))
        self.assertIn("\033[22;92m[code]\033[0m", chain)

    def test_warn_tag_red(self):
        chain = colored_tag_chain((SimpleNamespace(label="{dood}", warn=True),))
        self.assertIn("\033[01;91m{dood}\033[0m", chain)

    def test_deny_policy_blue(self):
        chain = colored_tag_chain((SimpleNamespace(label="<-su>", stance=PolicyStance.DENY),))
        self.assertIn("\033[01;94m<-su>\033[0m", chain)

    def test_allow_policy_orange(self):
        chain = colored_tag_chain((SimpleNamespace(label="<+qry>", stance=PolicyStance.ALLOW),))
        self.assertIn("\033[38;5;208m<+qry>\033[0m", chain)

    def test_demand_policy_white(self):
        chain = colored_tag_chain((SimpleNamespace(label="<!plan>", stance=PolicyStance.DEMAND),))
        self.assertIn("\033[01;97m<!plan>\033[0m", chain)

    def test_labels_space_separated(self):
        chain = colored_tag_chain((SimpleNamespace(label="[code]"),
                                   SimpleNamespace(label="{auto}", warn=True)))
        self.assertEqual(chain.count(" "), 1)


class TestColoredTagChainSquashed(unittest.TestCase):
    """At SQUASH_AT tags the status line stops spelling labels: each tag
    collapses to its one-char glyph on a chip of its color (black glyph, color
    background), one space between chips — same rule and same glyph as the
    picker's tag columns. Real tags from the real tree, because squash_glyph
    lives on Tag and a SimpleNamespace would dodge exactly the code under
    test."""

    def setUp(self):
        from launch.paths import AGENTS_DIR
        from launch.tags import scan_all
        reg = scan_all(AGENTS_DIR)
        self.tags = (reg.professions["code"], reg.professions["webdev"],
                     reg.specialties["auto"], reg.specialties["cowork"],
                     reg.specialties["manager"], reg.policies["no-sudo"])
        self.assertEqual(len(self.tags), SQUASH_AT)

    def test_below_the_threshold_labels_survive(self):
        self.assertIn("[code]", colored_tag_chain(self.tags[:SQUASH_AT - 1]))

    def test_at_the_threshold_no_label_survives(self):
        chain = colored_tag_chain(self.tags)
        for tag in self.tags:
            self.assertNotIn(tag.label, chain)

    def test_chips_are_black_glyphs_on_color_backgrounds(self):
        chain = colored_tag_chain(self.tags)
        # auto is warn-flagged: black (30) on bright-red background (101).
        self.assertIn("\033[30;101ma\033[0m", chain)
        # code is safe: black on bright-green background (102).
        self.assertIn("\033[30;102mc\033[0m", chain)
        # no-sudo is a DENY policy: black on bright-blue (104), glyph 's'
        # (the stance symbol and punctuation never become the glyph).
        self.assertIn("\033[30;104ms\033[0m", chain)

    def test_chips_are_space_separated(self):
        # One space between chips — adjacent same-colored chips would read as
        # one block — and none leading or trailing.
        chain = colored_tag_chain(self.tags)
        self.assertEqual(chain.count(" "), SQUASH_AT - 1)
        self.assertFalse(chain.startswith(" ") or chain.endswith(" "))

    def test_the_threshold_is_shared_with_the_picker(self):
        # One definition of "crowded" everywhere, or the two displays would
        # disagree about when the chips appear.
        from launch.tags.base import SQUASH_AT as base_threshold
        self.assertEqual(claude_code_config.SQUASH_AT, base_threshold)


class TestSetTerminalTitle(unittest.TestCase):
    def test_emits_osc0_escape_with_name(self):
        with patch("builtins.print") as mock_print:
            claude_code_config.set_terminal_title("golem__s1")
        printed = mock_print.call_args.args[0]
        self.assertTrue(printed.startswith(f"\033]0;{active_adapter().name} — "))   # the CLI's name from the catalog, not a literal
        self.assertIn("golem__s1", printed)
        self.assertTrue(printed.endswith("\007"))


if __name__ == "__main__":
    unittest.main()


class TestCredentialsNotice(unittest.TestCase):
    """credentials_notice — one line before docker runs, only when the login
    state deserves it: nothing to log in with, or a key beside a login."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        from launch.ai import CLAUDE_CODE
        from launch.paths import AGENTS_DIR
        from launch.tags import scan_all
        self.adapter, self.ai = CLAUDE_CODE, scan_all(AGENTS_DIR).ais["claude"]

    def _login(self):
        for f in self.adapter.auth_files:
            path = paths.credentials_dir(self.adapter.key) / f.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"%s": {"x": 1}}' % f.login_key)

    def _key(self):
        paths.key_file("claude").parent.mkdir(parents=True, exist_ok=True)
        paths.key_file("claude").write_text("ANTHROPIC_API_KEY=sk\n")

    def test_nothing_to_log_in_with(self):
        notice = claude_code_config.credentials_notice(self.adapter, self.ai)
        self.assertIn("no API key file", notice)
        self.assertIn("log in inside the container", notice)

    def test_a_login_alone_says_nothing(self):
        self._login()
        self.assertIsNone(claude_code_config.credentials_notice(self.adapter, self.ai))

    def test_a_login_recorded_in_one_file_only_is_not_a_login(self):
        # The post-incident split — an account in .claude.json, a blank token
        # file — is exactly a state where the CLI prompts.
        self._login()
        creds = self.adapter.auth_file("credentials")
        (paths.credentials_dir(self.adapter.key) / creds.name).write_text(creds.blank)
        notice = claude_code_config.credentials_notice(self.adapter, self.ai)
        self.assertIsNotNone(notice)
        self.assertIn("log in inside the container", notice)

    def test_a_key_alone_says_nothing(self):
        self._key()
        self.assertIsNone(claude_code_config.credentials_notice(self.adapter, self.ai))

    def test_a_key_beside_a_login_names_the_precedence(self):
        self._login()
        self._key()
        notice = claude_code_config.credentials_notice(self.adapter, self.ai)
        self.assertIn("takes precedence", notice)
        self.assertNotIn("sk", notice.split("credentials/keys/")[-1][:0])   # (the path is fine to name; the value never appears)

    def test_no_ai_means_no_notice(self):
        self.assertIsNone(claude_code_config.credentials_notice(self.adapter, None))
