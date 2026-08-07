"""Tests for launch.cowork.relay — the hub loop's routing policy.

Injection is the one thing here that needs a live container, so it is patched
throughout and its calls are recorded: what matters is WHO the hub decided to
wake and WHAT it decided to say, not that a pty behaved.

The attribution path is driven through real transcript files rather than by
stubbing `mailbox.attribute`, because the pairing of a reply to its prompt is the
single most load-bearing behaviour in this package — a stub would prove only that
the stub was called.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import group as grp
from launch.cowork import journal, mailbox, relay, sync
from launch.cowork.group import GroupStatus, HubState, ParticipantState, Session
from launch.cowork.relay import EventKind, poll_once, send

MANAGER = "boss__proj"
COWORKER = "golem__a"
OTHER = "golem__b"


class RelayHarness(unittest.TestCase):
    """A whole group-hosting tree in a tmpdir, with injection recorded.

    Redirecting `AGENTS_STATE` moves the group-hosting tree AND the instance state
    dirs in one patch — the latter matters because attribution rebases a capture's
    container-side transcript path onto the instance's real state dir, so both have
    to land in the same tmpdir."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patch(patch.object(paths, "AGENTS_STATE", self.tmp))
        self.root = paths.group_hosting_dir()
        self.instances = paths.instances_dir()
        self.injected: list[tuple[str, str]] = []
        self._patch(patch.object(relay, "docker_attach_inject",
                                 lambda inst, prompt: self._inject(inst, prompt)))

    def _patch(self, p):
        p.start()
        self.addCleanup(p.stop)

    def _inject(self, instance: str, prompt: str) -> bool:
        self.injected.append((instance, prompt))
        return self.inject_succeeds

    inject_succeeds = True

    # --- fixture builders ---

    def make_session(self, *coworkers: str, budget: int = 6) -> Session:
        session = grp.create_session(MANAGER, "widget", "build it", round_budget=budget)
        for coworker in coworkers:
            session = session.with_coworker(coworker)
        return grp.save_session(session)

    def transcript_for(self, instance: str, prompt_id: str, text: str) -> str:
        """Write a real transcript entry and return the CONTAINER path a capture
        would carry, so attribution has to do the rebase for itself."""
        relative = Path("projects") / "-workspace" / "session.jsonl"
        host = self.instances / instance / relative
        host.parent.mkdir(parents=True, exist_ok=True)
        with host.open("a") as handle:
            handle.write(json.dumps({"promptId": prompt_id, "uuid": "u1",
                                     "message": {"role": "user", "content": text}}) + "\n")
        return str(paths.CLAUDE_CONFIG_IN_CONTAINER / relative)

    def drop_capture(self, instance: str, answer: str, *, prompt_id: str = "p1",
                     group: str | None = None, tagged: bool = True) -> None:
        """Put a Stop-hook capture in `instance`'s outbox, with a transcript entry
        its prompt_id resolves to."""
        text = mailbox.tag_message(group, "do the thing") if tagged and group else "hi there"
        container_path = self.transcript_for(instance, prompt_id, text)
        outbox = paths.cowork_outbox_path(instance)
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"{prompt_id}.json").write_text(json.dumps({
            "last_assistant_message": answer, "prompt_id": prompt_id,
            "session_id": "s1", "transcript_path": container_path}))

    def plant_work(self, instance: str, session: Session, name: str, body: str) -> None:
        path = paths.cowork_group_path(instance, session.key) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)

    def prompts_to(self, instance: str) -> list[str]:
        return [prompt for target, prompt in self.injected if target == instance]

    def staged_for(self, instance: str, session: Session) -> list[str]:
        staged = paths.cowork_group_path(instance, session.key) / mailbox.MESSAGES_SUBDIR
        return [p.read_text() for p in sorted(staged.glob("*.md"))]


