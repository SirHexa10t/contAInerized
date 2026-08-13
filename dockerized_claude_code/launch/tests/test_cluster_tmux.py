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


def sub(argv: tuple[str, ...]) -> str:
    """The tmux SUBCOMMAND, whatever the invocation prefix is.

    Indexing `argv[1]` broke twice — once when `-u` was added for UTF-8, once when
    `-L muxer` moved the session to its own socket. Deriving the offset from
    `tmux.TMUX` means the next prefix change breaks nothing here."""
    return argv[len(tmux.TMUX)]


def set_options(argv_list: tuple[tuple[str, ...], ...]) -> dict[str, str]:
    """`{option: value}` for every `set-option` in the sequence, found by position
    relative to the `-g` flag rather than by absolute index."""
    found = {}
    for argv in argv_list:
        if sub(argv) == "set-option":
            found[argv[-2]] = argv[-1]
    return found


def bindings(argv_list: tuple[tuple[str, ...], ...]) -> list[tuple[str, str, str]]:
    """`(table, key, command)` for every binding in the sequence.

    Chained invocations are split on their `;` separator elements, so a batched
    `bind-key … ; bind-key …` reports each of its bindings rather than only the
    first — which is what makes a "this key is not rebound" assertion mean
    something once batching exists."""
    found: list[tuple[str, str, str]] = []
    for argv in argv_list:
        if sub(argv) != "bind-key":
            continue
        chunk: list[str] = []
        for element in (*argv[len(tmux.TMUX):], ";"):
            if element != ";":
                chunk.append(element)
                continue
            if chunk:
                table_at = chunk.index("-T")
                found.append((chunk[table_at + 1], chunk[table_at + 2], chunk[-1]))
            chunk = []
    return found


