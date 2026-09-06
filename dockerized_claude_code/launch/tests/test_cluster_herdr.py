"""Tests for launch.cluster.herdr — the agent-native backend's assembly.

Every rule here mirrors a fact from the live probe (2026-08-29, v0.8.2 in a
launcher container — recorded in the module docstring and the plan): the
server must exist before the CLI speaks, per-tab env rides `--env`, and a
claude member must go through `agent start` or herdr never detects it. The
generated text is assembly, so like tmux.py it stays testable with no herdr
binary installed.
"""

import unittest
from pathlib import Path

from launch.cluster import herdr
from launch.cluster.panes import Pane


def a_pane(name: str = "golem", command: tuple[str, ...] = ("claude",),
           **env: str) -> Pane:
    return Pane(name=name, command=command, cwd=Path("/workspace"), env=env)


class TestHerdrScript(unittest.TestCase):
    def script(self, *panes: Pane, **kw) -> str:
        return herdr.script("team", panes or (a_pane(),),
                            shell_cwd=Path("/workspace"), **kw)

    def test_it_is_a_loud_failing_shell_script(self):
        text = self.script()
        self.assertTrue(text.startswith("#!/bin/sh"))
        self.assertIn("set -eu", text)

    def test_the_server_comes_up_before_anything_speaks_to_it(self):
        # The CLI talks to the server's socket; racing it loses. The poll has
        # a ceiling so a server that never binds FAILS the launch rather than
        # assembling into the void.
        text = self.script()
        server_at = text.index("herdr server >/dev/null")
        ready_at = text.index("until herdr status server")
        workspace_at = text.index("herdr workspace create")
        self.assertLess(server_at, ready_at)
        self.assertLess(ready_at, workspace_at)
        self.assertIn("did not come up", text)

    def test_liveness_is_the_status_line_never_the_exit_code(self):
        # MEASURED: `herdr status server` exits 0 whether or not the server
        # runs — it reports, it doesn't probe. Exit-code loops made the
        # readiness gate a no-op and the post-stop container IMMORTAL; both
        # loops must grep the printed status. Found by executing the generated
        # script against the real binary, not by reading it.
        text = self.script()
        self.assertEqual(text.count('grep -q "status: running"'), 2)
        self.assertNotRegex(text, r"status server >/dev/null 2>&1; do")

    def test_unset_and_setup_precede_the_server(self):
        # Same contract as the tmux script: the kill-switch unset and the
        # sessions-symlink plumbing must exist before any member process.
        text = self.script(unset_env=("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",),
                           setup_commands=("mkdir -p /cluster/sessions",))
        self.assertLess(text.index("unset CLAUDE_CODE"),
                        text.index("mkdir -p /cluster/sessions"))
        self.assertLess(text.index("mkdir -p /cluster/sessions"),
                        text.index("herdr server"))

    def test_the_shell_tab_is_created_LAST_so_it_sits_rightmost(self):
        # herdr has no `tab move` (v0.8.2: list/create/get/focus/rename/
        # close), so creation order IS tab order — the shell goes last
        # (operator request, 2026-09-02). It used to be the workspace's root
        # tab, where herdr's default label "1" read as "this cluster has no
        # shell" at all.
        text = self.script(a_pane("m1"), a_pane("m2"))
        creates = [line for line in text.splitlines()
                   if "tab create" in line or "workspace create" in line]
        self.assertIn("--label shell", creates[-1])       # last => rightmost
        self.assertIn("--no-focus", creates[-1])
        self.assertNotIn("shell", creates[0])             # not the root tab

    def test_the_cluster_reports_the_key_hint_too(self):
        # Same sidebar hint as the solo shape; here the workspace id is fished
        # from the create reply (no root-pane variable exists on this path).
        text = self.script()
        self.assertIn("REPLY=$(herdr workspace create", text)
        self.assertIn('WS="${PANE%%:*}"', text)
        line = next(ln for ln in text.splitlines()
                    if "report-metadata" in ln)
        self.assertIn(f"{herdr.HINT_TOKEN}=", line)
        self.assertIn("|| :", line)

    def test_every_member_gets_its_env_whichever_pane_hosts_it(self):
        # Env key-sorted, one --env per pair — verified live to reach the new
        # shell (and so the agent started in it). The FIRST member's env
        # rides `workspace create` (it owns the root pane, so the shell tab
        # can be created last); every other member's rides `tab create`.
        env = dict(ANTHROPIC_MODEL="claude-opus-5", CLUSTER_MEMBER="x")
        text = self.script(a_pane("first", **env), a_pane("second", **env))
        flags = "--env ANTHROPIC_MODEL=claude-opus-5 --env CLUSTER_MEMBER=x"
        root = next(ln for ln in text.splitlines() if "workspace create" in ln)
        tab = next(ln for ln in text.splitlines() if "tab create --cwd" in ln
                   and "--label second" in ln)
        self.assertIn(flags, root)
        self.assertIn(flags, tab)
        # The first member is the ROOT tab, renamed — never a `tab create`.
        self.assertIn('herdr tab rename "$TAB" first', text)

    def test_attach_lands_on_the_first_member_not_the_shell(self):
        # tmux-path parity. The first member IS the root tab (focused by
        # being the only tab at creation), so every later tab — members and
        # the shell alike — is created --no-focus and nothing steals it.
        text = self.script(a_pane("first"), a_pane("second"))
        self.assertIn('herdr tab rename "$TAB" first', text)
        self.assertNotIn("--focus ", text.replace("--no-focus", ""))
        for label in ("second", "shell"):
            line = next(ln for ln in text.splitlines()
                        if f"--label {label}" in ln)
            with self.subTest(tab=label):
                self.assertIn("--no-focus", line)

    def test_a_claude_member_starts_through_agent_start(self):
        # THE herdr payoff: `agent start <name> --kind claude` is what makes
        # the member DETECTED — named in `agent list`, idle/working in the
        # sidebar. `pane run` would launch the same process invisibly.
        text = self.script(a_pane("root"),
                           a_pane("golem", command=("claude", "--effort", "max")))
        self.assertIn("herdr agent start golem --kind claude --pane \"$PANE\" "
                      "-- --effort max", text)
        # The pane id is fished from tab create's JSON reply (and from
        # workspace create's, for the member on the root pane).
        self.assertIn("pane_id", text)
        self.assertIn("PANE=$(herdr tab create", text)
        self.assertIn("REPLY=$(herdr workspace create", text)

    def test_a_non_claude_command_falls_back_to_pane_run(self):
        text = self.script(a_pane("watcher", command=("htop",)))
        self.assertIn('herdr pane run "$PANE" htop', text)
        self.assertNotIn("agent start watcher", text)

    def test_a_member_that_fails_to_start_does_not_kill_the_container(self):
        # The script is PID 1 under `set -eu`; without a fallback, one member's
        # `agent start` timing out (observed on v0.8.2 when the process exits
        # before registering) aborts the boot — every other member and the
        # attach with it, nothing left to read. The warning names the member;
        # the empty tab stays inspectable.
        text = self.script(a_pane("golem"), a_pane("watcher", command=("htop",)))
        for line in text.splitlines():
            if "agent start" in line or "pane run" in line:
                with self.subTest(line=line[:60]):
                    self.assertIn("|| echo", line)
        self.assertIn("did not start", text)

    def test_detach_leaves_everything_running(self):
        # The muxer contract: the script is PID 1, so after the attach client
        # exits (prefix+q) it must HOLD while the server lives — and say how
        # to get back in and how to actually end it.
        text = self.script()
        attach_at = text.index("\nherdr ||")
        self.assertIn("re-attach with:  docker exec", text)
        self.assertIn("herdr server stop", text)
        wait_at = text.index("while herdr status server")
        self.assertLess(attach_at, wait_at)

    def test_no_members_is_refused(self):
        with self.assertRaises(ValueError):
            herdr.script("team", (), shell_cwd=Path("/workspace"))


