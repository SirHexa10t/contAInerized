"""Tests for launch.cowork.control — the agent-facing control channel.

The property that matters most here is the GATE: `control/` sits inside
`/cowork`, the one surface any `{cowork}` agent can write, so the requester's
tags are the only thing separating a manager's command from a coworker's. Most
of these tests are therefore about who is refused, and that a refusal has no
side effects — an ignored request that still created a group would be a gate in
name only.

Identity lookup is injected (who is a manager is a dict here); the group state,
files, and replies are all real.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import control, group as grp, journal, relay
from launch.cowork.control import (
    CONTROL_SUBDIR, REJECTED_SUBDIR, REPLIES_SUBDIR, poll_control,
)
from launch.cowork.relay import EventKind
from launch.tags import scan_all

MANAGER = "boss__proj"
PEER = "golem__a"


class ControlHarness(unittest.TestCase):
    """A real group-hosting tree; injection recorded; managerness is a set."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._patch(patch.object(paths, "AGENTS_STATE", Path(self._tmp.name)))
        self.registry = scan_all(paths.AGENTS_DIR)
        self.managers = {MANAGER}
        self.injected: list[tuple[str, str]] = []
        for module in (control, relay):
            self._patch(patch.object(module, "docker_attach_inject",
                                     lambda i, p: (self.injected.append((i, p)),
                                                   True)[1]))

    def _patch(self, p):
        p.start()
        self.addCleanup(p.stop)

    def _lookup(self, instance: str):
        """Stands in for instance_from_store: managerness only."""
        class _Identity:
            is_manager = instance in self.managers
        return _Identity()

    def drop_request(self, asker: str, first_line: str, body: str = "",
                     *, name: str = "req.txt", old: bool = True) -> Path:
        request = paths.cowork_dir_path(asker) / CONTROL_SUBDIR / name
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(f"{first_line}\n{body}" if body else first_line)
        if old:                     # age it past the settle window
            import os
            os.utime(request, (1, 1))
        return request

    def poll(self):
        return poll_control(self.registry, lookup=self._lookup)

    def replies(self, asker: str) -> list[str]:
        replies = paths.cowork_dir_path(asker) / CONTROL_SUBDIR / REPLIES_SUBDIR
        return [p.read_text() for p in sorted(replies.glob("*.md"))]

    def a_group(self, *coworkers: str, budget: int = 6, project: str = "widget"):
        session = grp.create_session(MANAGER, project, "build it",
                                     round_budget=budget)
        for coworker in coworkers:
            session = session.with_coworker(coworker)
        return grp.save_session(session)


class TestQuietFlag(ControlHarness):
    """`+quiet` on any verb: the reply file is written as ever, only the wake
    prompt to the ASKER is skipped — for managers that poll replies/ themselves,
    whose wake would otherwise land as a redundant turn."""

    def test_quiet_roster_writes_the_reply_without_waking(self):
        self.drop_request(MANAGER, "roster +quiet")
        self.poll()
        self.assertEqual(len(self.replies(MANAGER)), 1)
        self.assertEqual(self.injected, [])

    def test_quiet_send_still_wakes_the_recipient(self):
        # Quiet silences the control ANSWER, never the message DELIVERY — a
        # recipient that is not woken would never read its message at all.
        self.a_group(PEER)
        self.drop_request(MANAGER, f"send {MANAGER}-widget {PEER} +files +quiet",
                          body="please review")
        self.poll()
        self.assertEqual([target for target, _ in self.injected], [PEER])
        self.assertIn("Delivered", self.replies(MANAGER)[0])

    def test_quiet_refusals_are_quiet_too(self):
        # The requester polls either way; a refusal is an answer like any other.
        self.drop_request(MANAGER, "done nope-x +quiet")
        self.poll()
        self.assertIn("Refused", self.replies(MANAGER)[0])
        self.assertEqual(self.injected, [])

    def test_without_the_flag_the_wake_still_happens(self):
        self.drop_request(MANAGER, "roster")
        self.poll()
        self.assertEqual([target for target, _ in self.injected], [MANAGER])


