"""Tests for launch.staging — the one per-agent staging every run shape
calls. Driven with a REAL Instance against the real tag tree, under a
redirected state dir, with the mount accumulator isolated per test."""

import contextlib
import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import docker_config, paths
from launch.ai import CLAUDE_CODE
from launch.staging import stage_instance
from launch.tags import TagError

from launch.tests.fixtures import REGISTRY, make_inst

SOLO_CONFIG = str(paths.CLAUDE_CONFIG_IN_CONTAINER)
MEMBER_CONFIG = "/cluster/members/poet"


def _real(inst):
    """The fixture's Instance with its agent's REAL persona file — the
    staging installs it, so a fake path would be read."""
    return dataclasses.replace(inst, md_path=paths.AGENTS_DIR / f"{inst.agent}.md")


class StagingTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        docker_config._docker_mounts.clear()
        self.addCleanup(docker_config._docker_mounts.clear)
        self.inst = _real(make_inst("poet", "s1"))

    def _stage(self, *, config=SOLO_CONFIG, relocated=False):
        with contextlib.redirect_stdout(io.StringIO()):
            return stage_instance(self.inst, REGISTRY, harness=CLAUDE_CODE, config=config, relocated=relocated)


class TestStageInstance(StagingTmp):
    def test_installs_the_state_dir(self):
        self._stage()
        state = self.inst.state_dir
        self.assertTrue((state / CLAUDE_CODE.persona_filename).is_file())
        self.assertTrue((state / CLAUDE_CODE.settings_filename).is_file())
        self.assertTrue((state / CLAUDE_CODE.commands_dirname).is_dir())
        self.assertTrue(any((state / CLAUDE_CODE.commands_dirname).iterdir()), "the shared commands were assembled")

    def test_mounts_the_shadows_read_only_and_the_login_files_where_the_shape_expects_them(self):
        staged = self._stage()
        pairs = dict(staged.mounts)          # sources are unique here, so this direction can be a dict
        state = self.inst.state_dir
        self.assertEqual(pairs[str(state / "settings.json")], f"{SOLO_CONFIG}/settings.json:ro")
        self.assertEqual(pairs[str(state / "commands")], f"{SOLO_CONFIG}/commands:ro")
        creds_dir = paths.credentials_dir(CLAUDE_CODE.key)
        self.assertEqual(pairs[str(creds_dir / ".credentials.json")], f"{SOLO_CONFIG}/.credentials.json")
        self.assertEqual(pairs[str(creds_dir / ".claude.json")], f"{paths.CLAUDE_HOME_IN_CONTAINER}/.claude.json")
        # ... and they are STAGED, not merely reported: the accumulator both
        # shapes run their `docker run` from holds every pair.
        self.assertEqual(set(docker_config.staged_mounts()), set(staged.mounts))

    def test_the_login_files_exist_before_docker_binds_them(self):
        self._stage()
        for f in CLAUDE_CODE.auth_files:
            with self.subTest(file=f.name):
                path = paths.credentials_dir(CLAUDE_CODE.key) / f.name
                self.assertTrue(path.is_file())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_the_notices_are_the_credentials_stagings(self):
        (notice,) = self._stage().notices
        self.assertIn("no API key file", notice)

    def test_solo_and_member_differ_only_in_the_config_root_and_the_relocated_anchor(self):
        # THE parity pin: one function, two shapes. Same sources in the same
        # order; targets equal once the config prefix is normalised, except
        # the `account`-anchored file, which sits at HOME for a solo instance
        # and inside the config dir for a relocated member (paths.auth_file_mounts).
        solo = self._stage().mounts
        docker_config._docker_mounts.clear()
        member = self._stage(config=MEMBER_CONFIG, relocated=True).mounts
        self.assertEqual([s for s, _ in solo], [s for s, _ in member])
        account = CLAUDE_CODE.auth_file("account")
        for (_, solo_target), (_, member_target) in zip(solo, member):
            with self.subTest(target=member_target):
                if member_target.endswith(f"/{account.name}"):
                    self.assertEqual(solo_target, f"{paths.CLAUDE_HOME_IN_CONTAINER}/{account.name}")
                    self.assertEqual(member_target, f"{MEMBER_CONFIG}/{account.name}")
                else:
                    self.assertEqual(solo_target.replace(SOLO_CONFIG, "", 1), member_target.replace(MEMBER_CONFIG, "", 1))

    def test_two_members_share_sources_and_never_targets(self):
        # The cluster case the pair accumulator exists for: the same login
        # file staged for two members is two mounts, and their own files
        # land at distinct targets — no collision, nothing dropped.
        first = self._stage(config="/cluster/members/a", relocated=True).mounts
        other = _real(make_inst("golem", "s2"))
        with contextlib.redirect_stdout(io.StringIO()):
            second = stage_instance(other, REGISTRY, harness=CLAUDE_CODE, config="/cluster/members/b", relocated=True).mounts
        self.assertEqual(len(docker_config.staged_mounts()), len(first) + len(second))
        targets = [t for _, t in docker_config.staged_mounts()]
        self.assertEqual(len(targets), len(set(targets)))
        creds = str(paths.credentials_dir(CLAUDE_CODE.key) / ".credentials.json")
        self.assertEqual(sum(1 for s, _ in docker_config.staged_mounts() if s == creds), 2)


class TestStageInstanceErrors(StagingTmp):
    """Nothing here prints or exits: each shape wraps once at its boundary."""

    def test_a_policy_conflict_propagates_as_the_tag_error(self):
        with patch("launch.staging.install_settings", side_effect=TagError("loose vs tight")), \
             self.assertRaisesRegex(TagError, "loose vs tight"):
            self._stage()

    def test_a_lax_key_file_propagates_as_the_runtime_error_naming_the_line_not_the_value(self):
        key = paths.key_file("claude")
        key.parent.mkdir(parents=True)
        key.write_text('ANTHROPIC_API_KEY="sk-quoted"\n')
        with self.assertRaisesRegex(RuntimeError, "is quoted") as caught:
            self._stage()
        self.assertNotIn("sk-quoted", str(caught.exception))

    def test_a_shadowing_mount_propagates_as_the_accumulators_runtime_error(self):
        docker_config.add_docker_mount("/elsewhere/settings.json", f"{SOLO_CONFIG}/settings.json:ro")
        with self.assertRaisesRegex(RuntimeError, "already staged"):
            self._stage()
