"""Tests for run.py — CLI parsing of --dry-run and the launch() orchestrator's
dry-run short-circuit.

The orchestrator tests mock every individual stage (select_pick, resolve_target,
compose_runtime, setup_state, etc.) so we can verify that the only behavior
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


class TestParseCliDryRunFlag(unittest.TestCase):
    """parse_cli's third return value is the --dry-run boolean."""

    def test_default_false(self):
        with patch.object(sys, "argv", ["run.py"]):
            _, _, dry_run = run.parse_cli()
        self.assertFalse(dry_run)

    def test_flag_sets_true(self):
        with patch.object(sys, "argv", ["run.py", "--dry-run"]):
            _, _, dry_run = run.parse_cli()
        self.assertTrue(dry_run)

    def test_flag_with_unknown_target(self):
        # Unknown target → picked=None, target string flows into claude_args.
        # dry-run still parsed correctly.
        with patch.object(sys, "argv", ["run.py", "bogus_agent_name", "--dry-run"]):
            picked, claude_args, dry_run = run.parse_cli()
        self.assertIsNone(picked)
        self.assertIn("bogus_agent_name", claude_args)
        self.assertTrue(dry_run)

    def test_dry_run_before_target(self):
        # Order shouldn't matter (argparse handles position-independence for
        # the optional flag).
        with patch.object(sys, "argv", ["run.py", "--dry-run", "bogus"]):
            _, _, dry_run = run.parse_cli()
        self.assertTrue(dry_run)

    def test_flag_doesnt_leak_into_claude_args(self):
        # --dry-run is OUR flag, not claude's — must not appear in the passthrough.
        with patch.object(sys, "argv", ["run.py", "--dry-run"]):
            _, claude_args, _ = run.parse_cli()
        self.assertNotIn("--dry-run", claude_args)

    def test_unknown_flags_still_passthrough(self):
        # Unknown flags (claude's) get passed through as claude_args.
        with patch.object(sys, "argv", ["run.py", "--print"]):
            _, claude_args, _ = run.parse_cli()
        self.assertIn("--print", claude_args)


class TestLaunchOrchestrator(unittest.TestCase):
    """launch() drives the pipeline of stages. We mock each stage so the test
    is purely about the flow shape — specifically whether the final
    run_compose runs based on --dry-run."""

    def _mock_pipeline_through_to_run_compose(self, *, dry_run):
        """Patch every stage launch() calls. Returns a dict of the active mocks
        so individual tests can inspect call args."""
        inst_id = MagicMock(is_brand_new=False)
        sess_id = MagicMock()
        chain = ["base"]
        conf = MagicMock()
        cred_names = []

        mocks = {
            "select_pick":      patch.object(run, "select_pick", return_value=(MagicMock(), [], dry_run)),
            "require_docker":   patch.object(run, "require_docker"),
            "resolve_target":   patch.object(run, "resolve_target", return_value=inst_id),
            "compute_resume_flag": patch.object(run, "compute_resume_flag", return_value=[]),
            "update_workspace_map": patch.object(run, "update_workspace_map"),
            "compose_runtime":  patch.object(run, "compose_runtime", return_value=(sess_id, chain)),
            "setup_state":      patch.object(run, "setup_state", return_value=(conf, cred_names)),
            "print_launch_banner": patch.object(run, "print_launch_banner"),
            "run_compose":      patch.object(run, "run_compose"),
        }
        active = {name: p.start() for name, p in mocks.items()}
        for p in mocks.values():
            self.addCleanup(p.stop)
        # Suppress the dry-run notice print so test output stays clean.
        p_print = patch("builtins.print")
        p_print.start()
        self.addCleanup(p_print.stop)
        return active

    def test_run_compose_called_in_normal_launch(self):
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=False)
        run.launch()
        mocks["run_compose"].assert_called_once()

    def test_run_compose_skipped_in_dry_run(self):
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["run_compose"].assert_not_called()

    def test_setup_state_runs_in_dry_run(self):
        # Dry-run still runs all the state-setup stages — that's the whole
        # point (it's the pipeline-up-to-the-final-step that we want to
        # exercise in tests).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["setup_state"].assert_called_once()
        mocks["compose_runtime"].assert_called_once()
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

    def test_require_docker_skipped_in_dry_run(self):
        # Dry-run shouldn't require docker — that's why we moved the check
        # out of module-import scope. Lets a CI environment without docker
        # still validate state-setup correctness.
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=True)
        run.launch()
        mocks["require_docker"].assert_not_called()


if __name__ == "__main__":
    unittest.main()
