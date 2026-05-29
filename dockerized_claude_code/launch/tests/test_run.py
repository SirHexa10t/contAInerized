"""Tests for run.py — CLI parsing of --dry-run and the launch() orchestrator's
dry-run short-circuit.

The orchestrator tests mock every individual stage (gather_input, resolve_target,
compose_chain, setup_state, etc.) so we can verify that the only behavior
that differs between a normal launch and a dry-run launch is whether
run_compose gets invoked at the end."""

import sys
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
    """parse_cli returns (picked, claude_args, dry_run, refresh_installs).
    Both boolean flags default False; each is exposed as its own CLI arg."""

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
        # exists in agents/ so resolve_pick finds it via AGENT_MD_BY_NAME.
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
        # exist in agents/ via AGENT_MD_BY_NAME.
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
    require_docker, ensure_image, and run_compose."""

    def _mock_pipeline_through_to_run_compose(self, *, dry_run):
        """Patch every stage launch() calls. Returns a dict of the active mocks
        so individual tests can inspect call args."""
        inst_id = MagicMock(is_brand_new=False)
        chain = ["base"]
        conf = MagicMock()
        cred_names = []

        mocks = {
            "gather_input":     patch.object(run, "gather_input", return_value=(MagicMock(), [], dry_run, False)),
            "set_dry_run":      patch.object(run, "set_dry_run"),
            "require_docker":   patch.object(run, "require_docker"),
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

    def test_require_docker_called_in_normal_launch(self):
        # Sanity: a normal launch still gates on docker being on PATH.
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=False)
        run.launch()
        mocks["require_docker"].assert_called_once()

    def test_require_docker_called_in_dry_run(self):
        # require_docker fires regardless of dry_run — dry-run is a projection
        # of "what would happen", so the docker-missing failure mode should
        # surface in dry-run too (not be hidden by a guard).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["require_docker"].assert_called_once()

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
        # Both fire in dry-run — ensure_image's docker compose calls no-op
        # inside docker_compose_subprocess; prompt_install_failures still
        # runs (the underlying shell_capture either reads a stale log or
        # silently fails when the image doesn't exist).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["ensure_image"].assert_called_once()
        mocks["prompt_install_failures"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