class TestSend(RelayHarness):
    def test_stages_the_body_and_injects_a_pointer(self):
        session = self.make_session(COWORKER)
        result = send(session, sender=MANAGER, recipient=COWORKER, body="please review")
        self.assertTrue(result.delivered)
        self.assertIn("please review", self.staged_for(COWORKER, session)[0])
        self.assertEqual(len(self.prompts_to(COWORKER)), 1)

    def test_the_injected_prompt_is_a_single_line(self):
        # Injection types the text, so a newline would submit a fragment.
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER,
             body="line one\nline two\nline three")
        self.assertNotIn("\n", self.prompts_to(COWORKER)[0])

    def test_the_prompt_carries_the_group_tag_so_the_reply_can_be_attributed(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER, body="x")
        self.assertEqual(mailbox.group_from_prompt(self.prompts_to(COWORKER)[0]),
                         session.key)

    def test_a_send_to_a_coworker_consumes_a_round(self):
        session = self.make_session(COWORKER, budget=3)
        self.assertEqual(send(session, sender=MANAGER, recipient=COWORKER,
                              body="x").session.rounds_used, 1)

    def test_notifying_the_manager_does_not_consume_a_round(self):
        # Otherwise hub bookkeeping would eat the budget the manager asked for.
        session = self.make_session(COWORKER, budget=3)
        self.assertEqual(send(session, sender=COWORKER, recipient=MANAGER,
                              body="x").session.rounds_used, 0)

    def test_the_round_is_persisted_not_just_returned(self):
        session = self.make_session(COWORKER, budget=3)
        send(session, sender=MANAGER, recipient=COWORKER, body="x")
        self.assertEqual(grp.load_session(grp.session_dir(session)).rounds_used, 1)

    def test_messages_are_numbered_so_none_is_overwritten(self):
        session = self.make_session(COWORKER)
        for body in ("first", "second"):
            send(session, sender=MANAGER, recipient=COWORKER, body=body)
        self.assertEqual(len(self.staged_for(COWORKER, session)), 2)

    def test_exhausting_the_budget_refuses_the_send_and_closes_the_group(self):
        session = self.make_session(COWORKER, budget=1)
        first = send(session, sender=MANAGER, recipient=COWORKER, body="one")
        second = send(first.session, sender=MANAGER, recipient=COWORKER, body="two")
        self.assertFalse(second.delivered)
        self.assertIs(second.session.status, GroupStatus.CLOSED)
        self.assertEqual(len(self.prompts_to(COWORKER)), 1)

    def test_a_closed_group_delivers_nothing(self):
        session = grp.save_session(self.make_session(COWORKER).closed())
        self.assertFalse(send(session, sender=MANAGER, recipient=COWORKER,
                              body="x").delivered)
        self.assertEqual(self.injected, [])

    def test_a_refusal_explains_itself(self):
        session = grp.save_session(self.make_session(COWORKER).closed())
        self.assertIn("closed", send(session, sender=MANAGER, recipient=COWORKER,
                                     body="x").reason)

    def test_a_delivered_send_has_no_reason(self):
        session = self.make_session(COWORKER)
        self.assertEqual(send(session, sender=MANAGER, recipient=COWORKER,
                              body="x").reason, "")

    def test_both_sides_of_a_delivery_are_logged(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER, body="the ask")
        self.assertIn("the ask", journal.read_journal(session))


