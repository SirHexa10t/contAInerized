"""Tests for launch.cowork.cli — the `cowork` command line.

Driven through `main(argv)` with stdout captured, because that is the whole
contract: an operator types a command and reads what came back. Asserting on the
printed text is not incidental here — a refusal a person cannot understand is a
broken refusal, even when the exit code is right.

Injection is patched (it needs a live container); everything else is real.
"""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import cli, group as grp, journal, lifecycle, relay, roster
from launch.cowork.cli import EXIT_OK, EXIT_REFUSED, main
from launch.tags import store
from launch.tags.identity import COWORK_SPECIALTY

MANAGER_SESSION = "hub"
PEER_SESSION = "peer"


class CliHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patch(patch.object(paths, "AGENTS_STATE", self.state))
        self.addCleanup(self._drop_pidfile)

        from launch.file_access import agent_md_index
        self.agent = sorted(agent_md_index())[0]
        self.manager = f"{self.agent}__{MANAGER_SESSION}"
        self.peer = f"{self.agent}__{PEER_SESSION}"

        self.injected: list[tuple[str, str]] = []
        self._patch(patch.object(relay, "docker_attach_inject",
                                 lambda i, p: (self.injected.append((i, p)), True)[1]))
        self.live = {self.manager, self.peer}
        self._patch(patch.object(roster, "docker_running_instances_subprocess",
                                 lambda: frozenset(self.live)))

    def _patch(self, p):
        p.start()
        self.addCleanup(p.stop)

    def _drop_pidfile(self):
        if paths.hub_pid_path().exists():
            paths.hub_pid_path().unlink()

    def run_cli(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(list(argv))
        return code, buffer.getvalue()

    def register(self, session: str, *specialties: str) -> str:
        instance_id = f"{self.agent}__{session}"
        paths.instance_state_dir_path(instance_id).mkdir(parents=True, exist_ok=True)
        mapping = store.load()
        mapping[instance_id] = {"workspace": "/work", "specialties": list(specialties)}
        store.save(mapping)
        return instance_id

    def a_group(self, *coworkers: str, budget: int = 6, project: str = "widget"):
        session = grp.create_session(self.manager, project, "build it",
                                     round_budget=budget)
        for coworker in coworkers:
            session = session.with_coworker(coworker)
        return grp.save_session(session)

    def plant(self, instance: str, session, name: str, body: str) -> None:
        path = paths.cowork_group_path(instance, session.key) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)


class TestParser(unittest.TestCase):
    """argparse writes its own usage to stderr on a parse failure, so the two
    rejection tests swallow it — a suite that prints expected noise trains the
    reader to ignore output, which is where real failures go to hide."""

    def _reject(self, argv: list[str]) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(argv)

    def test_a_command_is_required(self):
        self._reject([])

    def test_every_advertised_command_parses(self):
        # Guards the gap between the help text and the dispatch table.
        for argv in (["roster"], ["recruit", "m", "p"], ["send", "g", "r", "hi"],
                     ["status"], ["serve"], ["close", "g"]):
            with self.subTest(command=argv[0]):
                self.assertEqual(cli.build_parser().parse_args(argv).command, argv[0])

    def test_an_unknown_command_is_rejected(self):
        self._reject(["teleport"])


class TestRoster(CliHarness):
    def test_lists_a_cowork_capable_peer(self):
        peer = self.register(PEER_SESSION, COWORK_SPECIALTY)
        code, out = self.run_cli("roster")
        self.assertEqual(code, EXIT_OK)
        self.assertIn(peer, out)

    def test_the_asker_excludes_itself(self):
        asker = self.register(MANAGER_SESSION, COWORK_SPECIALTY)
        code, out = self.run_cli("roster", "--as", asker)
        self.assertNotIn(asker, out)

    def test_reachable_hides_a_stopped_peer(self):
        peer = self.register(PEER_SESSION, COWORK_SPECIALTY)
        self.live = set()
        _, out = self.run_cli("roster", "--reachable")
        self.assertNotIn(peer, out)

    def test_reachable_still_reports_who_needs_a_relaunch(self):
        # Filtering the candidates must not swallow the actionable half.
        plain = self.register("plain")
        self.live = {plain}
        _, out = self.run_cli("roster", "--reachable")
        self.assertIn(plain, out)


