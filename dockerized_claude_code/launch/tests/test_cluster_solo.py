"""Tests for launch.cluster.solo — what `{muxer}` does to a solo launch.

The property worth guarding: the container path appears in TWO places, once as
data (`agents/specialty/muxer/tag.docker`'s `entrypoint`, which is what the
launcher actually acts on) and once in code (the constant that writes the file
that declaration points at). If they drift, the container starts, fails to find
its entrypoint, and dies with a docker error that names neither tag nor module.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.cluster import solo
from launch.tags import AgentBuild, Instance, resolve_build, scan_all


def pin_backend(case: unittest.TestCase, *, herdr: bool | None) -> None:
    """Redirect AGENTS_STATE to a fresh tmp for this test and persist the
    muxer preference there (None = no file: the first-launch default path).
    The profile file is what `cluster.backend()` reads — the MUXER_BACKEND
    env var is retired."""
    state_tmp = tempfile.TemporaryDirectory()
    case.addCleanup(state_tmp.cleanup)
    patcher = patch.object(paths, "AGENTS_STATE", Path(state_tmp.name))
    patcher.start()
    case.addCleanup(patcher.stop)
    if herdr is not None:
        paths.ui_profile_path().write_text(
            f"herdr_instead_of_tmux = {'true' if herdr else 'false'}\n")


def a_muxer_instance(specialties=("muxer",), workspace="/home/someone/code/thing",
                     state_root=None) -> Instance:
    registry = scan_all(paths.AGENTS_DIR)
    build = AgentBuild(engine="thinker", professions=("code",),
                       specialties=tuple(specialties), policies=())
    return Instance(agent="refactorer", md_path=paths.AGENTS_DIR / "refactorer.md",
                    session="proj", workspace=workspace, is_brand_new=False,
                    state_dir_override=state_root,
                    **resolve_build(build, "refactorer", registry))


class TestEntrypointAgreement(unittest.TestCase):
    """The one drift that produces an unreadable failure."""

    def test_the_tag_declares_the_path_the_code_writes(self):
        muxer = scan_all(paths.AGENTS_DIR).specialties["muxer"]
        self.assertIsNotNone(muxer.docker, "{muxer} needs a tag.docker")
        self.assertEqual(muxer.docker.entrypoint, solo.CONTAINER_SCRIPT)

    def test_the_declared_path_is_inside_the_mounted_state_dir(self):
        # It only exists in the container because the state dir is bind-mounted
        # there; a path outside that mount would silently not be found.
        self.assertTrue(
            solo.CONTAINER_SCRIPT.startswith(str(paths.CLAUDE_CONFIG_IN_CONTAINER)))

    def test_the_tag_declares_no_layer_of_its_own(self):
        # tmux rides {muxer}'s hidden profession layer, so tag.docker should carry
        # run-time config only — a build contribution here would be a second home
        # for the same concern.
        muxer = scan_all(paths.AGENTS_DIR).specialties["muxer"]
        self.assertEqual(tuple(muxer.docker.mounts), ())
        self.assertEqual(tuple(muxer.docker.cap_add), ())


class TestInstallLauncher(unittest.TestCase):
    """The TMUX solo shape — pinned via a redirected ui profile so the
    assertions cannot flap with the operator's real preference; TestHerdrSolo
    owns the herdr twin and the first-launch default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        pin_backend(self, herdr=False)

    def install(self, argv=("claude", "--continue"), **kw):
        inst = a_muxer_instance(state_root=self.root, **kw)
        returned = solo.install_launcher(inst, argv)
        host, _ = solo.script_paths(inst)
        return host, returned

    def test_it_writes_an_executable_script_and_returns_the_container_path(self):
        host, returned = self.install()
        self.assertTrue(host.is_file())
        self.assertEqual(returned, solo.CONTAINER_SCRIPT)
        self.assertTrue(host.stat().st_mode & 0o111, "must be executable to be an entrypoint")

    def test_the_agents_argv_is_baked_in_quoted(self):
        # Passing it through as "$@" would mean quoting an argv across two shells;
        # a flag value containing spaces is exactly what comes apart there.
        host, _ = self.install(argv=("claude", "--append-system-prompt", "be terse now"))
        text = host.read_text()
        self.assertIn("be terse now", text)
        self.assertIn("--append-system-prompt", text)

    def test_the_operators_tmux_conf_is_wired_in(self):
        # settings/tmux.conf rides every launch as a read-only mount; the
        # script must source it (last — tmux.py's tests pin the ordering) or
        # the file the user was told to tinker with silently does nothing.
        host, _ = self.install()
        self.assertIn(f"source-file -q {paths.TMUX_CONF_IN_CONTAINER}",
                      host.read_text())

    def test_the_label_is_the_host_workspace_not_the_container_mount(self):
        host, _ = self.install(workspace="/home/someone/code/thing")
        text = host.read_text()
        self.assertIn("/home/someone/code/thing", text)
        # `/workspace` still appears as the cwd; what must NOT happen is the LABEL
        # carrying it, since it is identical for every instance.
        label = next(line for line in text.splitlines() if "status-left " in line)
        self.assertNotIn("/workspace", label)

    def test_it_is_rewritten_rather_than_appended_on_relaunch(self):
        host, _ = self.install(argv=("claude", "--first"))
        self.install(argv=("claude", "--second"))
        text = host.read_text()
        self.assertIn("--second", text)
        self.assertNotIn("--first", text)


