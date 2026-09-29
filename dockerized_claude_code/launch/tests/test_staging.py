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
from launch.ai import CLAUDE_CODE, GEMINI_CLI
from launch.staging import stage_instance, stale_model_notice
from launch.tags import TagError, resolve_build

from launch.tests.fixtures import REGISTRY, make_inst

SOLO_CONFIG = str(paths.container_config_root())   # the default harness's root: Claude Code's
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
            return stage_instance(self.inst, REGISTRY, harness=CLAUDE_CODE, config=config, relocated=relocated,
                                  who=self.inst.instance)


class TestStagingARulesFileHarness(StagingTmp):
    """A harness that keeps its rules in a file of their own (Gemini CLI):
    the file is staged read-only over its own path — the FILE, never its
    directory, which the CLI writes its own auto-saved policies into — and
    that directory exists host-side before any mount is staged, since docker
    would otherwise create it root-owned (gate policy-tier, 2026-09-26)."""

    GEMINI_CONFIG = "/home/claude/.gemini"

    def _stage_gemini(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return stage_instance(self.inst, REGISTRY, harness=GEMINI_CLI, config=self.GEMINI_CONFIG, relocated=False,
                                  who=self.inst.instance)

    def test_the_rules_file_is_mounted_read_only_and_its_dir_is_not(self):
        pairs = dict(self._stage_gemini().mounts)
        source = self.inst.state_dir / REGISTRY.harnesses[GEMINI_CLI.key].policy_file
        self.assertEqual(pairs[str(source)], f"{self.GEMINI_CONFIG}/policies/launcher.toml:ro")
        self.assertNotIn(f"{self.GEMINI_CONFIG}/policies", [t.removesuffix(":ro") for t in pairs.values()])

    def test_the_rules_dir_exists_host_side_before_any_mount_is_staged(self):
        rules_dir = self.inst.state_dir / "policies"
        existed = []
        real = docker_config.add_docker_mount

        def record(source, target):
            existed.append(rules_dir.is_dir())
            real(source, target)
        with patch("launch.staging.add_docker_mount", side_effect=record):
            self._stage_gemini()
        self.assertTrue(existed and all(existed))

    def test_claude_code_keeps_no_rules_file(self):
        pairs = dict(self._stage().mounts)
        self.assertFalse(any("policies" in target for target in pairs.values()))


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
            second = stage_instance(other, REGISTRY, harness=CLAUDE_CODE, config="/cluster/members/b", relocated=True,
                                    who="b").mounts
        self.assertEqual(len(docker_config.staged_mounts()), len(first) + len(second))
        targets = [t for _, t in docker_config.staged_mounts()]
        self.assertEqual(len(targets), len(set(targets)))
        creds = str(paths.credentials_dir(CLAUDE_CODE.key) / ".credentials.json")
        self.assertEqual(sum(1 for s, _ in docker_config.staged_mounts() if s == creds), 2)


class TestStaleModelNotice(StagingTmp):
    """A picked model the instance's AI no longer offers: the launch drops it
    and says so in the staging's notices — which pick, why, and what runs
    instead, named for the agent the operator knows (gate model-picker)."""

    def _with_model(self, spelling):
        build = dataclasses.replace(self.inst.build, model=spelling)
        return dataclasses.replace(self.inst, **resolve_build(build, self.inst.agent, REGISTRY))

    def test_a_stale_pick_is_announced_with_both_models_and_why(self):
        self.inst = self._with_model("claude-opus-4-1")
        notices = self._stage().notices
        (line,) = [n for n in notices if "claude-opus-4-1" in n]
        self.assertIn(f"{self.inst.instance}'s model claude-opus-4-1 is not among ⟪Claude⟫'s models", line)
        self.assertIn(f"running {self.inst.engine.label}'s {self.inst.engine_model}", line)
        self.assertIn("F2 in the picker picks another", line)

    def test_in_a_cluster_the_notice_names_the_member(self):
        self.inst = self._with_model("claude-opus-4-1")
        with contextlib.redirect_stdout(io.StringIO()):
            staged = stage_instance(self.inst, REGISTRY, harness=CLAUDE_CODE, config=MEMBER_CONFIG, relocated=True,
                                    who="poet__writer")
        self.assertTrue(any(n.startswith("  Note: poet__writer's model claude-opus-4-1") for n in staged.notices))

    def _with_picks(self, model, effort):
        build = dataclasses.replace(self.inst.build, model=model, effort=effort)
        return dataclasses.replace(self.inst, **resolve_build(build, self.inst.agent, REGISTRY))

    def test_a_stale_effort_names_both_levels_and_the_member(self):
        # Opus 4.6 takes max but not xhigh: the pin falls to the nearest level
        # BELOW it, never up (bug-investigator, strict-reviewer, gate
        # model-picker-3).
        self.inst = self._with_picks("claude-opus-4-6", "xhigh")
        (line,) = [n for n in self._stage().notices if "xhigh" in n]
        self.assertIn(f"{self.inst.instance}'s effort xhigh is not one claude-opus-4-6 takes — running high instead", line)

    def test_a_stale_model_takes_its_effort_pin_and_names_the_pair_that_runs(self):
        self.inst = self._with_picks("claude-opus-4-1", "low")
        notices = self._stage().notices
        (line,) = [n for n in notices if "claude-opus-4-1" in n]
        self.assertIn("claude-opus-4-1, with its effort low, is not among", line)
        self.assertIn(f"running {self.inst.engine.label}'s {self.inst.engine_model} at {self.inst.effort}", line)
        self.assertFalse([n for n in notices if "effort low is not one" in n])    # one notice for the pair

    def test_a_live_pick_and_no_pick_say_nothing_about_the_model(self):
        for spelling in ("claude-opus-5", None):
            with self.subTest(model=spelling):
                self.inst = self._with_model(spelling)
                self.assertIsNone(stale_model_notice(self.inst, self.inst.instance))


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
