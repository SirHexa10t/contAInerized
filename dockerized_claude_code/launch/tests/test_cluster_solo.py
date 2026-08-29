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

from launch import paths
from launch.cluster import solo
from launch.tags import AgentBuild, Instance, resolve_build, scan_all


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
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

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
        # `"$@"` here would silently drop the agent command.
        with tempfile.TemporaryDirectory() as tmp:
            inst = a_muxer_instance(state_root=Path(tmp))
            solo.install_launcher(inst, ("claude", "--continue"))
            host, _ = solo.script_paths(inst)
            self.assertNotIn('"$@"', host.read_text())


if __name__ == "__main__":
    unittest.main()
