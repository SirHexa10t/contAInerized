"""Tests for launch.cluster.tmux and launch.cluster.launch_plan — the assembly.

tmux is not installed in every environment this suite runs in, and that is fine
BY DESIGN: the module assembles argv and never executes, so the rules worth
testing (ordering, quoting, target naming, host-vs-container paths) are all
checkable without the binary. What cannot be checked here is that tmux *accepts*
the flags — that rests on the researched documentation (`-e` per window,
`#(command)` in the status line) and needs a container to confirm.
"""

import shlex
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cluster import launch_plan, state, tmux
from launch.cluster.member import ClusterError, Member


def a_pane(name: str = "golem", command: tuple[str, ...] = ("claude",),
           cwd: str = "/workspaces/golem", **env: str) -> tmux.Pane:
    return tmux.Pane(name=name, command=command, cwd=Path(cwd), env=env)


class TestPane(unittest.TestCase):
    def test_env_flags_are_key_sorted(self):
        # Deterministic assembly: a dict's insertion order must not leak into
        # the command line, or a diff of two plans shows phantom changes.
        pane = a_pane(ZULU="1", ALPHA="2")
        self.assertEqual(pane.env_flags(), ("-e", "ALPHA=2", "-e", "ZULU=1"))

    def test_no_env_means_no_flags(self):
        self.assertEqual(a_pane().env_flags(), ())

    def test_command_is_shell_quoted_exactly_once(self):
        # tmux hands this string to a shell, so an argument containing a space
        # must survive as ONE argument.
        pane = a_pane(command=("claude", "--append-system-prompt", "be terse now"))
        self.assertEqual(shlex.split(pane.shell_command),
                         ["claude", "--append-system-prompt", "be terse now"])

    def test_window_name_is_validated(self):
        # ':' would make `-t session:name` address a different window.
        with self.assertRaises(ClusterError):
            a_pane(name="bad:name")

    def test_a_pane_needs_a_command(self):
        with self.assertRaises(ValueError):
            tmux.Pane(name="golem", command=(), cwd=Path("/tmp"))


