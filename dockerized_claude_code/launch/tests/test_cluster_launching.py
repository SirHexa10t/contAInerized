"""Tests for launch.cluster.launching — the integration step.

Everything host-side is REAL here (installs into a redirected AGENTS_STATE,
script/banner writes, the real registry and agent .lego files); only docker is
stubbed. The spike's three recipes are each pinned — kill-switch unset, shared
sessions/ symlinks, per-member CLAUDE_CODE_SESSION_NAME — because losing any
one of them degrades silently: members launch fine and simply cannot hear
each other.
"""

import contextlib
import dataclasses
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import docker_config, paths
from launch.cluster import launching, state
from launch.container_env import ContainerEnvKey, _container_env, container_env_args, staged_env
from launch.file_access import present_optional_cred_services
from launch.cluster.legoset import assemble
from launch.cluster.member import ClusterError, Member
from launch.ai import ADAPTERS
from launch.docker_config import CONTAINER_NAME_PREFIX, run_cluster_container
from launch.tags import AgentBuild, TagError, scan_all

REGISTRY = scan_all(paths.AGENTS_DIR)


def write_ui_profile(*, herdr: bool) -> None:
    """Pin the muxer preference inside this test's redirected AGENTS_STATE —
    the file `cluster.backend()` reads (the MUXER_BACKEND env var is retired)."""
    paths.ui_profile_path().write_text(
        f"herdr_instead_of_tmux = {'true' if herdr else 'false'}\n")


class LaunchingTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        # prepare() stages into the launcher's two module-level accumulators
        # — the mounts (shared with the solo path since 2026-09-15) and the
        # container env — so each test starts from empty ones and leaves none
        # of its own behind.
        self._env_snapshot = dict(_container_env)
        _container_env.clear()
        docker_config._docker_mounts.clear()
        self.addCleanup(docker_config._docker_mounts.clear)
        self.addCleanup(lambda: (_container_env.clear(), _container_env.update(self._env_snapshot)))
        # apply_tags over the union probe fires [code]'s handler; its cache
        # prep and pruning touch the REAL cache root (a constant), so both
        # are stubbed — the mounts it stages are what the tests look at. The
        # first-launch template plant writes the real user_extras/ too.
        for target in ("launch.tag_handlers.prepare_caches", "launch.tag_handlers.prune_caches",
                       "launch.cluster.launching.plant_user_extras"):
            p = patch(target)
            self.mocks = getattr(self, "mocks", {})
            self.mocks[target.rsplit(".", 1)[1]] = p.start()
            self.addCleanup(p.stop)
        # present_optional_cred_services is LRU-cached for the process; the
        # creds tests redirect OPTIONAL_CREDS_DIR, so the cache must not
        # carry an answer across tests.
        present_optional_cred_services.cache_clear()
        self.addCleanup(present_optional_cred_services.cache_clear)
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

    def test_a_members_own_container_level_tag_is_refused_from_data_naming_the_member(self):
        # {firewall} declares forbid_on = ["member", "cluster"] (cap_add +
        # mounts + a wrapper entrypoint: one container cannot honour it per
        # member, and wrapping the cluster script is not built). A member
        # adding it is a `forbidden` TagProblem — member_instances names the
        # member, the tag and where it can go, before refusal() ever runs;
        # launching WITHOUT the firewall would be the silent-degradation failure.
        clustered = self.cluster.with_member(Member.of(
            "researcher", role="guarded", build=AgentBuild(specialties=("firewall",))))
        with self.assertRaisesRegex(ClusterError, r"researcher__guarded.*\{firewall\}.*not in a cluster.*F2"):
            launching.member_instances(clustered, REGISTRY)

    def test_a_members_own_dood_points_at_the_cluster_row(self):
        # {dood} forbids only `member`: the fix is to set it cluster-wide.
        clustered = self.cluster.with_member(Member.of(
            "researcher", role="docker", build=AgentBuild(professions=("code",), specialties=("dood",))))
        with self.assertRaisesRegex(ClusterError, r"researcher__docker.*\{dood\}.*cluster-wide only: F2 on the cluster row"):
            launching.member_instances(clustered, REGISTRY)

    def test_a_member_in_a_harness_without_an_adapter_refuses_by_member_name(self):
        # Same rule and message as a solo launch (launch/ai.refusal_for), so
        # a cluster never starts a member with Claude Code's binary and
        # another CLI's settings. The member names only its AI: its default
        # harness — unadapted — is what the refusal names.
        unadapted = next(h for h in REGISTRY.harnesses.values() if h.name not in ADAPTERS)
        clustered = self.cluster.with_member(Member(
            agent="researcher", role="alien",
            build=AgentBuild(ai=unadapted.ais[0])))   # its OWN tags only — the cluster's locked pair comes from the cluster
        pairs = launching.member_instances(clustered, REGISTRY)
        reason = launching.refusal(pairs)
        self.assertIsNotNone(reason)
        self.assertIn("researcher__alien", reason)
        self.assertIn(unadapted.label, reason)
        self.assertIn("plans/adding_an_ai.md", reason)


