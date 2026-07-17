"""Tests for launch.docker_config — image naming, the tag.docker flag
emitters (build args / env forwards / entrypoint), the plain-docker build
loop, and set_container_mounts (workspace fallback).

Env-formatter tests (install_creds_flags, token_env_dict, etc.) live in
test_container_env.py alongside the accumulator they feed."""

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from launch import docker_config, paths
from launch.container_env import ContainerEnvKey, _container_env, stage_container_env
from launch.docker_config import (
    build_arg_flags, effort_args, entrypoint_flags, env_forward_flags,
    image_tag, set_container_mounts,
)
from launch.tags import DockerContribution


def _run_inst(**over):
    """Duck-typed Instance for run_container tests — only the attrs it reads."""
    defaults = dict(docker_contributions=[], conf={}, claude_args=[], instance="poet__x")
    defaults.update(over)
    return SimpleNamespace(**defaults)


class TestRunContainerCriticalDnsFailure(unittest.TestCase):
    """run_container aborts with the codebase's clean one-liner (sys.exit)
    when the {firewall} phase-1 critical-DNS resolve terminally failed — the
    worker's RuntimeError must not escape as a raw traceback, and nothing
    docker-touching may run after the failure."""

    def test_runtime_error_exits_cleanly_without_docker_run(self):
        boom = RuntimeError(
            "Critical Anthropic domains failed to resolve: ['api.anthropic.com']. "
            "Claude Code cannot operate without them; aborting launch.")
        with patch.object(docker_config, "stage_container_env"), \
             patch.object(docker_config, "set_terminal_title"), \
             patch.object(docker_config, "is_critical_pending", return_value=False), \
             patch.object(docker_config, "wait_for_critical_addresses", side_effect=boom), \
             patch.object(docker_config, "start_firewall_updater") as updater, \
             patch.object(docker_config, "docker_subprocess") as run:
            with self.assertRaises(SystemExit) as ctx:
                docker_config.run_container(_run_inst(), "claude-agents:base", [], [])
        self.assertIn("Critical Anthropic domains", str(ctx.exception))
        updater.assert_not_called()
        run.assert_not_called()


class TestEffortArgs(unittest.TestCase):
    """effort_args — the explicit --effort CLI flag derived from the conf's
    CLAUDE_CODE_EFFORT_LEVEL. Pure function: (conf, claude_args) → arg list."""

    def test_conf_effort_becomes_flag_pair(self):
        self.assertEqual(effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, []),
                         ["--effort", "max"])

    def test_conf_without_effort_yields_nothing(self):
        self.assertEqual(effort_args({"ANTHROPIC_MODEL": "claude-fable-5"}, []), [])

    def test_valueless_conf_key_yields_nothing(self):
        # dotenv parses a bare `CLAUDE_CODE_EFFORT_LEVEL` line to None — no
        # flag should be emitted for it.
        self.assertEqual(effort_args({"CLAUDE_CODE_EFFORT_LEVEL": None}, []), [])

    def test_user_passed_effort_wins(self):
        # `python3 run.py poet -- --effort low` must reach claude unchallenged —
        # emitting ours too would either conflict or silently override the user.
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, ["--effort", "low"]), [])

    def test_user_passed_effort_equals_form_wins(self):
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, ["--effort=low"]), [])

    def test_unrelated_claude_args_dont_suppress(self):
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "medium"}, ["--print", "hi"]),
            ["--effort", "medium"])


class TestImageTag(unittest.TestCase):
    def test_no_layers_is_base(self):
        self.assertEqual(image_tag([]), "claude-agents:base")

    def test_single_layer(self):
        self.assertEqual(image_tag(["code"]), "claude-agents:code")

    def test_layers_joined_with_dot(self):
        self.assertEqual(image_tag(["code", "web", "dood"]), "claude-agents:code.web.dood")


class ContainerEnvFixture(unittest.TestCase):
    """Snapshot + clear the container-env accumulator around each test."""

    def setUp(self):
        self._snapshot = dict(_container_env)
        _container_env.clear()

    def tearDown(self):
        _container_env.clear()
        _container_env.update(self._snapshot)