class TestStartupArgv(unittest.TestCase):
    def setUp(self):
        self.panes = (a_pane("first", cwd="/workspaces/first", MODEL="opus"),
                      a_pane("second", cwd="/workspaces/second", MODEL="haiku"),
                      a_pane("third", cwd="/workspaces/third"))
        self.argv = tmux.startup_argv("poc", self.panes, banner=Path("/cluster/banner"))

    def test_the_first_member_arrives_via_new_session(self):
        # tmux cannot make an empty session, so member one is special whether we
        # like it or not — and it must not ALSO get a new-window.
        self.assertEqual(self.argv[0][:7],
                         ("tmux", "-u", "new-session", "-d", "-s", "poc", "-n"))
        self.assertEqual(sum(1 for a in self.argv if a[2] == "new-session"), 1)

    def test_remaining_members_each_get_one_window_in_order(self):
        windows = [a[a.index("-n") + 1] for a in self.argv if a[2] == "new-window"]
        self.assertEqual(windows, ["second", "third"])

    def test_every_member_gets_its_own_cwd_and_env(self):
        first = self.argv[0]
        self.assertIn("-c", first)
        self.assertEqual(first[first.index("-c") + 1], "/workspaces/first")
        self.assertIn("MODEL=opus", first)
        second = next(a for a in self.argv if "second" in a)
        self.assertIn("MODEL=haiku", second)
        self.assertNotIn("MODEL=opus", second)

    def test_the_first_members_env_is_scrubbed_from_the_session(self):
        # MEASURED REGRESSION (tmux 3.5a): `new-session -e` writes into the
        # SESSION environment, so any later window created without that variable
        # inherits the first member's value — the free shell window reported
        # `CLUSTER_MEMBER=<first member>`, i.e. the operator's shell claimed to be
        # a cluster member. Verified live that scrubbing afterwards leaves the
        # first member's already-exec'd process untouched.
        scrubs = [a for a in self.argv if a[2] == "set-environment"]
        self.assertEqual([a[-1] for a in scrubs], ["MODEL"])
        self.assertTrue(all("-u" in a[3:] for a in scrubs))

    def test_the_scrub_lands_between_the_session_and_the_later_windows(self):
        # Too early is impossible (no session yet); too late and a window created
        # before it still inherits.
        scrub_at = next(i for i, a in enumerate(self.argv) if a[2] == "set-environment")
        self.assertEqual(self.argv[0][2], "new-session")
        first_window = next(i for i, a in enumerate(self.argv) if a[2] == "new-window")
        self.assertLess(scrub_at, first_window)

    def test_hook_commands_are_shell_strings_not_python_reprs(self):
        # REGRESSION, found in a screenshot of a real launch: `{TMUX}` inside an
        # f-string wrote the tuple's repr — `('tmux', '-u') resize-pane …` — into
        # the hook, so every resize failed with exit 2 and tmux printed the error
        # into the agent's pane. Argv elements unpack; SHELL strings must join.
        for argv in self.argv:
            for element in argv:
                with self.subTest(element=element):
                    self.assertNotIn("('tmux'", element)
                    self.assertNotIn('"tmux"', element)

    def test_no_scrub_when_the_first_member_has_no_env(self):
        argv = tmux.startup_argv("poc", (a_pane("solo"),))
        self.assertFalse(any(a[2] == "set-environment" for a in argv))

    def test_options_come_after_the_windows(self):
        # `set-option -t <session>` needs the session to exist.
        first_option = next(i for i, a in enumerate(self.argv) if a[2] == "set-option")
        last_window = max(i for i, a in enumerate(self.argv)
                          if a[2] in ("new-session", "new-window"))
        self.assertGreater(first_option, last_window)

    def test_it_selects_the_first_member_last(self):
        # Otherwise the user lands on whichever window was created last.
        self.assertEqual(self.argv[-1],
                         ("tmux", "-u", "select-window", "-t", "poc:first"))

    def test_attach_is_not_included(self):
        # It blocks, and the caller decides where it happens.
        self.assertFalse(any("attach-session" in a for a in self.argv))

    def test_a_dead_member_stays_visible(self):
        # remain-on-exit: a crashed member must look crashed, not un-started.
        self.assertIn(("tmux", "-u", "set-option", "-t", "poc", "-g",
                       "remain-on-exit", "on"), self.argv)

    def test_the_status_line_lists_members_and_reads_the_banner(self):
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertIn("#W", options["window-status-format"])
        self.assertIn("/cluster/banner", options["status-right"])
        self.assertIn("poc", options["status-left"])

    def test_a_missing_banner_file_does_not_print_an_error(self):
        # The status line is rendered every few seconds; a stderr leak there
        # would be permanent visual noise.
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertIn("2>/dev/null", options["status-right"])

    def test_banner_is_optional(self):
        argv = tmux.startup_argv("poc", self.panes, banner=None)
        options = {a[6]: a[7] for a in argv if a[2] == "set-option"}
        self.assertNotIn("cat", options["status-right"])

    def test_no_members_is_refused(self):
        with self.assertRaises(ValueError):
            tmux.startup_argv("poc", ())

    def test_session_name_is_validated(self):
        with self.assertRaises(ClusterError):
            tmux.startup_argv("bad:name", self.panes)