class TestPrepare(LaunchingTmp):
    def setUp(self):
        super().setUp()
        # Pinned to the TMUX backend via the ui profile, written into this
        # test's redirected AGENTS_STATE: one test here asserts the tmux
        # cluster shape, and the rest must not flap with the operator's real
        # preference. TestBackendSwitch owns the herdr twin and the default.
        write_ui_profile(herdr=False)
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

    def test_every_member_gets_its_own_bottom_status_line(self):
        # Members had a BLANK bottom row while solo instances have always
        # shown one (operator report, 2026-09-02): AGENT_STATUS_LINE was
        # never staged for them. It has to be PER-MEMBER env — container-wide
        # it could only carry one member's line — and it must lead with the
        # member ID, since two members built from the same agent would
        # otherwise render identically.
        golem = self.window_env("golem")
        researcher = self.window_env("researcher__primary")
        for member_id, env in (("golem", golem),
                               ("researcher__primary", researcher)):
            with self.subTest(member=member_id):
                line = env["AGENT_STATUS_LINE"]
                self.assertIn(member_id, line)
                self.assertIn(str(self.cluster.project), line)   # the path
                self.assertIn(self.cluster.session, line)        # the cluster
        self.assertNotEqual(golem["AGENT_STATUS_LINE"],
                            researcher["AGENT_STATUS_LINE"])

    def test_each_member_points_at_its_own_transcripts(self):
        # Per-member, like the status line: a member's conversation lives in
        # ITS config dir, so a container-wide value would aim every member's
        # reader at /home/claude/.claude — the bug `_dump_last_msg.py`
        # shipped by hardcoding that path (2026-09-19).
        for member_id in ("golem", "researcher__primary"):
            with self.subTest(member=member_id):
                self.assertEqual(
                    self.window_env(member_id)["AGENT_TRANSCRIPTS_DIR"],
                    f"/cluster/members/{member_id}/projects")

    def test_the_work_protocol_rides_every_cluster_launch(self):
        # The queue's plumbing: the package + its tunables RO-mounted at the
        # PACKAGE-OWNED /opt targets (importing them here IS the drift-pin),
        # and the member-owned protocol dir (chat + cursors) mkdir'd by the
        # ENTRYPOINT — never by docker, whose auto-created mountpoint parents
        # arrive root-owned (the recorded herdr lesson).
        from launch.cluster_work_protocol import (
            CONFIG_IN_CONTAINER, PACKAGE_IN_CONTAINER, PROTOCOL_DIR_IN_CONTAINER,
        )
        from launch.cluster_work_protocol.queue import CURSORS_DIRNAME
        pairs = set(self.prepared.mounts)
        self.assertIn((str(paths.CLUSTER_WORK_PROTOCOL_DIR),
                       f"{PACKAGE_IN_CONTAINER}:{paths.RO_MOUNT_OPTION}"), pairs)
        self.assertIn((str(paths.CLUSTER_PROTOCOL_CONF),
                       f"{CONFIG_IN_CONTAINER}:{paths.RO_MOUNT_OPTION}"), pairs)
        self.assertIn(f"mkdir -p {PROTOCOL_DIR_IN_CONTAINER / CURSORS_DIRNAME}",
                      self.prepared.script_host.read_text())

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
        creds_host = str(paths.credentials_dir("claude-code") / ".credentials.json")
        cred_targets = {target for source, target in self.prepared.mounts if source == creds_host}
        member_targets = {f"/cluster/members/{member_id}/.credentials.json"
                          for member_id in self.cluster.ids}
        self.assertLessEqual(member_targets, cred_targets)
        # The base set adds one more, at ~/.claude — for a human running
        # `claude` by hand in the free shell pane, not for any member.
        self.assertEqual(cred_targets - member_targets,
                         {"/home/claude/.claude/.credentials.json"})
        account_host = str(paths.credentials_dir("claude-code") / ".claude.json")
        account_targets = {target for source, target in self.prepared.mounts if source == account_host}
        # Per member — INSIDE its config dir, where the relocation variable
        # makes Claude Code look for it — plus HOME for the shell pane.
        self.assertEqual(account_targets, {f"/cluster/members/{m}/.claude.json" for m in self.cluster.ids} | {"/home/claude/.claude.json"})
        self.assertEqual(self.prepared.env_files, ())                 # no key file in this fixture
        (notice,) = self.prepared.notices                              # the same notice a solo launch prints — once, not per member
        self.assertIn("no API key file", notice)

    def test_a_members_key_file_rides_as_an_env_file_and_a_lax_one_refuses(self):
        keys = paths.key_file("claude")
        keys.parent.mkdir(parents=True)
        keys.write_text("ANTHROPIC_API_KEY=sk-test\n")
        keys.chmod(0o644)
        prepared = launching.prepare(self.cluster, REGISTRY)
        self.assertEqual(prepared.env_files, (str(keys),))
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)         # the preflight fixed the mode
        keys.write_text('ANTHROPIC_API_KEY="sk-quoted"\n')
        with self.assertRaisesRegex(ClusterError, "is quoted") as caught:
            launching.prepare(self.cluster, REGISTRY)
        self.assertNotIn("sk-quoted", str(caught.exception))         # the message names the line, never the value

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