class TestBuildArgFlags(ContainerEnvFixture):
    """build_arg_flags resolves a layer's `[build] arg_forward` names against
    the staged env — plain names pull their value, globs expand, unstaged
    names drop silently (the Dockerfile's ARG default then applies)."""

    def test_plain_name_emits_flag_pair(self):
        stage_container_env(ContainerEnvKey.DOCKER_GID, "988")
        self.assertEqual(build_arg_flags(("DOCKER_GID",)),
                         ["--build-arg", "DOCKER_GID=988"])

    def test_unstaged_name_skipped(self):
        self.assertEqual(build_arg_flags(("DOCKER_GID",)), [])

    def test_glob_expands_against_staged_keys(self):
        _container_env.update({"INSTALL_GH": "1", "INSTALL_AWS": "0", "OTHER": "x"})
        flags = build_arg_flags(("INSTALL_*",))
        self.assertEqual(flags, ["--build-arg", "INSTALL_AWS=0", "--build-arg", "INSTALL_GH=1"])

    def test_glob_with_no_matches_is_empty(self):
        self.assertEqual(build_arg_flags(("INSTALL_*",)), [])


class TestEnvForwardFlags(ContainerEnvFixture):
    """env_forward_flags emits `-e NAME=VALUE` for the active tags'
    `[run] env_forward` names — the gating that keeps WHITELIST_ADDRESSES
    scoped to launches where the resolve actually ran."""

    def _fw(self):
        return DockerContribution(env_forward=("WHITELIST_ADDRESSES",))

    def test_staged_name_emitted(self):
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4:443")
        self.assertEqual(env_forward_flags([self._fw()]),
                         ["-e", "WHITELIST_ADDRESSES=1.2.3.4:443"])

    def test_unstaged_name_skipped(self):
        self.assertEqual(env_forward_flags([self._fw()]), [])

    def test_no_contributions_no_flags(self):
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4:443")
        self.assertEqual(env_forward_flags([]), [])


class TestEntrypointFlags(unittest.TestCase):
    def test_no_override_uses_image_entrypoint(self):
        self.assertEqual(entrypoint_flags([DockerContribution()]), [])

    def test_bare_name_resolves_to_local_bin(self):
        flags = entrypoint_flags([DockerContribution(entrypoint="firewall-entrypoint.sh")])
        self.assertEqual(flags, ["--entrypoint",
                                 f"{paths.LOCAL_BIN_IN_CONTAINER}/firewall-entrypoint.sh"])

    def test_path_used_as_is(self):
        flags = entrypoint_flags([DockerContribution(entrypoint="/opt/custom/entry.sh")])
        self.assertEqual(flags, ["--entrypoint", "/opt/custom/entry.sh"])

    def test_two_overrides_conflict_loudly(self):
        with self.assertRaises(RuntimeError):
            entrypoint_flags([DockerContribution(entrypoint="a.sh"),
                              DockerContribution(entrypoint="b.sh")])


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
        inst_id = SimpleNamespace(workspace="/some/host/path", state_dir=Path("/tmp/state"), workspace_readonly=False)
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], "/some/host/path")

    def test_workspace_none_falls_back_to_default(self):
        inst_id = SimpleNamespace(workspace=None, state_dir=Path("/tmp/state"), workspace_readonly=False)
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_workspace_empty_string_falls_back_to_default(self):
        # `or` covers None AND empty string — both treated as "no workspace".
        inst_id = SimpleNamespace(workspace="", state_dir=Path("/tmp/state"), workspace_readonly=False)
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_generated_settings_mounted_read_only(self):
        # The launcher-generated settings file shadows the state-dir's rw
        # view of ~/.claude/settings.json — the leash the agent can't undo.
        inst_id = SimpleNamespace(workspace="/w", state_dir=Path("/tmp/state"), workspace_readonly=False)
        mounts = self._capture_mounts(inst_id)
        settings_pair = next(p for p in mounts if "settings.json" in p[1])
        self.assertEqual(settings_pair[0], "/tmp/state/settings.json")
        self.assertTrue(settings_pair[1].endswith(":ro"))

    def test_readonly_specialty_mounts_workspace_ro(self):
        # A workspace_readonly specialty ({ro}) makes the /workspace mount
        # :ro; the state dir stays writable (Claude Code writes history there).
        inst_id = SimpleNamespace(workspace="/w", state_dir=Path("/tmp/state"), workspace_readonly=True)
        mounts = self._capture_mounts(inst_id)
        ws = next(p for p in mounts if p[1].startswith("/workspace"))
        self.assertEqual(ws, ("/w", "/workspace:ro"))
        state = next(p for p in mounts if p[1] == "/home/claude/.claude")  # CLAUDE_CONFIG_IN_CONTAINER
        self.assertFalse(state[1].endswith(":ro"))

    def test_workspace_read_write_by_default(self):
        inst_id = SimpleNamespace(workspace="/w", state_dir=Path("/tmp/state"), workspace_readonly=False)
        mounts = self._capture_mounts(inst_id)
        self.assertIn(("/w", "/workspace"), mounts)