def typethrough(argv_list: tuple[tuple[str, ...], ...]) -> dict[str, str]:
    """`{key: command}` for the copy-mode bindings that hand a keystroke back to
    the application."""
    return {key: command for table, key, command in bindings(argv_list)
            if table == tmux.COPY_TABLE and command.startswith("send -X cancel ;")}


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
        self.assertEqual(sub(self.argv[0]), "new-session")
        self.assertEqual(self.argv[0][len(tmux.TMUX):len(tmux.TMUX) + 5],
                         ("new-session", "-d", "-s", "poc", "-n"))
        self.assertEqual(sum(1 for a in self.argv if sub(a) == "new-session"), 1)

    def test_remaining_members_each_get_one_window_in_order(self):
        windows = [a[a.index("-n") + 1] for a in self.argv if sub(a) == "new-window"]
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
        scrubs = [a for a in self.argv if sub(a) == "set-environment"]
        self.assertEqual([a[-1] for a in scrubs], ["MODEL"])
        self.assertTrue(all("-u" in a[3:] for a in scrubs))

    def test_the_scrub_lands_between_the_session_and_the_later_windows(self):
        # Too early is impossible (no session yet); too late and a window created
        # before it still inherits.
        scrub_at = next(i for i, a in enumerate(self.argv) if sub(a) == "set-environment")
        self.assertEqual(sub(self.argv[0]), "new-session")
        first_window = next(i for i, a in enumerate(self.argv) if sub(a) == "new-window")
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
        self.assertFalse(any(sub(a) == "set-environment" for a in argv))

    def test_options_come_after_the_windows(self):
        # `set-option -t <session>` needs the session to exist.
        first_option = next(i for i, a in enumerate(self.argv) if sub(a) == "set-option")
        last_window = max(i for i, a in enumerate(self.argv)
                          if sub(a) in ("new-session", "new-window"))
        self.assertGreater(first_option, last_window)

    def test_it_selects_the_first_member_last(self):
        # Otherwise the user lands on whichever window was created last.
        self.assertEqual(self.argv[-1], (*tmux.TMUX, "select-window", "-t", "poc:first"))

    def test_attach_is_not_included(self):
        # It blocks, and the caller decides where it happens.
        self.assertFalse(any("attach-session" in a for a in self.argv))

    def test_a_dead_member_stays_visible(self):
        # remain-on-exit: a crashed member must look crashed, not un-started.
        self.assertEqual(set_options(self.argv)["remain-on-exit"], "on")

    def test_the_status_line_lists_members_and_reads_the_banner(self):
        options = set_options(self.argv)
        self.assertIn("#W", options["window-status-format"])
        self.assertIn("/cluster/banner", options["status-right"])
        self.assertIn("poc", options["status-left"])

    def test_a_missing_banner_file_does_not_print_an_error(self):
        # The status line is rendered every few seconds; a stderr leak there
        # would be permanent visual noise.
        options = set_options(self.argv)
        self.assertIn("2>/dev/null", options["status-right"])

    def test_banner_is_optional(self):
        argv = tmux.startup_argv("poc", self.panes, banner=None)
        options = set_options(argv)
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
                 if sub(a) in ("new-session", "new-window")]
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
        binding = next(a for a in argv if sub(a) == "bind-key")
        self.assertIn(tmux.KILL_KEY, binding)
        self.assertIn("confirm-before", binding)
        self.assertIn("kill-session", binding)

    def test_the_quit_and_help_keys_are_advertised_in_the_status_line(self):
        # A binding nobody can see is a binding nobody uses — and quit is the one
        # a tmux newcomer would otherwise have to guess.
        argv = tmux.startup_argv("poc", self.panes, banner=Path("/cluster/banner"))
        options = set_options(argv)
        # Spelled "shift-Q": read as lowercase `q`, users hit tmux's own
        # display-panes overlay instead (the red/blue 0/1 blocks in a bug report).
        self.assertIn(f"^b shift-{tmux.KILL_KEY} quit", options["status-right"])
        self.assertIn(f"^b {tmux.HELP_KEY} help", options["status-right"])

    def test_the_quit_binding_carries_a_note_so_tmux_help_lists_it(self):
        # `prefix ?` runs `list-keys -N`, which shows ONLY noted bindings — an
        # unnoted binding is absent from the very list meant to reveal it.
        argv = tmux.startup_argv("poc", self.panes)
        binding = next(a for a in argv if sub(a) == "bind-key")
        self.assertIn("-N", binding)
        self.assertEqual(binding[binding.index("-N") + 1], tmux.KILL_NOTE)

    def test_the_help_key_is_tmuxs_own_and_is_not_rebound(self):
        # `?` ships as list-keys -N; overriding it would break what a tmux user
        # already knows, so we only advertise it. Read through `bindings` rather
        # than by index: the type-through batch holds 95 bindings in one argv, and
        # indexing would only ever inspect the first of them.
        argv = tmux.startup_argv("poc", self.panes)
        prefix_keys = [key for table, key, _ in bindings(argv) if table == "prefix"]
        self.assertNotIn(tmux.HELP_KEY, prefix_keys)


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
        self.assertEqual(sum(1 for a in self.argv if sub(a) == "new-window"), 0)
        self.assertEqual(sum(1 for a in self.argv if sub(a) == "split-window"), 1)

    def test_the_split_is_stacked_not_side_by_side(self):
        # Claude Code's output is width-sensitive — code blocks and diffs wrap
        # badly in half a terminal — so the agent keeps the full width.
        split = next(a for a in self.argv if sub(a) == "split-window")
        self.assertIn("-v", split)
        self.assertNotIn("-h", split)

    def test_the_shell_gets_a_minority_of_the_height(self):
        split = next(a for a in self.argv if sub(a) == "split-window")
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
                         (*tmux.TMUX, "select-pane", "-t", "inst__proj:claude.0"))

    def test_both_panes_are_labelled_on_the_divider(self):
        titles = [a[a.index("-T") + 1] for a in self.argv
                  if sub(a) == "select-pane" and "-T" in a]
        self.assertEqual(titles, [tmux.AGENT_PANE, tmux.SHELL_WINDOW])
        options = set_options(self.argv)
        self.assertEqual(options["pane-border-status"], "top")

    def test_a_solo_instance_does_not_claim_to_be_a_cluster(self):
        # REGRESSION: the first render said "cluster:<name>" on a solo instance.
        options = set_options(self.argv)
        self.assertNotIn("cluster:", options["status-left"])
        self.assertIn("inst__proj", options["status-left"])

    def test_no_banner_by_default_so_the_bar_does_not_repeat_itself(self):
        # The first version showed the instance name at BOTH ends of the bar.
        options = set_options(self.argv)
        self.assertNotIn("cat", options["status-right"])
        self.assertIn(tmux.KILL_KEY, options["status-right"])

    def test_the_quit_binding_is_present_here_too(self):
        # It is the only way out now that quitting the agent no longer ends the
        # container.
        self.assertTrue(any(sub(a) == "bind-key" for a in self.argv))

    def test_the_agents_env_is_scrubbed_from_the_session_here_too(self):
        scrubs = [a[-1] for a in self.argv if sub(a) == "set-environment"]
        self.assertEqual(scrubs, ["ANTHROPIC_MODEL"])

    def test_the_session_lives_on_its_own_socket(self):
        # THE crash fix. Sharing tmux's default socket meant an agent's own
        # `tmux kill-server` — a normal thing to type while testing tmux — killed
        # the session hosting it and the container with it. Twice, to this author.
        self.assertEqual(tmux.TMUX, ("tmux", "-u", "-L", tmux.SOCKET))
        for argv in self.argv:
            with self.subTest(cmd=sub(argv)):
                self.assertEqual(argv[:len(tmux.TMUX)], tmux.TMUX)

    def test_the_agents_pane_has_TMUX_unset(self):
        # `$TMUX` points a bare `tmux` at the server owning the pane, so the named
        # socket alone would NOT protect the session — the variable has to go.
        session = next(a for a in self.argv if sub(a) == "new-session")
        self.assertIn("unset TMUX;", session[-1])
        self.assertIn("exec ", session[-1])

    def test_the_operators_shell_keeps_TMUX(self):
        # Asymmetric on purpose: a human in that pane should be able to drive
        # their own session without knowing the socket name.
        shell = next((a for a in self.argv if sub(a) == "split-window"), None)
        self.assertIsNotNone(shell)
        self.assertNotIn("unset TMUX", shell[-1])

    def test_the_reattach_hint_names_the_socket(self):
        # Without `-L`, the command we print finds no session at all.
        text = tmux.script("inst__proj", (a_pane("agent", cwd="/workspace"),),
                           shell_cwd=Path("/workspace"), solo=True)
        self.assertIn(f"tmux -L {tmux.SOCKET} attach", text)

    def test_the_deferred_resize_hook_is_a_runnable_command(self):
        # The hook body is a SHELL string, so it must name the binary, not a
        # Python tuple — see the repr regression in TestStartupArgv.
        hook = next(a[-1] for a in self.argv
                    if sub(a) == "set-hook" and "client-resized" in a)
        self.assertIn(f'{" ".join(tmux.TMUX)} resize-pane', hook)
        self.assertIn("sleep", hook)

    def test_the_label_carries_the_HOST_project_path(self):
        # The agent's cwd is `/workspace` — the container mount, identical for
        # every instance and so useless to the reader. The label must carry the
        # host path the operator recognises, which only the caller knows.
        options = set_options(self.argv)
        self.assertIn("inst__proj", options["status-left"])
        self.assertIn("/home/someone/code/thing", options["status-left"])
        self.assertNotIn("/workspace", options["status-left"])

    def test_without_a_label_it_is_just_the_instance_name(self):
        argv = tmux.solo_argv("inst__proj", self.agent, shell_cwd=Path("/workspace"))
        options = set_options(argv)
        self.assertNotIn("(", options["status-left"])

    def test_no_window_list_for_a_solo_instance(self):
        # One window, so a list would only repeat the label. It stays for
        # clusters, where it IS the member roster.
        options = set_options(self.argv)
        self.assertEqual(options["window-status-format"], "")
        self.assertEqual(options["window-status-current-format"], "")

    def test_help_is_a_curated_popup_listing_what_matters(self):
        # tmux's own `list-keys -N` is 85 entries — right, and useless to someone
        # who wants to know how to switch panes and leave.
        binding = next(a for a in self.argv
                       if sub(a) == "bind-key" and a[a.index("-T") + 2] == tmux.HELP_KEY)
        body = binding[-1]
        self.assertIn("display-popup", binding)
        for topic in ("move between the agent", "side by side", "new shell pane",
                      "scroll back", "detach", "quit", "full key list"):
            with self.subTest(topic=topic):
                self.assertIn(topic, body)

    def test_the_popup_text_is_printf_safe(self):
        # It is interpolated into `printf '...'`: a stray apostrophe would end the
        # quoting and a bare % would be read as a format specifier.
        body = next(a for a in self.argv if sub(a) == "bind-key"
                    and a[a.index("-T") + 2] == tmux.HELP_KEY)[-1]
        text = body.split("printf '", 1)[1]
        self.assertNotIn("'", text.rsplit("\n'", 1)[0])
        for fragment in text.split("%%"):
            self.assertNotIn("%", fragment)

    def test_tmuxs_full_list_stays_reachable_on_a_genuinely_free_key(self):
        # `/` and `-` LOOKED free until the key column was read from the right
        # field: `/` ships as describe-key, `-` as delete-buffer.
        keys = {a[a.index("-T") + 2]: a[-1] for a in self.argv if sub(a) == "bind-key"}
        # A POPUP, not a bare `list-keys`: that opens a window named `[tmux]`,
        # which is invisible in a status bar whose window list is blank for solo.
        self.assertIn("list-keys -N", keys[tmux.FULL_KEYS_KEY])
        self.assertNotEqual(tmux.FULL_KEYS_KEY, "/")

    def test_layout_keys_chain_as_one_argument(self):
        # REGRESSION: a bare ";" argv element ends `bind-key`, so tmux bound only
        # the layout change and ran the resize once at setup.
        for key in (tmux.STACK_KEY, tmux.SIDE_KEY):
            with self.subTest(key=key):
                value = next(a[-1] for a in self.argv if sub(a) == "bind-key"
                             and a[a.index("-T") + 2] == key)
                self.assertIn(";", value)
                self.assertIn("select-layout", value)
                self.assertIn("resize-pane", value)

    def test_layout_keys_avoid_braced_pane_targets(self):
        # REGRESSION: `{bottom}` collides with tmux's command-BLOCK syntax inside
        # a command string — it failed with "unknown command: bottom".
        for key in (tmux.STACK_KEY, tmux.SIDE_KEY):
            with self.subTest(key=key):
                value = next(a[-1] for a in self.argv if sub(a) == "bind-key"
                             and a[a.index("-T") + 2] == key)
                self.assertNotIn("{", value)

    def test_a_cluster_window_is_not_split(self):
        # Members need full height; the split is the SOLO affordance.
        argv = tmux.startup_argv("poc", (a_pane("m1"), a_pane("m2")),
                                 shell_cwd=Path("/workspace"))
        self.assertFalse(any(sub(a) == "split-window" for a in argv))
        self.assertFalse(any(sub(a) == "set-option" and a[-2] == "pane-border-status"
                             for a in argv))