class TestOneLaunchCore(LaunchingTmp):
    """The cluster runs the SAME core a solo launch runs — per member
    (`staging.stage_instance`) and per container (`apply_tags`,
    `set_container_env`, `optional_creds_mounts`, one mount accumulator) —
    so the two shapes cannot drift again (2026-09-15: a member's commands
    dir was writable, the union image was built from Dockerfile defaults,
    members' shells had no BASH_ENV)."""

    def setUp(self):
        super().setUp()
        write_ui_profile(herdr=False)

    def _prepared(self):
        with patch("builtins.print"):
            return launching.prepare(self.cluster, REGISTRY)

    def test_a_members_commands_are_read_only_like_a_solo_instances(self):
        prepared = self._prepared()
        dict_of = {target: source for source, target in prepared.mounts}
        for member_id in self.cluster.ids:
            with self.subTest(member=member_id):
                self.assertEqual(dict_of[f"/cluster/members/{member_id}/commands:ro"],
                                 str(paths.cluster_member_dir("team", member_id) / "commands"))

    def test_a_members_staging_error_names_the_member_and_keeps_the_message(self):
        with patch("launch.staging.install_settings", side_effect=TagError("loose vs tight: cleanupPeriodDays")), \
             patch("builtins.print"), \
             self.assertRaisesRegex(ClusterError, r"member '[a-z_]+'.*loose vs tight"):
            launching.prepare(self.cluster, REGISTRY)

    def test_the_container_env_is_the_container_half_only(self):
        # BASH_ENV, the busters and the union's INSTALL flags — and NEITHER
        # instance-scoped key: a member's identity rides its own pane.
        self._prepared()
        args = container_env_args()
        self.assertIn(f"BASH_ENV={paths.BASHRC_IN_CONTAINER}", args)
        staged = staged_env()
        for key in (ContainerEnvKey.SOFTWARE_STACK_REFRESH, ContainerEnvKey.FORCE_INSTALLS_REFRESH):
            self.assertIn(str(key), staged)
        self.assertTrue(any(k.startswith("INSTALL_") for k in staged), "the union's [code] toolkit flags")
        for key in ContainerEnvKey.instance_scoped_keys():
            self.assertNotIn(str(key), staged)
        # And the union image build sees them: ensure_image's build-arg
        # forward reads the same staged env.
        self.assertTrue(docker_config.build_arg_flags(("INSTALL_*",)))

    def test_a_staged_member_identity_is_refused_before_docker_runs(self):
        from launch.container_env import set_instance_env
        pairs = launching.member_instances(self.cluster, REGISTRY)
        set_instance_env(pairs[0][1])
        with patch("launch.docker_config._interactive_docker_run") as docker, \
             patch("launch.docker_config.set_terminal_title"), \
             self.assertRaisesRegex(RuntimeError, "instance-scoped"):
            run_cluster_container("team", "img", (), "/cluster/cluster-start.sh")
        docker.assert_not_called()

    def test_the_operators_optional_creds_ride_along_and_the_persona_says_so(self):
        # The [code] addendum promises "credentials for the following CLI
        # tools ... already installed and ready to use" — rendered from the
        # same host presence the mounts and the INSTALL flags derive from, so
        # the sentence, the mount and the install cannot disagree. Pinned per
        # member: a [code] member names exactly the mounted CLIs, a member
        # without [code] gets no sentence, and no creds means no section.
        creds_root = Path(self._tmp.name) / "optional_creds"
        for service in ("gcloud", "ssh"):
            (creds_root / service).mkdir(parents=True)
        with patch.object(paths, "OPTIONAL_CREDS_DIR", creds_root):
            present_optional_cred_services.cache_clear()
            prepared = self._prepared()
        self.assertEqual(prepared.cred_names, ("gcloud", "ssh"))
        targets = {target for _, target in prepared.mounts}
        self.assertLessEqual({paths.OPTIONAL_CREDS_MOUNTS["gcloud"][0], paths.OPTIONAL_CREDS_MOUNTS["ssh"][0]}, targets)
        self.assertEqual(staged_env()["INSTALL_GCLOUD"], "1")
        for member, inst in launching.member_instances(self.cluster, REGISTRY):
            persona = (paths.cluster_member_dir("team", member.id) / "CLAUDE.md").read_text()
            with self.subTest(member=member.id):
                if any(p.name == "code" for p in inst.professions):
                    self.assertIn("ready to use: gcloud ssh", persona)
                else:
                    self.assertNotIn("ready to use", persona)
        self.assertEqual(self.mocks["plant_user_extras"].call_count, 1)   # the first-launch files, like every solo launch
        # The banner names them exactly as a solo launch's does.
        buf = io.StringIO()
        with patch.object(launching, "ensure_image", return_value="img"), \
             patch.object(launching, "run_cluster_container"), \
             patch.object(launching, "prompt_install_failures"), \
             patch.object(launching, "prepare", return_value=prepared), \
             contextlib.redirect_stdout(buf):
            launching.launch(self.cluster, REGISTRY)
        self.assertIn("Optional creds:   gcloud, ssh", buf.getvalue())

    def test_no_creds_means_no_section_and_no_line(self):
        prepared = self._prepared()
        self.assertEqual(prepared.cred_names, ())
        for member_id in self.cluster.ids:
            self.assertNotIn("Credentials", (paths.cluster_member_dir("team", member_id) / "CLAUDE.md").read_text())

    def test_the_caches_come_from_the_code_handler_not_a_name_check(self):
        # apply_tags over the union probe dispatches [code]'s handler — the
        # one that preps, PRUNES and stages the caches for a solo launch.
        prepared = self._prepared()
        self.assertEqual(self.mocks["prepare_caches"].call_count, 1)
        self.assertEqual(self.mocks["prune_caches"].call_count, 1)
        for cache_host, cache_target in paths.CACHE_MOUNTS.items():
            self.assertIn((str(cache_host), str(cache_target)), prepared.mounts)

    def test_a_shadowing_mount_is_a_clean_stop_not_a_docker_error(self):
        # The one collision rule both shapes share, hit from the cluster side.
        docker_config.add_docker_mount("/elsewhere/settings.json", "/cluster/members/golem/settings.json:ro")
        with patch("builtins.print"), \
             self.assertRaisesRegex(ClusterError, r"member 'golem'.*already staged"):
            launching.prepare(self.cluster, REGISTRY)