class TestAddDockerMountCollisions(unittest.TestCase):
    """add_docker_mount rejects conflicting duplicates at staging time. Two
    `-v` flags for one target make docker error out at run time with a
    message that names neither culprit; the source-keyed accumulator would
    silently *drop* a mount on same-source/new-target. Both now fail fast
    with a message naming the paths. Identical re-stages stay no-ops."""

    def setUp(self):
        docker_config._docker_mounts.clear()

    def tearDown(self):
        docker_config._docker_mounts.clear()

    def test_identical_restage_is_idempotent(self):
        docker_config.add_docker_mount("/src", "/tgt")
        docker_config.add_docker_mount("/src", "/tgt")
        self.assertEqual(docker_config._docker_mounts, {"/src": "/tgt"})

    def test_same_target_from_different_source_raises(self):
        docker_config.add_docker_mount("/src1", "/tgt")
        with self.assertRaises(RuntimeError) as ctx:
            docker_config.add_docker_mount("/src2", "/tgt")
        self.assertIn("/tgt", str(ctx.exception))
        self.assertIn("/src2", str(ctx.exception))

    def test_target_collision_ignores_access_mode_suffix(self):
        # `/x:ro` and `/x` are the same container path — still a collision.
        docker_config.add_docker_mount("/src1", "/tgt:ro")
        with self.assertRaises(RuntimeError):
            docker_config.add_docker_mount("/src2", "/tgt")

    def test_same_source_at_new_target_raises(self):
        # The dict is keyed by source — a second target for the same source
        # used to silently REPLACE the first mount. Now it's an error.
        docker_config.add_docker_mount("/src", "/tgt1")
        with self.assertRaises(RuntimeError):
            docker_config.add_docker_mount("/src", "/tgt2")
        self.assertEqual(docker_config._docker_mounts, {"/src": "/tgt1"})   # original intact

    def test_distinct_mounts_accumulate(self):
        docker_config.add_docker_mount("/a", "/x")
        docker_config.add_docker_mount("/b", "/y:ro")
        self.assertEqual(len(docker_config._docker_mounts), 2)

    def test_nested_targets_are_not_collisions(self):
        # /home/claude/.config and /home/claude/.config/.jira are different
        # mount points (docker nests them) — only exact-path matches collide.
        docker_config.add_docker_mount("/a", "/home/claude/.config")
        docker_config.add_docker_mount("/b", "/home/claude/.config/.jira")
        self.assertEqual(len(docker_config._docker_mounts), 2)


class TestPromptInstallFailuresDryRun(unittest.TestCase):
    """--dry-run builds nothing, so prompt_install_failures must not spin up
    a container: the only log it could read is a stale one from a previous
    real build, and `docker run` is a real side effect dry-run promises not
    to have. (Pre-fix, dry-run ran the read for real.)"""

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_dry_run_skips_the_docker_read(self):
        docker_config.set_dry_run(True)
        with patch("launch.docker_config.shell_capture") as mock_capture:
            docker_config.prompt_install_failures("claude-agents:code", "poet__x")
        mock_capture.assert_not_called()

    def test_real_run_reads_the_image_log(self):
        completed = SimpleNamespace(returncode=1, stdout="")   # rc!=0 → no log in image → silent return
        with patch("launch.docker_config.shell_capture", return_value=completed) as mock_capture:
            docker_config.prompt_install_failures("claude-agents:code", "poet__x")
        mock_capture.assert_called_once()


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


