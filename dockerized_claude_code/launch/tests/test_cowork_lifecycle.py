"""Tests for launch.cowork.lifecycle — the hub's singleton guard.

Singleton-ness is a correctness property, not tidiness: two hubs draining the same
outboxes would each consume about half the captures. So these tests care most about
the two ways it could fail open — a second claim succeeding, and a stale pidfile
from a crash blocking every future hub forever.
"""

import os
import unittest
from types import SimpleNamespace
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


class TestEnsureHubRunning(LifecycleTmpRoot):
    """run.py's hook: start a hub only when nobody serves. The spawn itself is
    patched — what matters is WHETHER it happens, HOW detached it is, and where
    its output goes; an actual child is exercised by the reparenting test
    below and the manual smoke run."""

    def setUp(self):
        super().setUp()
        self.spawned: list[dict] = []
        done = SimpleNamespace(returncode=0, stdout="4242\n", stderr="")
        p = patch.object(lifecycle.subprocess, "run",
                         side_effect=lambda argv, **kw: (self.spawned.append(
                             {"argv": argv, **kw}), done)[1])
        p.start()
        self.addCleanup(p.stop)

    def _command(self) -> str:
        argv = self.spawned[0]["argv"]
        self.assertEqual(argv[:2], ["sh", "-c"])
        return argv[2]

    def test_spawns_the_entry_script_when_nobody_serves(self):
        line = lifecycle.ensure_hub_running()
        self.assertEqual(len(self.spawned), 1)
        command = self._command()
        self.assertIn(str(paths.COWORK_SCRIPT), command)
        self.assertIn(" serve ", command)
        # -u because stdout is a file: unbuffered, or the log trails reality.
        self.assertIn(" -u ", command)
        self.assertIn("4242", line)

    def test_the_hub_is_detached_and_reparented(self):
        # nohup survives the launching terminal closing; `& echo $!` is the
        # reparenting: sh exits at once, init adopts the hub, and a dead hub
        # can never linger as OUR zombie fooling the liveness probe.
        command = (lifecycle.ensure_hub_running(), self._command())[1]
        self.assertIn("nohup ", command)
        self.assertIn("& echo $!", command)

    def test_output_goes_to_the_log_not_the_terminal(self):
        # A daemon inheriting this terminal would scribble its event stream
        # over the Claude session about to start.
        command = (lifecycle.ensure_hub_running(), self._command())[1]
        self.assertIn(f">> {paths.hub_log_path()}", command)
        self.assertIn("2>&1", command)
        self.assertIn("< /dev/null", command)

    def test_a_live_hub_is_left_alone(self):
        claimed = lifecycle.claim()
        self.addCleanup(lifecycle.release, claimed)
        line = lifecycle.ensure_hub_running()
        self.assertEqual(self.spawned, [])
        self.assertIn("already serving", line)
        self.assertIn(str(claimed.pid), line)

    def test_a_stale_pidfile_does_not_block_the_spawn(self):
        self.write_pid(f"{self.a_dead_pid()}\n")
        lifecycle.ensure_hub_running()
        self.assertEqual(len(self.spawned), 1)

    def test_works_before_the_group_hosting_dir_exists(self):
        # The first manager ever launched: nothing has created the tree yet,
        # and the spawn must not crash the whole launch over it.
        self.assertFalse(paths.group_hosting_dir().exists())
        lifecycle.ensure_hub_running()
        self.assertEqual(len(self.spawned), 1)

    def test_a_failed_spawn_reports_instead_of_pretending(self):
        self.spawned_result = None
        with patch.object(lifecycle.subprocess, "run",
                          return_value=SimpleNamespace(returncode=127, stdout="",
                                                       stderr="sh: not found")):
            line = lifecycle.ensure_hub_running()
        self.assertIn("FAILED", line)
        self.assertIn("cowork serve", line)