class TestSendMembership(RelayHarness):
    """The hub carries traffic between MEMBERS of a group and nobody else.

    Every test here asserts the absence of a side effect as well as the refusal:
    an unvalidated send does not merely return the wrong code, it wakes the wrong
    instance and writes into its tree."""

    def test_an_unrecruited_recipient_is_refused(self):
        session = self.make_session(COWORKER)
        result = send(session, sender=MANAGER, recipient="stranger__x", body="hi")
        self.assertFalse(result.delivered)
        self.assertIn("stranger__x", result.reason)

    def test_an_unrecruited_recipient_is_never_woken(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient="stranger__x", body="hi")
        self.assertEqual(self.injected, [])

    def test_an_unrecruited_recipient_gets_no_group_dir(self):
        # stage_message would otherwise create one in an uninvolved instance's tree.
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient="stranger__x", body="hi")
        self.assertFalse(paths.cowork_group_path("stranger__x", session.key).exists())

    def test_an_unrecruited_sender_is_refused(self):
        session = self.make_session(COWORKER)
        result = send(session, sender="stranger__x", recipient=COWORKER, body="hi")
        self.assertFalse(result.delivered)
        self.assertIn("stranger__x", result.reason)

    def test_a_refused_send_burns_no_round(self):
        session = self.make_session(COWORKER, budget=3)
        send(session, sender=MANAGER, recipient="stranger__x", body="hi")
        self.assertEqual(grp.load_session(grp.session_dir(session)).rounds_used, 0)

    def test_a_refused_send_is_not_logged_as_group_history(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient="stranger__x", body="hi")
        self.assertEqual(journal.read_journal(session), "")

    def test_both_outsiders_are_named_at_once(self):
        # So a caller fixing a two-ended mistake does not need two attempts.
        session = self.make_session(COWORKER)
        reason = send(session, sender="a__x", recipient="b__y", body="hi").reason
        self.assertIn("a__x", reason)
        self.assertIn("b__y", reason)

    def test_the_manager_is_a_member_without_being_recruited(self):
        session = self.make_session(COWORKER)
        self.assertTrue(send(session, sender=COWORKER, recipient=MANAGER,
                             body="hi").delivered)

    def test_a_recruited_coworker_may_be_reached(self):
        session = self.make_session(COWORKER)
        self.assertTrue(send(session, sender=MANAGER, recipient=COWORKER,
                             body="hi").delivered)


class TestSendWhenInjectionFails(RelayHarness):
    inject_succeeds = False

    def test_a_failed_injection_is_not_reported_as_delivered(self):
        session = self.make_session(COWORKER)
        self.assertFalse(send(session, sender=MANAGER, recipient=COWORKER,
                              body="x").delivered)

    def test_a_failed_injection_leaves_nothing_outstanding_to_wait_for(self):
        # Recording the send first would leave the hub waiting forever for a reply
        # to a prompt nobody received.
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER, body="x")
        self.assertIsNone(
            grp.load_hub_state().for_participant(COWORKER).outstanding_send)

    def test_the_failure_is_recorded_in_the_conversation(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER, body="x")
        self.assertIn("injection failed", journal.read_journal(session))

    def test_a_failed_injection_does_not_burn_a_round(self):
        session = self.make_session(COWORKER, budget=2)
        send(session, sender=MANAGER, recipient=COWORKER, body="x")
        self.assertEqual(grp.load_session(grp.session_dir(session)).rounds_used, 0)


