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
from launch.cluster.tmux import Pane


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


class TestSoloShape(unittest.TestCase):
    """script(solo=True): the agent IS the workspace root pane, the free
    shell a split beneath it — ONE tab, renamed after the agent, because the
    tab row STAYS: its right corner carries the key hint (tab_bar_right)."""

    def script(self, pane: Pane | None = None) -> str:
        return herdr.script("inst__proj", (pane or a_pane("agent"),),
                            shell_cwd=Path("/workspace"), solo=True)

    def test_the_agent_is_the_root_pane_not_a_tab_of_its_own(self):
        # The agent runs in the WORKSPACE's root pane (so the shell can split
        # beneath it in the same tab); the only tab this shape creates is the
        # extra full-height shell.
        text = self.script()
        self.assertIn("REPLY=$(herdr workspace create", text)
        self.assertIn('herdr agent start agent --kind claude --pane "$PANE"',
                      text)
        creates = [line for line in text.splitlines() if "tab create" in line]
        self.assertEqual(len(creates), 1)
        self.assertIn("--label shell", creates[0])

    def test_the_one_tab_is_renamed_after_the_agent(self):
        # herdr labels a workspace's root tab "1"; with the tab row kept (it
        # carries the hint), the solo tab must read as the agent line it is.
        # The id is fished from the create reply, tolerant like every
        # cosmetic line — a failed rename must not kill PID 1.
        text = self.script()
        rename = next(line for line in text.splitlines()
                      if "tab rename" in line)
        self.assertIn('"$TAB"', rename)
        self.assertIn(" agent ", rename)
        self.assertIn("|| :", rename)

    def test_the_shell_splits_below_without_stealing_focus(self):
        text = self.script()
        split = next(line for line in text.splitlines()
                     if "pane split" in line)
        self.assertIn("--direction down", split)
        self.assertIn(f"--ratio {herdr.AGENT_RATIO}", split)
        self.assertIn("--cwd /workspace", split)
        self.assertIn("--no-focus", split)       # attach lands on the agent
        # A failed split leaves $SHELL_PANE empty (the capture pipeline's
        # status is sed's, so `set -e` cannot fire) — the guard line is what
        # says so instead of killing PID 1.
        self.assertIn("SHELL_PANE=$(", split)
        self.assertIn('[ -n "$SHELL_PANE" ] || echo', text)

    def test_no_greeting_is_typed_into_the_shell(self):
        # A hint typed into the shell pane was SHIPPED and then REVERSED by
        # the operator (2026-08-29): "they don't belong there" — the hint
        # lives at the top, in the tab row the solo shape keeps. `pane run`
        # types into a pane's shell, so its presence here would mean the
        # script is writing into the operator's terminal again.
        self.assertNotIn("pane run", self.script())

    def test_the_shell_pane_is_named_plainly(self):
        # The label RENDERS on the split frame (screenshot-verified
        # 2026-08-30, correcting an earlier "draws nothing" reading) — which
        # is exactly why it must stay a bare "shell": hotkey text on the
        # shell's frame was reported as clutter the moment the tab-row corner
        # hint landed. The hint has ONE home (tab_bar_right).
        text = self.script()
        rename = next(line for line in text.splitlines()
                      if "pane rename" in line)
        self.assertIn('"$SHELL_PANE"', rename)
        self.assertIn(f" {herdr.SHELL_LABEL} ", rename + " ")
        self.assertNotIn("alt+", herdr.SHELL_LABEL)
        self.assertIn("|| :", rename)

    def test_the_agent_keeps_the_larger_share(self):
        # `--ratio` sizes the pane BEING SPLIT — the agent. A 0.22 first guess
        # shipped Claude Code into the small pane and the shell into the big
        # one (caught by the operator's screenshot of the first live launch),
        # so the semantics are pinned as an inequality, not a number.
        self.assertGreater(float(herdr.AGENT_RATIO), 0.5)

    def test_the_key_hint_is_reported_as_workspace_metadata(self):
        # settings/herdr.toml renders `$keys` under the workspace's sidebar
        # entry — the hint surface that survives the hidden tab row. The id
        # comes from the pane id's prefix; `|| :` because a lost hint must
        # never kill PID 1.
        text = self.script()
        self.assertIn('WS="${PANE%%:*}"', text)
        line = next(ln for ln in text.splitlines()
                    if "report-metadata" in ln)
        self.assertIn(f"{herdr.HINT_TOKEN}=", line)
        self.assertIn("--token", line)
        self.assertIn("|| :", line)

    def test_the_shell_split_precedes_the_agent_start(self):
        # `agent start` BLOCKS until registration (or its timeout) — the
        # shell must already exist so a slow or failed agent leaves a usable
        # pane rather than an empty workspace.
        text = self.script()
        self.assertLess(text.index("pane split"), text.index("agent start"))

    def test_solo_also_gets_a_full_height_shell_tab(self):
        # The bottom split stays (the operator likes glancing at it); the tab
        # is the full-screen one, asked for "in both" shapes.
        text = self.script()
        create = next(line for line in text.splitlines()
                      if "tab create" in line)
        self.assertIn("--label shell", create)
        self.assertIn("--cwd /workspace", create)
        self.assertIn("--no-focus", create)     # attach stays on the agent
        self.assertIn("|| :", create)
        # ...and it comes AFTER the agent is started, so a slow agent-start
        # never delays the pane the operator watches.
        self.assertLess(text.index("agent start"), text.index("tab create"))

    def test_solo_is_exactly_one_pane(self):
        with self.assertRaises(ValueError):
            herdr.script("x", (a_pane("a"), a_pane("b")),
                         shell_cwd=Path("/workspace"), solo=True)


if __name__ == "__main__":
    unittest.main()
