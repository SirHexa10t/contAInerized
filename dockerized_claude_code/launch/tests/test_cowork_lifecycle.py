"""Tests for launch.cowork.lifecycle — the hub's singleton guard.

Singleton-ness is a correctness property, not tidiness: two hubs draining the same
outboxes would each consume about half the captures. So these tests care most about
the two ways it could fail open — a second claim succeeding, and a stale pidfile
from a crash blocking every future hub forever.
"""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import lifecycle


class LifecycleTmpRoot(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._release)

    def _release(self):
        """Never leave a claim behind for the next test."""
        path = paths.hub_pid_path()
        if path.exists():
            path.unlink()

    def write_pid(self, value: str) -> None:
        path = paths.hub_pid_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def a_dead_pid(self) -> int:
        """A pid that is certainly not running: fork a child and reap it."""
        pid = os.fork()
        if pid == 0:                      # pragma: no cover - child exits immediately
            os._exit(0)
        os.waitpid(pid, 0)
        return pid


class TestClaiming(LifecycleTmpRoot):
    def test_a_first_claim_succeeds_and_records_this_process(self):
        claimed = lifecycle.claim()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.pid, os.getpid())
        self.assertTrue(claimed.ours)

    def test_the_claim_creates_the_pidfile(self):
        lifecycle.claim()
        self.assertEqual(paths.hub_pid_path().read_text().strip(), str(os.getpid()))

    def test_a_second_claim_is_refused_while_the_first_lives(self):
        # The whole point: two hubs would each drain half the captures.
        self.assertIsNotNone(lifecycle.claim())
        self.assertIsNone(lifecycle.claim())

    def test_a_claim_after_release_succeeds_again(self):
        first = lifecycle.claim()
        lifecycle.release(first)
        self.assertIsNotNone(lifecycle.claim())

    def test_a_stale_pidfile_does_not_block_a_new_hub(self):
        # A crash or reboot leaves one behind; refusing to start because of it is
        # the worse failure.
        self.write_pid(f"{self.a_dead_pid()}\n")
        self.assertIsNotNone(lifecycle.claim())

    def test_a_stale_pidfile_is_replaced_with_ours(self):
        self.write_pid(f"{self.a_dead_pid()}\n")
        lifecycle.claim()
        self.assertEqual(paths.hub_pid_path().read_text().strip(), str(os.getpid()))

    def test_a_malformed_pidfile_does_not_block_a_new_hub(self):
        self.write_pid("not-a-pid\n")
        self.assertIsNotNone(lifecycle.claim())

    def test_an_empty_pidfile_does_not_block_a_new_hub(self):
        self.write_pid("")
        self.assertIsNotNone(lifecycle.claim())

    def test_a_nonsense_pid_number_does_not_block_a_new_hub(self):
        self.write_pid("0\n")
        self.assertIsNotNone(lifecycle.claim())


class TestOwner(LifecycleTmpRoot):
    def test_no_pidfile_means_nobody_is_serving(self):
        self.assertIsNone(lifecycle.owner())

    def test_reports_our_own_claim_as_ours(self):
        lifecycle.claim()
        holder = lifecycle.owner()
        self.assertEqual(holder.pid, os.getpid())
        self.assertTrue(holder.ours)

    def test_a_live_foreign_pid_is_an_owner_but_not_ours(self):
        # pid 1 always exists and is not us.
        self.write_pid("1\n")
        holder = lifecycle.owner()
        self.assertEqual(holder.pid, 1)
        self.assertFalse(holder.ours)

    def test_a_dead_pid_is_not_an_owner(self):
        self.write_pid(f"{self.a_dead_pid()}\n")
        self.assertIsNone(lifecycle.owner())

    def test_every_unreadable_state_reads_as_nobody(self):
        # One answer, because they mean one thing to a caller.
        for content in ("", "   ", "not-a-pid", "-5", "0"):
            with self.subTest(pidfile=content):
                self.write_pid(content)
                self.assertIsNone(lifecycle.owner())


class TestRelease(LifecycleTmpRoot):
    def test_releasing_removes_the_pidfile(self):
        lifecycle.release(lifecycle.claim())
        self.assertFalse(paths.hub_pid_path().exists())

    def test_a_non_holder_cannot_release(self):
        # A hub that lost the race must not delete the winner's pidfile on its way
        # out — that would let a third hub start alongside the winner.
        lifecycle.claim()
        lifecycle.release(lifecycle.Owner(pid=999999, ours=False))
        self.assertTrue(paths.hub_pid_path().exists())

    def test_a_stale_claim_does_not_remove_someone_elses_file(self):
        # Our claim, then another process takes over the file. Releasing ours must
        # leave theirs alone.
        claimed = lifecycle.claim()
        self.write_pid("1\n")
        lifecycle.release(claimed)
        self.assertTrue(paths.hub_pid_path().exists())

    def test_releasing_twice_does_not_raise(self):
        claimed = lifecycle.claim()
        lifecycle.release(claimed)
        lifecycle.release(claimed)
        self.assertFalse(paths.hub_pid_path().exists())


class TestPidFileLocation(LifecycleTmpRoot):
    def test_lives_at_the_group_hosting_root_outside_every_mount(self):
        # Alongside hub.state.json: hub-private, so no agent can reach it.
        self.assertEqual(lifecycle.pid_file().parent, paths.group_hosting_dir())
        self.assertEqual(lifecycle.pid_file(), paths.hub_pid_path())


if __name__ == "__main__":
    unittest.main()