class TestFreeShellAndQuit(unittest.TestCase):
    """`{muxer}`'s two operator affordances: a window the agent does not own, and
    one deliberate way out."""

    def setUp(self):
        self.panes = (a_pane("member-one"), a_pane("member-two"))

    def test_the_shell_window_is_added_last(self):
        # Members first, so window order still follows the template and the user
        # lands on a member rather than a shell.
        argv = tmux.startup_argv("poc", self.panes, shell_cwd=Path("/workspace"))
        names = [a[a.index("-n") + 1] for a in argv
                 if a[2] in ("new-session", "new-window")]
        self.assertEqual(names, ["member-one", "member-two", tmux.SHELL_WINDOW])

    def test_the_shell_is_a_login_shell(self):
        # So the operator's own bashrc/aliases apply — "tune bashrc properly" is
        # one of the reasons this window exists.
        argv = tmux.startup_argv("poc", self.panes, shell_cwd=Path("/workspace"))
        shell = next(a for a in argv if tmux.SHELL_WINDOW in a)
        self.assertIn("-l", shell[-1])

    def test_no_shell_when_none_is_asked_for(self):
        argv = tmux.startup_argv("poc", self.panes)
        self.assertFalse(any(tmux.SHELL_WINDOW in a for a in argv))

    def test_the_first_member_is_still_selected_with_a_shell_present(self):
        argv = tmux.startup_argv("poc", self.panes, shell_cwd=Path("/workspace"))
        self.assertEqual(argv[-1][-1], "poc:member-one")

    def test_a_quit_binding_exists_and_confirms_first(self):
        # remain-on-exit means windows LINGER when a member dies, so the session
        # never ends by itself — this binding is the only clean way out, and it
        # kills every member at once, hence the confirmation.
        argv = tmux.startup_argv("poc", self.panes)
        binding = next(a for a in argv if a[2] == "bind-key")
        self.assertIn(tmux.KILL_KEY, binding)
        self.assertIn("confirm-before", binding)
        self.assertIn("kill-session", binding)

    def test_the_quit_and_help_keys_are_advertised_in_the_status_line(self):
        # A binding nobody can see is a binding nobody uses — and quit is the one
        # a tmux newcomer would otherwise have to guess.
        argv = tmux.startup_argv("poc", self.panes, banner=Path("/cluster/banner"))
        options = {a[6]: a[7] for a in argv if a[2] == "set-option"}
        # Spelled "shift-Q": read as lowercase `q`, users hit tmux's own
        # display-panes overlay instead (the red/blue 0/1 blocks in a bug report).
        self.assertIn(f"^b shift-{tmux.KILL_KEY} quit", options["status-right"])
        self.assertIn(f"^b {tmux.HELP_KEY} help", options["status-right"])

    def test_the_quit_binding_carries_a_note_so_tmux_help_lists_it(self):
        # `prefix ?` runs `list-keys -N`, which shows ONLY noted bindings — an
        # unnoted binding is absent from the very list meant to reveal it.
        argv = tmux.startup_argv("poc", self.panes)
        binding = next(a for a in argv if a[2] == "bind-key")
        self.assertIn("-N", binding)
        self.assertEqual(binding[binding.index("-N") + 1], tmux.KILL_NOTE)

    def test_the_help_key_is_tmuxs_own_and_is_not_rebound(self):
        # `?` ships as list-keys -N; overriding it would break what a tmux user
        # already knows, so we only advertise it.
        argv = tmux.startup_argv("poc", self.panes)
        bound = [a[a.index("-T") + 2] for a in argv if a[2] == "bind-key"]
        self.assertNotIn(tmux.HELP_KEY, bound)


