"""Tests for launch.docker_config — image naming, the tag.docker flag
emitters (build args / env forwards / entrypoint), the plain-docker build
loop, and set_container_mounts (workspace fallback).

Env-formatter tests (toolkit_install_flags, token_env_dict, etc.) live in
test_container_env.py alongside the accumulator they feed."""

import contextlib
import io
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from launch import docker_config, paths
from launch.container_env import ContainerEnvKey, _container_env, stage_container_env
from launch.docker_config import (
    build_arg_flags, effort_args, entrypoint_chain, env_forward_flags,
    image_tag, set_container_mounts,
)
from launch.tags import DockerContribution


def _run_inst(**over):
    """Duck-typed Instance for run_container tests — only the attrs it reads."""
    # `is_muxer` is read by run_container to decide whether the command becomes a
    # generated tmux script; False keeps these tests about the ordinary path.
    defaults = dict(docker_contributions=[], conf={}, claude_args=[],
                    instance="poet__x", is_muxer=False)
    defaults.update(over)
    return SimpleNamespace(**defaults)


class TestRunContainerMuxer(unittest.TestCase):
    """`{muxer}` replaces the container command with a generated tmux script, so
    claude's own argv must NOT also be appended — it is baked into the script, and
    passing it twice would hand the script stray arguments."""

    def _capture(self, **over):
        # The entrypoint flag comes from {muxer}'s tag.docker via entrypoint_flags,
        # so the stub carries that contribution exactly as the real tag does.
        from launch.cluster.solo import CONTAINER_SCRIPT
        from launch.tags.base import DockerContribution
        recorded = []
        contribution = DockerContribution(entrypoint=CONTAINER_SCRIPT)
        with patch.object(docker_config, "docker_subprocess",
                          side_effect=lambda a: recorded.append(a)), \
             patch.object(docker_config, "start_firewall_updater"), \
             patch("launch.cluster.solo.install_launcher",
                   return_value=CONTAINER_SCRIPT):
            docker_config.run_container(
                _run_inst(is_muxer=True, docker_contributions=[contribution], **over),
                "claude-agents:base", ["--extra"], ["--continue"])
        return recorded[0]

    def test_the_entrypoint_comes_from_the_tag_not_from_code(self):
        from launch.cluster.solo import CONTAINER_SCRIPT
        argv = self._capture()
        self.assertIn("--entrypoint", argv)
        self.assertEqual(argv[argv.index("--entrypoint") + 1], CONTAINER_SCRIPT)

    def test_claudes_argv_is_not_appended_as_well(self):
        argv = self._capture()
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--extra", argv)

    def test_firewall_plus_muxer_composes_into_a_chain(self):
        from launch.cluster.solo import CONTAINER_SCRIPT
        from launch.tags.base import DockerContribution
        recorded = []
        contributions = [DockerContribution(entrypoint="firewall-entrypoint.sh"),
                         DockerContribution(entrypoint=CONTAINER_SCRIPT)]
        with patch.object(docker_config, "docker_subprocess",
                          side_effect=lambda a: recorded.append(a)), \
             patch.object(docker_config, "start_firewall_updater"), \
             patch("launch.cluster.solo.install_launcher",
                   return_value=CONTAINER_SCRIPT):
            docker_config.run_container(
                _run_inst(is_muxer=True, docker_contributions=contributions),
                "claude-agents:base", [], [])
        argv = recorded[0]
        # firewall is docker's entrypoint; muxer's script is its argument, so
        # firewall's `exec "$@"` hands off to it. Nothing follows muxer.
        self.assertEqual(argv[argv.index("--entrypoint") + 1],
                         "/usr/local/bin/firewall-entrypoint.sh")
        self.assertEqual(argv[-1], CONTAINER_SCRIPT)

    def test_a_wrapper_without_muxer_gets_claude_explicitly(self):
        # With any entrypoint override the image's own ENTRYPOINT is bypassed, so
        # `claude` has to be named — it used to be hardcoded in firewall's script.
        from launch.tags.base import DockerContribution
        recorded = []
        with patch.object(docker_config, "docker_subprocess",
                          side_effect=lambda a: recorded.append(a)), \
             patch.object(docker_config, "start_firewall_updater"):
            docker_config.run_container(
                _run_inst(docker_contributions=[
                    DockerContribution(entrypoint="firewall-entrypoint.sh")]),
                "claude-agents:base", ["--extra"], ["--continue"])
        argv = recorded[0]
        self.assertIn("claude", argv)
        self.assertLess(argv.index("claude-agents:base"), argv.index("claude"))
        self.assertIn("--continue", argv)

    def test_a_non_muxer_launch_is_untouched(self):
        recorded = []
        with patch.object(docker_config, "docker_subprocess",
                          side_effect=lambda a: recorded.append(a)), \
             patch.object(docker_config, "start_firewall_updater"):
            docker_config.run_container(_run_inst(), "claude-agents:base",
                                        ["--extra"], ["--continue"])
        self.assertIn("--continue", recorded[0])
        self.assertNotIn("--entrypoint", recorded[0])