class TestGate(ControlHarness):
    def test_a_non_managers_request_is_denied(self):
        self.drop_request(PEER, "roster")
        events = self.poll()
        self.assertEqual([e.kind for e in events], [EventKind.DENIED])

    def test_a_denied_request_gets_no_reply_and_no_injection(self):
        # Answering would invite probing; the plan says ignore.
        self.drop_request(PEER, "roster")
        self.poll()
        self.assertEqual(self.replies(PEER), [])
        self.assertEqual(self.injected, [])

    def test_a_denied_request_is_parked_not_deleted(self):
        self.drop_request(PEER, "recruit sneaky golem__b")
        self.poll()
        rejected = paths.cowork_dir_path(PEER) / CONTROL_SUBDIR / REJECTED_SUBDIR
        self.assertEqual(len(list(rejected.glob("*"))), 1)

    def test_a_denied_recruit_creates_no_group(self):
        # The refusal must be inert, not just labelled.
        self.drop_request(PEER, "recruit sneaky golem__b")
        self.poll()
        self.assertEqual(grp.discover_sessions(), [])

    def test_a_parked_request_is_not_reprocessed(self):
        self.drop_request(PEER, "roster")
        self.poll()
        self.assertEqual(self.poll(), ())

    def test_an_unknown_instance_is_denied_like_a_non_manager(self):
        # instance_from_store returns None for an orphan — same answer.
        self.managers = set()
        self.drop_request("ghost__x", "roster")
        self.assertEqual([e.kind for e in self.poll()], [EventKind.DENIED])

    def test_a_managers_request_is_honoured(self):
        self.drop_request(MANAGER, "roster")
        self.assertEqual([e.kind for e in self.poll()], [EventKind.CONTROL])


class TestRequestLifecycle(ControlHarness):
    def test_a_fresh_file_is_left_alone_until_it_settles(self):
        # Write-in-progress guard: the agent's Write may not be atomic.
        self.drop_request(MANAGER, "roster", old=False)
        self.assertEqual(self.poll(), ())

    def test_a_settled_file_is_consumed(self):
        request = self.drop_request(MANAGER, "roster")
        self.poll()
        self.assertFalse(request.exists())

    def test_an_empty_request_is_parked(self):
        self.drop_request(MANAGER, "   ")
        events = self.poll()
        self.assertIn("empty", events[0].detail)
        rejected = paths.cowork_dir_path(MANAGER) / CONTROL_SUBDIR / REJECTED_SUBDIR
        self.assertEqual(len(list(rejected.glob("*"))), 1)

    def test_an_unknown_verb_is_answered_with_the_expected_ones(self):
        self.drop_request(MANAGER, "teleport somewhere")
        self.poll()
        reply = self.replies(MANAGER)[0]
        self.assertIn("teleport", reply)
        self.assertIn("roster", reply)

    def test_the_injected_pointer_names_the_container_path(self):
        self.drop_request(MANAGER, "roster")
        self.poll()
        target, prompt = self.injected[0]
        self.assertEqual(target, MANAGER)
        self.assertIn(f"{paths.COWORK_IN_CONTAINER}/{CONTROL_SUBDIR}/{REPLIES_SUBDIR}/", prompt)
        self.assertNotIn("\n", prompt)

    def test_the_pointer_is_untagged_so_the_acknowledgement_is_not_routed(self):
        self.drop_request(MANAGER, "roster")
        self.poll()
        self.assertFalse(self.injected[0][1].startswith("[cowork"))

    def test_replies_number_upward_and_never_overwrite(self):
        self.drop_request(MANAGER, "roster", name="a.txt")
        self.poll()
        self.drop_request(MANAGER, "roster", name="b.txt")
        self.poll()
        self.assertEqual(len(self.replies(MANAGER)), 2)