class TestSoloSplit(unittest.TestCase):
    """A SOLO `{muxer}` instance: one window, agent on top, free shell beneath.
    Rendered and eyeballed live against tmux 3.5a before these were written."""

    def setUp(self):
        self.agent = a_pane("claude", cwd="/workspace", ANTHROPIC_MODEL="opus")
        self.argv = tmux.solo_argv("inst__proj", self.agent,
                                   shell_cwd=Path("/workspace"),
                                   project_label="/home/someone/code/thing")

    def test_one_window_two_panes(self):
        # Not two windows: the point is seeing both at once.
        self.assertEqual(sum(1 for a in self.argv if a[2] == "new-window"), 0)
        self.assertEqual(sum(1 for a in self.argv if a[2] == "split-window"), 1)

    def test_the_split_is_stacked_not_side_by_side(self):
        # Claude Code's output is width-sensitive — code blocks and diffs wrap
        # badly in half a terminal — so the agent keeps the full width.
        split = next(a for a in self.argv if a[2] == "split-window")
        self.assertIn("-v", split)
        self.assertNotIn("-h", split)

    def test_the_shell_gets_a_minority_of_the_height(self):
        split = next(a for a in self.argv if a[2] == "split-window")
        percent = int(split[split.index("-l") + 1].rstrip("%"))
        self.assertLess(percent, 50)

    def test_an_absurd_percentage_is_refused(self):
        for bad in (0, 91, -5):
            with self.subTest(percent=bad), self.assertRaises(ValueError):
                tmux.solo_argv("inst__proj", self.agent,
                               shell_cwd=Path("/tmp"), shell_percent=bad)

    def test_focus_lands_on_the_agent_not_the_shell(self):
        # The shell is created last, so without this the user would be typing
        # into bash when the session opens.
        self.assertEqual(self.argv[-1],
                         ("tmux", "-u", "select-pane", "-t", "inst__proj:claude.0"))

    def test_both_panes_are_labelled_on_the_divider(self):
        titles = [a[a.index("-T") + 1] for a in self.argv
                  if a[2] == "select-pane" and "-T" in a]
        self.assertEqual(titles, [tmux.AGENT_PANE, tmux.SHELL_WINDOW])
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertEqual(options["pane-border-status"], "top")

    def test_a_solo_instance_does_not_claim_to_be_a_cluster(self):
        # REGRESSION: the first render said "cluster:<name>" on a solo instance.
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertNotIn("cluster:", options["status-left"])
        self.assertIn("inst__proj", options["status-left"])

    def test_no_banner_by_default_so_the_bar_does_not_repeat_itself(self):
        # The first version showed the instance name at BOTH ends of the bar.
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertNotIn("cat", options["status-right"])
        self.assertIn(tmux.KILL_KEY, options["status-right"])

    def test_the_quit_binding_is_present_here_too(self):
        # It is the only way out now that quitting the agent no longer ends the
        # container.
        self.assertTrue(any(a[2] == "bind-key" for a in self.argv))

    def test_the_agents_env_is_scrubbed_from_the_session_here_too(self):
        scrubs = [a[-1] for a in self.argv if a[2] == "set-environment"]
        self.assertEqual(scrubs, ["ANTHROPIC_MODEL"])

    def test_the_deferred_resize_hook_is_a_runnable_command(self):
        # The hook body is a SHELL string, so it must name the binary, not a
        # Python tuple — see the repr regression in TestStartupArgv.
        hook = next(a[-1] for a in self.argv
                    if a[2] == "set-hook" and "client-resized" in a)
        self.assertIn("tmux -u resize-pane", hook)
        self.assertIn("sleep", hook)

    def test_the_label_carries_the_HOST_project_path(self):
        # The agent's cwd is `/workspace` — the container mount, identical for
        # every instance and so useless to the reader. The label must carry the
        # host path the operator recognises, which only the caller knows.
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertIn("inst__proj", options["status-left"])
        self.assertIn("/home/someone/code/thing", options["status-left"])
        self.assertNotIn("/workspace", options["status-left"])

    def test_without_a_label_it_is_just_the_instance_name(self):
        argv = tmux.solo_argv("inst__proj", self.agent, shell_cwd=Path("/workspace"))
        options = {a[6]: a[7] for a in argv if a[2] == "set-option"}
        self.assertNotIn("(", options["status-left"])

    def test_no_window_list_for_a_solo_instance(self):
        # One window, so a list would only repeat the label. It stays for
        # clusters, where it IS the member roster.
        options = {a[6]: a[7] for a in self.argv if a[2] == "set-option"}
        self.assertEqual(options["window-status-format"], "")
        self.assertEqual(options["window-status-current-format"], "")

    def test_help_is_a_curated_popup_listing_what_matters(self):
        # tmux's own `list-keys -N` is 85 entries — right, and useless to someone
        # who wants to know how to switch panes and leave.
        binding = next(a for a in self.argv
                       if a[2] == "bind-key" and a[a.index("-T") + 2] == tmux.HELP_KEY)
        body = binding[-1]
        self.assertIn("display-popup", binding)
        for topic in ("move between the agent", "side by side", "new shell pane",
                      "scroll back", "detach", "quit", "full key list"):
            with self.subTest(topic=topic):
                self.assertIn(topic, body)

    def test_the_popup_text_is_printf_safe(self):
        # It is interpolated into `printf '...'`: a stray apostrophe would end the
        # quoting and a bare % would be read as a format specifier.
        body = next(a for a in self.argv if a[2] == "bind-key"
                    and a[a.index("-T") + 2] == tmux.HELP_KEY)[-1]
        text = body.split("printf '", 1)[1]
        self.assertNotIn("'", text.rsplit("\n'", 1)[0])
        for fragment in text.split("%%"):
            self.assertNotIn("%", fragment)

    def test_tmuxs_full_list_stays_reachable_on_a_genuinely_free_key(self):
        # `/` and `-` LOOKED free until the key column was read from the right
        # field: `/` ships as describe-key, `-` as delete-buffer.
        keys = {a[a.index("-T") + 2]: a[-1] for a in self.argv if a[2] == "bind-key"}
        # A POPUP, not a bare `list-keys`: that opens a window named `[tmux]`,
        # which is invisible in a status bar whose window list is blank for solo.
        self.assertIn("list-keys -N", keys[tmux.FULL_KEYS_KEY])
        self.assertNotEqual(tmux.FULL_KEYS_KEY, "/")

    def test_layout_keys_chain_as_one_argument(self):
        # REGRESSION: a bare ";" argv element ends `bind-key`, so tmux bound only
        # the layout change and ran the resize once at setup.
        for key in (tmux.STACK_KEY, tmux.SIDE_KEY):
            with self.subTest(key=key):
                value = next(a[-1] for a in self.argv if a[2] == "bind-key"
                             and a[a.index("-T") + 2] == key)
                self.assertIn(";", value)
                self.assertIn("select-layout", value)
                self.assertIn("resize-pane", value)

    def test_layout_keys_avoid_braced_pane_targets(self):
        # REGRESSION: `{bottom}` collides with tmux's command-BLOCK syntax inside
        # a command string — it failed with "unknown command: bottom".
        for key in (tmux.STACK_KEY, tmux.SIDE_KEY):
            with self.subTest(key=key):
                value = next(a[-1] for a in self.argv if a[2] == "bind-key"
                             and a[a.index("-T") + 2] == key)
                self.assertNotIn("{", value)

    def test_a_cluster_window_is_not_split(self):
        # Members need full height; the split is the SOLO affordance.
        argv = tmux.startup_argv("poc", (a_pane("m1"), a_pane("m2")),
                                 shell_cwd=Path("/workspace"))
        self.assertFalse(any(a[2] == "split-window" for a in argv))
        self.assertFalse(any(a[2] == "set-option" and a[6] == "pane-border-status"
                             for a in argv))


