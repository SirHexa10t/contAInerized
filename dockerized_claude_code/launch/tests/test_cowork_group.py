"""Tests for launch.cowork.group — group identity, durable state, discovery.

Every test redirects the group-hosting root into a tmpdir, because these
functions are all about real on-disk layout: asserting against a mocked
filesystem would test the mock, not the scan.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import group as grp
from launch.cowork.group import (
    GroupStatus, HubState, ParticipantState, Session, create_session,
    discover_sessions, hosted_by, load_hub_state, load_session, save_hub_state,
    save_session, session_dir, sessions_for,
)


class CoworkTmpRoot(unittest.TestCase):
    """Point the group-hosting root at a tmpdir for the duration of a test.

    ONE patch does it: every group-hosting path is built by a `paths` lambda that
    reads `AGENTS_STATE` at call time, so redirecting that constant moves the whole
    feature — including modules that imported a builder by name, since the lookup
    happens inside `paths`. That is exactly why the root is a builder rather than a
    constant; a module holding an imported constant would silently keep writing to
    the real state dir.
    """

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        p = patch.object(paths, "AGENTS_STATE", self.state)
        p.start()
        self.addCleanup(p.stop)
        self.root = paths.group_hosting_dir()      # derived, so a layout change follows
        self.root.mkdir(parents=True)              # the launcher creates it as a mount target


class TestSessionShape(unittest.TestCase):
    """Pure Session behaviour — no filesystem involved."""

    def _s(self, **over):
        fields = dict(manager="planner__x", project="edge_case_tests", task="do a thing")
        return Session(**{**fields, **over})

    def test_key_is_manager_and_project(self):
        self.assertEqual(self._s().key, "planner__x-edge_case_tests")

    def test_participants_lists_manager_first(self):
        s = self._s(coworkers=("golem__a", "poet__b"))
        self.assertEqual(s.participants, ("planner__x", "golem__a", "poet__b"))

    def test_with_coworker_is_idempotent(self):
        s = self._s().with_coworker("golem__a")
        self.assertIs(s.with_coworker("golem__a"), s)   # re-recruit must not duplicate

    def test_manager_is_never_added_as_a_coworker(self):
        s = self._s()
        self.assertEqual(s.with_coworker("planner__x").coworkers, ())

    def test_rounds_left_never_negative(self):
        self.assertEqual(self._s(round_budget=2, rounds_used=5).rounds_left, 0)

    def test_round_and_close_transitions(self):
        s = self._s(round_budget=3)
        self.assertEqual(s.with_round_used().rounds_used, 1)
        self.assertEqual(s.closed().status, GroupStatus.CLOSED)
        self.assertEqual(s.status, GroupStatus.ACTIVE)   # frozen: original untouched

    def test_json_round_trip(self):
        s = self._s(coworkers=("golem__a",), rounds_used=2, status=GroupStatus.CLOSED)
        self.assertEqual(Session.from_payload(json.loads(s.to_json())), s)


class TestSeparatorGuard(CoworkTmpRoot):
    """An inbox dir is `<group>@<sender>`, sitting as a sibling of the group dirs.
    Nothing that composes a group key may carry `@`, or an inbox name could
    collide with a real group's directory."""

    def test_a_coworker_id_with_the_separator_is_rejected(self):
        s = Session(manager="planner__x", project="proj", task="t")
        with self.assertRaises(ValueError):
            s.with_coworker(f"golem{paths.INBOX_SEPARATOR}a__b")

    def test_a_manager_id_with_the_separator_is_rejected(self):
        with self.assertRaises(ValueError):
            grp.create_session(f"planner{paths.INBOX_SEPARATOR}x", "proj", "t")

    def test_a_project_label_with_the_separator_is_rejected(self):
        with self.assertRaises(ValueError):
            grp.create_session("planner__x", f"proj{paths.INBOX_SEPARATOR}b", "t")

    def test_a_rejected_session_is_not_persisted(self):
        # The guard runs before any write, so a bad name leaves no half-made group.
        with self.assertRaises(ValueError):
            grp.create_session("planner__x", f"proj{paths.INBOX_SEPARATOR}b", "t")
        self.assertEqual(grp.discover_sessions(), [])

    def test_the_error_names_the_offending_value(self):
        with self.assertRaises(ValueError) as caught:
            grp.create_session("planner__x", "bad@label", "t")
        self.assertIn("bad@label", str(caught.exception))

    def test_ordinary_names_pass(self):
        s = grp.create_session("planner__x", "edge_case_tests", "t")
        self.assertEqual(s.with_coworker("golem__a").coworkers, ("golem__a",))

    def test_another_manager_is_a_valid_coworker(self):
        # {manager} nests inside {cowork}, so a manager-tagged instance is one of
        # the most capable coworkers available — only SELF is refused.
        s = Session(manager="planner__x", project="proj", task="t")
        self.assertEqual(s.with_coworker("planner__y").coworkers, ("planner__y",))


