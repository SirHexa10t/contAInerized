"""Tests for launch.container_probe — the four calls that talk to an already
running container. The gate below is the reason the module exists as a LEAF:
the firewall's updater needs it, and `docker_config` imports the firewall, so
neither could own it without a cycle (see the module docstring)."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from launch import container_probe, paths


class TestWaitForFirewallApplied(unittest.TestCase):
    """The phase-2 updater's gate: don't insert rules until init-firewall.sh
    finished (its completion marker exists); bail when the container died
    without it; proceed best-effort on a timed-out-but-alive container."""

    def _wait(self, exec_returncodes, running, timeout=5):
        codes = iter(exec_returncodes)
        with patch.object(container_probe, "docker_exec_root_subprocess",
                          side_effect=lambda *a: SimpleNamespace(returncode=next(codes))) as ex, \
             patch.object(container_probe, "docker_check_running_subprocess", side_effect=running), \
             patch.object(container_probe.time, "sleep"):
            result = container_probe.wait_for_firewall_applied("claude-code_test", timeout_seconds=timeout)
        return result, ex

    def test_marker_present_immediately(self):
        result, ex = self._wait([0], running=[True])
        self.assertTrue(result)
        ex.assert_called_once()
        self.assertIn(str(paths.FIREWALL_DONE_IN_CONTAINER), ex.call_args.args)

    def test_marker_appears_after_polling(self):
        result, _ = self._wait([1, 1, 0], running=[True, True])
        self.assertTrue(result)

    def test_container_death_without_marker_bails(self):
        # init-firewall failed its self-test and took the container down —
        # the updater has nothing to update.
        result, _ = self._wait([1, 1], running=[True, False])
        self.assertFalse(result)

    def test_timeout_with_live_container_proceeds_best_effort(self):
        result, _ = self._wait([], running=[True], timeout=0)
        self.assertTrue(result)

    def test_timeout_with_dead_container_bails(self):
        result, _ = self._wait([], running=[False], timeout=0)
        self.assertFalse(result)

    def test_marker_path_matches_the_shell_script(self):
        # paths.FIREWALL_DONE_IN_CONTAINER mirrors a literal in
        # the {firewall} specialty's init-firewall.sh (shell can't import Python constants) —
        # this is the drift guard the constant's comment promises.
        script = (paths.INIT_FIREWALL_SH).read_text()
        self.assertIn(f"touch {paths.FIREWALL_DONE_IN_CONTAINER}", script)


if __name__ == "__main__":
    unittest.main()