class TestPollAttribution(RelayHarness):
    def test_a_tagged_reply_is_attributed_and_routed(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "here is my answer", group=session.key)
        events = poll_once()
        self.assertEqual([e.kind for e in events], [EventKind.REPLY])
        self.assertEqual(events[0].group, session.key)

    def test_an_untagged_turn_is_unsolicited_and_not_routed(self):
        # A human typed it: no tag the hub ever wrote, so it is not the hub's.
        self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answering my human", tagged=False)
        events = poll_once()
        self.assertEqual([e.kind for e in events], [EventKind.UNSOLICITED])
        self.assertEqual(self.injected, [])

    def test_an_unsolicited_turn_is_logged_to_no_conversation(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "private business", tagged=False)
        poll_once()
        self.assertEqual(journal.read_journal(session), "")

    def test_a_tag_for_an_unknown_group_is_not_routed(self):
        self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answer", group="ghost__x-gone")
        self.assertEqual([e.kind for e in poll_once()], [EventKind.UNKNOWN_GROUP])

    def test_a_closed_groups_traffic_is_drained_but_not_routed(self):
        session = self.make_session(COWORKER)
        grp.save_session(session.closed())
        self.drop_capture(COWORKER, "answer", group=session.key)
        self.assertEqual([e.kind for e in poll_once()], [EventKind.UNKNOWN_GROUP])
        self.assertEqual(self.injected, [])

    def test_an_instance_in_no_group_still_has_its_outbox_drained(self):
        # The {cowork} Stop hook fires on EVERY turn for the life of a tagged
        # instance, so draining only active participants would leak one file per
        # turn, forever, in every instance between groups.
        self.drop_capture("lonely__x", "a turn nobody asked about", tagged=False)
        outbox = paths.cowork_outbox_path("lonely__x")
        self.assertEqual([e.kind for e in poll_once()], [EventKind.UNSOLICITED])
        self.assertEqual(list(outbox.glob("*.json")), [])

    def test_draining_survives_an_instance_with_an_empty_outbox(self):
        paths.cowork_outbox_path("idle__x").mkdir(parents=True)
        self.assertEqual(poll_once(), ())

    def test_captures_are_consumed_so_a_second_pass_is_quiet(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answer", group=session.key)
        poll_once()
        self.assertEqual(poll_once(), ())

    def test_a_capture_left_behind_by_a_crash_is_seen_as_a_duplicate(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answer", prompt_id="pX", group=session.key)
        poll_once()
        self.drop_capture(COWORKER, "answer", prompt_id="pX", group=session.key)
        self.assertEqual([e.kind for e in poll_once()], [EventKind.DUPLICATE])

    def test_an_instance_in_two_groups_has_its_outbox_drained_once(self):
        # One outbox per instance: draining per group would double-handle it.
        first = self.make_session(COWORKER)
        second = grp.save_session(
            grp.create_session(MANAGER, "second", "other work").with_coworker(COWORKER))
        self.assertNotEqual(first.key, second.key)
        self.drop_capture(COWORKER, "answer", group=first.key)
        self.assertEqual(len(poll_once()), 1)

    def test_a_reply_is_filed_against_its_own_group_not_the_other(self):
        first = self.make_session(COWORKER)
        second = grp.save_session(
            grp.create_session(MANAGER, "second", "other work").with_coworker(COWORKER))
        self.drop_capture(COWORKER, "this belongs to the second group", group=second.key)
        poll_once()
        self.assertIn("second group", journal.read_journal(second))
        self.assertEqual(journal.read_journal(first), "")


class TestPollRouting(RelayHarness):
    def test_a_coworkers_reply_notifies_the_manager(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "I finished it", group=session.key)
        poll_once()
        self.assertEqual(len(self.prompts_to(MANAGER)), 1)
        self.assertIn("I finished it", self.staged_for(MANAGER, session)[0])

    def test_a_managers_own_turn_is_logged_but_never_forwarded(self):
        # The hub injects notifications INTO the manager, so forwarding its
        # replies would close a notify -> reply -> notify loop.
        session = self.make_session(COWORKER)
        self.drop_capture(MANAGER, "noted, thanks", group=session.key)
        events = poll_once()
        self.assertEqual([e.kind for e in events], [EventKind.LOGGED])
        self.assertEqual(self.injected, [])
        self.assertIn("noted, thanks", journal.read_journal(session))

    def test_a_reply_is_logged_before_it_is_forwarded(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "my reply text", group=session.key)
        poll_once()
        self.assertIn("my reply text", journal.read_journal(session))

    def test_the_log_does_not_carry_the_reply_text_twice(self):
        # The notification quotes the reply, so logging it verbatim would repeat
        # the text a relaunched participant then pays to re-read.
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "a distinctive sentence", group=session.key)
        poll_once()
        self.assertEqual(journal.read_journal(session).count("a distinctive sentence"), 1)

    def test_the_log_still_records_that_the_manager_was_notified(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "reply", group=session.key)
        poll_once()
        self.assertIn("notified", journal.read_journal(session))

    def test_the_manager_still_receives_the_full_reply(self):
        # Shortening the LOG entry must not shorten the message itself.
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "a distinctive sentence", group=session.key)
        poll_once()
        self.assertIn("a distinctive sentence", self.staged_for(MANAGER, session)[0])

    def test_submitted_files_reach_the_managers_inbox(self):
        session = self.make_session(COWORKER)
        self.plant_work(COWORKER, session, "answer.py", "the code\n")
        self.drop_capture(COWORKER, "done", group=session.key)
        poll_once()
        inbox = paths.cowork_inbox_path(MANAGER, session.key, COWORKER)
        self.assertEqual((inbox / "answer.py").read_text(), "the code\n")

    def test_the_files_are_already_in_the_inbox_when_the_manager_is_woken(self):
        # The ordering guarantee, not just the end state: a manager told about a
        # reply before its files landed would diff an empty inbox and conclude the
        # coworker did nothing. Asserted AT the moment of injection, because that
        # is when the manager can first act.
        session = self.make_session(COWORKER)
        self.plant_work(COWORKER, session, "answer.py", "the code\n")
        inbox = paths.cowork_inbox_path(MANAGER, session.key, COWORKER)
        seen: list[bool] = []

        def record(instance, prompt):
            if instance == MANAGER:
                seen.append((inbox / "answer.py").is_file())
            return True

        with patch.object(relay, "docker_attach_inject", record):
            self.drop_capture(COWORKER, "done", group=session.key)
            poll_once()
        self.assertEqual(seen, [True])

    def test_the_notification_names_the_changed_files(self):
        session = self.make_session(COWORKER)
        self.plant_work(MANAGER, session, "task.py", "before\n")
        self.plant_work(COWORKER, session, "task.py", "after\n")
        self.drop_capture(COWORKER, "edited it", group=session.key)
        poll_once()
        self.assertIn("task.py", self.staged_for(MANAGER, session)[0])

    def test_the_notification_quotes_a_command_the_manager_can_actually_run(self):
        # Container paths, not the host paths the hub copies between.
        session = self.make_session(COWORKER)
        self.plant_work(COWORKER, session, "task.py", "x\n")
        self.drop_capture(COWORKER, "done", group=session.key)
        poll_once()
        body = self.staged_for(MANAGER, session)[0]
        self.assertIn(f"{paths.COWORK_IN_CONTAINER}/{session.key}", body)
        self.assertNotIn(str(self.root), body)

    def test_a_reply_with_no_files_gets_no_file_section(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "just answering, no files", group=session.key)
        poll_once()
        self.assertNotIn("Files submitted", self.staged_for(MANAGER, session)[0])

    def test_the_event_reports_how_many_files_changed(self):
        session = self.make_session(COWORKER)
        self.plant_work(COWORKER, session, "a.py", "1\n")
        self.drop_capture(COWORKER, "done", group=session.key)
        self.assertIn("1 file", poll_once()[0].detail)

    def test_two_coworkers_replying_in_one_pass_both_route(self):
        session = self.make_session(COWORKER, OTHER)
        self.drop_capture(COWORKER, "from a", prompt_id="pa", group=session.key)
        self.drop_capture(OTHER, "from b", prompt_id="pb", group=session.key)
        events = poll_once()
        self.assertEqual({e.instance for e in events}, {COWORKER, OTHER})
        self.assertEqual(len(self.prompts_to(MANAGER)), 2)

    def test_a_reply_clears_what_the_hub_was_waiting_for(self):
        session = self.make_session(COWORKER)
        send(session, sender=MANAGER, recipient=COWORKER, body="the ask")
        self.assertEqual(
            grp.load_hub_state().for_participant(COWORKER).outstanding_send, session.key)
        self.drop_capture(COWORKER, "the answer", group=session.key)
        poll_once()
        self.assertIsNone(
            grp.load_hub_state().for_participant(COWORKER).outstanding_send)


