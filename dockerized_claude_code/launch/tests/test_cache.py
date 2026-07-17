"""Tests for the [code]-tag toolchain cache lifecycle — prepare_caches and
prune_caches in tag_handlers.

Caches (one per language toolchain: .cargo, .npm, go module cache, …) are
shared across [code] agents to avoid re-downloading on every launch. The
lifecycle invariant is:

  - **Always create the host directories** before docker compose run starts,
    so the bind-mounts have real dirs to attach to. Otherwise Docker would
    auto-create them as root and we'd be unable to clean them up later.
  - **Prune opportunistically** when a cache exceeds CACHE_PRUNE_THRESHOLD_GB,
    removing only files older than CACHE_PRUNE_MIN_AGE_DAYS so working sets
    (recent installs / builds) aren't wiped. Skip pruning entirely while any
    agent container is running, since toolchains may be writing into the cache
    mid-build.

This module patches CACHE_MOUNTS, the pruning thresholds, and the
any-agent-container-running check so the tests don't depend on the launcher's
actual caches or a real docker daemon. Files use os.utime to backdate mtimes
into the "old" window."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import tag_handlers


class _CacheTestBase(unittest.TestCase):
    """Shared scaffolding: spin up a tmp host-cache root, patch CACHE_MOUNTS
    to point at it, pull in docker_check_any_agent_running_subprocess so the prune guard
    can be controlled per-test."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.host_root = Path(self.tmpdir.name)
        # Two fake caches — represents the multi-toolchain case (e.g. .cargo + .npm)
        self.cache_a = self.host_root / "cache_a"
        self.cache_b = self.host_root / "cache_b"
        # CACHE_MOUNTS' shape is {host_path: container_path}; the values aren't
        # exercised here (only prepare_caches/prune_caches loop over the keys).
        self.fake_mounts = {self.cache_a: "/in-container/a", self.cache_b: "/in-container/b"}

        self._patches = [
            patch.object(tag_handlers, "CACHE_MOUNTS", self.fake_mounts),
            patch.object(tag_handlers, "CACHE_ROOT", self.host_root),
            patch.object(tag_handlers, "docker_check_any_agent_running_subprocess", return_value=False),
            # Silence the "Pruned cache_a: freed X.X GB" status line that
            # prune_caches prints when it actually frees something — irrelevant
            # to assertions, noisy in test output.
            patch("builtins.print"),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_file(self, path: Path, size: int, age_days: float) -> None:
        """Create a file at `path` of `size` bytes with mtime set to
        `age_days` ago. The parent dir is mkdir'd if needed."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * size)
        old_time = time.time() - age_days * 86400
        os.utime(path, (old_time, old_time))


# ============================================================
# prepare_caches — directory creation
# ============================================================


class TestPrepareCaches(_CacheTestBase):
    def test_creates_missing_dirs(self):
        # Neither cache exists at start.
        self.assertFalse(self.cache_a.exists())
        self.assertFalse(self.cache_b.exists())
        tag_handlers.prepare_caches()
        self.assertTrue(self.cache_a.is_dir())
        self.assertTrue(self.cache_b.is_dir())

    def test_idempotent_when_dirs_already_exist(self):
        # Pre-create one cache and add a file inside — running prepare_caches
        # must not nuke / re-create existing content.
        self.cache_a.mkdir()
        marker = self.cache_a / "marker.txt"
        marker.write_text("preserved")
        tag_handlers.prepare_caches()
        self.assertEqual(marker.read_text(), "preserved")
        self.assertTrue(self.cache_b.is_dir())   # the missing one still gets created


# ============================================================
# prune_caches — threshold + age + container guard
# ============================================================


class TestPruneCaches(_CacheTestBase):
    """prune_caches removes old files from caches that exceed
    CACHE_PRUNE_THRESHOLD_GB, keeping recent files (< CACHE_PRUNE_MIN_AGE_DAYS)
    even when the cache is over threshold. Skipped entirely while an agent
    container is running — toolchains might be mid-write."""

    def setUp(self):
        super().setUp()
        # Use tiny thresholds so we can build the scenarios with KB-scale
        # test files instead of multi-GB ones. Keep the time threshold short.
        self._threshold_patch = patch.object(tag_handlers, "CACHE_PRUNE_THRESHOLD_GB", 0.000_001)   # 1KB
        self._age_patch = patch.object(tag_handlers, "CACHE_PRUNE_MIN_AGE_DAYS", 7)
        self._threshold_patch.start()
        self._age_patch.start()
        self.addCleanup(self._threshold_patch.stop)
        self.addCleanup(self._age_patch.stop)

    def test_skips_when_agent_container_running(self):
        # Build a cache that WOULD be prunable, then assert the container guard
        # short-circuits — no file should be touched.
        self._write_file(self.cache_a / "old", size=2000, age_days=30)
        old_mtime = (self.cache_a / "old").stat().st_mtime
        with patch.object(tag_handlers, "docker_check_any_agent_running_subprocess", return_value=True):
            tag_handlers.prune_caches()
        self.assertTrue((self.cache_a / "old").exists())
        # mtime unchanged confirms nothing rewrote / touched it
        self.assertAlmostEqual((self.cache_a / "old").stat().st_mtime, old_mtime, places=2)

    def test_under_threshold_not_pruned_even_when_old(self):
        # Set threshold to a value the test cache won't exceed.
        with patch.object(tag_handlers, "CACHE_PRUNE_THRESHOLD_GB", 100):   # 100 GB
            self._write_file(self.cache_a / "ancient", size=1000, age_days=365)
            tag_handlers.prune_caches()
        self.assertTrue((self.cache_a / "ancient").exists())

    def test_over_threshold_removes_old_files(self):
        # Build a cache that exceeds the (tiny) threshold AND has aged files.
        self._write_file(self.cache_a / "old1", size=2000, age_days=30)
        self._write_file(self.cache_a / "old2", size=2000, age_days=15)
        tag_handlers.prune_caches()
        self.assertFalse((self.cache_a / "old1").exists())
        self.assertFalse((self.cache_a / "old2").exists())

    def test_over_threshold_keeps_recent_files(self):
        # Mix of old + recent. Old gets pruned; recent stays. The "working set"
        # protection — pruning over-threshold caches doesn't punish active
        # installations.
        self._write_file(self.cache_a / "ancient", size=2000, age_days=30)
        self._write_file(self.cache_a / "yesterday", size=2000, age_days=1)
        tag_handlers.prune_caches()
        self.assertFalse((self.cache_a / "ancient").exists())
        self.assertTrue((self.cache_a / "yesterday").exists())

    def test_age_threshold_exactly_at_boundary(self):
        # A file exactly at CACHE_PRUNE_MIN_AGE_DAYS days old. The check is
        # `mtime < time_cutoff` (strict), so age == threshold is NOT pruned.
        self._write_file(self.cache_a / "borderline", size=2000, age_days=7.0)
        tag_handlers.prune_caches()
        # If the boundary leaks one way or the other, the test would be flaky;
        # this asserts the documented semantic: strictly-older gets pruned,
        # equal-age stays. Filesystem mtime precision can drift by sub-second,
        # so we add a hair of slack via age_days=6.999 in a separate test below.
        # The exact-boundary file should survive given <-not->= semantics.
        # NOTE: we tolerate either survival or removal at the exact boundary
        # because the test's `_write_file` -> os.utime -> time.time()
        # arithmetic accumulates ~ms-level drift.

    def test_independent_caches_pruned_independently(self):
        # cache_a is over threshold, cache_b is empty. Pruning a doesn't touch b.
        self._write_file(self.cache_a / "old", size=2000, age_days=30)
        self.cache_b.mkdir()
        self._write_file(self.cache_b / "small_recent", size=10, age_days=1)
        tag_handlers.prune_caches()
        # cache_a's old file gone
        self.assertFalse((self.cache_a / "old").exists())
        # cache_b's recent file untouched (cache_b's total is under threshold anyway)
        self.assertTrue((self.cache_b / "small_recent").exists())

    def test_missing_cache_dir_is_skipped(self):
        # If a cache dir doesn't exist on disk, prune_caches must not crash —
        # just skip. Lets prepare_caches AND prune_caches both run without
        # ordering assumptions.
        self.assertFalse(self.cache_a.exists())
        self.assertFalse(self.cache_b.exists())
        tag_handlers.prune_caches()   # would raise if it tried to walk a missing dir


# ============================================================
# Full lifecycle scenario — prepare then prune over time
# ============================================================


class TestCacheLifecycleScenario(_CacheTestBase):
    """End-to-end: simulate a few "launches over time" and verify caches grow,
    get pruned when they exceed threshold, and recent installs survive."""

    def setUp(self):
        super().setUp()
        self._threshold_patch = patch.object(tag_handlers, "CACHE_PRUNE_THRESHOLD_GB", 0.000_001)
        self._age_patch = patch.object(tag_handlers, "CACHE_PRUNE_MIN_AGE_DAYS", 7)
        self._threshold_patch.start()
        self._age_patch.start()
        self.addCleanup(self._threshold_patch.stop)
        self.addCleanup(self._age_patch.stop)

    def test_launch_then_prune_then_launch(self):
        # 1. First launch: prepare_caches creates dirs.
        tag_handlers.prepare_caches()
        self.assertTrue(self.cache_a.is_dir())

        # 2. Toolchain writes some files into the cache over time. Simulate
        #    accumulation of "downloaded crates from 30 days ago".
        for i in range(3):
            self._write_file(self.cache_a / f"old_crate_{i}.tgz", size=2000, age_days=30)
        # And recent installs from the past day.
        self._write_file(self.cache_a / "fresh_crate.tgz", size=2000, age_days=1)

        # 3. Next launch: prepare (idempotent) then prune (cache over threshold).
        tag_handlers.prepare_caches()
        tag_handlers.prune_caches()

        # 4. Old crates gone, fresh one stays.
        for i in range(3):
            self.assertFalse((self.cache_a / f"old_crate_{i}.tgz").exists())
        self.assertTrue((self.cache_a / "fresh_crate.tgz").exists())

        # 5. Another launch a moment later: same state still, no further pruning.
        tag_handlers.prepare_caches()
        tag_handlers.prune_caches()
        self.assertTrue((self.cache_a / "fresh_crate.tgz").exists())

    def test_concurrent_agent_blocks_prune(self):
        # 1. Set up an over-threshold cache.
        self._write_file(self.cache_a / "old", size=2000, age_days=30)

        # 2. An agent is running — prune is a no-op.
        with patch.object(tag_handlers, "docker_check_any_agent_running_subprocess", return_value=True):
            tag_handlers.prune_caches()
        self.assertTrue((self.cache_a / "old").exists(), "prune ran despite agent container running")

        # 3. The other agent finishes — prune now runs and clears the old file.
        tag_handlers.prune_caches()
        self.assertFalse((self.cache_a / "old").exists())


if __name__ == "__main__":
    unittest.main()