class TestRecruit(CliHarness):
    def test_creates_a_group_and_reports_it(self):
        code, out = self.run_cli("recruit", self.manager, "widget", self.peer)
        self.assertEqual(code, EXIT_OK)
        self.assertIn(paths.group_key(self.manager, "widget"), out)
        self.assertIn(self.peer, out)

    def test_the_group_is_persisted(self):
        self.run_cli("recruit", self.manager, "widget", self.peer)
        self.assertEqual([s.key for s in grp.discover_sessions()],
                         [paths.group_key(self.manager, "widget")])

    def test_recruiting_again_adds_without_resetting_progress(self):
        self.run_cli("recruit", self.manager, "widget", self.peer, "--budget", "4")
        session = grp.save_session(self.a_group(self.peer, budget=4).with_round_used())
        self.run_cli("recruit", self.manager, "widget", "other__x")
        reloaded = grp.load_session(grp.session_dir(session))
        self.assertEqual(reloaded.rounds_used, 1)
        self.assertIn("other__x", reloaded.coworkers)

    def test_a_group_with_no_coworkers_says_so(self):
        _, out = self.run_cli("recruit", self.manager, "widget")
        self.assertIn("none yet", out)

    def test_the_budget_is_recorded(self):
        self.run_cli("recruit", self.manager, "widget", self.peer, "--budget", "2")
        self.assertEqual(grp.discover_sessions()[0].round_budget, 2)

    def test_a_separator_bearing_project_is_refused_loudly(self):
        with self.assertRaises(ValueError):
            self.run_cli("recruit", self.manager, f"bad{paths.INBOX_SEPARATOR}label")


class TestSend(CliHarness):
    def test_delivers_and_reports_the_remaining_budget(self):
        session = self.a_group(self.peer, budget=3)
        code, out = self.run_cli("send", session.key, self.peer, "do", "the", "thing")
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(len(self.injected), 1)
        self.assertIn("2 of 3", out)

    def test_the_message_words_are_joined(self):
        session = self.a_group(self.peer)
        self.run_cli("send", session.key, self.peer, "please", "review", "this")
        staged = (paths.cowork_group_path(self.peer, session.key) / "messages")
        self.assertIn("please review this", sorted(staged.glob("*.md"))[0].read_text())

    def test_an_unknown_group_is_refused_with_a_pointer_to_status(self):
        code, out = self.run_cli("send", "nope__x-ghost", self.peer, "hi")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("status", out)
        self.assertEqual(self.injected, [])

    def test_a_closed_group_is_refused(self):
        session = grp.save_session(self.a_group(self.peer).closed())
        code, out = self.run_cli("send", session.key, self.peer, "hi")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("closed", out)

    def test_an_exhausted_budget_is_refused_with_the_reason(self):
        session = self.a_group(self.peer, budget=1)
        self.run_cli("send", session.key, self.peer, "one")
        code, out = self.run_cli("send", session.key, self.peer, "two")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("budget", out)

    def test_with_files_pushes_the_working_copy_too(self):
        session = self.a_group(self.peer)
        self.plant(self.manager, session, "task.py", "TODO\n")
        code, out = self.run_cli("send", session.key, self.peer, "start", "here",
                                 "--with-files")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("1 file", out)
        inbox = paths.cowork_inbox_path(self.peer, session.key, self.manager)
        self.assertEqual((inbox / "task.py").read_text(), "TODO\n")

    def test_with_files_is_refused_for_a_non_manager_sender(self):
        # It pushes the MANAGER's working copy; doing that under a coworker's name
        # would send the wrong tree and mislabel it.
        session = self.a_group(self.peer)
        self.plant(self.manager, session, "task.py", "TODO\n")
        code, out = self.run_cli("send", session.key, self.manager, "here",
                                 "--from", self.peer, "--with-files")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("--with-files", out)
        self.assertEqual(self.injected, [])

    def test_from_defaults_to_the_manager(self):
        session = self.a_group(self.peer)
        self.run_cli("send", session.key, self.peer, "hi")
        self.assertIn(f"from: {self.manager}", (
            paths.cowork_group_path(self.peer, session.key) / "messages"
        ).glob("*.md").__next__().read_text())

    def test_a_non_member_recipient_is_refused(self):
        session = self.a_group(self.peer)
        code, out = self.run_cli("send", session.key, "stranger__x", "hi")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("recruit first", out)
        self.assertEqual(self.injected, [])

    def test_a_non_member_sender_is_refused(self):
        session = self.a_group(self.peer)
        code, out = self.run_cli("send", session.key, self.peer, "hi",
                                 "--from", "stranger__x")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("stranger__x", out)

    def test_membership_is_checked_before_any_file_moves(self):
        # --with-files copies the manager's working copy into the recipient's
        # READABLE mount; refusing the message afterwards does not take that back.
        session = self.a_group(self.peer)
        self.plant(self.manager, session, "secret.py", "the manager's copy\n")
        code, _ = self.run_cli("send", session.key, "stranger__x", "hi",
                               "--with-files")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertFalse(paths.cowork_dir_path("stranger__x").exists())

    def test_a_failed_injection_is_reported_as_not_delivered(self):
        session = self.a_group(self.peer)
        with patch.object(relay, "docker_attach_inject", lambda i, p: False):
            code, out = self.run_cli("send", session.key, self.peer, "hi")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("Not delivered", out)


