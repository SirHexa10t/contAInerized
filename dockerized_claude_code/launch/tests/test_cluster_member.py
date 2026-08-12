"""Tests for launch.cluster.member — member identity and name legality.

The property that matters most: a member id is simultaneously a directory name, a
tmux window name, and a git branch component. Every test here is really asking
"could this name mean something different in one of those three places than it
does in the others?" — because that is how a launcher acts on the wrong target
instead of failing.
"""

import unittest

from launch.cluster.member import (
    MEMBER_SEPARATOR, ClusterError, Member, member_id, split_member_id,
    valid_label,
)
from launch.tags.lego import AgentBuild


class TestMemberId(unittest.TestCase):
    def test_role_equal_to_agent_collapses(self):
        # The common one-of-each cluster gets clean names, rather than
        # `project-starter__project-starter`.
        self.assertEqual(member_id("refactorer", "refactorer"), "refactorer")

    def test_distinct_role_is_appended(self):
        self.assertEqual(member_id("researcher", "primary"), "researcher__primary")

    def test_round_trip_both_shapes(self):
        for agent, role in (("researcher", "primary"), ("refactorer", "refactorer")):
            with self.subTest(role=role):
                self.assertEqual(split_member_id(member_id(agent, role)),
                                 (agent, role))

    def test_hyphenated_agent_names_round_trip(self):
        # Agent names legitimately contain '-' (bug-investigator), and the id
        # separator is '__', so the two must not interfere.
        self.assertEqual(split_member_id(member_id("bug-investigator", "fuzz")),
                         ("bug-investigator", "fuzz"))

    def test_split_of_a_bare_agent_returns_it_as_its_own_role(self):
        self.assertEqual(split_member_id("golem"), ("golem", "golem"))


class TestNameLegality(unittest.TestCase):
    """Every rejected character is rejected for a NAMED reason; a test per
    reason, so a future relaxation has to argue with the specific hazard."""

    def test_colon_rejected_because_tmux_targets_use_it(self):
        # `-t session:window` — a role with ':' addresses a different window.
        with self.assertRaises(ClusterError):
            member_id("researcher", "a:b")

    def test_dot_rejected_because_tmux_panes_use_it(self):
        # `-t session:window.pane`
        with self.assertRaises(ClusterError):
            member_id("researcher", "a.b")

    def test_slash_rejected_because_ids_become_paths(self):
        with self.assertRaises(ClusterError):
            member_id("researcher", "a/b")

    def test_whitespace_rejected(self):
        for bad in ("a b", "a\tb", "a\nb"):
            with self.subTest(role=bad), self.assertRaises(ClusterError):
                member_id("researcher", bad)

    def test_at_sign_rejected_to_stay_legible_beside_cowork(self):
        with self.assertRaises(ClusterError):
            member_id("researcher", "a@b")

    def test_separator_inside_a_role_rejected(self):
        # Would break the round-trip: split_member_id picks the first '__'.
        with self.assertRaises(ClusterError):
            member_id("researcher", f"a{MEMBER_SEPARATOR}b")

    def test_empty_rejected(self):
        with self.assertRaises(ClusterError):
            valid_label("", "role")

    def test_a_legal_label_is_returned_unchanged(self):
        # Validation must not sanitise: a rewritten id would leave the member
        # keyed under a name it does not answer to.
        self.assertEqual(valid_label("bug-investigator", "agent name"),
                         "bug-investigator")


class TestMember(unittest.TestCase):
    def test_of_defaults_the_role_to_the_agent(self):
        member = Member.of("refactorer")
        self.assertEqual(member.role, "refactorer")
        self.assertEqual(member.id, "refactorer")

    def test_of_keeps_an_explicit_role(self):
        self.assertEqual(Member.of("researcher", "primary").id, "researcher__primary")

    def test_construction_validates(self):
        # The guard is in __post_init__, so an illegal member cannot exist even
        # if a caller builds it directly rather than through `of`.
        with self.assertRaises(ClusterError):
            Member(agent="researcher", role="bad:role")

    def test_build_defaults_to_empty_and_is_carried(self):
        self.assertEqual(Member.of("golem").build, AgentBuild())
        build = AgentBuild(engine="researcher", policies=("all-actions",))
        self.assertEqual(Member.of("researcher", "primary", build).build, build)

    def test_members_are_frozen(self):
        # Every change is a cluster.toml rewrite, so mutation-in-place would
        # silently diverge from disk.
        with self.assertRaises(Exception):
            Member.of("golem").role = "other"      # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