class TestComposition(unittest.TestCase):
    """`{muxer}` and `{firewall}` used to be mutually exclusive — both had to be
    the container's entrypoint. They now chain, so this checks the shape that
    makes that work rather than a refusal."""

    def test_muxer_is_the_last_link_in_the_chain(self):
        inst = a_muxer_instance(specialties=("muxer", "firewall"))
        chain = [c.entrypoint for c in inst.docker_contributions if c.entrypoint]
        self.assertEqual(chain[-1], solo.CONTAINER_SCRIPT)

    def test_the_generated_script_does_not_expect_arguments(self):
        # Being last, nothing is passed to it — the agent's argv is baked in. A
        # `"$@"` here would silently drop the agent command. Both backends must
        # hold the property, so both scripts are checked.
        for herdr_value in (False, True):
            with self.subTest(herdr=herdr_value), \
                 tempfile.TemporaryDirectory() as tmp, \
                 patch.object(paths, "AGENTS_STATE", Path(tmp)):
                paths.ui_profile_path().write_text(
                    f"herdr_instead_of_tmux = "
                    f"{'true' if herdr_value else 'false'}\n")
                inst = a_muxer_instance(state_root=Path(tmp))
                solo.install_launcher(inst, ("claude", "--continue"))
                host, _ = solo.script_paths(inst)
                self.assertNotIn('"$@"', host.read_text())


class TestHerdrSolo(unittest.TestCase):
    """The DEFAULT backend's solo shape: herdr server + one detected agent
    tab, the workspace root pane as the free shell — and the cluster-only
    trades staying out of it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def script_with(self, herdr: bool | None,
                    argv=("claude", "--continue")) -> str:
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(paths, "AGENTS_STATE", Path(tmp)):
            if herdr is not None:
                paths.ui_profile_path().write_text(
                    f"herdr_instead_of_tmux = {'true' if herdr else 'false'}\n")
            inst = a_muxer_instance(state_root=self.root)
            solo.install_launcher(inst, argv)
            host, _ = solo.script_paths(inst)
            return host.read_text()

    def test_herdr_is_the_default_for_solo_launches_too(self):
        # The operator decision (2026-08-29) covers every {muxer} shape, not
        # just clusters — no ui profile on disk yet means the herdr script.
        text = self.script_with(None)
        self.assertIn("herdr server", text)
        self.assertNotIn("new-session", text)

    def test_the_agent_is_the_root_pane_and_the_shell_splits_below(self):
        # `agent start --kind claude` into the workspace's own root pane puts
        # the agent in herdr's sidebar with live idle/working state; the free
        # shell is a split beneath it (both visible — the tmux solo layout)
        # in ONE tab, which the script renames after the agent: the tab row
        # stays, carrying the key hint in its right corner.
        text = self.script_with(True)
        self.assertIn("workspace create --cwd /workspace "
                      "--label refactorer__proj", text)
        self.assertIn("herdr agent start agent --kind claude", text)
        self.assertIn("pane split", text)
        # ...plus the extra full-height shell TAB (operator request
        # 2026-09-02, "in both" shapes) — the only tab this shape creates.
        creates = [line for line in text.splitlines() if "tab create" in line]
        self.assertEqual(len(creates), 1)
        self.assertIn("--label shell", creates[0])

    def test_the_argv_is_baked_in_quoted_here_too(self):
        text = self.script_with(
            True, argv=("claude", "--append-system-prompt", "be terse now"))
        self.assertIn("be terse now", text)
        self.assertIn("--append-system-prompt", text)

    def test_solo_keeps_the_messaging_kill_switch(self):
        # Unsetting it — and accepting the telemetry it re-admits — is a
        # CLUSTER trade; a solo script must carry no unsets at all.
        self.assertNotIn("unset CLAUDE_CODE", self.script_with(True))

    def test_tmux_stays_one_profile_edit_away(self):
        self.assertIn("tmux -u -L muxer new-session", self.script_with(False))


if __name__ == "__main__":
    unittest.main()