class TestOverdueSends(RelayHarness):
    def _state(self, **over) -> HubState:
        fields = dict(outstanding_send="boss__proj-widget", sent_at=1000.0)
        return HubState(participants={COWORKER: ParticipantState(**{**fields, **over})})

    def test_a_recent_send_is_not_overdue(self):
        self.assertEqual(relay.overdue_sends(self._state(), now=1001.0), ())

    def test_a_long_unanswered_send_is_reported_with_how_long(self):
        overdue = relay.overdue_sends(self._state(),
                                      now=1000.0 + relay.UNDELIVERED_AFTER + 60)
        self.assertEqual(len(overdue), 1)
        instance, group, waited = overdue[0]
        self.assertEqual(instance, COWORKER)
        self.assertGreater(waited, relay.UNDELIVERED_AFTER)

    def test_a_participant_owing_nothing_is_never_overdue(self):
        self.assertEqual(relay.overdue_sends(self._state(outstanding_send=None),
                                             now=9_999_999.0), ())

    def test_a_missing_timestamp_does_not_crash_the_check(self):
        self.assertEqual(relay.overdue_sends(self._state(sent_at=None),
                                             now=9_999_999.0), ())


class TestServe(RelayHarness):
    def test_a_bounded_run_drains_and_returns(self):
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answer", group=session.key)
        with patch.object(relay.time, "sleep", lambda _: None):
            relay.serve(interval=0, report=False, passes=1)
        self.assertEqual(len(self.prompts_to(MANAGER)), 1)

    def test_stop_ends_an_endless_run_with_the_reason(self):
        # passes=None would loop forever; `stop` is what ends the hub's real life.
        import io
        from contextlib import redirect_stdout
        answers = iter([None, "no managers remain"])
        buffer = io.StringIO()
        with patch.object(relay.time, "sleep", lambda _: None), redirect_stdout(buffer):
            relay.serve(interval=0, passes=None, stop=lambda: next(answers))
        self.assertIn("hub stopping: no managers remain", buffer.getvalue())

    def test_stop_is_consulted_after_the_pass_not_before(self):
        # A shutdown-worthy state must not leave that pass's captures undrained.
        session = self.make_session(COWORKER)
        self.drop_capture(COWORKER, "answer", group=session.key)
        with patch.object(relay.time, "sleep", lambda _: None):
            relay.serve(interval=0, report=False, passes=None, stop=lambda: "done")
        self.assertEqual(len(self.prompts_to(MANAGER)), 1)   # the capture was routed first