class TestBackendSwitch(LaunchingTmp):
    """The ui_profile.toml preference selects the multiplexer per launch —
    herdr by default (a fresh profile is generated on first launch), tmux a
    persisted `herdr_instead_of_tmux = false` away. The switch is
    package-level (`cluster.backend()` → tags/ui_profile.muxer_backend,
    shared with the solo path); this class pins the CLUSTER script following
    it, plus the strict launch-read semantics the operator specified."""

    def prepared(self):
        with patch("builtins.print"):
            return launching.prepare(self.cluster, REGISTRY)

    def test_herdr_is_the_default_and_first_launch_writes_the_profile(self):
        # No profile file yet → the launch generates one from settings/
        # ui.form defaults (so the operator finds it editable afterwards) and
        # herdr assembles.
        text = self.prepared().script_host.read_text()
        self.assertIn("herdr server", text)
        self.assertNotIn("new-session", text)
        self.assertIn("herdr_instead_of_tmux = true",
                      paths.ui_profile_path().read_text())

    def test_tmux_stays_one_profile_edit_away(self):
        write_ui_profile(herdr=False)
        text = self.prepared().script_host.read_text()
        self.assertIn("tmux -u -L muxer new-session", text)
        self.assertNotIn("herdr server", text)

    def test_herdr_assembles_the_herdr_script(self):
        write_ui_profile(herdr=True)
        text = self.prepared().script_host.read_text()
        self.assertIn("herdr server", text)
        self.assertIn("herdr agent start", text)
        self.assertNotIn("new-session", text)
        # The shared contracts ride BOTH backends: messaging activation and
        # the sessions-symlink plumbing are backend-independent.
        self.assertIn(f"unset {launching.MESSAGING_KILL_SWITCH}", text)
        self.assertIn(f"mkdir -p {launching.SHARED_SESSIONS}", text)

    def test_only_the_backend_that_reads_the_banner_writes_it(self):
        """The banner file is tmux's status-bar source. It used to be written
        on every launch regardless of backend, so the default path produced a
        file with no reader — the kind of leftover that later reads as a
        contract ("something must need this") and never gets removed."""
        write_ui_profile(herdr=True)
        self.prepared()
        self.assertFalse(paths.cluster_banner_path(self.cluster.session).exists())

        write_ui_profile(herdr=False)
        self.prepared()
        self.assertIn("2 member(s)",
                      paths.cluster_banner_path(self.cluster.session).read_text())

    def test_a_profile_that_lost_the_field_is_a_loud_stop(self):
        # The operator's spec: a hand-edit that dropped the field must never
        # silently flip the muxer — the stop names the field and the fix
        # (rename the file away; the next launch regenerates it).
        paths.ui_profile_path().write_text("some_other_toggle = true\n")
        with self.assertRaisesRegex(SystemExit, "herdr_instead_of_tmux"):
            self.prepared()


