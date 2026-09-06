"""Tests for launch.firewall.iptables — token → rules, and the batched apply.

The validation tests here are the security ones: every string this module
emits gets joined into a `sh -c` script that runs as root inside the
container, so a token that does not match the strict address/port shape must
be DROPPED, never escaped and never passed through. Moved here with the
module 2026-09-03 (they were test_firewall's).

No docker: the exec is patched, and what the tests assert on is the script
text that would have been run."""

import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from launch.firewall import iptables


class TestRulesFor(unittest.TestCase):
    def test_default_ports_open_https_and_http(self):
        rules = iptables.rules_for("1.2.3.4")
        self.assertEqual(rules, [
            "iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 443 -j ACCEPT",
            "iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 80 -j ACCEPT",
        ])

    def test_explicit_port_opens_only_that_port(self):
        rules = iptables.rules_for("1.2.3.4:8443")
        self.assertEqual(rules, ["iptables -I OUTPUT 1 -d 1.2.3.4 -p tcp --dport 8443 -j ACCEPT"])

    def test_cidr_token_accepted(self):
        rules = iptables.rules_for("104.16.0.0/13")
        self.assertEqual(len(rules), 2)
        self.assertIn("-d 104.16.0.0/13", rules[0])

    def test_malformed_token_dropped_with_warning(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rules = iptables.rules_for("$(reboot)")
        self.assertEqual(rules, [])
        self.assertIn("dropping malformed", err.getvalue())

    def test_shell_injection_attempt_dropped(self):
        # These strings are `&&`-joined into a `sh -c` script — nothing that
        # fails the strict address shape may produce a rule.
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(iptables.rules_for("1.2.3.4; reboot"), [])
            self.assertEqual(iptables.rules_for("1.2.3.4:80; reboot"), [])


class TestFlush(unittest.TestCase):
    def _flush(self, tokens, returncodes):
        """Run `flush` with a scripted docker-exec; returns the scripts
        executed (one per exec call)."""
        scripts = []
        codes = iter(returncodes)

        def fake_exec(container, *cmd):
            scripts.append(cmd[-1])
            return SimpleNamespace(returncode=next(codes, 0), stdout="", stderr="boom")

        with patch("launch.firewall.iptables.docker_exec_root_subprocess", side_effect=fake_exec):
            iptables.flush("c", tokens)
        return scripts

    def test_burst_becomes_single_exec(self):
        # 10 tokens × 2 default ports = 20 rules — well under the chunk cap.
        scripts = self._flush([f"1.2.3.{i}" for i in range(10)], [0])
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0].count("iptables -I"), 20)
        self.assertIn(" && ", scripts[0])

    def test_large_burst_chunks_at_cap(self):
        # 75 tokens × 2 ports = 150 rules → 2 execs at the 100-rule cap.
        scripts = self._flush([f"10.0.0.{i}:443" for i in range(150)], [0, 0])
        self.assertEqual(len(scripts), 2)

    def test_failed_chunk_retries_once_then_warns(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            scripts = self._flush(["1.2.3.4"], [1, 1])
        self.assertEqual(len(scripts), 2)   # first try + one retry
        self.assertIn("batched iptables insert failed", err.getvalue())

    def test_retry_success_does_not_warn(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            scripts = self._flush(["1.2.3.4"], [1, 0])
        self.assertEqual(len(scripts), 2)
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