class TestHubReparenting(LifecycleTmpRoot):
    """The zombie corner, exercised for real: a child THIS process spawns and
    never waits on reads as alive after death (`os.kill(pid, 0)` succeeds on
    zombies), which would have made a crashed hub unrestartable for as long as
    the manager's run.py lived. The sh-reparented spawn must not have that
    property."""

    def test_a_direct_unwaited_child_would_fool_the_liveness_probe(self):
        # The failure mode itself, pinned so the WHY survives refactors.
        import subprocess
        import sys
        import time
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        time.sleep(0.3)                        # child has exited; we never waited
        self.assertTrue(lifecycle._running(child.pid))   # the zombie reads as alive

    def test_the_reparented_hub_reads_as_dead_once_it_exits(self):
        # Valid only where pid 1 reaps orphans — every real host (systemd,
        # launchd) does, and the launcher runs host-side by design. A minimal
        # container whose pid 1 is an ordinary program does not, so there the
        # orphan stays a zombie through no fault of the spawn — detect that
        # and skip rather than fail the suite on an environment that cannot
        # host the hub anyway.
        import shlex
        import subprocess
        import sys
        import time
        probe = (f"nohup {shlex.quote(sys.executable)} -c 'pass' "
                 f">> /dev/null 2>&1 < /dev/null & echo $!")
        result = subprocess.run(["sh", "-c", probe], capture_output=True, text=True)
        pid = int(result.stdout.strip())
        deadline = time.monotonic() + 5
        while lifecycle._running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)                   # a reaping init collects it within moments
        if lifecycle._running(pid):
            state = Path(f"/proc/{pid}/stat").read_text().split()[2] \
                if Path(f"/proc/{pid}/stat").exists() else "?"
            if state == "Z":
                self.skipTest("pid 1 here does not reap orphans (not a real "
                              "host init) — the reparenting cannot be observed")
        self.assertFalse(lifecycle._running(pid))


class TestManagerWatch(LifecycleTmpRoot):
    """The exit condition: grace measured in TIME, docker-unknown never counts,
    and a manager reappearing resets the clock."""

    def setUp(self):
        super().setUp()
        self.now = 0.0
        self.managers: frozenset | None = frozenset()
        p = patch.object(lifecycle, "running_managers", lambda registry: self.managers)
        p.start()
        self.addCleanup(p.stop)
        self.watch = lifecycle.ManagerWatch(registry=None, grace_seconds=60.0,
                                            clock=lambda: self.now)

    def test_keeps_serving_while_a_manager_runs(self):
        self.managers = frozenset({"boss__p"})
        for self.now in (0.0, 100.0, 1000.0):
            self.assertIsNone(self.watch.reason_to_stop())

    def test_managerless_but_inside_the_grace_keeps_serving(self):
        self.now = 0.0
        self.assertIsNone(self.watch.reason_to_stop())
        self.now = 59.0
        self.assertIsNone(self.watch.reason_to_stop())

    def test_exits_once_the_grace_has_fully_elapsed(self):
        self.now = 0.0
        self.watch.reason_to_stop()
        self.now = 60.0
        reason = self.watch.reason_to_stop()
        self.assertIsNotNone(reason)
        self.assertIn("manager", reason)

    def test_a_manager_reappearing_resets_the_clock(self):
        # The relaunch-thrash case the grace exists for.
        self.now = 0.0
        self.watch.reason_to_stop()
        self.now, self.managers = 59.0, frozenset({"boss__p"})
        self.assertIsNone(self.watch.reason_to_stop())
        self.now, self.managers = 60.0, frozenset()
        self.assertIsNone(self.watch.reason_to_stop())    # a fresh 60s starts here

    def test_docker_unknown_never_counts_toward_the_grace(self):
        # A probe hiccup must not be read as "every manager is gone".
        self.now, self.managers = 0.0, None
        for self.now in (0.0, 100.0, 10_000.0):
            self.assertIsNone(self.watch.reason_to_stop())

    def test_docker_unknown_mid_gap_resets_the_clock(self):
        self.now, self.managers = 0.0, frozenset()
        self.watch.reason_to_stop()
        self.now, self.managers = 30.0, None
        self.watch.reason_to_stop()
        self.now, self.managers = 61.0, frozenset()
        self.assertIsNone(self.watch.reason_to_stop())    # 61-ish is a NEW start


class TestPidFileLocation(LifecycleTmpRoot):
    def test_lives_at_the_group_hosting_root_outside_every_mount(self):
        # Alongside hub.state.json: hub-private, so no agent can reach it.
        self.assertEqual(lifecycle.pid_file().parent, paths.group_hosting_dir())
        self.assertEqual(lifecycle.pid_file(), paths.hub_pid_path())


if __name__ == "__main__":
    unittest.main()