class TestRoundTrip(RelayHarness):
    def test_ask_reply_notify_end_to_end(self):
        session = self.make_session(COWORKER)
        self.plant_work(MANAGER, session, "task.py", "TODO\n")

        # The manager asks, and the hub hands the work over with the message.
        asked = send(session, sender=MANAGER, recipient=COWORKER, body="please finish this")
        self.assertTrue(asked.delivered)
        sync.hand_over(asked.session, COWORKER)
        handed = paths.cowork_inbox_path(COWORKER, session.key, MANAGER)
        self.assertEqual((handed / "task.py").read_text(), "TODO\n")

        # The coworker takes it up, works, and its turn ends.
        self.plant_work(COWORKER, session, "task.py", "DONE\n")
        self.drop_capture(COWORKER, "finished — see task.py", group=session.key)
        events = poll_once()

        self.assertEqual([e.kind for e in events], [EventKind.REPLY])
        inbox = paths.cowork_inbox_path(MANAGER, session.key, COWORKER)
        self.assertEqual((inbox / "task.py").read_text(), "DONE\n")
        notification = self.staged_for(MANAGER, session)[0]
        self.assertIn("finished", notification)
        self.assertIn("task.py", notification)
        log = journal.read_journal(session)
        self.assertLess(log.index("please finish this"), log.index("finished"))


if __name__ == "__main__":
    unittest.main()
