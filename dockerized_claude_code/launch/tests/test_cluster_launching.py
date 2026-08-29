"""Tests for launch.cluster.launching — the integration step.

Everything host-side is REAL here (installs into a redirected AGENTS_STATE,
script/banner writes, the real registry and agent .lego files); only docker is
stubbed. The spike's three recipes are each pinned — kill-switch unset, shared
sessions/ symlinks, per-member CLAUDE_CODE_SESSION_NAME — because losing any
one of them degrades silently: members launch fine and simply cannot hear
each other.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.cluster import launching, state
from launch.cluster.legoset import assemble
from launch.cluster.member import ClusterError, Member
from launch.docker_config import CONTAINER_NAME_PREFIX, run_cluster_container
from launch.tags import AgentBuild, scan_all

REGISTRY = scan_all(paths.AGENTS_DIR)


class LaunchingTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cluster = state.save(state.from_template(
            "team", Path("/tmp/project"),
            assemble([("golem", None), ("researcher", "primary")],
                     paths.AGENTS_DIR),
            template="devteam"))


class TestMemberInstances(LaunchingTmp):
    def test_each_member_resolves_to_an_instance_in_its_own_dir(self):
        pairs = launching.member_instances(self.cluster, REGISTRY)
        self.assertEqual([m.id for m, _ in pairs],
                         ["golem", "researcher__primary"])
        _, researcher = pairs[1]
        self.assertEqual(researcher.state_dir,
                         paths.cluster_member_dir("team", "researcher__primary"))
        # The ordinary pipeline resolved the member's own .lego engine.
        self.assertEqual(researcher.engine.name, "researcher")

    def test_a_vanished_agent_is_a_loud_stop_naming_the_member(self):
        broken = self.cluster.with_member(Member.of("nobody"))
        with self.assertRaisesRegex(ClusterError, "nobody"):
            launching.member_instances(broken, REGISTRY)

    def test_a_stale_tag_is_a_loud_stop_pointing_at_the_fix(self):
        broken = self.cluster.with_member(
            Member.of("poet", build=AgentBuild(specialties=("ghost-tag",))))
        with self.assertRaisesRegex(ClusterError, "poet.*ghost-tag.*F2"):
            launching.member_instances(broken, REGISTRY)


class TestRefusal(LaunchingTmp):
    def test_forced_tags_alone_are_launchable(self):
        # {muxer} carries an entrypoint contribution (the SOLO startup script)
        # — exempt, because the cluster script replaces it. Without the
        # exemption every cluster would refuse itself.
        pairs = launching.member_instances(self.cluster, REGISTRY)
        self.assertIsNone(launching.refusal(pairs))

    def test_a_container_level_tag_refuses_by_member_name(self):
        # {firewall} means cap_add + mounts + a foreign entrypoint — one
        # container cannot honour it per-member, and launching WITHOUT a
        # member's firewall would be the silent-degradation failure.
        clustered = self.cluster.with_member(Member(
            agent="researcher", role="guarded",
            build=AgentBuild(specialties=("muxer", "cluster", "firewall"))))
        pairs = launching.member_instances(clustered, REGISTRY)
        reason = launching.refusal(pairs)
        self.assertIsNotNone(reason)
        self.assertIn("researcher__guarded", reason)
        self.assertNotIn("golem,", reason)


class TestPrepare(LaunchingTmp):
    def setUp(self):
        super().setUp()
        # print silenced: compute_resume_flag narrates "starting fresh" per
        # member, which is launch-time information and test-time noise.
        with patch("builtins.print"):
            self.prepared = launching.prepare(self.cluster, REGISTRY)

    def test_every_member_gets_its_state_installed(self):
        for member_id in self.cluster.ids:
            member_dir = paths.cluster_member_dir("team", member_id)
            with self.subTest(member=member_id):
                self.assertTrue((member_dir / "CLAUDE.md").is_file())
                self.assertTrue((member_dir / "settings.json").is_file())
                self.assertTrue((member_dir / "commands").is_dir())

    def test_the_personas_differ_because_the_agents_do(self):
        golem = (paths.cluster_member_dir("team", "golem") / "CLAUDE.md").read_text()
        researcher = (paths.cluster_member_dir("team", "researcher__primary")
                      / "CLAUDE.md").read_text()
        self.assertNotEqual(golem, researcher)
        # And both carry the cluster addendum — they know they cohabit.
        self.assertIn("Cohabiting", golem)
        self.assertIn("Cohabiting", researcher)

    def window_env(self, member_id):
        plan_member = next(m for m in self.prepared.plan.members
                           if m.member.id == member_id)
        return plan_member.env

    def test_members_are_addressable_by_id(self):
        # The spike's naming recipe: without it siblings appear as derived
        # "workspace-xx" names and /address-by-role is impossible.
        env = self.window_env("researcher__primary")
        self.assertEqual(env["CLAUDE_CODE_SESSION_NAME"], "researcher__primary")

    def test_each_member_gets_its_own_config_dir_on_the_cluster_mount(self):
        env = self.window_env("golem")
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], "/cluster/members/golem")

    def test_the_engine_conf_rides_the_window_env(self):
        # The per-pane `-e` property that chose tmux: two members, two models.
        golem = self.window_env("golem")
        researcher = self.window_env("researcher__primary")
        self.assertIn("ANTHROPIC_MODEL", golem)
        self.assertIn("ANTHROPIC_MODEL", researcher)
        self.assertNotEqual(golem["ANTHROPIC_MODEL"],
                            researcher["ANTHROPIC_MODEL"])

    def test_member_commands_carry_their_own_effort(self):
        commands = {m.member.id: m.command for m in self.prepared.plan.members}
        for member_id, command in commands.items():
            with self.subTest(member=member_id):
                self.assertEqual(command[0], "claude")
        # Engine confs declare effort levels; the flag is the supported way to
        # pin one (see docker_config.effort_args).
        self.assertIn("--effort", commands["researcher__primary"])

    def test_the_script_bakes_in_the_three_spike_recipes(self):
        text = self.prepared.script_host.read_text()
        # 1. messaging activation — the sticky kill-switch is UNSET, not =0.
        self.assertIn(f"unset {launching.MESSAGING_KILL_SWITCH}", text)
        # 2. discovery across isolated config dirs — every member's sessions/
        #    symlinked to the one shared dir.
        self.assertIn(f"mkdir -p {launching.SHARED_SESSIONS}", text)
        for member_id in self.cluster.ids:
            self.assertIn(
                f"ln -sfn {launching.SHARED_SESSIONS} "
                f"/cluster/members/{member_id}/sessions", text)
        # 3. the shared ~/.claude assets a member's config dir would hide.
        self.assertIn("ln -sfn /home/claude/.claude/skills", text)

    def test_windows_follow_the_derived_picker_order(self):
        # The boundary reorder in prepare() is one droppable line; without it
        # windows follow storage order (id-alphabetical) and stop matching the
        # picker's member rows — `^b 2` and the second row would name
        # different members.
        expected = [m.id for m in state.picker_order(self.cluster.members,
                                                     REGISTRY)]
        self.assertEqual([m.member.id for m in self.prepared.plan.members],
                         expected)
        self.assertEqual(list(self.prepared.cluster.ids), expected)

    def test_the_script_is_the_cluster_shape_with_the_free_shell(self):
        text = self.prepared.script_host.read_text()
        self.assertIn("new-session", text)
        self.assertIn("new-window", text)            # second member + shell
        self.assertIn("shell", text)
        self.assertIn("source-file -q", text)        # the user's tmux.conf still wins
        self.assertIn("CLAUDE_CODE_SESSION_NAME=researcher__primary", text)

    def test_the_mounts_cover_project_cluster_and_member_settings(self):
        # target → source: targets ARE unique, so this direction can be a dict.
        dict_of = {target: source for source, target in self.prepared.mounts}
        self.assertEqual(dict_of["/workspace"], str(self.cluster.project))
        self.assertEqual(dict_of["/cluster"], str(paths.cluster_path("team")))
        # Per-member settings mount read-only OVER the rw /cluster view — the
        # solo shadowing trick, same reason: no member relaxes its own policy.
        member_settings = paths.state_settings_path(
            paths.cluster_member_dir("team", "golem"))
        self.assertEqual(dict_of["/cluster/members/golem/settings.json:ro"],
                         str(member_settings))

    def test_every_member_gets_the_shared_credentials_not_just_the_last(self):
        # THE bug the pair representation exists for: the credentials file is
        # the SOURCE of one mount per member. A source-keyed dict held one
        # entry, so only the LAST member could log in — caught while writing
        # this test. Docker repeats a source across -v flags legally.
        cred_targets = {target for source, target in self.prepared.mounts
                        if source == str(paths.CREDENTIALS_FILE)}
        member_targets = {f"/cluster/members/{member_id}/.credentials.json"
                          for member_id in self.cluster.ids}
        self.assertLessEqual(member_targets, cred_targets)
        # The base set adds one more, at ~/.claude — for a human running
        # `claude` by hand in the free shell pane, not for any member.
        self.assertEqual(cred_targets - member_targets,
                         {"/home/claude/.claude/.credentials.json"})
        account_targets = {target for source, target in self.prepared.mounts
                           if source == str(paths.ACCOUNT_FILE)}
        # Per member, plus the base set's ~/.claude.json for the shell pane.
        self.assertEqual(len(account_targets), len(self.cluster.ids) + 1)

    def test_the_union_image_probe_carries_everyones_layers(self):
        probe = self.prepared.image_probe
        self.assertEqual({p.name for p in probe.professions}, {"code"})
        self.assertLessEqual({"muxer", "cluster"},
                             {s.name for s in probe.specialties})

    def test_the_always_on_base_mounts_ride_along(self):
        # Missing base mounts degrade SILENTLY: the entrypoint's `source-file
        # -q` skips an unmounted tmux.conf, booting a session with no quit /
        # help / mouse bindings; the help popup cats nothing; the skills and
        # keybindings symlinks dangle. This gap shipped once (caught by an
        # operator question before any real boot) — each critical target is
        # pinned by name.
        targets = {target for _, target in self.prepared.mounts}
        for needle in ("tmux.conf", "muxer-help.txt", ".bashrc",
                       "statusline.sh", "skills"):
            with self.subTest(mount=needle):
                self.assertTrue(any(needle in t for t in targets),
                                f"no mount targets {needle}")
        # And the whole base set, not a hand-picked subset.
        for host, target in paths.DOCKER_BASE_MOUNTS.items():
            with self.subTest(source=host.name):
                self.assertIn((str(host), str(target)), self.prepared.mounts)

    def test_code_members_bring_the_shared_toolchain_caches(self):
        cache_host = str(next(iter(paths.CACHE_MOUNTS)))
        self.assertIn(cache_host,
                      {source for source, _ in self.prepared.mounts})

    def test_the_banner_names_the_members_and_project(self):
        banner = paths.cluster_banner_path("team").read_text()
        self.assertIn("2 member(s)", banner)


class TestLaunch(LaunchingTmp):
    def test_launch_builds_the_union_and_hands_over_to_docker(self):
        with patch.object(launching, "ensure_image",
                          return_value="claude-agents:test") as build, \
             patch.object(launching, "run_cluster_container") as run, \
             patch("builtins.print"):
            launching.launch(self.cluster, REGISTRY)
        build.assert_called_once()
        (session, image, mounts, entrypoint), _ = run.call_args
        self.assertEqual(session, "team")
        self.assertEqual(image, "claude-agents:test")
        self.assertEqual(entrypoint, "/cluster/cluster-start.sh")
        self.assertIn(str(self.cluster.project),
                      {source for source, _ in mounts})


class TestRunClusterContainer(unittest.TestCase):
    def test_the_docker_invocation_shape(self):
        with patch("launch.docker_config.docker_subprocess") as docker, \
             patch("launch.docker_config.set_terminal_title"), \
             patch("launch.docker_config.container_env_args", return_value=["-e", "X=1"]):
            run_cluster_container("team", "img:tag",
                                  (("/host/p", "/workspace"),
                                   ("/host/c", "/cluster")),
                                  "/cluster/cluster-start.sh")
        (args,), _ = docker.call_args
        self.assertEqual(args[:2], ["run", "--rm"])
        self.assertIn("-it", args)
        self.assertIn(f"{CONTAINER_NAME_PREFIX}cluster-team", args)
        self.assertIn("--entrypoint", args)
        self.assertEqual(args[args.index("--entrypoint") + 1],
                         "/cluster/cluster-start.sh")
        self.assertIn("-v", args)
        self.assertIn("/host/p:/workspace", args)
        self.assertEqual(args[-1], "img:tag")   # image last: everything after would be argv for the entrypoint


if __name__ == "__main__":
    unittest.main()