class TestOneAgentShape(unittest.TestCase):
    """A SOLO launch is just `script()` with one agent — there is no shape
    flag any more. It briefly also split a shell beneath the agent (the tmux
    layout, translated); the operator asked for the tab only (2026-09-03),
    which left the two shapes identical and the `solo=` parameter dead."""

    def script(self, pane: Pane | None = None) -> str:
        return herdr.script("inst__proj", (pane or a_pane("agent"),),
                            shell_cwd=Path("/workspace"))

    def test_the_agent_is_the_root_pane_and_the_shell_is_a_tab(self):
        text = self.script()
        self.assertIn("REPLY=$(herdr workspace create", text)
        self.assertIn('herdr agent start agent --kind claude --pane "$PANE"',
                      text)
        creates = [line for line in text.splitlines() if "tab create" in line]
        self.assertEqual(len(creates), 1)
        self.assertIn("--label shell", creates[0])

    def test_NO_pane_is_split_anywhere(self):
        # THE bug: an extra shell sat at the bottom of every solo screen once
        # the shell tab existed. One shell, one place — the rightmost tab.
        text = self.script()
        self.assertNotIn("pane split", text)
        self.assertNotIn("SHELL_PANE", text)
        self.assertNotIn("--ratio", text)

    def test_the_one_agent_tab_is_renamed_after_the_agent(self):
        # herdr labels a workspace's root tab "1"; the tab row is the key
        # hint's surface, so it must read as the agent line it is.
        rename = next(line for line in self.script().splitlines()
                      if "tab rename" in line)
        self.assertIn('"$TAB"', rename)
        self.assertIn(" agent ", rename)
        self.assertIn("|| :", rename)

    def test_the_key_hint_is_reported_as_workspace_metadata(self):
        # settings/herdr.toml renders `$keys` under the workspace's sidebar
        # entry. The id comes from the pane id's prefix; `|| :` because a
        # lost hint must never kill PID 1.
        text = self.script()
        self.assertIn('WS="${PANE%%:*}"', text)
        line = next(ln for ln in text.splitlines() if "report-metadata" in ln)
        self.assertIn(f"{herdr.HINT_TOKEN}=", line)
        self.assertIn("--token", line)
        self.assertIn("|| :", line)

    def test_no_greeting_is_typed_into_any_shell(self):
        # A hint typed into the shell pane was shipped and REVERSED ("they
        # don't belong there"): the hint lives in the tab row's corner.
        self.assertNotIn("pane run", self.script())


if __name__ == "__main__":
    unittest.main()