class TestStatus(CliHarness):
    def test_says_when_no_hub_is_running_and_how_to_start_one(self):
        _, out = self.run_cli("status")
        self.assertIn("not running", out)
        self.assertIn("cowork serve", out)

    def test_names_the_running_hub_by_pid(self):
        claimed = lifecycle.claim()
        self.addCleanup(lifecycle.release, claimed)
        _, out = self.run_cli("status")
        self.assertIn(str(claimed.pid), out)

    def test_says_when_there_are_no_groups(self):
        self.assertIn("no groups yet", self.run_cli("status")[1])

    def test_reports_a_group_with_its_rounds_and_coworkers(self):
        session = self.a_group(self.peer, budget=5)
        _, out = self.run_cli("status")
        self.assertIn(session.key, out)
        self.assertIn(self.peer, out)
        self.assertIn("of 5", out)

    def test_a_group_with_no_coworkers_is_flagged(self):
        self.a_group()
        self.assertIn("recruit some", self.run_cli("status")[1])

    def test_limits_to_one_group_when_asked(self):
        first = self.a_group(self.peer, project="alpha")
        second = self.a_group(self.peer, project="beta")
        _, out = self.run_cli("status", first.key)
        self.assertIn(first.key, out)
        self.assertNotIn(second.key, out)

    def test_an_unknown_group_says_so_rather_than_listing_everything(self):
        self.a_group(self.peer)
        self.assertIn("no group", self.run_cli("status", "nope__x-ghost")[1])

    def test_reports_material_sent_but_never_picked_up(self):
        session = self.a_group(self.peer)
        self.plant(self.manager, session, "task.py", "TODO\n")
        self.run_cli("send", session.key, self.peer, "start", "--with-files")
        self.assertIn("never picked up", self.run_cli("status")[1])

    def test_reports_files_waiting_in_the_managers_inbox(self):
        session = self.a_group(self.peer)
        inbox = paths.cowork_inbox_path(self.manager, session.key, self.peer)
        inbox.mkdir(parents=True)
        (inbox / "answer.py").write_text("done\n")
        self.assertIn("waiting in your inbox", self.run_cli("status")[1])

    def test_warns_about_a_send_that_was_never_answered(self):
        session = self.a_group(self.peer)
        grp.update_participant(self.peer, grp.ParticipantState(
            outstanding_send=session.key, sent_at=0.0))     # epoch 0 = very overdue
        self.assertIn("may not have landed", self.run_cli("status")[1])


class TestServe(CliHarness):
    def test_a_single_pass_drains_and_releases_the_pidfile(self):
        code, out = self.run_cli("serve", "--once", "--interval", "0")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("hub serving", out)
        self.assertIsNone(lifecycle.owner())

    def test_a_pass_routes_a_waiting_capture(self):
        session = self.a_group(self.peer)
        self._drop_capture(session)
        self.run_cli("serve", "--once", "--interval", "0")
        self.assertEqual([i for i, _ in self.injected], [self.manager])

    def test_a_second_hub_refuses_rather_than_halving_the_captures(self):
        claimed = lifecycle.claim()
        self.addCleanup(lifecycle.release, claimed)
        code, out = self.run_cli("serve", "--once")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("already running", out)

    def test_an_interrupted_hub_leaves_no_pidfile_behind(self):
        # Ctrl-C is ordinary; it must not need the stale-file path to recover.
        with patch.object(relay, "serve", side_effect=KeyboardInterrupt):
            code, out = self.run_cli("serve")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stopped", out)
        self.assertFalse(paths.hub_pid_path().exists())

    def _drop_capture(self, session) -> None:
        relative = Path("projects") / "-workspace" / "s.jsonl"
        host = paths.instance_state_dir_path(self.peer) / relative
        host.parent.mkdir(parents=True, exist_ok=True)
        from launch.cowork import mailbox
        host.write_text(json.dumps({
            "promptId": "p1",
            "message": {"role": "user",
                        "content": mailbox.tag_message(session.key, "go")}}) + "\n")
        outbox = paths.cowork_outbox_path(self.peer)
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "c.json").write_text(json.dumps({
            "last_assistant_message": "finished it", "prompt_id": "p1",
            "transcript_path": str(paths.CLAUDE_CONFIG_IN_CONTAINER / relative)}))


class TestClose(CliHarness):
    def test_closes_an_active_group(self):
        session = self.a_group(self.peer)
        code, out = self.run_cli("close", session.key)
        self.assertEqual(code, EXIT_OK)
        self.assertIs(grp.load_session(grp.session_dir(session)).status,
                      grp.GroupStatus.CLOSED)
        self.assertIn("kept", out)

    def test_closing_twice_is_not_an_error(self):
        session = self.a_group(self.peer)
        self.run_cli("close", session.key)
        code, out = self.run_cli("close", session.key)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("already closed", out)

    def test_the_conversation_survives_closing(self):
        session = self.a_group(self.peer)
        journal.append(session, journal.Direction.TO, self.peer, "the ask")
        self.run_cli("close", session.key)
        self.assertIn("the ask", journal.read_journal(session))

    def test_an_unknown_group_is_refused(self):
        self.assertEqual(self.run_cli("close", "nope__x-ghost")[0], EXIT_REFUSED)


if __name__ == "__main__":
    unittest.main()
