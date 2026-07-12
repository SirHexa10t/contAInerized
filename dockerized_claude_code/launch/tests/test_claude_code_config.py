"""Tests for launch.claude_code_config — the host-staged in-container UX
strings. build_status_line is pure string assembly over an InstanceIdentity
plus one JSON field read (patched here), so it tests without any launcher
state."""

import unittest
from unittest.mock import patch

from launch import claude_code_config
from launch.structs import InstanceIdentity


def _inst() -> InstanceIdentity:
    """A cont-shaped identity for a real repo agent (golem — tagless, so the
    modifier chain stays empty)."""
    return InstanceIdentity(agent="golem", session="s1", workspace="/tmp/ws",
                            is_brand_new=False, modes=())


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


class TestSetTerminalTitle(unittest.TestCase):
    def test_emits_osc0_escape_with_name(self):
        with patch("builtins.print") as mock_print:
            claude_code_config.set_terminal_title("golem__s1")
        printed = mock_print.call_args.args[0]
        self.assertTrue(printed.startswith("\033]0;"))
        self.assertIn("golem__s1", printed)
        self.assertTrue(printed.endswith("\007"))


if __name__ == "__main__":
    unittest.main()