class TestRunContainerModes(unittest.TestCase):
    """Interactive (default) allocates a TTY (`-it`); the quickie tool's
    print mode drops it and injects `claude -p "<question>"` right after the
    image. The firewall await no-ops (no resolution started), so we only
    capture the assembled `docker run` argv."""

    def _capture(self, **run_kw):
        with patch.object(docker_config, "set_terminal_title"), \
             patch.object(docker_config, "is_critical_pending", return_value=False), \
             patch.object(docker_config, "wait_for_critical_addresses", return_value=None), \
             patch.object(docker_config, "start_firewall_updater"), \
             patch.object(docker_config, "docker_subprocess") as run:
            docker_config.run_container(_run_inst(), "claude-agents:base", [], [], **run_kw)
        return run.call_args.args[0]

    def test_interactive_default_allocates_tty(self):
        self.assertIn("-it", self._capture())

    def test_print_mode_drops_tty_and_injects_prompt(self):
        argv = self._capture(interactive=False, print_prompt="why do elephants have big ears?")
        self.assertNotIn("-it", argv)
        # `-p "<question>"` sits immediately after the image name.
        img = argv.index("claude-agents:base")
        self.assertEqual(argv[img + 1:img + 3], ["-p", "why do elephants have big ears?"])

    def test_stream_renderer_routes_to_streaming_subprocess(self):
        # quickie's path: a renderer → docker_stream_subprocess (pipe), never
        # the plain docker_subprocess (inherit-terminal) path.
        renderer = Mock()
        with patch.object(docker_config, "set_terminal_title"), \
             patch.object(docker_config, "is_critical_pending", return_value=False), \
             patch.object(docker_config, "wait_for_critical_addresses", return_value=None), \
             patch.object(docker_config, "start_firewall_updater"), \
             patch.object(docker_config, "docker_stream_subprocess") as stream, \
             patch.object(docker_config, "docker_subprocess") as plain:
            docker_config.run_container(_run_inst(), "claude-agents:base", ["--output-format", "stream-json"],
                                        [], interactive=False, print_prompt="q", stream_renderer=renderer)
        stream.assert_called_once()
        plain.assert_not_called()
        self.assertIs(stream.call_args.args[1], renderer)


class TestRunContainerCriticalDnsFailure(unittest.TestCase):
    """run_container aborts with the codebase's clean one-liner (sys.exit)
    when the {firewall} phase-1 critical-DNS resolve terminally failed — the
    worker's RuntimeError must not escape as a raw traceback, and nothing
    docker-touching may run after the failure."""

    def test_runtime_error_exits_cleanly_without_docker_run(self):
        boom = RuntimeError(
            "Critical Anthropic domains failed to resolve: ['api.anthropic.com']. "
            "Claude Code cannot operate without them; aborting launch.")
        with patch.object(docker_config, "stage_container_env"), \
             patch.object(docker_config, "set_terminal_title"), \
             patch.object(docker_config, "is_critical_pending", return_value=False), \
             patch.object(docker_config, "wait_for_critical_addresses", side_effect=boom), \
             patch.object(docker_config, "start_firewall_updater") as updater, \
             patch.object(docker_config, "docker_subprocess") as run:
            with self.assertRaises(SystemExit) as ctx:
                docker_config.run_container(_run_inst(), "claude-agents:base", [], [])
        self.assertIn("Critical Anthropic domains", str(ctx.exception))
        updater.assert_not_called()
        run.assert_not_called()


class TestDockerStreamSubprocess(unittest.TestCase):
    """docker_stream_subprocess pipes docker stdout through the renderer; on
    dry-run it prints the would-be invocation and spawns neither docker nor the
    renderer (parity with docker_subprocess's no-op)."""

    def test_dry_run_skips_popen_and_renderer(self):
        renderer = Mock()
        docker_config.set_dry_run(True)
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                docker_config.docker_stream_subprocess(["run", "--rm", "img"], renderer)
        finally:
            docker_config.set_dry_run(False)
        renderer.assert_not_called()
        self.assertIn("dry-run", out.getvalue())


class TestEffortArgs(unittest.TestCase):
    """effort_args — the explicit --effort CLI flag derived from the conf's
    CLAUDE_CODE_EFFORT_LEVEL. Pure function: (conf, claude_args) → arg list."""

    def test_conf_effort_becomes_flag_pair(self):
        self.assertEqual(effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, []),
                         ["--effort", "max"])

    def test_conf_without_effort_yields_nothing(self):
        self.assertEqual(effort_args({"ANTHROPIC_MODEL": "claude-fable-5"}, []), [])

    def test_valueless_conf_key_yields_nothing(self):
        # dotenv parses a bare `CLAUDE_CODE_EFFORT_LEVEL` line to None — no
        # flag should be emitted for it.
        self.assertEqual(effort_args({"CLAUDE_CODE_EFFORT_LEVEL": None}, []), [])

    def test_user_passed_effort_wins(self):
        # `python3 run.py poet -- --effort low` must reach claude unchallenged —
        # emitting ours too would either conflict or silently override the user.
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, ["--effort", "low"]), [])

    def test_user_passed_effort_equals_form_wins(self):
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "max"}, ["--effort=low"]), [])

    def test_unrelated_claude_args_dont_suppress(self):
        self.assertEqual(
            effort_args({"CLAUDE_CODE_EFFORT_LEVEL": "medium"}, ["--print", "hi"]),
            ["--effort", "medium"])


class TestImageTag(unittest.TestCase):
    def test_no_layers_is_base(self):
        self.assertEqual(image_tag([]), "claude-agents:base")

    def test_single_layer(self):
        self.assertEqual(image_tag(["code"]), "claude-agents:code")

    def test_layers_joined_with_dot(self):
        self.assertEqual(image_tag(["code", "web", "dood"]), "claude-agents:code.web.dood")


class ContainerEnvFixture(unittest.TestCase):
    """Snapshot + clear the container-env accumulator around each test."""

    def setUp(self):
        self._snapshot = dict(_container_env)
        _container_env.clear()

    def tearDown(self):
        _container_env.clear()
        _container_env.update(self._snapshot)


