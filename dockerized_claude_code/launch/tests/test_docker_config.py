"""Tests for launch.docker_config — chain naming + compose-file selection +
set_container_mounts (workspace fallback).

Env-formatter tests (install_creds_flags, token_env_dict, etc.) live in
test_compose_env.py since that's where the formatters were moved."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from launch import docker_config, paths
from launch.docker_config import chain_compose_files, chain_image_tag, set_container_mounts


class TestChainImageTag(unittest.TestCase):
    def test_base_only(self):
        self.assertEqual(chain_image_tag(["base"]), "claude-agents:base")

    def test_single_layer(self):
        self.assertEqual(chain_image_tag(["base", "code"]), "claude-agents:code")

    def test_two_layers_joined_with_dot(self):
        self.assertEqual(chain_image_tag(["base", "code", "auto"]), "claude-agents:code.auto")

    def test_dood_lowercased(self):
        # DooD's value is mixed-case; the tag form is lowercased to match
        # compose / Dockerfile filename conventions.
        self.assertEqual(
            chain_image_tag(["base", "code", "auto", "DooD"]),
            "claude-agents:code.auto.dood",
        )

    def test_just_dood_with_base(self):
        self.assertEqual(chain_image_tag(["base", "DooD"]), "claude-agents:dood")


class TestChainComposeFiles(unittest.TestCase):
    def test_base_only_uses_compose_yml(self):
        result = chain_compose_files(["base"])
        self.assertEqual(result, ["-f", str(paths.COMPOSE_FILE_PATH)])

    def test_appends_one_layer(self):
        result = chain_compose_files(["base", "code"])
        self.assertEqual(result, [
            "-f", str(paths.COMPOSE_FILE_PATH),
            "-f", str(paths.compose_layer_path("code")),
        ])

    def test_appends_multiple_layers_in_order(self):
        result = chain_compose_files(["base", "code", "auto", "DooD"])
        self.assertEqual(result, [
            "-f", str(paths.COMPOSE_FILE_PATH),
            "-f", str(paths.compose_layer_path("code")),
            "-f", str(paths.compose_layer_path("auto")),
            "-f", str(paths.compose_layer_path("DooD")),   # compose_layer_path lowercases internally
        ])

    def test_dood_layer_uses_lowercased_filename(self):
        # Spot-check: compose_layer_path("DooD") → compose.dood.yml, not compose.DooD.yml.
        result = chain_compose_files(["base", "DooD"])
        self.assertIn(str(paths.DOCKER_DIR / "compose.dood.yml"), result)


class TestSetContainerMountsWorkspaceFallback(unittest.TestCase):
    """Regression: set_container_mounts must never try to bind-mount a None
    workspace. If inst_id.workspace is None (stale workspace-map entry that
    slipped past resolve_target's re-prompt), fall back to DEFAULT_WORKSPACE."""

    def _capture_mounts(self, inst_id):
        """Drive set_container_mounts through a patched add_docker_mount that
        records every (source, target) pair. Returns the list of pairs in
        call order."""
        recorded = []
        with patch("launch.docker_config.add_docker_mount", side_effect=lambda s, t: recorded.append((str(s), str(t)))):
            set_container_mounts(inst_id)
        return recorded

    def test_workspace_set_uses_provided_path(self):
        inst_id = SimpleNamespace(workspace="/some/host/path", state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], "/some/host/path")

    def test_workspace_none_falls_back_to_default(self):
        inst_id = SimpleNamespace(workspace=None, state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_workspace_empty_string_falls_back_to_default(self):
        # `or` covers None AND empty string — both treated as "no workspace".
        inst_id = SimpleNamespace(workspace="", state_dir="/tmp/state")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))


class TestMountTargetIsStaged(unittest.TestCase):
    """`mount_target_is_staged` underpins the home-overlay clash check —
    any prior mount with the same target makes the helper return True so
    `home_overlay_mounts` can refuse to shadow it."""

    def setUp(self):
        docker_config._docker_mounts.clear()

    def tearDown(self):
        docker_config._docker_mounts.clear()

    def test_returns_false_when_no_mounts(self):
        self.assertFalse(docker_config.mount_target_is_staged("/home/claude/.gitconfig"))

    def test_returns_true_for_exact_target(self):
        docker_config.add_docker_mount("/host/.bashrc", "/home/claude/.bashrc")
        self.assertTrue(docker_config.mount_target_is_staged("/home/claude/.bashrc"))

    def test_returns_false_for_unrelated_target(self):
        docker_config.add_docker_mount("/host/.bashrc", "/home/claude/.bashrc")
        self.assertFalse(docker_config.mount_target_is_staged("/home/claude/.gitconfig"))

    def test_ignores_access_mode_suffix(self):
        # Targets staged with `:ro` etc. should still match by the bare path.
        docker_config.add_docker_mount("/host/whitelist.txt", "/etc/whitelist.txt:ro")
        self.assertTrue(docker_config.mount_target_is_staged("/etc/whitelist.txt"))