class TestScript(unittest.TestCase):
    def test_it_is_a_runnable_shell_script_that_fails_loudly(self):
        text = tmux.script("poc", (a_pane(),))
        self.assertTrue(text.startswith("#!/bin/sh"))
        # A half-built session is worse than a failed launch.
        self.assertIn("set -eu", text)
        self.assertIn("attach-session", text)

    def test_unset_env_lines_come_before_any_member_starts(self):
        # The kill-switch var is STICKY (=0 still disables), so it must be unset
        # before `claude` runs — and once, not per window.
        text = tmux.script("poc", (a_pane(),),
                           unset_env=("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",))
        lines = text.splitlines()
        unset_at = next(i for i, line in enumerate(lines) if line.startswith("unset "))
        first_tmux = next(i for i, line in enumerate(lines) if line.startswith("tmux "))
        self.assertLess(unset_at, first_tmux)

    def test_no_unset_by_default(self):
        # PoC-0 has no messaging, so the image's privacy posture is untouched.
        self.assertNotIn("unset ", tmux.script("poc", (a_pane(),)))

    def test_every_line_is_shell_safe(self):
        # The script is generated, so quoting bugs would be invisible until a
        # container failed to start.
        text = tmux.script("poc", (a_pane(command=("claude", "--flag", "a b")),))
        for line in text.splitlines():
            if line.startswith("tmux "):
                shlex.split(line)      # raises on unbalanced quoting