class TestBuildArgFlags(ContainerEnvFixture):
    """build_arg_flags resolves a layer's `[build] arg_forward` names against
    the staged env — plain names pull their value, globs expand, unstaged
    names drop silently (the Dockerfile's ARG default then applies)."""

    def test_plain_name_emits_flag_pair(self):
        stage_container_env(ContainerEnvKey.DOCKER_GID, "988")
        self.assertEqual(build_arg_flags(("DOCKER_GID",)),
                         ["--build-arg", "DOCKER_GID=988"])

    def test_unstaged_name_skipped(self):
        self.assertEqual(build_arg_flags(("DOCKER_GID",)), [])

    def test_glob_expands_against_staged_keys(self):
        _container_env.update({"INSTALL_GH": "1", "INSTALL_AWS": "0", "OTHER": "x"})
        flags = build_arg_flags(("INSTALL_*",))
        self.assertEqual(flags, ["--build-arg", "INSTALL_AWS=0", "--build-arg", "INSTALL_GH=1"])

    def test_glob_with_no_matches_is_empty(self):
        self.assertEqual(build_arg_flags(("INSTALL_*",)), [])


class TestEnvForwardFlags(ContainerEnvFixture):
    """env_forward_flags emits `-e NAME=VALUE` for the active tags'
    `[run] env_forward` names — the gating that keeps WHITELIST_ADDRESSES
    scoped to launches where the resolve actually ran."""

    def _fw(self):
        return DockerContribution(env_forward=("WHITELIST_ADDRESSES",))

    def test_staged_name_emitted(self):
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4:443")
        self.assertEqual(env_forward_flags([self._fw()]),
                         ["-e", "WHITELIST_ADDRESSES=1.2.3.4:443"])

    def test_unstaged_name_skipped(self):
        self.assertEqual(env_forward_flags([self._fw()]), [])

    def test_no_contributions_no_flags(self):
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4:443")
        self.assertEqual(env_forward_flags([]), [])

    def test_selftest_addr_forwards_alongside_whitelist(self):
        # The DNS-free self-test target rides the same {firewall} gate.
        contribution = DockerContribution(
            env_forward=("WHITELIST_ADDRESSES", "FIREWALL_SELFTEST_ADDR"))
        stage_container_env(ContainerEnvKey.WHITELIST_ADDRESSES, "1.2.3.4:443")
        stage_container_env(ContainerEnvKey.FIREWALL_SELFTEST_ADDR, "1.2.3.4")
        self.assertEqual(env_forward_flags([contribution]),
                         ["-e", "WHITELIST_ADDRESSES=1.2.3.4:443",
                          "-e", "FIREWALL_SELFTEST_ADDR=1.2.3.4"])


class TestEntrypointFlags(unittest.TestCase):
    def test_no_override_uses_image_entrypoint(self):
        self.assertEqual(entrypoint_chain([DockerContribution()]), ([], []))

    def test_one_override_becomes_the_entrypoint(self):
        flags, inner = entrypoint_chain([DockerContribution(entrypoint="firewall-entrypoint.sh")])
        self.assertEqual(flags, ["--entrypoint", "/usr/local/bin/firewall-entrypoint.sh"])
        self.assertEqual(inner, [])

    def test_an_absolute_path_is_used_as_is(self):
        flags, _ = entrypoint_chain([DockerContribution(entrypoint="/opt/custom/entry.sh")])
        self.assertEqual(flags, ["--entrypoint", "/opt/custom/entry.sh"])

    def test_two_overrides_CHAIN_instead_of_failing(self):
        # They used to be mutually exclusive. Several tags legitimately wrap the
        # agent — {firewall} applies iptables, {muxer} starts a multiplexer — so
        # the first becomes docker's entrypoint and the rest are its arguments,
        # each `exec "$@"`-ing the next.
        flags, inner = entrypoint_chain([
            DockerContribution(entrypoint="firewall-entrypoint.sh"),
            DockerContribution(entrypoint="/home/claude/.claude/muxer-start.sh")])
        self.assertEqual(flags, ["--entrypoint", "/usr/local/bin/firewall-entrypoint.sh"])
        self.assertEqual(inner, ["/home/claude/.claude/muxer-start.sh"])

    def test_the_firewall_wrapper_hands_off_generically(self):
        # A hardcoded `exec claude "$@"` cannot be followed by another wrapper,
        # which is exactly why {muxer} and {firewall} could not compose.
        text = (paths.AGENTS_DIR / "specialty" / "firewall"
                / "firewall-entrypoint.sh").read_text()
        code = [line for line in text.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
        self.assertIn('exec "$@"', code)
        self.assertFalse([line for line in code if "exec claude" in line])

    def test_firewall_sorts_before_muxer_in_the_real_registry(self):
        # Ordering is load-bearing: iptables must be applied, and `sudo -k` run,
        # before the agent starts. It comes from the contribution order rather
        # than an explicit priority, so it is pinned here.
        from launch.tags import AgentBuild, Instance, resolve_build, scan_all
        registry = scan_all(paths.AGENTS_DIR)
        inst = Instance(
            agent="refactorer", md_path=paths.AGENTS_DIR / "refactorer.md",
            session="s", workspace="/w", is_brand_new=False,
            **resolve_build(AgentBuild(engine=None, professions=(),
                                       specialties=("firewall", "muxer"),
                                       policies=()), "refactorer", registry))
        chain = [c.entrypoint for c in inst.docker_contributions if c.entrypoint]
        self.assertEqual(len(chain), 2)
        self.assertIn("firewall", chain[0])
        self.assertIn("muxer", chain[1])


class TestSetContainerMountsWorkspaceFallback(unittest.TestCase):
    """Regression: set_container_mounts must never try to bind-mount a None
    workspace. If inst_id.workspace is None (stale workspace-map entry that
    slipped past resolve_target's re-prompt), fall back to DEFAULT_WORKSPACE."""

    @staticmethod
    def _inst(**overrides):
        """Stand-in for Instance carrying only what set_container_mounts reads.
        Centralised so a new field on the real Instance costs one line here
        rather than one per test."""
        fields = {"workspace": "/w", "state_dir": Path("/tmp/state"),
                  "workspace_readonly": False, "is_cowork": False,
                  "instance": "poet__s"}
        return SimpleNamespace(**{**fields, **overrides})

    def _capture_mounts(self, inst_id):
        """Drive set_container_mounts through a patched add_docker_mount that
        records every (source, target) pair. Returns the list of pairs in
        call order. ensure_dir is patched out so no real directory is created."""
        recorded = []
        with patch("launch.docker_config.add_docker_mount", side_effect=lambda s, t: recorded.append((str(s), str(t)))), \
             patch("launch.docker_config.ensure_dir"):
            set_container_mounts(inst_id)
        return recorded

    def test_workspace_set_uses_provided_path(self):
        inst_id = self._inst(workspace="/some/host/path")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], "/some/host/path")

    def test_workspace_none_falls_back_to_default(self):
        inst_id = self._inst(workspace=None)
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_workspace_empty_string_falls_back_to_default(self):
        # `or` covers None AND empty string — both treated as "no workspace".
        inst_id = self._inst(workspace="")
        mounts = self._capture_mounts(inst_id)
        workspace_pair = next(p for p in mounts if p[1] == "/workspace")
        self.assertEqual(workspace_pair[0], str(paths.DEFAULT_WORKSPACE))

    def test_generated_settings_mounted_read_only(self):
        # The launcher-generated settings file shadows the state-dir's rw
        # view of ~/.claude/settings.json — the leash the agent can't undo.
        inst_id = self._inst()
        mounts = self._capture_mounts(inst_id)
        settings_pair = next(p for p in mounts if "settings.json" in p[1])
        self.assertEqual(settings_pair[0], "/tmp/state/settings.json")
        self.assertTrue(settings_pair[1].endswith(":ro"))

    def test_readonly_specialty_mounts_workspace_ro(self):
        # A workspace_readonly specialty ({ro}) makes the /workspace mount
        # :ro; the state dir stays writable (Claude Code writes history there).
        inst_id = self._inst(workspace_readonly=True)
        mounts = self._capture_mounts(inst_id)
        ws = next(p for p in mounts if p[1].startswith("/workspace"))
        self.assertEqual(ws, ("/w", "/workspace:ro"))
        state = next(p for p in mounts if p[1] == "/home/claude/.claude")  # CLAUDE_CONFIG_IN_CONTAINER
        self.assertFalse(state[1].endswith(":ro"))

    def test_workspace_read_write_by_default(self):
        inst_id = self._inst()
        mounts = self._capture_mounts(inst_id)
        self.assertIn(("/w", "/workspace"), mounts)