# ============================================================
# Dry-run gating — moved from launch() into docker_compose_subprocess
# ============================================================
# Before this change, --dry-run early-returned from launch() before
# ensure_image / run_compose ever ran, leaving most of the orchestration
# unexercised by tests. The flag now sits on the module and only gates
# the actual `docker compose` invocation inside docker_compose_subprocess.
# Every test in this section asserts a path that was previously skipped on
# dry-run and is now reachable.

class TestSetDryRun(unittest.TestCase):
    """set_dry_run is the single point of write for the module-level flag.
    The setter exists (rather than callers poking `docker_config._dry_run`
    directly) so the read site stays a module-private and any future
    auditing of who flips the flag has one entry point to instrument."""

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_sets_flag_true(self):
        docker_config.set_dry_run(True)
        self.assertTrue(docker_config._dry_run)

    def test_resets_flag_false(self):
        docker_config.set_dry_run(True)
        docker_config.set_dry_run(False)
        self.assertFalse(docker_config._dry_run)


class TestDockerComposeSubprocessDryRun(unittest.TestCase):
    """docker_compose_subprocess gates its shell_returncode call on the
    module-level _dry_run flag. Real-run forwards to shell_returncode with
    the docker-compose prefix + the staged env; dry-run prints the would-be
    invocation and returns without touching subprocess."""

    def setUp(self):
        docker_config.set_dry_run(False)

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_dry_run_skips_shell_returncode(self):
        docker_config.set_dry_run(True)
        with patch("launch.docker_config.shell_returncode") as mock_run, \
             patch("builtins.print"):
            docker_config.docker_compose_subprocess(["build", "--no-cache"])
        mock_run.assert_not_called()

    def test_real_run_invokes_shell_returncode_with_docker_compose_prefix(self):
        with patch("launch.docker_config.shell_returncode", return_value=0) as mock_run:
            docker_config.docker_compose_subprocess(["build"])
        mock_run.assert_called_once()
        # Positional args are ("docker", "compose", *args); env is a kwarg.
        positional = mock_run.call_args.args
        self.assertEqual(positional[:2], ("docker", "compose"))
        self.assertEqual(positional[2:], ("build",))

    def test_dry_run_prints_would_invoke_line(self):
        docker_config.set_dry_run(True)
        with patch("builtins.print") as mock_print, \
             patch("launch.docker_config.shell_returncode"):
            docker_config.docker_compose_subprocess(["-f", "x.yml", "build"])
        mock_print.assert_called_once()
        printed = mock_print.call_args.args[0]
        self.assertIn("dry-run", printed)
        self.assertIn("docker compose -f x.yml build", printed)


class TestEnsureImageRunsOnDryRun(unittest.TestCase):
    """Before the dry-run refactor, ensure_image was entirely skipped on
    dry-run — the per-step env staging (TARGET_IMAGE + PARENT_IMAGE) and
    the per-step build invocation were unreachable. With the gate moved
    into docker_compose_subprocess, ensure_image now runs its loop in
    both modes; each iteration stages the env and calls
    docker_compose_subprocess (which no-ops internally on dry-run)."""

    def setUp(self):
        docker_config.set_dry_run(True)

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_calls_compose_subprocess_once_per_chain_step(self):
        with patch("launch.docker_config.docker_compose_subprocess") as mock_compose, \
             patch("launch.docker_config.stage_compose_env"), \
             patch("builtins.print"):
            docker_config.ensure_image(["base", "code", "auto"])
        self.assertEqual(mock_compose.call_count, 3)

    def test_stages_target_image_for_each_step(self):
        # First call stages base, second stages code (with PARENT_IMAGE=base),
        # third stages auto (with PARENT_IMAGE=code). The env-staging is the
        # core observable side effect — previously hidden behind the dry-run
        # short-circuit.
        with patch("launch.docker_config.docker_compose_subprocess"), \
             patch("launch.docker_config.stage_compose_env") as mock_stage, \
             patch("builtins.print"):
            docker_config.ensure_image(["base", "code"])
        # TARGET_IMAGE staged twice (once per step); PARENT_IMAGE once (for the non-base step).
        staged_keys = [call.args[0].name for call in mock_stage.call_args_list]
        self.assertEqual(staged_keys.count("TARGET_IMAGE"), 2)
        self.assertEqual(staged_keys.count("PARENT_IMAGE"), 1)


if __name__ == "__main__":
    unittest.main()