class TestRecruit(ControlHarness):
    def test_creates_the_group_with_the_body_as_task(self):
        self.drop_request(MANAGER, f"recruit widget {PEER}", "Fix the retry loop")
        self.poll()
        session = grp.discover_sessions()[0]
        self.assertEqual(session.key, paths.group_key(MANAGER, "widget"))
        self.assertEqual(session.coworkers, (PEER,))
        self.assertEqual(session.task, "Fix the retry loop")

    def test_the_reply_names_the_group_and_the_working_copy(self):
        self.drop_request(MANAGER, f"recruit widget {PEER}")
        self.poll()
        reply = self.replies(MANAGER)[0]
        self.assertIn(paths.group_key(MANAGER, "widget"), reply)
        self.assertIn(str(paths.COWORK_IN_CONTAINER), reply)

    def test_reissuing_extends_without_resetting(self):
        session = grp.save_session(self.a_group(PEER).with_round_used())
        self.drop_request(MANAGER, "recruit widget golem__b")
        self.poll()
        reloaded = grp.load_session(grp.session_dir(session))
        self.assertEqual(reloaded.rounds_used, 1)
        self.assertEqual(set(reloaded.coworkers), {PEER, "golem__b"})

    def test_a_separator_bearing_project_is_refused_in_the_reply(self):
        self.drop_request(MANAGER, f"recruit bad{paths.INBOX_SEPARATOR}label {PEER}")
        self.poll()
        self.assertIn("Refused", self.replies(MANAGER)[0])
        self.assertEqual(grp.discover_sessions(), [])

    def test_missing_arguments_get_usage_back(self):
        self.drop_request(MANAGER, "recruit")
        self.poll()
        self.assertIn("usage", self.replies(MANAGER)[0])


class TestSend(ControlHarness):
    def _transcriptless_send(self, extra: str = "") -> None:
        session = self.a_group(PEER)
        self.drop_request(MANAGER, f"send {session.key} {PEER}{extra}",
                          "Please fix the retry loop")
        self.poll()

    def test_delivers_the_body_and_wakes_the_peer(self):
        self._transcriptless_send()
        staged = (paths.cowork_group_path(PEER, f"{MANAGER}-widget") / "messages")
        self.assertIn("Please fix the retry loop",
                      sorted(staged.glob("*.md"))[0].read_text())
        self.assertIn(PEER, [i for i, _ in self.injected])

    def test_consumes_a_round(self):
        session = self.a_group(PEER, budget=3)
        self.drop_request(MANAGER, f"send {session.key} {PEER}", "go")
        self.poll()
        self.assertEqual(grp.load_session(grp.session_dir(session)).rounds_used, 1)

    def test_plus_files_hands_the_working_copy_over_first(self):
        session = self.a_group(PEER)
        working = paths.cowork_group_path(MANAGER, session.key)
        working.mkdir(parents=True, exist_ok=True)
        (working / "task.py").write_text("TODO\n")
        self.drop_request(MANAGER, f"send {session.key} {PEER} +files", "start here")
        self.poll()
        inbox = paths.cowork_inbox_path(PEER, session.key, MANAGER)
        self.assertEqual((inbox / "task.py").read_text(), "TODO\n")

    def test_an_empty_body_is_refused_with_instructions(self):
        session = self.a_group(PEER)
        self.drop_request(MANAGER, f"send {session.key} {PEER}")
        self.poll()
        self.assertIn("body is empty", self.replies(MANAGER)[0])

    def test_sending_into_a_group_the_asker_does_not_host_is_refused(self):
        # A manager-tagged peer recruited into someone else's group is just a
        # coworker there — it must not be able to spend that group's rounds.
        other = grp.save_session(
            grp.create_session("other__boss", "theirs", "t").with_coworker(MANAGER))
        self.drop_request(MANAGER, f"send {other.key} other__boss", "hijack")
        self.poll()
        reply = self.replies(MANAGER)[0]
        self.assertIn("hosted by you", reply)
        self.assertEqual(self.injected[0][0], MANAGER)      # only the reply pointer

    def test_the_ownership_refusal_lists_what_would_work(self):
        self.a_group(PEER)
        self.drop_request(MANAGER, f"send nope__x-ghost {PEER}", "hi")
        self.poll()
        self.assertIn(f"{MANAGER}-widget", self.replies(MANAGER)[0])

    def test_a_non_member_recipient_is_refused_before_any_files_move(self):
        session = self.a_group(PEER)
        working = paths.cowork_group_path(MANAGER, session.key)
        working.mkdir(parents=True, exist_ok=True)
        (working / "secret.py").write_text("private\n")
        self.drop_request(MANAGER, f"send {session.key} stranger__x +files", "hi")
        self.poll()
        self.assertIn("recruit first", self.replies(MANAGER)[0])
        self.assertFalse(paths.cowork_dir_path("stranger__x").exists())