class TestSessionPayloadRejection(unittest.TestCase):
    """A corrupt session.json must yield None, not an exception — one bad group
    directory cannot be allowed to break discovery for every other group."""

    def test_missing_manager_or_project(self):
        self.assertIsNone(Session.from_payload({"project": "p"}))
        self.assertIsNone(Session.from_payload({"manager": "m"}))

    def test_wrong_types(self):
        self.assertIsNone(Session.from_payload({"manager": 1, "project": "p"}))
        self.assertIsNone(Session.from_payload(
            {"manager": "m", "project": "p", "coworkers": "not-a-list"}))
        self.assertIsNone(Session.from_payload(
            {"manager": "m", "project": "p", "coworkers": [1, 2]}))

    def test_unknown_status(self):
        self.assertIsNone(Session.from_payload(
            {"manager": "m", "project": "p", "status": "sideways"}))


class TestSessionPersistence(CoworkTmpRoot):
    def test_save_then_load(self):
        saved = save_session(Session(manager="planner__x", project="proj", task="t"))
        self.assertEqual(load_session(session_dir(saved)), saved)

    def test_save_stamps_timestamps(self):
        saved = save_session(Session(manager="m__1", project="p", task="t"), now=1234.0)
        self.assertEqual((saved.created_at, saved.updated_at), (1234.0, 1234.0))
        again = save_session(saved, now=5678.0)
        self.assertEqual(again.created_at, 1234.0)      # created_at is not reset
        self.assertEqual(again.updated_at, 5678.0)

    def test_load_missing_returns_none_without_raising(self):
        # Regression: read_text raises FileNotFoundError, and "no session.json
        # here" is the COMMON case (working copies, inbox dirs), not an error.
        self.assertIsNone(load_session(self.root / "nobody" / "nothing"))

    def test_load_unparseable_returns_none(self):
        d = self.root / "m__1" / "m__1-p"
        d.mkdir(parents=True)
        (d / "session.json").write_text("{not json")
        self.assertIsNone(load_session(d))

    def test_create_session_is_idempotent(self):
        first = create_session("m__1", "p", "task one")
        second = create_session("m__1", "p", "task two")
        # Re-issuing recruit must not reset progress or overwrite the task.
        self.assertEqual(second.task, "task one")
        self.assertEqual(second.created_at, first.created_at)

    def test_create_session_preserves_round_progress(self):
        save_session(save_session(Session(manager="m__1", project="p", task="t")).with_round_used())
        self.assertEqual(create_session("m__1", "p", "t").rounds_used, 1)


