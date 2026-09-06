"""Tests for launch.cluster.panes — the record both multiplexer backends read.

These moved out of test_cluster_tmux 2026-09-03 with the record itself. The
`Pane` contract is deliberately narrow (a name, an argv, a cwd, an env) and
these tests pin the two things that are NOT just field access: quoting the
argv exactly once, and refusing a pane that could not be addressed or run.
The class below about neutrality is the guard on the split itself."""

import shlex
import unittest
from pathlib import Path

from launch.cluster import herdr, panes, tmux
from launch.cluster.member import ClusterError


def a_pane(name: str = "golem", command: tuple[str, ...] = ("claude",),
           cwd: str = "/workspaces/golem", **env: str) -> panes.Pane:
    return panes.Pane(name=name, command=command, cwd=Path(cwd), env=env)


class TestTheCommand(unittest.TestCase):
    def test_command_is_shell_quoted_exactly_once(self):
        # Both backends hand this string to a shell, so an argument
        # containing a space must survive as ONE argument.
        pane = a_pane(command=("claude", "--append-system-prompt", "be terse now"))
        self.assertEqual(shlex.split(pane.shell_command),
                         ["claude", "--append-system-prompt", "be terse now"])

    def test_a_pane_needs_a_command(self):
        # A pane with nothing to run is a window that opens onto an error;
        # the caller that assembled it is the one that can still fix it.
        with self.assertRaises(ValueError):
            panes.Pane(name="golem", command=(), cwd=Path("/tmp"))


class TestTheName(unittest.TestCase):
    def test_window_name_is_validated(self):
        # ':' would make tmux's `-t session:name` address a different window,
        # and herdr rejects it as a label — validated once, here, so both
        # backends get the loud version.
        with self.assertRaises(ClusterError):
            a_pane(name="bad:name")


class TestTheEnv(unittest.TestCase):
    def test_the_record_stays_a_plain_mapping(self):
        """No flag rendering on the record: `-e K=V` is tmux's dialect and
        `--env K=V` is herdr's, so each backend formats its own. A formatter
        here would put one backend's syntax in the shared layer — which is
        exactly the crookedness this split fixed."""
        self.assertFalse(hasattr(a_pane(), "env_flags"))
        self.assertEqual(dict(a_pane(MODEL="opus").env), {"MODEL": "opus"})

    def test_both_backends_render_the_same_mapping_their_own_way(self):
        pane = a_pane(ZULU="1", ALPHA="2")
        self.assertEqual(tmux._env_flags(pane), ("-e", "ALPHA=2", "-e", "ZULU=1"))
        self.assertEqual(herdr._env_flags(pane),
                         ["--env", "ALPHA=2", "--env", "ZULU=1"])


class TestTheSplitStaysNeutral(unittest.TestCase):
    """The regression this module exists to prevent: the DEFAULT backend
    importing its vocabulary from the FALLBACK one. `herdr.py` used to open
    with `from .tmux import SHELL_LABEL, Pane`."""

    def test_neither_backend_defines_the_record(self):
        for backend in (tmux, herdr):
            with self.subTest(backend=backend.__name__):
                self.assertIs(backend.Pane, panes.Pane)

    def test_herdr_does_not_import_from_tmux(self):
        source = (Path(herdr.__file__)).read_text()
        self.assertNotIn("from .tmux import", source)
        self.assertNotIn("import tmux", source)

    def test_the_shared_labels_have_one_definition(self):
        self.assertIs(tmux.SHELL_LABEL, panes.SHELL_LABEL)
        self.assertIs(herdr.SHELL_LABEL, panes.SHELL_LABEL)
        self.assertIs(tmux.AGENT_PANE, panes.AGENT_PANE)


if __name__ == "__main__":
    unittest.main()