class TestSoloScript(unittest.TestCase):
    """The generated script IS the container's entrypoint (PID 1), which makes its
    tail load-bearing in a way the cluster script's is not."""

    def script(self, **kw):
        return tmux.script("inst__proj", (a_pane("agent", cwd="/workspace"),),
                           shell_cwd=Path("/workspace"), solo=True, **kw)

    def test_it_builds_the_split_not_the_cluster_shape(self):
        text = self.script()
        self.assertIn("split-window", text)
        self.assertIn("pane-border-status", text)

    def test_detach_does_not_kill_the_container(self):
        # PID 1 exiting stops the container and takes the tmux server with it, so
        # a bare `attach` would make `prefix d` destructive — the opposite of what
        # the tag promises. It must WAIT while the session lives.
        text = self.script()
        self.assertIn("has-session", text)
        attach_at = text.index("attach-session")
        self.assertGreater(text.index("has-session"), attach_at)

    def test_it_tells_the_user_how_to_get_back_in(self):
        text = self.script()
        self.assertIn("re-attach with", text)
        self.assertIn("docker exec", text)

    def test_the_label_uses_the_host_path_it_is_given(self):
        text = self.script(project_label="/home/someone/code/thing")
        self.assertIn("/home/someone/code/thing", text)

    def test_the_label_length_is_sized_to_the_label(self):
        # tmux TRUNCATES silently, so a fixed 40 made a long host path look like a
        # wrong label rather than a clipped one.
        long = "/home/someone/very/deeply/nested/project/directory"
        text = self.script(project_label=long)
        length = int(next(line for line in text.splitlines()
                          if "status-left-length" in line).split()[-1])
        self.assertGreater(length, len(long))

    def test_solo_mode_refuses_a_multi_pane_call(self):
        with self.assertRaises(ValueError):
            tmux.script("inst__proj", (a_pane("a"), a_pane("b")),
                        shell_cwd=Path("/workspace"), solo=True)

    def test_solo_mode_needs_a_shell_cwd(self):
        with self.assertRaises(ValueError):
            tmux.script("inst__proj", (a_pane("agent"),), solo=True)


class TestBannerText(unittest.TestCase):
    def test_it_names_the_count_and_project(self):
        text = tmux.banner_text(("a", "b"), project="/tmp/p")
        self.assertIn("2 member(s)", text)
        self.assertIn("/tmp/p", text)

    def test_project_is_optional(self):
        self.assertEqual(tmux.banner_text(("a",)), "1 member(s)")