class TestLaunch(LaunchingTmp):
    def test_launch_builds_the_union_and_hands_over_to_docker(self):
        with patch.object(launching, "ensure_image",
                          return_value="claude-agents:test") as build, \
             patch.object(launching, "run_cluster_container") as run, \
             patch.object(launching, "prompt_install_failures") as prompt, \
             patch("builtins.print"):
            launching.launch(self.cluster, REGISTRY)
        build.assert_called_once()
        # The failed-installs gate, with THIS shape's retry command, before the terminal changes hands.
        prompt.assert_called_once_with("claude-agents:test", "python3 cluster.py launch team --refresh-installs")
        (session, image, mounts, entrypoint), _ = run.call_args
        self.assertEqual(session, "team")
        self.assertEqual(image, "claude-agents:test")
        self.assertEqual(entrypoint, "/cluster/cluster-start.sh")
        self.assertIn(str(self.cluster.project),
                      {source for source, _ in mounts})


class TestClusterWideTags(LaunchingTmp):
    """A container-level tag is the CLUSTER's (forbid_on = ["member"]): picked
    cluster-wide, it reaches the one container exactly as a solo instance's
    does — {dood}'s socket and GID through apply_tags over the union probe,
    {ro}'s read-only workspace mount, capabilities and env forwards through
    run_cluster_container (gate tag-scopes, 2026-09-16)."""

    def setUp(self):
        super().setUp()
        write_ui_profile(herdr=False)
        gid = patch("launch.tag_handlers.detect_docker_gid", return_value="988")   # no docker group in the test host
        gid.start()
        self.addCleanup(gid.stop)

    def _with_cluster_tags(self, *specialties, professions=()):
        return state.save(dataclasses.replace(self.cluster, tags=AgentBuild(
            professions=tuple(professions), specialties=("muxer", "cluster", *specialties))))

    def test_cluster_wide_dood_reaches_the_one_container(self):
        cluster = self._with_cluster_tags("dood", professions=("code",))
        with patch("builtins.print"):
            prepared = launching.prepare(cluster, REGISTRY)
        self.assertIn(("/var/run/docker.sock", "/var/run/docker.sock"), prepared.mounts)   # the layer's mount, via apply_tags
        self.assertEqual(staged_env()["DOCKER_GID"], "988")                                # _apply_dood, forwarded to the _dood layer
        self.assertIn("dood", [name for name, _, _ in prepared.image_probe.build_steps])
        self.assertTrue(any(c.mounts for c in prepared.contributions))
        self.assertIsNone(launching.refusal(launching.member_instances(cluster, REGISTRY)))

    def test_cluster_wide_read_only_makes_the_one_workspace_mount_read_only(self):
        cluster = self._with_cluster_tags("read-only")
        with patch("builtins.print"):
            prepared = launching.prepare(cluster, REGISTRY)
        self.assertIn((str(cluster.project), "/workspace:ro"), prepared.mounts)
        self.assertNotIn((str(cluster.project), "/workspace"), prepared.mounts)

    def test_cluster_wide_firewall_is_refused_from_its_own_declaration(self):
        cluster = self._with_cluster_tags("firewall")
        with patch("builtins.print"), \
             self.assertRaisesRegex(ClusterError, r"\{firewall\}.*not on a cluster"):
            launching.prepare(cluster, REGISTRY)

    def test_the_unions_capabilities_and_env_forwards_reach_the_cluster_run(self):
        # No shipped tag produces either at cluster level today ({firewall},
        # the only source, forbids cluster) — a synthetic contribution drives
        # the path so a real bug there cannot hide until the first one lands.
        from launch.container_env import ContainerEnvKey, stage_container_env
        from launch.tags import DockerContribution
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4")
        contribution = DockerContribution(cap_add=("NET_ADMIN",), env_forward=("WHITELIST_ADDRESSES",))
        with patch("launch.docker_config._interactive_docker_run") as docker, \
             patch("launch.docker_config.set_terminal_title"):
            run_cluster_container("team", "img", (), "/cluster/cluster-start.sh", contributions=(contribution,))
        (args,), _ = docker.call_args
        self.assertIn("--cap-add=NET_ADMIN", args)
        self.assertIn("WHITELIST_ADDRESSES=1.2.3.4", args)


