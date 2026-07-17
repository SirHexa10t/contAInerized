"""Tests for run.py — CLI parsing of --dry-run and the launch() orchestrator's
dry-run short-circuit.

The orchestrator tests mock every individual stage (gather_input, resolve_target,
compose_chain, setup_state, etc.) so we can verify that the only behavior
that differs between a normal launch and a dry-run launch is whether
run_compose gets invoked at the end. CLI-parsing tests resolve against the
real agents/ tree registry (scan_all) — the same taxonomy a launch uses."""

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
from launch.paths import AGENTS_DIR  # noqa: E402  — same reason
from launch.tags import Instance, scan_all  # noqa: E402  — same reason

REGISTRY = scan_all(AGENTS_DIR)


class TestParseCliFlags(unittest.TestCase):
    """parse_cli returns a LaunchOptions NamedTuple — (picked, claude_args,
    dry_run, refresh_installs); tuple-unpacking keeps working. Both boolean
    flags default False; each is exposed as its own CLI arg."""

    def _parse(self, argv):
        with patch.object(sys, "argv", argv):
            return run.parse_cli(REGISTRY)

    def test_default_false(self):
        _, _, dry_run, refresh = self._parse(["run.py"])
        self.assertFalse(dry_run)
        self.assertFalse(refresh)

    def test_flag_sets_true(self):
        _, _, dry_run, refresh = self._parse(["run.py", "--dry-run"])
        self.assertTrue(dry_run)
        self.assertFalse(refresh)

    def test_refresh_installs_sets_true(self):
        _, _, dry_run, refresh = self._parse(["run.py", "--refresh-installs"])
        self.assertFalse(dry_run)
        self.assertTrue(refresh)

    def test_both_flags_independent(self):
        _, _, dry_run, refresh = self._parse(["run.py", "--dry-run", "--refresh-installs"])
        self.assertTrue(dry_run)
        self.assertTrue(refresh)

    def test_flag_with_unknown_target(self):
        # Unknown target → picked=None, target string flows into claude_args.
        picked, claude_args, dry_run, _ = self._parse(["run.py", "bogus_agent_name", "--dry-run"])
        self.assertIsNone(picked)
        self.assertIn("bogus_agent_name", claude_args)
        self.assertTrue(dry_run)

    def test_dry_run_before_target(self):
        # Order shouldn't matter (argparse handles position-independence).
        _, _, dry_run, _ = self._parse(["run.py", "--dry-run", "bogus"])
        self.assertTrue(dry_run)

    def test_flag_doesnt_leak_into_claude_args(self):
        # Both flags are OURS; must not appear in the passthrough.
        _, claude_args, _, _ = self._parse(["run.py", "--dry-run", "--refresh-installs"])
        self.assertNotIn("--dry-run", claude_args)
        self.assertNotIn("--refresh-installs", claude_args)

    def test_unknown_flags_still_passthrough(self):
        # Unknown flags (claude's) get passed through as claude_args.
        _, claude_args, _, _ = self._parse(["run.py", "--print"])
        self.assertIn("--print", claude_args)

    def test_known_target_resolves_and_name_doesnt_leak(self):
        # Known agent name → picked is set; the name doesn't reach claude_args
        # (otherwise claude would receive it as a positional duplicate). "golem"
        # exists in agents/ so resolve_pick finds it via agent_md_index().
        picked, claude_args, _, _ = self._parse(["run.py", "golem"])
        self.assertIsNotNone(picked)
        self.assertNotIn("golem", claude_args)

    def test_known_target_combines_with_dry_run(self):
        # Known target + --dry-run — both register; target doesn't leak.
        picked, claude_args, dry_run, _ = self._parse(["run.py", "golem", "--dry-run"])
        self.assertIsNotNone(picked)
        self.assertTrue(dry_run)
        self.assertNotIn("golem", claude_args)

    def test_dash_dash_routes_known_flag_to_claude(self):
        # `python3 run.py golem -- --dry-run` — `--` ends argparse's optional
        # parsing, so --dry-run goes through to claude rather than firing
        # our flag. Important because some claude flags share names with ours.
        _, claude_args, dry_run, _ = self._parse(["run.py", "golem", "--", "--dry-run"])
        self.assertFalse(dry_run)
        self.assertIn("--dry-run", claude_args)

    def test_known_instance_resolves_as_instance(self):
        # A name like 'golem__<session>' with a state dir on disk → Instance
        # (not Agent). resolve_pick checks the state dir + the instances.json
        # store; we patch those points so the test stays hermetic (no writes
        # to ~/.claude-agents/). The agent itself ("golem") must be a real one
        # — the instance branch still requires <agent>.md via agent_md_index().
        instance_name = "golem__test_fixture"
        entry = {"workspace": "/tmp/ws", "engine": None,
                 "professions": [], "specialties": [], "policies": []}
        with patch.object(sys, "argv", ["run.py", instance_name]), \
             patch("launch.agents_crud.is_dir", return_value=True), \
             patch("launch.tags.store.load", return_value={instance_name: entry}):
            picked, claude_args, _, _ = run.parse_cli(REGISTRY)
        self.assertIsInstance(picked, Instance)
        self.assertEqual(picked.agent, "golem")
        self.assertEqual(picked.session, "test_fixture")
        self.assertEqual(picked.workspace, "/tmp/ws")
        self.assertFalse(picked.is_brand_new)
        self.assertNotIn(instance_name, claude_args)

    def test_no_target_doesnt_crash_resolve_pick(self):
        # Bare `run.py` → args.target is None; resolve_pick must accept it and
        # return None without exploding. Regression guard for the None-safe contract.
        picked, claude_args, _, _ = self._parse(["run.py"])
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
        inst = MagicMock(is_brand_new=False)
        chain = ["base"]
        opts = run.LaunchOptions(MagicMock(), [], dry_run, False)

        mocks = {
            "gather_input":     patch.object(run, "gather_input", return_value=(opts, MagicMock())),
            "set_dry_run":      patch.object(run, "set_dry_run"),
            "resolve_target":   patch.object(run, "resolve_target", return_value=inst),
            "compute_resume_flag": patch.object(run, "compute_resume_flag", return_value=[]),
            "persist_instance": patch.object(run, "persist_instance"),
            "compose_chain":    patch.object(run, "compose_chain", return_value=chain),
            "setup_state":      patch.object(run, "setup_state", return_value=[]),
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
        # dry-run too. The actual docker invocation is gated inside
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
        mocks["persist_instance"].assert_called_once()

    def test_persist_fires_for_cont_launches_too(self):
        # The store entry always reflects the last-launched configuration —
        # cont launches rewrite it idempotently rather than gating on
        # is_brand_new (the old two-map model persisted modes only for new).
        mocks = self._mock_pipeline_through_to_run_compose(dry_run=False)
        run.launch()
        mocks["persist_instance"].assert_called_once_with(mocks["resolve_target"].return_value)

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
        # Both are still *called* in dry-run — ensure_image's docker
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
    to happen — the pre-fix behavior). It also scans the tag tree first and
    runs the one-shot legacy-store migration before anything reads the store."""

    def _gather(self, parse_result, **stage_patches):
        patches = {
            "scan_all":       patch.object(run, "scan_all", return_value=REGISTRY),
            "ensure":         patch("launch.tags.store.ensure_migrated"),
            "parse_cli":      patch.object(run, "parse_cli", return_value=parse_result),
            **stage_patches,
        }
        active = {name: p.start() for name, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        return active

    def test_docker_gate_precedes_picker(self):
        calls = []
        picked = MagicMock()
        self._gather(
            run.LaunchOptions(None, [], False, False),
            require_docker=patch.object(run, "require_docker",
                                        side_effect=lambda: calls.append("require_docker")),
            select_agent=patch.object(run, "select_agent",
                                      side_effect=lambda registry: calls.append("select_agent") or picked),
        )
        opts, registry = run.gather_input()
        self.assertEqual(calls, ["require_docker", "select_agent"])
        self.assertIs(opts.picked, picked)
        self.assertIs(registry, REGISTRY)

    def test_docker_gate_fires_even_with_direct_target(self):
        # A CLI-named target skips the picker but not the docker gate.
        target = MagicMock()
        active = self._gather(
            run.LaunchOptions(target, ["--verbose"], True, False),
            require_docker=patch.object(run, "require_docker"),
            select_agent=patch.object(run, "select_agent"),
        )
        opts, _ = run.gather_input()
        active["require_docker"].assert_called_once()
        active["select_agent"].assert_not_called()
        self.assertIs(opts.picked, target)
        self.assertEqual(opts.claude_args, ["--verbose"])
        self.assertTrue(opts.dry_run)

    def test_missing_docker_stops_before_picker(self):
        # require_docker exits via exit_if_missing — the picker must never
        # open after that.
        active = self._gather(
            run.LaunchOptions(None, [], False, False),
            require_docker=patch.object(run, "require_docker",
                                        side_effect=SystemExit("docker is required")),
            select_agent=patch.object(run, "select_agent"),
        )
        with self.assertRaises(SystemExit):
            run.gather_input()
        active["select_agent"].assert_not_called()

    def test_picker_cancel_exits_zero(self):
        self._gather(
            run.LaunchOptions(None, [], False, False),
            require_docker=patch.object(run, "require_docker"),
            select_agent=patch.object(run, "select_agent", return_value=None),
        )
        with self.assertRaises(SystemExit) as ctx:
            run.gather_input()
        self.assertEqual(ctx.exception.code, 0)

    def test_migration_runs_before_cli_resolution(self):
        # ensure_migrated must precede parse_cli — resolve_pick reads the
        # store, and reading it pre-migration would miss legacy instances.
        calls = []
        self._gather(
            run.LaunchOptions(MagicMock(), [], False, False),
            require_docker=patch.object(run, "require_docker"),
            select_agent=patch.object(run, "select_agent"),
        )
        with patch("launch.tags.store.ensure_migrated", side_effect=lambda: calls.append("migrate")), \
             patch.object(run, "parse_cli", side_effect=lambda reg: calls.append("parse") or run.LaunchOptions(MagicMock(), [], False, False)):
            run.gather_input()
        self.assertEqual(calls, ["migrate", "parse"])


class TestResolveTarget(unittest.TestCase):
    """resolve_target re-prompts for cont identities whose stored workspace is
    missing — which means None (no store entry) AND "" (empty entry). The
    two used to diverge: "" slipped past the `is None` check and silently
    fell back to DEFAULT_WORKSPACE downstream instead of re-prompting."""

    def _cont(self, workspace):
        return Instance(agent="golem", md_path=Path("/fake/golem.md"), session="s",
                        workspace=workspace, is_brand_new=False, engine=None)

    def test_none_workspace_reprompts(self):
        with patch.object(run, "ask_for_workspace", return_value="/tmp") as mock_ask:
            out = run.resolve_target(self._cont(None), REGISTRY)
        mock_ask.assert_called_once()
        self.assertEqual(out.workspace, "/tmp")
        self.assertFalse(out.is_brand_new)   # dataclasses.replace must not disturb the cont flag

    def test_empty_string_workspace_reprompts(self):
        with patch.object(run, "ask_for_workspace", return_value="/tmp") as mock_ask:
            out = run.resolve_target(self._cont(""), REGISTRY)
        mock_ask.assert_called_once()
        self.assertEqual(out.workspace, "/tmp")

    def test_valid_workspace_passes_through_unprompted(self):
        with tempfile.TemporaryDirectory() as real_dir, \
             patch.object(run, "ask_for_workspace") as mock_ask:
            out = run.resolve_target(self._cont(real_dir), REGISTRY)
        mock_ask.assert_not_called()
        self.assertEqual(out.workspace, real_dir)

    def test_invalid_workspace_exits(self):
        # Set-but-bogus path is a stale store entry — exit with the
        # fix-the-entry message rather than mounting garbage.
        with self.assertRaises(SystemExit):
            run.resolve_target(self._cont("/no/such/dir/for/sure"), REGISTRY)


if __name__ == "__main__":
    unittest.main()