class TestLaunchPlan(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        patcher = patch.object(paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.cluster = state.from_template(
            "poc", Path("/tmp/project"),
            (Member.of("refactorer"), Member.of("researcher", "primary")))

    def test_shared_is_the_default_and_every_member_sees_one_project(self):
        # PoC-0's simplification: no parallel file work, so no per-member
        # checkout. Every member starts in the one project, as a solo instance
        # does.
        plan = launch_plan.build(self.cluster)
        self.assertEqual({str(m.container_cwd) for m in plan.members},
                         {"/workspace"})
        self.assertFalse(plan.personal_workspaces)

    def test_personal_workspaces_give_each_member_its_own_cwd(self):
        # The cohabitation asymmetry: one container cannot mount two trees at one
        # point, so the PATH differs per member while the mount point does not.
        plan = launch_plan.build(self.cluster, personal_workspaces=True)
        self.assertEqual([str(m.container_cwd) for m in plan.members],
                         ["/workspaces/refactorer", "/workspaces/researcher__primary"])
        self.assertTrue(plan.personal_workspaces)

    def test_members_learn_which_member_they_are(self):
        # A cohabiting agent shares its persona with siblings and `hostname` is
        # the container's, so without this it cannot identify itself.
        plan = launch_plan.build(self.cluster)
        env = plan.members[1].env
        self.assertEqual(env["CLUSTER_MEMBER"], "researcher__primary")
        self.assertEqual(env["CLUSTER_ROLE"], "primary")
        self.assertEqual(env["CLUSTER_SESSION"], "poc")

    def test_injected_env_is_merged_and_cluster_vars_win(self):
        plan = launch_plan.build(
            self.cluster, env_for={"refactorer": {"ANTHROPIC_MODEL": "opus",
                                                  "CLUSTER_MEMBER": "spoofed"}})
        env = plan.members[0].env
        self.assertEqual(env["ANTHROPIC_MODEL"], "opus")
        self.assertEqual(env["CLUSTER_MEMBER"], "refactorer")

    def test_a_member_with_no_injected_env_still_works(self):
        plan = launch_plan.build(self.cluster, env_for={})
        self.assertIn("CLUSTER_MEMBER", plan.members[0].env)

    def test_panes_follow_member_order(self):
        plan = launch_plan.build(self.cluster)
        self.assertEqual([p.name for p in plan.panes()],
                         ["refactorer", "researcher__primary"])

    def test_shared_mode_mounts_the_project_itself(self):
        plan = launch_plan.build(self.cluster)
        self.assertEqual(plan.mounts(),
                         {Path("/tmp/project"): "/workspace",
                          paths.cluster_path("poc"): "/cluster"})

    def test_personal_mode_mounts_the_worktrees_dir_instead(self):
        plan = launch_plan.build(self.cluster, personal_workspaces=True)
        self.assertEqual(set(plan.mounts().values()), {"/workspaces", "/cluster"})
        # The project is NOT mounted directly — members reach it through their
        # own checkouts, which is the whole point of the mode.
        self.assertNotIn(Path("/tmp/project"), plan.mounts())

    def test_the_banner_is_referenced_by_its_CONTAINER_path(self):
        # Regression: the first version pointed tmux at the HOST path, so the
        # status line `cat`ted a file that does not exist in the container and
        # silently rendered empty. Same class of bug as cowork's review command.
        plan = launch_plan.build(self.cluster)
        self.assertEqual(plan.container_banner, Path("/cluster/banner"))
        self.assertFalse(str(plan.container_banner).startswith(str(paths.AGENTS_STATE)))

    def test_the_free_shell_opens_at_the_project_not_inside_a_member(self):
        # The operator's shell belongs to the cluster, not to one member; opening
        # it in a member's tree would imply an ownership it does not have.
        plan = launch_plan.build(self.cluster)
        self.assertEqual(plan.container_shell_cwd, Path("/workspace"))
        personal = launch_plan.build(self.cluster, personal_workspaces=True)
        self.assertEqual(personal.container_shell_cwd, Path("/workspaces"))

    def test_the_container_banner_shares_the_host_filename(self):
        # Derived, not a second literal — so renaming the host file cannot leave
        # the status line pointing at the old name.
        plan = launch_plan.build(self.cluster)
        self.assertEqual(plan.container_banner.name,
                         paths.cluster_banner_path("poc").name)


if __name__ == "__main__":
    unittest.main()