class TestTypeThrough(unittest.TestCase):
    """Scrolling back must not swallow what the operator types.

    REPORTED FROM A LIVE SESSION: "when I scroll up through Claude Code's
    conversation history and then type something, the chars don't get typed into
    the screen unless I go down". Cause, confirmed on 3.5a: `mouse on` leaves
    tmux's wheel binding at `copy-mode -e`, so scrolling puts the pane in copy-mode
    — where keys are dispatched through a KEY TABLE instead of reaching the
    process. 81 of the 95 printable characters are unbound there and dropped
    silently; the other 14 do something unrelated. `-e` exits at the bottom, which
    is why scrolling down appeared to "fix" it.
    """

    def setUp(self):
        self.argv = tmux.solo_argv("inst__proj", a_pane("claude", cwd="/workspace"),
                                   shell_cwd=Path("/workspace"))

    def test_every_printable_character_reaches_the_application(self):
        # Stated independently of the module's own spelling helpers: 95 printable
        # ASCII characters, two of which tmux cannot take literally.
        plain = {chr(code) for code in range(0x21, 0x7F)} - {";"}
        self.assertEqual(set(typethrough(self.argv)), plain | {"Space", "\\;"})

    def test_it_cancels_the_view_before_delivering_the_key(self):
        # VERIFIED LIVE, not assumed: a pane in copy-mode given
        # `send -X cancel ; send-keys -l Z` left the mode AND received the Z. The
        # other order would feed the character to the mode it was meant to leave.
        for key, command in typethrough(self.argv).items():
            with self.subTest(key=key):
                self.assertTrue(command.startswith("send -X cancel ;"), command)
                self.assertIn("send-keys", command.split(";", 1)[1])

    def test_the_two_characters_tmux_cannot_take_literally(self):
        keys = typethrough(self.argv)
        # A space is the key NAME `Space`; an argument that looks like whitespace
        # is not a key, and `-l " "` is a whitespace-only argument.
        self.assertEqual(keys["Space"], "send -X cancel ; send-keys Space")
        # `;` must be ESCAPED in the key position: tmux reads a lone `;` argv
        # element as a command separator, and `bind-key` then fails with "too few
        # arguments". It was the only one of the 95 to fail.
        self.assertEqual(keys["\\;"], "send -X cancel ; send-keys -l ';'")

    def test_a_single_quote_is_the_one_character_quoted_differently(self):
        # Single quotes protect the other 94 from tmux's lexer; this one would
        # close its own quoting.
        self.assertEqual(typethrough(self.argv)["'"],
                         'send -X cancel ; send-keys -l "\'"')

    def test_it_is_one_invocation_because_95_would_cost_most_of_a_second(self):
        # MEASURED: 95 separate `bind-key` calls spend 0.72s on process spawning at
        # container start, every launch, for bindings that never differ.
        batched = [a for a in self.argv if sub(a) == "bind-key" and ";" in a]
        self.assertEqual(len(batched), 1)
        self.assertEqual(len(typethrough(self.argv)), 95)

    def test_both_shapes_get_it_because_the_fault_is_in_a_key_table(self):
        # A cluster member's pane swallows keystrokes exactly like a solo agent's:
        # the key table is server-global, so this is not a property of the layout.
        cluster = tmux.startup_argv("poc", (a_pane("m1"), a_pane("m2")))
        self.assertEqual(len(typethrough(cluster)), 95)

    def test_the_bindings_land_in_the_table_tmux_will_actually_use(self):
        # tmux picks the copy-mode table from $EDITOR / $VISUAL at server start,
        # so with `vi` in either it dispatches through `copy-mode-vi` and bindings
        # made on `copy-mode` would silently not apply. Pinning is what makes the
        # single table correct.
        self.assertEqual(set_options(self.argv)["mode-keys"], "emacs")
        tables = {table for table, _, _ in bindings(self.argv)
                  if table != "prefix"}
        self.assertEqual(tables, {tmux.COPY_TABLE})

    def test_the_popup_stops_promising_that_q_leaves_the_view(self):
        # `q` is now type-through, so the old line ("scroll back - press q to
        # leave") became wrong the moment these bindings landed. Escape still
        # cancels, and the popup is the only place a user is told either.
        body = next(command for table, key, command in bindings(self.argv)
                    if key == tmux.HELP_KEY)
        self.assertNotIn("press q", body)
        self.assertIn("Escape to leave", body)


