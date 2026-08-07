"""Tests for launch.cowork.sync — the check-out / submit file plane.

The invariants under test are ownership ones. Every participant owns `<group>/`
and only the hub writes `<group>@<sender>/`, so no transfer may ever land on a
dir its owner writes — in EITHER direction. Several tests assert that from both
sides rather than once, because the symmetry is the design.

Paths are built with the `paths.py` builders rather than spelled out, so a layout
change moves these tests with it instead of breaking them.
"""

import shlex
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import sync
from launch.cowork.group import Session
from launch.cowork.sync import (
    HUB_OWNED, hand_over, not_taken_up, review_command, submit, work_files,
)

MANAGER = "boss__proj"
COWORKER = "golem__a"
OTHER = "golem__b"


def _session(**over) -> Session:
    """A group with both peers already recruited.

    Membership is part of the fixture because it is part of reality: the hub
    refuses to copy files to or from a non-member, so a session with no coworkers
    cannot exchange anything and would only ever test the refusal."""
    fields = dict(manager=MANAGER, project="widget", task="build it",
                  coworkers=(COWORKER, OTHER))
    return Session(**{**fields, **over})


class CoworkTempTree(unittest.TestCase):
    """Redirects the whole group-hosting tree into a temp dir.

    One patch covers every module: each group-hosting path is built by a `paths`
    lambda that reads `AGENTS_STATE` at call time, so nothing here can keep a stale
    binding to the real state dir."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.retarget(self.state)
        self.root = paths.group_hosting_dir()

    def retarget(self, state: Path) -> None:
        p = patch.object(paths, "AGENTS_STATE", state)
        p.start()
        self.addCleanup(p.stop)

    def group_dir(self, instance: str, session: Session) -> Path:
        """A participant's own working copy — the dir it writes."""
        return paths.cowork_group_path(instance, session.key)

    def inbox(self, owner: str, session: Session, sender: str) -> Path:
        """An inbox in `owner`'s tree holding what `sender` sent."""
        return paths.cowork_inbox_path(owner, session.key, sender)

    def plant(self, instance: str, session: Session, name: str, body: str) -> Path:
        """Write a file into a participant's working copy, creating parents."""
        return self.write(self.group_dir(instance, session) / name, body)

    def write(self, path: Path, body: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return path


class TestWorkFiles(CoworkTempTree):
    def test_lists_files_relative_to_the_group_dir(self):
        s = _session()
        self.plant(MANAGER, s, "notes.md", "x")
        self.assertEqual(work_files(self.group_dir(MANAGER, s)), (Path("notes.md"),))

    def test_recurses_into_subdirectories(self):
        s = _session()
        self.plant(MANAGER, s, "src/deep/mod.py", "x")
        self.assertEqual(work_files(self.group_dir(MANAGER, s)),
                         (Path("src/deep/mod.py"),))

    def test_excludes_every_hub_owned_root_entry(self):
        # Covers the whole set at once, so a name added to HUB_OWNED without a
        # matching exclusion shows up here rather than in production.
        s = _session()
        for name in HUB_OWNED:
            self.plant(MANAGER, s, name, "hub bookkeeping")
        self.plant(MANAGER, s, "real.py", "x")
        self.assertEqual(work_files(self.group_dir(MANAGER, s)), (Path("real.py"),))

    def test_excludes_a_hub_owned_directorys_contents(self):
        s = _session()
        self.plant(MANAGER, s, f"{sync.MESSAGES_SUBDIR}/001-from-boss.md", "the ask")
        self.assertEqual(work_files(self.group_dir(MANAGER, s)), ())

    def test_hub_owned_names_are_only_excluded_at_the_root(self):
        # A participant's own nested `messages/` is its work, not hub bookkeeping.
        s = _session()
        self.plant(MANAGER, s, "src/messages/greeting.md", "x")
        self.assertEqual(work_files(self.group_dir(MANAGER, s)),
                         (Path("src/messages/greeting.md"),))

    def test_missing_group_dir_lists_nothing(self):
        self.assertEqual(work_files(self.group_dir("nobody__x", _session())), ())

    def test_hub_owned_is_derived_from_the_path_builders(self):
        # Guards the drift this set exists to prevent: renaming either file in
        # paths.py must not leave sync copying it between trees.
        self.assertIn(paths.group_session_path(Path()).name, HUB_OWNED)
        self.assertIn(paths.group_conversation_path(Path()).name, HUB_OWNED)


class TestNonDestructive(CoworkTempTree):
    """The reason inboxes exist on both sides: neither direction may write onto a
    dir its owner is working in."""

    def test_hand_over_does_not_touch_the_coworkers_working_copy(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "the manager's newer version")
        self.plant(COWORKER, s, "task.py", "work in progress")
        hand_over(s, COWORKER)
        self.assertEqual((self.group_dir(COWORKER, s) / "task.py").read_text(),
                         "work in progress")

    def test_hand_over_lands_in_the_coworkers_inbox_from_the_manager(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "please do this")
        result = hand_over(s, COWORKER)
        self.assertEqual(result.inbox, self.inbox(COWORKER, s, MANAGER))
        self.assertEqual((result.inbox / "task.py").read_text(), "please do this")

    def test_submit_does_not_touch_the_managers_working_copy(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "canonical")
        self.plant(COWORKER, s, "task.py", "the coworker's version")
        submit(s, COWORKER)
        self.assertEqual((self.group_dir(MANAGER, s) / "task.py").read_text(),
                         "canonical")

    def test_submit_lands_in_the_managers_inbox_from_that_coworker(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "done")
        result = submit(s, COWORKER)
        self.assertEqual(result.inbox, self.inbox(MANAGER, s, COWORKER))
        self.assertEqual((result.inbox / "task.py").read_text(), "done")

    def test_an_unsubmitted_edit_survives_a_second_hand_over(self):
        # The exact loss the coworker inbox was introduced to prevent.
        s = _session()
        self.plant(MANAGER, s, "task.py", "round one")
        hand_over(s, COWORKER)
        self.write(self.group_dir(COWORKER, s) / "task.py", "half-finished work")
        self.plant(MANAGER, s, "task.py", "round two")
        hand_over(s, COWORKER)
        self.assertEqual((self.group_dir(COWORKER, s) / "task.py").read_text(),
                         "half-finished work")

    def test_the_two_directions_use_different_inboxes(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "down")
        self.plant(COWORKER, s, "task.py", "up")
        self.assertNotEqual(hand_over(s, COWORKER).inbox, submit(s, COWORKER).inbox)

    def test_an_inbox_name_can_never_equal_a_group_name(self):
        # Both are siblings in one dir, so a collision would merge two roles. The
        # separator is what makes it structurally impossible.
        s = _session()
        self.assertNotIn(paths.INBOX_SEPARATOR, s.key)
        self.assertIn(paths.INBOX_SEPARATOR, self.inbox(MANAGER, s, COWORKER).name)


class TestHandOver(CoworkTempTree):
    def test_reports_what_was_sent(self):
        s = _session()
        self.plant(MANAGER, s, "a.py", "1")
        self.plant(MANAGER, s, "b.py", "2")
        self.assertEqual(hand_over(s, COWORKER).files, (Path("a.py"), Path("b.py")))

    def test_preserves_the_directory_layout(self):
        s = _session()
        self.plant(MANAGER, s, "pkg/mod.py", "x")
        self.assertTrue((hand_over(s, COWORKER).inbox / "pkg" / "mod.py").is_file())

    def test_changed_is_what_moved_upstream_since_the_coworker_last_looked(self):
        # The signal overwrite semantics could not give a coworker at all.
        s = _session()
        self.plant(MANAGER, s, "steady.py", "same")
        self.plant(MANAGER, s, "moved.py", "after")
        self.plant(COWORKER, s, "steady.py", "same")
        self.plant(COWORKER, s, "moved.py", "before")
        self.assertEqual(hand_over(s, COWORKER).changed, (Path("moved.py"),))

    def test_everything_is_new_on_the_first_hand_over(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "x")
        result = hand_over(s, COWORKER)
        self.assertEqual(result.changed, result.files)

    def test_does_not_send_the_session_state(self):
        # session.json marks a dir as a group; copying it would make an inbox
        # look like a second group to discovery.
        s = _session()
        self.plant(MANAGER, s, "session.json", "{}")
        self.plant(MANAGER, s, "task.py", "x")
        self.assertFalse((hand_over(s, COWORKER).inbox / "session.json").exists())

    def test_does_not_send_the_conversation_log(self):
        s = _session()
        self.plant(MANAGER, s, "conversation.md", "the whole thread")
        self.plant(MANAGER, s, "task.py", "x")
        self.assertFalse((hand_over(s, COWORKER).inbox / "conversation.md").exists())

    def test_each_coworker_gets_its_own_inbox(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "shared")
        first, second = hand_over(s, COWORKER), hand_over(s, OTHER)
        self.assertNotEqual(first.inbox, second.inbox)
        (first.inbox / "task.py").write_text("tampered")
        self.assertEqual((second.inbox / "task.py").read_text(), "shared")

    def test_groups_stay_in_separate_directories(self):
        one, two = _session(project="alpha"), _session(project="beta")
        self.plant(MANAGER, one, "a.py", "alpha")
        self.plant(MANAGER, two, "b.py", "beta")
        self.assertFalse((hand_over(one, COWORKER).inbox / "b.py").exists())

    def test_nothing_to_send_creates_nothing(self):
        s = _session()
        result = hand_over(s, COWORKER)
        self.assertEqual(result.files, ())
        self.assertFalse(result.inbox.exists())


class TestSubmit(CoworkTempTree):
    def test_two_coworkers_land_in_separate_inboxes(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "a's answer")
        self.plant(OTHER, s, "task.py", "b's answer")
        first, second = submit(s, COWORKER), submit(s, OTHER)
        self.assertNotEqual(first.inbox, second.inbox)
        self.assertEqual((first.inbox / "task.py").read_text(), "a's answer")

    def test_inbox_is_a_full_snapshot_not_just_the_changes(self):
        s = _session()
        for name in ("a.py", "b.py"):
            self.plant(MANAGER, s, name, "same")
            self.plant(COWORKER, s, name, "same")
        self.plant(COWORKER, s, "c.py", "new")
        self.assertEqual(set(submit(s, COWORKER).files),
                         {Path("a.py"), Path("b.py"), Path("c.py")})

    def test_changed_names_only_what_differs_from_the_managers_copy(self):
        s = _session()
        self.plant(MANAGER, s, "untouched.py", "same")
        self.plant(MANAGER, s, "edited.py", "before")
        self.plant(COWORKER, s, "untouched.py", "same")
        self.plant(COWORKER, s, "edited.py", "after")
        self.assertEqual(submit(s, COWORKER).changed, (Path("edited.py"),))

    def test_a_file_the_manager_does_not_have_counts_as_changed(self):
        s = _session()
        self.plant(COWORKER, s, "brand_new.py", "x")
        self.assertEqual(submit(s, COWORKER).changed, (Path("brand_new.py"),))

    def test_a_turn_that_changed_nothing_is_visible_as_an_empty_change_set(self):
        # Distinguishes "did the work" from "touched nothing" — without it the
        # relay would send the manager to diff an identical tree.
        s = _session()
        self.plant(MANAGER, s, "task.py", "same")
        self.plant(COWORKER, s, "task.py", "same")
        result = submit(s, COWORKER)
        self.assertEqual(result.files, (Path("task.py"),))
        self.assertEqual(result.changed, ())

    def test_staged_messages_are_not_submitted_back(self):
        # The hub wrote them; echoing them into the inbox is pure noise.
        s = _session()
        self.plant(COWORKER, s, f"{sync.MESSAGES_SUBDIR}/001-from-boss.md", "the ask")
        self.plant(COWORKER, s, "task.py", "x")
        self.assertEqual(submit(s, COWORKER).files, (Path("task.py"),))

    def test_a_coworkers_own_inbox_is_not_submitted_back(self):
        # The coworker's inbox-from-manager is a sibling of its working copy, not
        # inside it — so what the manager sent can never echo back as new work.
        s = _session()
        self.plant(MANAGER, s, "task.py", "handed over")
        hand_over(s, COWORKER)
        self.plant(COWORKER, s, "answer.py", "mine")
        self.assertEqual(submit(s, COWORKER).files, (Path("answer.py"),))

    def test_submitting_nothing_leaves_no_empty_inbox_behind(self):
        # An inbox dir that exists reads as "a submission arrived".
        s = _session()
        result = submit(s, COWORKER)
        self.assertEqual(result.files, ())
        self.assertFalse(result.inbox.exists())

    def test_a_later_round_overwrites_the_earlier_submission(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "first pass")
        submit(s, COWORKER)
        self.plant(COWORKER, s, "task.py", "second pass")
        self.assertEqual((submit(s, COWORKER).inbox / "task.py").read_text(),
                         "second pass")

    def test_an_unchanged_file_keeps_its_inbox_mtime_across_rounds(self):
        # So a recipient sorting an inbox by mtime sees the work, not the transfer.
        s = _session()
        self.plant(COWORKER, s, "steady.py", "same")
        landed = submit(s, COWORKER).inbox / "steady.py"
        first = landed.stat().st_mtime_ns
        submit(s, COWORKER)
        self.assertEqual(landed.stat().st_mtime_ns, first)


class TestNotTakenUp(CoworkTempTree):
    """Material sent but never picked up. Absence, not difference — a recipient
    that took a file up and improved it must NOT be flagged, or the check fires on
    every healthy round and stops meaning anything."""

    def test_nothing_is_outstanding_before_anything_is_sent(self):
        self.assertEqual(
            not_taken_up(_session(), recipient=COWORKER, sender=MANAGER), ())

    def test_a_freshly_sent_file_is_outstanding(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "please do this")
        hand_over(s, COWORKER)
        self.assertEqual(not_taken_up(s, recipient=COWORKER, sender=MANAGER),
                         (Path("task.py"),))

    def test_copying_the_inbox_across_clears_it(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "please do this")
        hand_over(s, COWORKER)
        self.plant(COWORKER, s, "task.py", "please do this")
        self.assertEqual(not_taken_up(s, recipient=COWORKER, sender=MANAGER), ())

    def test_a_file_taken_up_and_then_revised_is_not_flagged(self):
        # The false positive that made a difference-based check useless: work in
        # progress necessarily diverges from what was sent.
        s = _session()
        self.plant(MANAGER, s, "task.py", "the ask")
        hand_over(s, COWORKER)
        self.plant(COWORKER, s, "task.py", "my own, better version")
        self.assertEqual(not_taken_up(s, recipient=COWORKER, sender=MANAGER), ())

    def test_reports_only_the_files_never_picked_up(self):
        s = _session()
        self.plant(MANAGER, s, "taken.py", "a")
        self.plant(MANAGER, s, "ignored.py", "b")
        hand_over(s, COWORKER)
        self.plant(COWORKER, s, "taken.py", "a, then edited")
        self.assertEqual(not_taken_up(s, recipient=COWORKER, sender=MANAGER),
                         (Path("ignored.py"),))

    def test_works_in_the_manager_direction_too(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "submitted")
        submit(s, COWORKER)
        self.assertEqual(not_taken_up(s, recipient=MANAGER, sender=COWORKER),
                         (Path("task.py"),))

    def test_distinguishes_between_two_coworkers_inboxes(self):
        s = _session()
        self.plant(COWORKER, s, "a.py", "from a")
        submit(s, COWORKER)
        self.assertEqual(not_taken_up(s, recipient=MANAGER, sender=OTHER), ())


class TestReviewCommand(CoworkTempTree):
    """The command handed to a recipient has to produce ONLY real differences —
    a plain `diff -r` reports the hub's own files as missing from every inbox.

    It is addressed to an agent INSIDE a container, so it names container paths.
    That means the command cannot be run as-issued from these tests; `_run_on_host`
    re-points the same argument list at the host tree to prove the exclusions have
    the effect claimed, while the string-level tests cover the paths themselves."""

    def _run_on_host(self, delivery) -> str:
        argv = shlex.split(review_command(delivery))
        argv[-2:] = [str(delivery.working), str(delivery.inbox)]
        return subprocess.run(argv, capture_output=True, text=True).stdout

    def test_excludes_every_hub_owned_name(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "x")
        command = review_command(submit(s, COWORKER))
        for name in HUB_OWNED:
            self.assertIn(f"-x {name}", command)

    def test_is_stable_across_calls(self):
        # HUB_OWNED is a set; an unsorted expansion would reorder between runs.
        s = _session()
        self.plant(COWORKER, s, "task.py", "x")
        result = submit(s, COWORKER)
        self.assertEqual(review_command(result), review_command(result))

    def test_names_container_paths_not_the_hosts(self):
        # A host path is meaningless inside the container that has to run this.
        s = _session()
        self.plant(COWORKER, s, "task.py", "x")
        result = submit(s, COWORKER)
        command = review_command(result)
        self.assertNotIn(str(self.root), command)
        self.assertIn(f"{paths.COWORK_IN_CONTAINER}/{s.key}", command)

    def test_both_operands_sit_under_the_mount_point(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "x")
        for operand in shlex.split(review_command(submit(s, COWORKER)))[-2:]:
            self.assertEqual(Path(operand).parent, paths.COWORK_IN_CONTAINER)

    def test_quotes_paths_so_a_space_cannot_split_an_argument(self):
        # A session suffix is free text, so a group key can hold a space.
        s = _session(manager="boss__two words")
        self.plant(COWORKER, s, "task.py", "x")
        result = submit(s, COWORKER)
        self.assertEqual(shlex.split(review_command(result))[-2:],
                         [f"{paths.COWORK_IN_CONTAINER}/{s.key}",
                          f"{paths.COWORK_IN_CONTAINER}/{result.inbox.name}"])

    def test_compares_the_recipients_own_copy_against_the_inbox(self):
        s = _session()
        self.plant(COWORKER, s, "task.py", "x")
        result = submit(s, COWORKER)
        self.assertEqual(result.working, self.group_dir(MANAGER, s))

    def test_reports_a_real_edit(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "before\n")
        self.plant(COWORKER, s, "task.py", "after\n")
        self.assertIn("after", self._run_on_host(submit(s, COWORKER)))

    def test_reports_nothing_when_the_sender_changed_nothing(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "same\n")
        self.plant(MANAGER, s, "session.json", "{}")
        self.plant(MANAGER, s, "conversation.md", "the log\n")
        self.plant(MANAGER, s, f"{sync.MESSAGES_SUBDIR}/001.md", "the ask\n")
        self.plant(COWORKER, s, "task.py", "same\n")
        self.assertEqual(self._run_on_host(submit(s, COWORKER)), "")

    def test_serves_the_coworker_direction_too(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "the newer version\n")
        self.plant(COWORKER, s, "task.py", "the older version\n")
        self.assertIn("newer", self._run_on_host(hand_over(s, COWORKER)))


class TestRoundTrip(CoworkTempTree):
    def test_the_full_cycle_delivers_an_edit_to_the_manager(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "TODO")
        # The coworker takes up its inbox, works, and submits.
        sent = hand_over(s, COWORKER)
        for relative in sent.files:
            self.write(self.group_dir(COWORKER, s) / relative,
                       (sent.inbox / relative).read_text())
        self.write(self.group_dir(COWORKER, s) / "task.py", "DONE")
        result = submit(s, COWORKER)
        self.assertEqual(result.changed, (Path("task.py"),))
        self.assertEqual((result.inbox / "task.py").read_text(), "DONE")
        self.assertEqual(not_taken_up(s, recipient=COWORKER, sender=MANAGER), ())

    def test_no_inbox_ever_becomes_a_group_of_its_own(self):
        # Inbox dirs are siblings of group dirs, so discovery has to key on
        # session.json — this proves the hub never puts one in an inbox.
        s = _session()
        self.plant(MANAGER, s, "task.py", "x")
        self.plant(MANAGER, s, "session.json", "{}")
        for delivery in (hand_over(s, COWORKER), submit(s, COWORKER)):
            self.assertFalse(paths.group_session_path(delivery.inbox).exists())


class TestWriteConfinement(CoworkTempTree):
    """Instance ids reach the hub from a manager's request, so a traversal
    attempt has to fail loudly rather than place files outside the tree."""

    ESCAPING = ("../escaped", "../../escaped", "a/../../escaped", "/etc")

    def _with_member(self, bad: str):
        """A session in which the traversing id IS a recruited coworker.

        Deliberate: an id reaches the hub from a manager's request and gets
        RECORDED, and `with_coworker` rejects the inbox separator but not `..`.
        Handing the guard a non-member instead would make these pass on the
        membership check and never exercise confinement at all."""
        return _session(coworkers=(bad,))

    def test_a_traversing_coworker_id_is_refused_in_both_directions(self):
        for bad in self.ESCAPING:
            session = self._with_member(bad)
            self.plant(MANAGER, session, "task.py", "x")
            for direction in (hand_over, submit):
                with self.subTest(coworker=bad, direction=direction.__name__):
                    with self.assertRaisesRegex(ValueError, "group-hosting tree"):
                        direction(session, bad)

    def test_a_traversing_manager_id_is_refused(self):
        s = _session(manager="../escaped")
        with self.assertRaisesRegex(ValueError, "group-hosting tree"):
            submit(s, COWORKER)

    def test_a_refused_destination_writes_nothing(self):
        session = self._with_member("../escaped")
        self.plant(MANAGER, session, "task.py", "x")
        with self.assertRaises(ValueError):
            hand_over(session, "../escaped")
        self.assertFalse((self.root.parent / "escaped").exists())

    def test_an_ordinary_id_is_not_mistaken_for_an_escape(self):
        for ok in ("golem__a", "a.b__c", "with-dash__p"):
            session = self._with_member(ok)
            self.plant(MANAGER, session, "task.py", "x")
            with self.subTest(coworker=ok):
                self.assertEqual(hand_over(session, ok).files, (Path("task.py"),))


class TestMembership(CoworkTempTree):
    """No files move to or from an instance that is not in the group. Reaching
    this guard means a caller skipped `relay.membership_problem`, so it raises
    rather than reporting."""

    def test_handing_over_to_a_non_member_is_refused(self):
        s = _session()
        self.plant(MANAGER, s, "task.py", "x")
        with self.assertRaisesRegex(ValueError, "not in group"):
            hand_over(s, "stranger__x")

    def test_the_non_members_tree_is_left_untouched(self):
        # The leak this closes: its `/cowork` mount is readable by that instance.
        s = _session()
        self.plant(MANAGER, s, "secret.py", "the manager's working copy")
        with self.assertRaises(ValueError):
            hand_over(s, "stranger__x")
        self.assertFalse(paths.cowork_dir_path("stranger__x").exists())

    def test_submitting_from_a_non_member_is_refused(self):
        s = _session()
        self.plant("stranger__x", s, "task.py", "x")
        with self.assertRaisesRegex(ValueError, "not in group"):
            submit(s, "stranger__x")

    def test_the_manager_needs_no_recruiting(self):
        s = _session(coworkers=(COWORKER,))
        self.plant(COWORKER, s, "task.py", "x")
        self.assertEqual(submit(s, COWORKER).files, (Path("task.py"),))


if __name__ == "__main__":
    unittest.main()
