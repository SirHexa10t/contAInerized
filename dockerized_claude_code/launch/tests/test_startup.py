"""Tests for launch.startup — the one first step every entry point takes:
migrate the host state, then scan the tree. Pinned per ENTRY POINT, because
that is where it broke (2026-09-15: the cluster CLI launched without the
migrations and mounted blank login files into every member)."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths, startup
from launch.ai import CLAUDE_CODE
from launch.cluster import cli, state
from launch.cluster.legoset import assemble
from launch.cowork import cli as cowork_cli
from launch.paths import AGENTS_DIR
from launch.quickie import cli as quickie_cli
from launch.tags import Registry, migrations

_ROOT = Path(__file__).resolve().parents[2]   # the repo root: where the entry scripts live
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
import run  # noqa: E402  — must come after the sys.path.insert above


def _old_layout(root: Path) -> None:
    """The pre-2026-09-15 state: the Claude Code login pair at the state root."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "REAL"}}))
    (root / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "who@example.test"}}))


def _migrated(root: Path) -> bool:
    new_dir = paths.credentials_dir(CLAUDE_CODE.key)
    return (all((new_dir / f.name).is_file() and "REAL" in (new_dir / f.name).read_text() or "who@" in (new_dir / f.name).read_text()
                for f in CLAUDE_CODE.auth_files)
            and not (root / ".credentials.json").exists())


class TestOpenLauncher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / ".ai-agents"
        patcher = patch.object(paths, "AGENTS_STATE", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_migrates_then_scans(self):
        calls = []
        with patch.object(migrations, "ensure_migrated", side_effect=lambda: calls.append("migrate")), \
             patch.object(startup, "scan_all", side_effect=lambda d: calls.append(("scan", d)) or Registry()):
            registry = startup.open_launcher(Path("/tree"))
        self.assertEqual(calls, ["migrate", ("scan", Path("/tree"))])
        self.assertIsInstance(registry, Registry)

    def test_the_real_thing_moves_an_old_login_pair_and_returns_the_tree(self):
        _old_layout(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            registry = startup.open_launcher(AGENTS_DIR)
        self.assertTrue(_migrated(self.root))
        self.assertIn("claude", registry.ais)

    def test_the_cluster_cli_launch_verb_migrates_before_it_touches_the_state(self):
        # THE incident: `cluster.py launch` used to skip the migrations; it then
        # created blank login files at the new place and mounted those.
        _old_layout(self.root)
        paths.ui_profile_path().parent.mkdir(parents=True, exist_ok=True)
        paths.ui_profile_path().write_text("herdr_instead_of_tmux = false\n")
        with contextlib.redirect_stdout(io.StringIO()):
            state.save(state.from_template("team", Path("/tmp/project"), assemble([("golem", None)], AGENTS_DIR), template="devteam"))
        launched = []
        with patch("launch.cluster.launching.launch", side_effect=lambda cluster, registry, **kw: launched.append(cluster.session)), \
             patch("launch.docker_config.require_docker"), \
             patch("launch.docker_config.running_cluster_report", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(["launch", "team"])
        self.assertEqual((code, launched), (0, ["team"]))
        self.assertTrue(_migrated(self.root), "the launch verb must run the migrations before anything reads the state dir")

    def test_every_cli_verb_starts_the_same_way(self):
        # Not only `launch`: `list` reads the state dir too, so it must see the
        # migrated layout — one startup for the whole CLI.
        _old_layout(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["list"])
        self.assertTrue(_migrated(self.root))


class _Reached(Exception):
    """Raised from the first migration step: proof an entry got to
    `open_launcher` before it touched anything else."""


class TestEveryRootEntryOpensTheLauncher(unittest.TestCase):
    """The RULE (`.claude_dev_guidelines`): every entry that reads or writes
    the state dir opens through `startup.open_launcher`. Pinned against the
    real entry functions, and the set of root scripts IS the set mapped here —
    a new entry script fails this test until it is mapped (and so opens the
    launcher). The audit is not a root script: it is the read-only exception
    and must never migrate (test_audit pins that)."""

    ENTRIES = {
        "run.py": lambda: run.gather_input(),
        "quick_question.py": lambda: quickie_cli.main(["hello"]),
        "cluster.py": lambda: cli.main(["list"]),
        "cowork.py": lambda: cowork_cli.main(["status"]),
    }

    def test_the_root_scripts_are_exactly_the_mapped_entries(self):
        self.assertEqual(set(self.ENTRIES), {p.name for p in _ROOT.glob("*.py")})

    def test_each_entry_migrates_before_anything_else(self):
        for script, enter in self.ENTRIES.items():
            with self.subTest(script=script), \
                 patch.object(migrations, "ensure_migrated", side_effect=_Reached), \
                 contextlib.redirect_stdout(io.StringIO()), self.assertRaises(_Reached):
                enter()