class TestDiscovery(CoworkTmpRoot):
    """Discovery is a scan for session.json, so the fixtures here are real
    directory trees rather than stubbed return values."""

    def _write(self, instance, dirname, payload):
        d = self.root / instance / dirname
        d.mkdir(parents=True)
        (d / "session.json").write_text(json.dumps(payload))
        return d

    def _valid(self, manager="m__1", project="p", **over):
        return {"manager": manager, "project": project, "task": "t", **over}

    def test_finds_a_group(self):
        self._write("m__1", "m__1-p", self._valid())
        self.assertEqual([s.key for s in discover_sessions()], ["m__1-p"])

    def test_ignores_dirs_without_session_json(self):
        (self.root / "golem__a" / "m__1-p").mkdir(parents=True)      # a coworker's working copy
        (self.root / "m__1" / "m__1-p-golem__a").mkdir(parents=True)  # a manager's inbox
        self.assertEqual(discover_sessions(), [])

    def test_ignores_group_filed_under_the_wrong_manager(self):
        # A group dir under someone else's tree is misplaced, not authoritative.
        self._write("golem__a", "m__1-p", self._valid(manager="m__1"))
        self.assertEqual(discover_sessions(), [])

    def test_ignores_dir_whose_name_disagrees_with_its_key(self):
        self._write("m__1", "m__1-somethingelse", self._valid(project="p"))
        self.assertEqual(discover_sessions(), [])

    def test_one_corrupt_group_does_not_hide_the_others(self):
        self._write("m__1", "m__1-good", self._valid(project="good"))
        bad = self.root / "m__2" / "m__2-bad"
        bad.mkdir(parents=True)
        (bad / "session.json").write_text("garbage")
        self.assertEqual([s.key for s in discover_sessions()], ["m__1-good"])

    def test_results_are_sorted_by_key(self):
        self._write("m__1", "m__1-b", self._valid(project="b"))
        self._write("m__1", "m__1-a", self._valid(project="a"))
        self.assertEqual([s.key for s in discover_sessions()], ["m__1-a", "m__1-b"])

    def test_sessions_for_matches_manager_and_coworker(self):
        self._write("m__1", "m__1-p", self._valid(coworkers=["golem__a"]))
        self.assertEqual(len(sessions_for("m__1")), 1)
        self.assertEqual(len(sessions_for("golem__a")), 1)
        self.assertEqual(sessions_for("stranger__z"), [])

    def test_hosted_by_excludes_groups_merely_joined(self):
        self._write("m__1", "m__1-p", self._valid(coworkers=["golem__a"]))
        self.assertEqual(len(hosted_by("m__1")), 1)
        self.assertEqual(hosted_by("golem__a"), [])

    def test_missing_root_yields_nothing(self):
        with patch.object(paths, "AGENTS_STATE", self.state / "never-created"):
            self.assertEqual(discover_sessions(), [])


class TestHubState(CoworkTmpRoot):
    """Hub state is the hub's private bookkeeping. It deliberately holds no
    group membership — that is session.json's job, rediscovered by scanning."""

    def test_absent_state_loads_empty(self):
        self.assertEqual(load_hub_state().participants, {})

    def test_round_trip(self):
        state = HubState(participants={}).with_participant(
            "golem__a", ParticipantState(last_prompt_id="uuid-1",
                                         outstanding_send="m__1-p", sent_at=99.0))
        save_hub_state(state)
        loaded = load_hub_state()
        self.assertEqual(loaded.for_participant("golem__a").last_prompt_id, "uuid-1")
        self.assertEqual(loaded.for_participant("golem__a").outstanding_send, "m__1-p")

    def test_unknown_participant_gets_a_default(self):
        self.assertEqual(load_hub_state().for_participant("nobody__x"), ParticipantState())

    def test_corrupt_state_degrades_to_empty(self):
        # Better to reprocess a few turns than to refuse to start and strand
        # every group.
        paths.hub_state_path().write_text("{{{")
        self.assertEqual(load_hub_state().participants, {})

    def test_future_schema_degrades_to_empty(self):
        paths.hub_state_path().write_text(
            json.dumps({"schema": 999, "participants": {"a": {}}}))
        self.assertEqual(load_hub_state().participants, {})

    def test_carries_no_group_membership(self):
        save_hub_state(HubState(participants={"golem__a": ParticipantState()}))
        payload = json.loads((self.root / "hub.state.json").read_text())
        self.assertEqual(set(payload), {"schema", "participants"})

    def test_with_participant_does_not_mutate_the_original(self):
        original = HubState(participants={})
        original.with_participant("a__1", ParticipantState(outstanding_send="g"))
        self.assertEqual(original.participants, {})


if __name__ == "__main__":
    unittest.main()