class TestCoworkMount(unittest.TestCase):
    """{cowork} bind-mounts the instance's group-hosting dir at /cowork. The
    mount and the `_cowork` Stop hook's hardcoded path are one mechanism: a hook
    writing to /cowork/outbox with no mount would write into the container's
    ephemeral layer instead of host-visible storage."""

    def _mounts(self, **overrides):
        recorded = []
        inst_id = TestSetContainerMountsWorkspaceFallback._inst(**overrides)
        with patch("launch.docker_config.add_docker_mount", side_effect=lambda s, t: recorded.append((str(s), str(t)))), \
             patch("launch.docker_config.ensure_dir"):
            set_container_mounts(inst_id)
        return recorded

    def test_cowork_instance_gets_the_mount(self):
        mounts = self._mounts(is_cowork=True, instance="poet__draft")
        pair = next(p for p in mounts if p[1] == str(paths.COWORK_IN_CONTAINER))
        self.assertEqual(pair[0], str(paths.cowork_dir_path("poet__draft")))

    def test_plain_instance_gets_no_cowork_mount(self):
        mounts = self._mounts(is_cowork=False)
        self.assertNotIn(str(paths.COWORK_IN_CONTAINER), [t for _, t in mounts])

    def test_cowork_mount_is_read_write(self):
        # The Stop hook writes captures there, so :ro would break capture.
        mounts = self._mounts(is_cowork=True)
        pair = next(p for p in mounts if p[1].startswith(str(paths.COWORK_IN_CONTAINER)))
        self.assertFalse(pair[1].endswith(":ro"))

    def test_source_dir_is_created_before_mounting(self):
        # docker would otherwise create a missing source as a root-owned dir
        # the container's unprivileged user could not write.
        inst_id = TestSetContainerMountsWorkspaceFallback._inst(is_cowork=True, instance="poet__x")
        with patch("launch.docker_config.add_docker_mount"), \
             patch("launch.docker_config.ensure_dir") as ensure:
            set_container_mounts(inst_id)
        ensure.assert_called_once_with(paths.cowork_dir_path("poet__x"))