class TestMouseCopy(unittest.TestCase):
    """REPORTED FROM A LIVE SESSION: "it seems like I can't mark the text with my
    mouse". Marking does work — `mouse on` binds a drag to `copy-mode -M` — but
    tmux's drag-end cancels the selection instantly and says nothing, so a copy
    that did happen is indistinguishable from one that did not."""

    def setUp(self):
        self.argv = tmux.solo_argv("inst__proj", a_pane("claude", cwd="/workspace"),
                                   shell_cwd=Path("/workspace"))

    def test_the_clipboard_capability_is_asserted_for_every_terminal(self):
        # tmux emits OSC 52 only when the CLIENT terminal's terminfo advertises
        # `Ms`, which varies with $TERM and with how current the terminfo database
        # is. Stating the feature removes that variable from the path.
        appended = [a for a in self.argv if sub(a) == "set-option" and "-as" in a]
        self.assertEqual([a[-2:] for a in appended],
                         [("terminal-features", ",*:clipboard")])

    def test_the_feature_is_appended_never_assigned(self):
        # terminal-features holds a LIST; assigning would drop tmux's own
        # per-terminal entries and take working capabilities with them.
        feature = next(a for a in self.argv
                       if sub(a) == "set-option" and "terminal-features" in a)
        self.assertIn("-as", feature)
        self.assertNotIn("-g", feature)

    def test_the_clipboard_option_is_stated_not_inherited(self):
        self.assertEqual(set_options(self.argv)["set-clipboard"], "on")

    def test_a_drag_copies_and_says_so(self):
        command = next(c for t, k, c in bindings(self.argv)
                       if k == "MouseDragEnd1Pane")
        self.assertIn("copy-pipe-and-cancel", command)
        self.assertIn("display-message", command)
        # It names the tmux-side paste key, because the terminal-side one may be
        # refused by the emulator and the operator needs a route that cannot be.
        self.assertIn("^b ]", command)

    def test_the_mouse_can_be_handed_to_the_terminal_and_taken_back(self):
        # The escape hatch for terminals that refuse OSC 52 writes, and the only
        # way to select ACROSS panes — tmux's selection is pane-aware by design.
        command = next(c for t, k, c in bindings(self.argv) if k == tmux.MOUSE_KEY)
        self.assertIn("set -g mouse ;", command)
        # VALUELESS on purpose: that is what makes tmux toggle the flag (verified
        # off → on → off). Pinning a value here would make the key one-way.
        self.assertNotRegex(command, r"set -g mouse (on|off)")
        self.assertIn("display-message", command)

    def test_the_toggle_reports_which_side_now_owns_the_mouse(self):
        # A mode switch with no feedback is a key nobody trusts twice.
        command = next(c for t, k, c in bindings(self.argv) if k == tmux.MOUSE_KEY)
        branches = command.split("#{?mouse,", 1)[1].rsplit("}", 1)[0]
        # tmux splits `#{?…}` on the FIRST comma, so a comma inside either branch
        # truncates the message at the point it would matter most.
        self.assertEqual(branches.count(","), 1)
        self.assertTrue(all(branches.split(",")))

    def test_the_toggle_carries_a_note_so_tmux_own_help_lists_it(self):
        binding = next(a for a in self.argv if sub(a) == "bind-key"
                       and tmux.MOUSE_KEY in a and "-N" in a)
        self.assertEqual(binding[binding.index("-N") + 1], tmux.MOUSE_NOTE)

    def test_the_mouse_key_is_advertised_where_a_newcomer_looks(self):
        body = next(c for t, k, c in bindings(self.argv) if k == tmux.HELP_KEY)
        self.assertIn(f"^b {tmux.MOUSE_KEY}", body)
        self.assertIn("drag", body)


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

    def test_the_type_through_batch_is_labelled_for_a_reader(self):
        # The script exists so a failed start can be READ; one 6KB line of
        # near-identical bindings would otherwise bury the four commands that
        # actually build the session.
        lines = tmux.script("poc", (a_pane(),)).splitlines()
        batch_at = next(i for i, line in enumerate(lines)
                        if line.count("bind-key") > 1)
        self.assertEqual(lines[batch_at - 1], tmux.TYPETHROUGH_LABEL)

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