class TestRefreshInstalls(LaunchingTmp):
    """`cluster.py launch --refresh-installs` — the same buster run.py's flag
    is, threaded through the verb, launch() and prepare() to the union build's
    staged env."""

    def test_the_flag_busts_the_union_builds_cache_busters(self):
        with patch("builtins.print"):
            launching.prepare(self.cluster, REGISTRY, refresh_installs=True)
        staged = staged_env()
        self.assertTrue(staged["SOFTWARE_STACK_REFRESH"].startswith("forced-"))
        self.assertEqual(staged["SOFTWARE_STACK_REFRESH"], staged["FORCE_INSTALLS_REFRESH"])

    def test_without_the_flag_the_weekly_rotation_and_stable_apply(self):
        with patch("builtins.print"):
            launching.prepare(self.cluster, REGISTRY)
        staged = staged_env()
        self.assertFalse(staged["SOFTWARE_STACK_REFRESH"].startswith("forced-"))
        self.assertEqual(staged["FORCE_INSTALLS_REFRESH"], "stable")

    def test_the_flag_repulls_the_base_of_the_union_build(self):
        with patch.object(launching, "ensure_image", return_value="img") as build, \
             patch.object(launching, "run_cluster_container"), \
             patch.object(launching, "prompt_install_failures"), \
             patch("builtins.print"):
            launching.launch(self.cluster, REGISTRY, refresh_installs=True)
            launching.launch(self.cluster, REGISTRY)
        self.assertEqual([c.kwargs["pull"] for c in build.call_args_list], [True, False])

    def test_the_verb_passes_the_flag_to_the_launch(self):
        from launch.cluster import cli
        with patch.object(launching, "launch") as launch, \
             patch("launch.docker_config.require_docker"), \
             patch("launch.docker_config.running_cluster_report", return_value=None), \
             patch("builtins.print"):
            self.assertEqual(cli.main(["launch", "team", "--refresh-installs"]), cli.EXIT_OK)
            self.assertEqual(cli.main(["launch", "team"]), cli.EXIT_OK)
        self.assertEqual([c.kwargs["refresh_installs"] for c in launch.call_args_list], [True, False])


class TestRunClusterContainer(unittest.TestCase):
    def test_the_docker_invocation_shape(self):
        with patch("launch.docker_config._interactive_docker_run") as docker, \
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