class TestCoworkMountRealInstance(unittest.TestCase):
    """The same mount, driven by a REAL Instance rather than a stand-in — so
    `Instance.is_cowork` itself is under test, not just the branch it gates.
    Also pins the invariant that binds this mount to the `_cowork` fragment:
    the Stop-hook command's hardcoded path must sit under the mount target, or
    captures land in the container's ephemeral layer and vanish on exit."""

    @staticmethod
    def _instance(specialties):
        from launch.tags import AgentBuild, Instance, resolve_build, scan_all
        registry = scan_all(paths.AGENTS_DIR)
        build = AgentBuild(engine=None, professions=(), specialties=tuple(specialties), policies=())
        return Instance(agent="poet", md_path=Path("/fake/poet.md"), session="grp",
                        workspace="/w", is_brand_new=False,
                        **resolve_build(build, "poet", registry))

    def _mounts(self, specialties):
        recorded = []
        with patch("launch.docker_config.add_docker_mount", side_effect=lambda s, t: recorded.append((str(s), str(t)))), \
             patch("launch.docker_config.ensure_dir"):
            set_container_mounts(self._instance(specialties))
        return recorded

    def test_real_cowork_instance_reports_is_cowork(self):
        self.assertTrue(self._instance(["cowork"]).is_cowork)
        self.assertFalse(self._instance([]).is_cowork)

    def test_real_cowork_instance_gets_the_mount(self):
        targets = [t for _, t in self._mounts(["cowork"])]
        self.assertIn(str(paths.COWORK_IN_CONTAINER), targets)

    def test_real_plain_instance_does_not(self):
        targets = [t for _, t in self._mounts([])]
        self.assertNotIn(str(paths.COWORK_IN_CONTAINER), targets)

    def test_stop_hook_writes_under_the_mount_target(self):
        # The mount and the hook path are one mechanism; if this drifts,
        # captures are written somewhere the hub will never read.
        from launch.tags import scan_all
        frag = scan_all(paths.AGENTS_DIR).specialties["cowork"].load_fragment()
        command = frag["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(f"{paths.COWORK_IN_CONTAINER}/outbox", command)

    def test_fragment_permits_writing(self):
        # dontAsk makes `allow` exhaustive, so a manager that cannot Write
        # cannot merge an inbox — the flow would be impossible.
        from launch.tags import scan_all
        perms = scan_all(paths.AGENTS_DIR).specialties["cowork"].load_fragment()["permissions"]
        self.assertEqual(perms["defaultMode"], "dontAsk")
        self.assertLessEqual({"Write", "Edit"}, set(perms["allow"]))

    def test_fragment_allows_every_command_the_protocol_instructs(self):
        # The floor is defined by the addendum: it tells a coworker to copy an
        # inbox into its working copy and clear it, and a manager to review with
        # `diff -r`. Under dontAsk, a command missing here makes the protocol's
        # own instructions impossible to follow.
        from launch.tags import scan_all
        cowork = scan_all(paths.AGENTS_DIR).specialties["cowork"]
        allow = set(cowork.load_fragment()["permissions"]["allow"])
        self.assertLessEqual({"Bash(mkdir:*)", "Bash(cp:*)", "Bash(mv:*)",
                              "Bash(diff:*)"}, allow)
        # And the addendum really does instruct those operations — if it stops
        # saying so, this floor needs rejustifying rather than quietly standing.
        _, body = cowork.addendum
        self.assertIn("Copy what you take up", body)

    def test_fragment_omits_shell_twins_of_allowed_tools(self):
        # Dropped deliberately: Read/Glob/Grep are allowed, and the addendum's
        # last bullet tells agents to prefer them over shell equivalents — so
        # allowing ls/cat/grep/... contradicted our own advice and only padded
        # settings.json. `wc` is the kept exception (counting lines without
        # reading a whole file into context).
        from launch.tags import scan_all
        allow = set(scan_all(paths.AGENTS_DIR).specialties["cowork"]
                    .load_fragment()["permissions"]["allow"])
        for command in ("ls", "find", "grep", "rg", "cat", "head", "tail"):
            with self.subTest(command=command):
                self.assertNotIn(f"Bash({command}:*)", allow)
        self.assertLessEqual({"Read", "Glob", "Grep", "Bash(wc:*)"}, allow)


class TestAddDockerMountCollisions(unittest.TestCase):
    """add_docker_mount rejects conflicting duplicates at staging time. Two
    `-v` flags for one target make docker error out at run time with a
    message that names neither culprit; the source-keyed accumulator would
    silently *drop* a mount on same-source/new-target. Both now fail fast
    with a message naming the paths. Identical re-stages stay no-ops."""

    def setUp(self):
        docker_config._docker_mounts.clear()

    def tearDown(self):
        docker_config._docker_mounts.clear()

    def test_identical_restage_is_idempotent(self):
        docker_config.add_docker_mount("/src", "/tgt")
        docker_config.add_docker_mount("/src", "/tgt")
        self.assertEqual(docker_config._docker_mounts, {"/src": "/tgt"})

    def test_same_target_from_different_source_raises(self):
        docker_config.add_docker_mount("/src1", "/tgt")
        with self.assertRaises(RuntimeError) as ctx:
            docker_config.add_docker_mount("/src2", "/tgt")
        self.assertIn("/tgt", str(ctx.exception))
        self.assertIn("/src2", str(ctx.exception))

    def test_target_collision_ignores_access_mode_suffix(self):
        # `/x:ro` and `/x` are the same container path — still a collision.
        docker_config.add_docker_mount("/src1", "/tgt:ro")
        with self.assertRaises(RuntimeError):
            docker_config.add_docker_mount("/src2", "/tgt")

    def test_same_source_at_new_target_raises(self):
        # The dict is keyed by source — a second target for the same source
        # used to silently REPLACE the first mount. Now it's an error.
        docker_config.add_docker_mount("/src", "/tgt1")
        with self.assertRaises(RuntimeError):
            docker_config.add_docker_mount("/src", "/tgt2")
        self.assertEqual(docker_config._docker_mounts, {"/src": "/tgt1"})   # original intact

    def test_distinct_mounts_accumulate(self):
        docker_config.add_docker_mount("/a", "/x")
        docker_config.add_docker_mount("/b", "/y:ro")
        self.assertEqual(len(docker_config._docker_mounts), 2)

    def test_nested_targets_are_not_collisions(self):
        # /home/claude/.config and /home/claude/.config/.jira are different
        # mount points (docker nests them) — only exact-path matches collide.
        docker_config.add_docker_mount("/a", "/home/claude/.config")
        docker_config.add_docker_mount("/b", "/home/claude/.config/.jira")
        self.assertEqual(len(docker_config._docker_mounts), 2)


class TestPromptInstallFailuresDryRun(unittest.TestCase):
    """--dry-run builds nothing, so prompt_install_failures must not spin up
    a container: the only log it could read is a stale one from a previous
    real build, and `docker run` is a real side effect dry-run promises not
    to have. (Pre-fix, dry-run ran the read for real.)"""

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_dry_run_skips_the_docker_read(self):
        docker_config.set_dry_run(True)
        with patch("launch.docker_config.shell_capture") as mock_capture:
            docker_config.prompt_install_failures("claude-agents:code", "poet__x")
        mock_capture.assert_not_called()

    def test_real_run_reads_the_image_log(self):
        completed = SimpleNamespace(returncode=1, stdout="")   # rc!=0 → no log in image → silent return
        with patch("launch.docker_config.shell_capture", return_value=completed) as mock_capture:
            docker_config.prompt_install_failures("claude-agents:code", "poet__x")
        mock_capture.assert_called_once()


class TestMountTargetIsStaged(unittest.TestCase):
    """`mount_target_is_staged` underpins the home-overlay clash check —
    any prior mount with the same target makes the helper return True so
    `home_overlay_mounts` can refuse to shadow it."""

    def setUp(self):
        docker_config._docker_mounts.clear()

    def tearDown(self):
        docker_config._docker_mounts.clear()

    def test_returns_false_when_no_mounts(self):
        self.assertFalse(docker_config.mount_target_is_staged("/home/claude/.gitconfig"))

    def test_returns_true_for_exact_target(self):
        docker_config.add_docker_mount("/host/.bashrc", "/home/claude/.bashrc")
        self.assertTrue(docker_config.mount_target_is_staged("/home/claude/.bashrc"))

    def test_returns_false_for_unrelated_target(self):
        docker_config.add_docker_mount("/host/.bashrc", "/home/claude/.bashrc")
        self.assertFalse(docker_config.mount_target_is_staged("/home/claude/.gitconfig"))

    def test_ignores_access_mode_suffix(self):
        # Targets staged with `:ro` etc. should still match by the bare path.
        docker_config.add_docker_mount("/host/whitelist.txt", "/etc/whitelist.txt:ro")
        self.assertTrue(docker_config.mount_target_is_staged("/etc/whitelist.txt"))


# ============================================================
# Dry-run gating — moved from launch() into docker_compose_subprocess
# ============================================================
# Before this change, --dry-run early-returned from launch() before
# ensure_image / run_compose ever ran, leaving most of the orchestration
# unexercised by tests. The flag now sits on the module and only gates
# the actual `docker compose` invocation inside docker_compose_subprocess.
# Every test in this section asserts a path that was previously skipped on
# dry-run and is now reachable.

class TestSetDryRun(unittest.TestCase):
    """set_dry_run is the single point of write for the module-level flag.
    The setter exists (rather than callers poking `docker_config._dry_run`
    directly) so the read site stays a module-private and any future
    auditing of who flips the flag has one entry point to instrument."""

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_sets_flag_true(self):
        docker_config.set_dry_run(True)
        self.assertTrue(docker_config._dry_run)

    def test_resets_flag_false(self):
        docker_config.set_dry_run(True)
        docker_config.set_dry_run(False)
        self.assertFalse(docker_config._dry_run)


class TestDockerSubprocessDryRun(unittest.TestCase):
    """docker_subprocess gates its shell_returncode call on the module-level
    _dry_run flag. Real-run forwards to shell_returncode with the `docker`
    prefix; dry-run prints the would-be invocation and returns without
    touching subprocess."""

    def setUp(self):
        docker_config.set_dry_run(False)

    def tearDown(self):
        docker_config.set_dry_run(False)

    def test_dry_run_skips_shell_returncode(self):
        docker_config.set_dry_run(True)
        with patch("launch.docker_config.shell_returncode") as mock_run, \
             patch("builtins.print"):
            docker_config.docker_subprocess(["build", "--no-cache"])
        mock_run.assert_not_called()

    def test_real_run_invokes_shell_returncode_with_docker_prefix(self):
        with patch("launch.docker_config.shell_returncode", return_value=0) as mock_run:
            docker_config.docker_subprocess(["build"])
        mock_run.assert_called_once()
        positional = mock_run.call_args.args
        self.assertEqual(positional, ("docker", "build"))

    def test_dry_run_prints_would_invoke_line(self):
        docker_config.set_dry_run(True)
        with patch("builtins.print") as mock_print, \
             patch("launch.docker_config.shell_returncode"):
            docker_config.docker_subprocess(["run", "--rm", "img"])
        mock_print.assert_called_once()
        printed = mock_print.call_args.args[0]
        self.assertIn("dry-run", printed)
        self.assertIn("docker run --rm img", printed)


class TestEnsureImage(unittest.TestCase):
    """ensure_image drives one `docker build` per step (base + each
    build_steps entry), threading PARENT_IMAGE explicitly and returning the
    final tag. docker_subprocess no-ops internally on dry-run, so the loop
    runs identically in both modes."""

    def _inst(self, steps):
        from pathlib import Path as P
        return SimpleNamespace(build_steps=[
            (name, P(f"/fake/{name}/Dockerfile"), contribution)
            for name, contribution in steps
        ])

    def test_builds_base_plus_each_step(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            tag = docker_config.ensure_image(self._inst([("code", None), ("dood", None)]))
        self.assertEqual(mock_run.call_count, 3)
        self.assertEqual(tag, "claude-agents:code.dood")

    def test_bare_agent_builds_base_only(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            tag = docker_config.ensure_image(self._inst([]))
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(tag, "claude-agents:base")

    def test_parent_image_threads_between_steps(self):
        with patch("launch.docker_config.docker_subprocess") as mock_run, \
             patch("builtins.print"):
            docker_config.ensure_image(self._inst([("code", None), ("dood", None)]))
        args_code = mock_run.call_args_list[1].args[0]
        args_dood = mock_run.call_args_list[2].args[0]
        self.assertIn("PARENT_IMAGE=claude-agents:base", args_code)
        self.assertIn("PARENT_IMAGE=claude-agents:code", args_dood)

    def test_step_forwards_its_own_build_args(self):
        from launch.container_env import _container_env
        snapshot = dict(_container_env)
        _container_env.clear()
        _container_env["DOCKER_GID"] = "988"
        try:
            contribution = DockerContribution(build_arg_forward=("DOCKER_GID",))
            with patch("launch.docker_config.docker_subprocess") as mock_run, \
                 patch("builtins.print"):
                docker_config.ensure_image(self._inst([("code", None), ("dood", contribution)]))
            args_code = mock_run.call_args_list[1].args[0]
            args_dood = mock_run.call_args_list[2].args[0]
            self.assertNotIn("DOCKER_GID=988", args_code)   # code doesn't forward it
            self.assertIn("DOCKER_GID=988", args_dood)      # dood's tag.docker does
        finally:
            _container_env.clear()
            _container_env.update(snapshot)


class TestDockerStopSubprocess(unittest.TestCase):
    """docker_stop_subprocess — the `--stop` flow's one docker verb. Callers
    speak the prefix-STRIPPED id (the running-snapshot's spelling); the
    prefix is re-attached here and nowhere else. Short grace deliberately:
    a {muxer} entrypoint ignores SIGTERM, so every stop of one rides out the
    whole timeout before docker KILLs."""

    def test_argv_reattaches_the_prefix_and_success_is_rc_zero(self):
        # `-t` exactly: `--time` warns "deprecated" on newer docker
        # (operator-observed) and `--timeout` doesn't exist on the 20.10
        # floor — the short flag is the only spelling both accept silently.
        with patch("launch.docker_config.shell_returncode",
                   return_value=0) as run_:
            self.assertTrue(docker_config.docker_stop_subprocess("golem__a"))
        run_.assert_called_once_with(
            "docker", "stop", "-t", "3", "claude-code_golem__a")

    def test_nonzero_rc_reports_false(self):
        with patch("launch.docker_config.shell_returncode", return_value=1):
            self.assertFalse(docker_config.docker_stop_subprocess("x"))


class TestDockerRunningInstances(unittest.TestCase):
    """docker_running_instances_subprocess maps `docker ps` output back to
    instance ids. The prefix check is done in Python (docker's name filter is
    only a cheap pre-narrow), and 'couldn't determine' is None — distinct from
    'nothing running' — because the two callers want opposite failure behaviour."""

    def _probe(self, stdout="", returncode=0, exc=None):
        target = SimpleNamespace(returncode=returncode, stdout=stdout)
        kw = {"side_effect": exc} if exc else {"return_value": target}
        with patch.object(docker_config, "shell_capture", **kw):
            return docker_config.docker_running_instances_subprocess()

    def test_prefix_stripped_to_instance_ids(self):
        out = "claude-code_poet__draft\nclaude-code_golem__notes\n"
        self.assertEqual(self._probe(out), frozenset({"poet__draft", "golem__notes"}))

    def test_unrelated_containers_ignored(self):
        # docker's `--filter name=` is a substring match, so a user's own
        # container can appear in the listing — only true prefixes count.
        out = "claude-code_poet__draft\nmy-claude-code_sidecar\nredis\n"
        self.assertEqual(self._probe(out), frozenset({"poet__draft"}))

    def test_no_containers_is_empty_set_not_none(self):
        self.assertEqual(self._probe(""), frozenset())

    def test_failed_probe_is_none(self):
        self.assertIsNone(self._probe("", returncode=1))

    def test_docker_missing_from_path_is_none(self):
        # subprocess raises rather than returning non-zero when the binary is absent.
        self.assertIsNone(self._probe(exc=FileNotFoundError("docker")))

    def test_any_agent_running_stays_conservative_on_failure(self):
        # The cache-pruning guard must assume "might be running" when unknown.
        with patch.object(docker_config, "docker_running_instances_subprocess", return_value=None):
            self.assertTrue(docker_config.docker_check_any_agent_running_subprocess())
        with patch.object(docker_config, "docker_running_instances_subprocess", return_value=frozenset()):
            self.assertFalse(docker_config.docker_check_any_agent_running_subprocess())


class TestRunningInstanceReport(unittest.TestCase):
    """The single launch guard: refuses an instance that already has a live
    container, naming both the instance and the container."""

    def _report(self, running):
        with patch.object(docker_config, "docker_running_instances_subprocess", return_value=running):
            return docker_config.running_instance_report(_run_inst())

    def test_running_instance_reported(self):
        inst = _run_inst()
        report = self._report(frozenset({inst.instance}))
        self.assertIsNotNone(report)
        self.assertIn(inst.instance, report)
        self.assertIn(f"{docker_config.CONTAINER_NAME_PREFIX}{inst.instance}", report)

    def test_idle_instance_passes(self):
        self.assertIsNone(self._report(frozenset({"someone__else"})))

    def test_undeterminable_state_passes(self):
        # Can't tell → don't block; docker itself still refuses a name clash.
        self.assertIsNone(self._report(None))


class TestRunningClusterReport(unittest.TestCase):
    """The cluster twin of the launch guard, plus the container-id symmetry it
    rests on: run_cluster_container names with the prefix + cluster_container_id,
    the probe strips the prefix — so `cluster_container_id(session) in probe()`
    is exact by construction, and a test pins the two ends together."""

    def _report(self, running):
        with patch.object(docker_config, "docker_running_instances_subprocess",
                          return_value=running):
            return docker_config.running_cluster_report("team")

    def test_the_probe_returns_exactly_the_cluster_container_id(self):
        # A live cluster container as `docker ps` prints it, through the real
        # strip — the round trip the picker's and the guard's checks rely on.
        out = (f"{docker_config.CONTAINER_NAME_PREFIX}"
               f"{docker_config.cluster_container_id('team')}\n")
        with patch.object(docker_config, "shell_capture",
                          return_value=SimpleNamespace(returncode=0, stdout=out)):
            probed = docker_config.docker_running_instances_subprocess()
        self.assertEqual(probed, frozenset({docker_config.cluster_container_id("team")}))

    def test_running_cluster_reported_with_its_container_name(self):
        report = self._report(frozenset({"cluster-team"}))
        self.assertIsNotNone(report)
        self.assertIn("'team'", report)
        self.assertIn(f"{docker_config.CONTAINER_NAME_PREFIX}cluster-team", report)

    def test_idle_cluster_passes(self):
        self.assertIsNone(self._report(frozenset({"cluster-other", "poet__x"})))

    def test_undeterminable_state_passes(self):
        self.assertIsNone(self._report(None))

    def test_an_instance_can_never_shadow_a_cluster(self):
        # Instance ids are `<agent>__<session>`; the `cluster-` infix contains
        # a hyphen an agent name cannot carry before `__`, so a running
        # instance never trips the cluster guard.
        self.assertIsNone(self._report(frozenset({"cluster__team"})))


class TestRequireDocker(unittest.TestCase):
    """require_docker gates the launch: exits (verbosely, with the client
    version) when docker is absent or the daemon is unreachable, else returns."""

    def test_missing_from_path_exits(self):
        with patch.object(docker_config.shutil, "which", return_value=None), \
             self.assertRaises(SystemExit) as cm:
            docker_config.require_docker()
        self.assertIn("PATH", str(cm.exception))

    def test_daemon_down_exits_with_client_version(self):
        def fake_capture(*cmd, **kw):
            if cmd == ("docker", "version"):   # the daemon probe
                return SimpleNamespace(returncode=1, stdout="",
                                       stderr="Cannot connect to the Docker daemon.")
            return SimpleNamespace(returncode=0, stdout="24.0.7\n", stderr="")   # --format client
        with patch.object(docker_config.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(docker_config, "shell_capture", side_effect=fake_capture), \
             self.assertRaises(SystemExit) as cm:
            docker_config.require_docker()
        msg = str(cm.exception)
        self.assertIn("24.0.7", msg)            # concrete version number in the message
        self.assertIn("not responding", msg)

    def test_healthy_docker_returns(self):
        with patch.object(docker_config.shutil, "which", return_value="/usr/bin/docker"), \
             patch.object(docker_config, "shell_capture",
                          return_value=SimpleNamespace(returncode=0, stdout="", stderr="")):
            docker_config.require_docker()      # no raise


if __name__ == "__main__":
    unittest.main()


class TestWaitForFirewallApplied(unittest.TestCase):
    """The phase-2 updater's gate: don't insert rules until init-firewall.sh
    finished (its completion marker exists); bail when the container died
    without it; proceed best-effort on a timed-out-but-alive container."""

    def _wait(self, exec_returncodes, running, timeout=5):
        codes = iter(exec_returncodes)
        with patch.object(docker_config, "docker_exec_root_subprocess",
                          side_effect=lambda *a: SimpleNamespace(returncode=next(codes))) as ex, \
             patch.object(docker_config, "docker_check_running_subprocess", side_effect=running), \
             patch.object(docker_config.time, "sleep"):
            result = docker_config.wait_for_firewall_applied("claude-code_test", timeout_seconds=timeout)
        return result, ex

    def test_marker_present_immediately(self):
        result, ex = self._wait([0], running=[True])
        self.assertTrue(result)
        ex.assert_called_once()
        self.assertIn(str(paths.FIREWALL_DONE_IN_CONTAINER), ex.call_args.args)

    def test_marker_appears_after_polling(self):
        result, _ = self._wait([1, 1, 0], running=[True, True])
        self.assertTrue(result)

    def test_container_death_without_marker_bails(self):
        # init-firewall failed its self-test and took the container down —
        # the updater has nothing to update.
        result, _ = self._wait([1, 1], running=[True, False])
        self.assertFalse(result)

    def test_timeout_with_live_container_proceeds_best_effort(self):
        result, _ = self._wait([], running=[True], timeout=0)
        self.assertTrue(result)

    def test_timeout_with_dead_container_bails(self):
        result, _ = self._wait([], running=[False], timeout=0)
        self.assertFalse(result)

    def test_marker_path_matches_the_shell_script(self):
        # paths.FIREWALL_DONE_IN_CONTAINER mirrors a literal in
        # the {firewall} specialty's init-firewall.sh (shell can't import Python constants) —
        # this is the drift guard the constant's comment promises.
        script = (paths.INIT_FIREWALL_SH).read_text()
        self.assertIn(f"touch {paths.FIREWALL_DONE_IN_CONTAINER}", script)