class TestDockerSubprocessDryRun(unittest.TestCase):
    """docker_subprocess gates its shell_returncode call on the module-level
    _dry_run flag. Real-run forwards to shell_returncode with the `docker`
    prefix; dry-run prints the would-be invocation and returns without
    touching subprocess."""

    def setUp(self):
        docker_config.set_dry_run(False)

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_dry_run_skips_shell_returncode(self):
        docker_config.set_dry_run(True)
        with patch("launch.docker_config.shell_returncode") as mock_run, \
             patch("builtins.print"):
            docker_config.docker_subprocess(["build", "--no-cache"])
        mock_run.assert_not_called()

    def test_real_run_invokes_shell_returncode_with_docker_prefix(self):
        with patch("launch.docker_config.shell_returncode", return_value=0) as mock_run:
            docker_config.docker_subprocess(["build"])
        mock_run.assert_called_once()
        positional = mock_run.call_args.args
        self.assertEqual(positional, ("docker", "build"))

    def test_dry_run_prints_would_invoke_line(self):
        docker_config.set_dry_run(True)
        with patch("builtins.print") as mock_print, \
             patch("launch.docker_config.shell_returncode"):
            docker_config.docker_subprocess(["run", "--rm", "img"])
        mock_print.assert_called_once()
        printed = mock_print.call_args.args[0]
        self.assertIn("dry-run", printed)
        self.assertIn("docker run --rm img", printed)


class TestEnsureImage(unittest.TestCase):
    """ensure_image drives one `docker build` per step (base + each
    build_steps entry), threading PARENT_IMAGE explicitly and returning the
    final tag. docker_subprocess no-ops internally on dry-run, so the loop
    runs identically in both modes."""

    def _inst(self, steps):
        from pathlib import Path as P
        return SimpleNamespace(build_steps=[
            (name, P(f"/fake/{name}/Dockerfile"), contribution)
            for name, contribution in steps
        ])

    def test_builds_base_plus_each_step(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            tag = docker_config.ensure_image(self._inst([("code", None), ("dood", None)]))
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(tag, "claude-agents:code.dood")

    def test_bare_agent_builds_base_only(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            tag = docker_config.ensure_image(self._inst([]))
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(tag, "claude-agents:base")

    def test_parent_image_threads_between_steps(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            docker_config.ensure_image(self._inst([("code", None), ("dood", None)]))
        args_code = mock_run.call_args_list[1].args[0]
        args_dood = mock_run.call_args_list[2].args[0]
        self.assertIn("PARENT_IMAGE=claude-agents:base", args_code)
        self.assertIn("PARENT_IMAGE=claude-agents:code", args_dood)

    def test_step_forwards_its_own_build_args(self):
        from launch.container_env import _container_env
        snapshot = dict(_container_env)
        _container_env.clear()
        _container_env["DOCKER_GID"] = "988"
        try:
            contribution = DockerContribution(build_arg_forward=("DOCKER_GID",))
            with patch("launch.docker_config.docker_subprocess") as mock_run, \
                 patch("builtins.print"):
                docker_config.ensure_image(self._inst([("code", None), ("dood", contribution)]))
            args_code = mock_run.call_args_list[1].args[0]
            args_dood = mock_run.call_args_list[2].args[0]
            self.assertNotIn("DOCKER_GID=988", args_code)   # code doesn't forward it
            self.assertIn("DOCKER_GID=988", args_dood)      # dood's tag.docker does
        finally:
            _container_env.clear()
            _container_env.update(snapshot)


if __name__ == "__main__":
    unittest.main()


class TestWaitForFirewallApplied(unittest.TestCase):
    """The phase-2 updater's gate: don't insert rules until init-firewall.sh
    finished (its completion marker exists); bail when the container died
    without it; proceed best-effort on a timed-out-but-alive container."""

    def _wait(self, exec_returncodes, running, timeout=5):
        codes = iter(exec_returncodes)
        with patch.object(docker_config, "docker_exec_root_subprocess",
                          side_effect=lambda *a: SimpleNamespace(returncode=next(codes))) as ex, \
             patch.object(docker_config, "docker_check_running_subprocess", side_effect=running), \
             patch.object(docker_config.time, "sleep"):
            result = docker_config.wait_for_firewall_applied("claude-code_test", timeout_seconds=timeout)
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
