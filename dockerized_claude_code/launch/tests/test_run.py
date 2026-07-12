"""Tests for run.py — CLI parsing of --dry-run and the launch() orchestrator's
dry-run short-circuit.

The orchestrator tests mock every individual stage (gather_input, resolve_target,
compose_chain, setup_state, etc.) so we can verify that the only behavior
that differs between a normal launch and a dry-run launch is whether
run_compose gets invoked at the end."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# /workspace is the project root; importing `run` requires it on sys.path.
# The same is required by `python -m unittest discover -s launch/tests`
# (which sets the top-level dir to /workspace), so this is a no-op in normal
# test runs but lets the file also be imported directly.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import run  # noqa: E402  — must come after the sys.path.insert above
from launch.structs import InstanceIdentity  # noqa: E402  — same reason


class TestParseCliFlags(unittest.TestCase):
    """parse_cli returns a LaunchOptions NamedTuple — (picked, claude_args,
    dry_run, refresh_installs); tuple-unpacking keeps working. Both boolean
    flags default False; each is exposed as its own CLI arg."""

    def test_default_false(self):
        with patch.object(sys, "argv", ["run.py"]):
            _, _, dry_run, refresh = run.parse_cli()
        self.assertFalse(dry_run)
        self.assertFalse(refresh)

    def test_flag_sets_true(self):
        with patch.object(sys, "argv", ["run.py", "--dry-run"]):
            _, _, dry_run, refresh = run.parse_cli()
        self.assertTrue(dry_run)
        self.assertFalse(refresh)

    def test_refresh_installs_sets_true(self):
        with patch.object(sys, "argv", ["run.py", "--refresh-installs"]):
            _, _, dry_run, refresh = run.parse_cli()
        self.assertFalse(dry_run)
        self.assertTrue(refresh)

    def test_both_flags_independent(self):
        with patch.object(sys, "argv", ["run.py", "--dry-run", "--refresh-installs"]):
            _, _, dry_run, refresh = run.parse_cli()
        self.assertTrue(dry_run)
        self.assertTrue(refresh)

    def test_flag_with_unknown_target(self):
        # Unknown target → picked=None, target string flows into claude_args.
        with patch.object(sys, "argv", ["run.py", "bogus_agent_name", "--dry-run"]):
            picked, claude_args, dry_run, _ = run.parse_cli()
        self.assertIsNone(picked)
        self.assertIn("bogus_agent_name", claude_args)
        self.assertTrue(dry_run)

    def test_dry_run_before_target(self):
        # Order shouldn't matter (argparse handles position-independence).
        with patch.object(sys, "argv", ["run.py", "--dry-run", "bogus"]):
            _, _, dry_run, _ = run.parse_cli()
        self.assertTrue(dry_run)

    def test_flag_doesnt_leak_into_claude_args(self):
        # Both flags are OURS; must not appear in the passthrough.
        with patch.object(sys, "argv", ["run.py", "--dry-run", "--refresh-installs"]):
            _, claude_args, _, _ = run.parse_cli()
        self.assertNotIn("--dry-run", claude_args)
        self.assertNotIn("--refresh-installs", claude_args)

    def test_unknown_flags_still_passthrough(self):
        # Unknown flags (claude's) get passed through as claude_args.
        with patch.object(sys, "argv", ["run.py", "--print"]):
            _, claude_args, _, _ = run.parse_cli()
        self.assertIn("--print", claude_args)

    def test_known_target_resolves_and_name_doesnt_leak(self):
        # Known agent name → picked is set; the name doesn't reach claude_args
        # (otherwise claude would receive it as a positional duplicate). "golem"
        # exists in agents/ so resolve_pick finds it via agent_md_index().
        with patch.object(sys, "argv", ["run.py", "golem"]):
            picked, claude_args, _, _ = run.parse_cli()
        self.assertIsNotNone(picked)
        self.assertNotIn("golem", claude_args)

    def test_known_target_combines_with_dry_run(self):
        # Known target + --dry-run — both register; target doesn't leak.
        with patch.object(sys, "argv", ["run.py", "golem", "--dry-run"]):
            picked, claude_args, dry_run, _ = run.parse_cli()
        self.assertIsNotNone(picked)
        self.assertTrue(dry_run)
        self.assertNotIn("golem", claude_args)

    def test_dash_dash_routes_known_flag_to_claude(self):
        # `python3 run.py golem -- --dry-run` — `--` ends argparse's optional
        # parsing, so --dry-run goes through to claude rather than firing
        # our flag. Important because some claude flags share names with ours.
        with patch.object(sys, "argv", ["run.py", "golem", "--", "--dry-run"]):
            _, claude_args, dry_run, _ = run.parse_cli()
        self.assertFalse(dry_run)
        self.assertIn("--dry-run", claude_args)

    def test_known_instance_resolves_as_instance_identity(self):
        # A name like 'golem__<session>' with a state dir on disk → InstanceIdentity
        # (not AgentIdentity). resolve_pick checks the state dir + workspace map +
        # modes map; we patch those file_access points so the test stays hermetic
        # (no writes to ~/.claude-agents/). The agent itself ("golem") must be a
        # real one — resolve_pick's instance branch still requires <agent>.md to
        # exist in agents/ via agent_md_index().
        instance_name = "golem__test_fixture"
        with patch.object(sys, "argv", ["run.py", instance_name]), \
             patch("launch.agents_crud.is_dir", return_value=True), \
             patch("launch.agents_crud.load_workspace_map", return_value={instance_name: "/tmp/ws"}), \
             patch("launch.agents_crud.load_modes_map", return_value={}):
            picked, claude_args, _, _ = run.parse_cli()
        self.assertIsInstance(picked, InstanceIdentity)
        self.assertEqual(picked.agent, "golem")
        self.assertEqual(picked.session, "test_fixture")
        self.assertEqual(picked.workspace, "/tmp/ws")
        self.assertFalse(picked.is_brand_new)
        self.assertNotIn(instance_name, claude_args)

    def test_no_target_doesnt_crash_resolve_pick(self):
        # Bare `run.py` → args.target is None; resolve_pick must accept it and
        # return None without exploding. Regression guard for the None-safe contract.
        with patch.object(sys, "argv", ["run.py"]):
            picked, claude_args, _, _ = run.parse_cli()
        self.assertIsNone(picked)
        self.assertEqual(claude_args, [])


class TestLaunchOrchestrator(unittest.TestCase):
    """launch() drives the pipeline of stages. We mock each stage so the test
    is purely about the flow shape. The dry-run gate now lives inside
    docker_compose_subprocess (set via docker_config.set_dry_run); from
    launch()'s perspective every stage fires in both modes — including
    ensure_image and run_compose. (require_docker fires inside gather_input —
    after CLI parsing, before the picker — so it's covered by TestGatherInput
    below, not by these orchestrator-level mocks.)"""

    def _mock_pipeline_through_to_run_compose(self, *, dry_run):
        """Patch every stage launch() calls. Returns a dict of the active mocks
        so individual tests can inspect call args."""
        inst_id = MagicMock(is_brand_new=False)
        chain = ["base"]
        conf = MagicMock()
        cred_names = []

        mocks = {
            "gather_input":     patch.object(run, "gather_input", return_value=run.LaunchOptions(MagicMock(), [], dry_run, False)),
            "set_dry_run":      patch.object(run, "set_dry_run"),
            "resolve_target":   patch.object(run, "resolve_target", return_value=inst_id),
            "compute_resume_flag": patch.object(run, "compute_resume_flag", return_value=[]),
            "update_workspace_map": patch.object(run, "update_workspace_map"),
            "set_instance_modes": patch.object(run, "set_instance_modes"),
            "compose_chain":    patch.object(run, "compose_chain", return_value=chain),
            "setup_state":      patch.object(run, "setup_state", return_value=(conf, cred_names)),
            "print_launch_banner": patch.object(run, "print_launch_banner"),
            "ensure_image":     patch.object(run, "ensure_image"),
            "prompt_install_failures": patch.object(run, "prompt_install_failures", return_value=None),
            "run_compose":      patch.object(run, "run_compose"),
        }
        active = {name: p.start() for name, p in mocks.items()}
        for p in mocks.values():
            self.addCleanup(p.stop)
        # Suppress any incidental prints so test output stays clean.
        p_print = patch("builtins.print")
        p_print.start()
        self.addCleanup(p_print.stop)
        return active

    def test_run_compose_called_in_normal_launch(self):
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=False)
        run.launch()
        mocks["run_compose"].assert_called_once()

    def test_run_compose_called_in_dry_run(self):
        # run_compose now fires in both modes — its orchestration (mount
        # flattening, env staging, firewall coordination) gets exercised on
        # dry-run too. The actual docker compose invocation is gated inside
        # docker_compose_subprocess by the dry-run flag.
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["run_compose"].assert_called_once()

    def test_setup_state_runs_in_dry_run(self):
        # Dry-run still runs all the state-setup stages — that's the whole
        # point (it's the pipeline-up-to-the-final-step that we want to
        # exercise in tests).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["setup_state"].assert_called_once()
        mocks["compose_chain"].assert_called_once()
        mocks["update_workspace_map"].assert_called_once()

    def test_banner_prints_in_dry_run(self):
        # The launch banner is part of the "what would happen" output — show it
        # even on dry-run so the user can see the resolved state.
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["print_launch_banner"].assert_called_once()

    def test_set_dry_run_called_with_flag_value(self):
        # The dry_run CLI flag propagates into docker_config via set_dry_run
        # immediately after gather_input. docker_compose_subprocess then
        # gates its real subprocess.call on that module-level flag.
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["set_dry_run"].assert_called_once_with(True)

    def test_set_dry_run_called_false_on_normal_launch(self):
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=False)
        run.launch()
        mocks["set_dry_run"].assert_called_once_with(False)

    def test_ensure_image_and_install_failures_run_in_dry_run(self):
        # Both are still *called* in dry-run — ensure_image's docker compose
        # invocations no-op inside docker_compose_subprocess, and
        # prompt_install_failures gates itself internally (it skips the image
        # read entirely on dry-run: nothing was built, so any log it found
        # would be stale — see test_docker_config for that gate).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["ensure_image"].assert_called_once()
        mocks["prompt_install_failures"].assert_called_once()


class TestGatherInput(unittest.TestCase):
    """gather_input owns the docker gate: it must fire after parse_cli (so
    `--help` still works on a docker-less machine) but before select_agent
    (so nobody answers the picker + prompts for a launch that was never going
    to happen — the pre-fix behavior)."""

    def test_docker_gate_precedes_picker(self):
        calls = []
        picked = MagicMock()
        with patch.object(run, "parse_cli", return_value=run.LaunchOptions(None, [], False, False)), \
             patch.object(run, "require_docker", side_effect=lambda: calls.append("require_docker")), \
             patch.object(run, "select_agent", side_effect=lambda: calls.append("select_agent") or picked):
            result = run.gather_input()
        self.assertEqual(calls, ["require_docker", "select_agent"])
        self.assertIs(result.picked, picked)

    def test_docker_gate_fires_even_with_direct_target(self):
        # A CLI-named target skips the picker but not the docker gate.
        target = MagicMock()
        with patch.object(run, "parse_cli", return_value=run.LaunchOptions(target, ["--verbose"], True, False)), \
             patch.object(run, "require_docker") as mock_docker, \
             patch.object(run, "select_agent") as mock_picker:
            opts = run.gather_input()
        mock_docker.assert_called_once()
        mock_picker.assert_not_called()
        self.assertIs(opts.picked, target)
        self.assertEqual(opts.claude_args, ["--verbose"])
        self.assertTrue(opts.dry_run)

    def test_missing_docker_stops_before_picker(self):
        # require_docker exits via exit_if_missing — the picker must never
        # open after that.
        with patch.object(run, "parse_cli", return_value=run.LaunchOptions(None, [], False, False)), \
             patch.object(run, "require_docker", side_effect=SystemExit("docker is required")), \
             patch.object(run, "select_agent") as mock_picker:
            with self.assertRaises(SystemExit):
                run.gather_input()
        mock_picker.assert_not_called()

    def test_picker_cancel_exits_zero(self):
        with patch.object(run, "parse_cli", return_value=run.LaunchOptions(None, [], False, False)), \
             patch.object(run, "require_docker"), \
             patch.object(run, "select_agent", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                run.gather_input()
        self.assertEqual(ctx.exception.code, 0)


class TestResolveTarget(unittest.TestCase):
    """resolve_target re-prompts for cont identities whose stored workspace is
    missing — which means None (no map entry) AND "" (empty map entry). The
    two used to diverge: "" slipped past the `is None` check and silently
    fell back to DEFAULT_WORKSPACE downstream instead of re-prompting."""

    def _cont(self, workspace):
        return InstanceIdentity(agent="golem", session="s", workspace=workspace,
                                is_brand_new=False, modes=())

    def test_none_workspace_reprompts(self):
        with patch.object(run, "ask_for_workspace", return_value="/tmp") as mock_ask:
            out = run.resolve_target(self._cont(None))
        mock_ask.assert_called_once()
        self.assertEqual(out.workspace, "/tmp")
        self.assertFalse(out.is_brand_new)   # dataclasses.replace must not disturb the cont flag

    def test_empty_string_workspace_reprompts(self):
        with patch.object(run, "ask_for_workspace", return_value="/tmp") as mock_ask:
            out = run.resolve_target(self._cont(""))
        mock_ask.assert_called_once()
        self.assertEqual(out.workspace, "/tmp")

    def test_valid_workspace_passes_through_unprompted(self):
        with tempfile.TemporaryDirectory() as real_dir, \
             patch.object(run, "ask_for_workspace") as mock_ask:
            out = run.resolve_target(self._cont(real_dir))
        mock_ask.assert_not_called()
        self.assertEqual(out.workspace, real_dir)

    def test_invalid_workspace_exits(self):
        # Set-but-bogus path is a stale map entry — validate_workspace exits
        # with the fix-the-map message rather than mounting garbage.
        with self.assertRaises(SystemExit):
            run.resolve_target(self._cont("/no/such/dir/for/sure"))


if __name__ == "__main__":
    unittest.main()