class TestReleaseAndDone(ControlHarness):
    def test_release_drops_the_peer_but_keeps_its_dirs(self):
        session = self.a_group(PEER, "golem__b")
        peer_dir = paths.cowork_group_path(PEER, session.key)
        peer_dir.mkdir(parents=True, exist_ok=True)
        (peer_dir / "work.py").write_text("kept\n")
        self.drop_request(MANAGER, f"release {session.key} {PEER}")
        self.poll()
        reloaded = grp.load_session(grp.session_dir(session))
        self.assertEqual(reloaded.coworkers, ("golem__b",))
        self.assertTrue((peer_dir / "work.py").exists())

    def test_releasing_a_non_member_is_refused(self):
        session = self.a_group(PEER)
        self.drop_request(MANAGER, f"release {session.key} stranger__x")
        self.poll()
        self.assertIn("not in", self.replies(MANAGER)[0])

    def test_release_is_recorded_in_the_conversation(self):
        session = self.a_group(PEER)
        self.drop_request(MANAGER, f"release {session.key} {PEER}")
        self.poll()
        self.assertIn("released", journal.read_journal(session))

    def test_done_closes_the_group_and_logs_it(self):
        session = self.a_group(PEER)
        self.drop_request(MANAGER, f"done {session.key}")
        self.poll()
        self.assertIs(grp.load_session(grp.session_dir(session)).status,
                      grp.GroupStatus.CLOSED)
        self.assertIn("closed by its manager", journal.read_journal(session))

    def test_done_on_someone_elses_group_is_refused(self):
        other = grp.save_session(
            grp.create_session("other__boss", "theirs", "t").with_coworker(MANAGER))
        self.drop_request(MANAGER, f"done {other.key}")
        self.poll()
        self.assertIs(grp.load_session(grp.session_dir(other)).status,
                      grp.GroupStatus.ACTIVE)


class TestRoundTripThroughServe(ControlHarness):
    """The full agent-visible cycle, driven through relay.serve's also_poll —
    proving the two event sources actually run in one loop."""

    def test_recruit_then_send_then_reply_reaches_the_manager(self):
        # 1. The manager asks for a group and sends work.
        self.drop_request(MANAGER, f"recruit widget {PEER}", "fix retry",
                          name="01.txt")
        with patch.object(relay.time, "sleep", lambda _: None):
            relay.serve(interval=0, report=False, passes=1, also_poll=self.poll)
        key = paths.group_key(MANAGER, "widget")
        self.drop_request(MANAGER, f"send {key} {PEER}", "please fix it",
                          name="02.txt")
        with patch.object(relay.time, "sleep", lambda _: None):
            relay.serve(interval=0, report=False, passes=1, also_poll=self.poll)
        pointer = next(p for i, p in self.injected if i == PEER)

        # 2. The peer's turn ends; its capture answers the tagged prompt.
        relative = Path("projects") / "-workspace" / "s.jsonl"
        transcript = paths.instance_state_dir_path(PEER) / relative
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(json.dumps(
            {"promptId": "p1", "message": {"role": "user", "content": pointer}}) + "\n")
        outbox = paths.cowork_outbox_path(PEER)
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / "c.json").write_text(json.dumps({
            "last_assistant_message": "fixed it", "prompt_id": "p1",
            "transcript_path": str(paths.CLAUDE_CONFIG_IN_CONTAINER / relative)}))
        with patch.object(relay.time, "sleep", lambda _: None):
            relay.serve(interval=0, report=False, passes=1, also_poll=self.poll)

        # 3. The manager was woken about the reply, and the log has the thread.
        notification = (paths.cowork_group_path(MANAGER, key) / "messages")
        texts = [p.read_text() for p in sorted(notification.glob("*.md"))]
        self.assertTrue(any("fixed it" in t for t in texts))
        log = journal.read_journal(grp.discover_sessions()[0])
        self.assertLess(log.index("please fix it"), log.index("fixed it"))


if __name__ == "__main__":
    unittest.main()
