"""Tests for launch.gui.cluster_form — the membership form's pure half.

The interactive Application stays out of unit scope (the house rule from
test_menu_picker's docstring); everything with rules — prefill, the
accumulate/remove semantics, the live id preview — is a pure helper tested
here. The rules encode two decisions: picking an agent ADDS AN ENTRY (that is
what makes a cluster hold two researchers), and there is NO per-member editing
mid-flow (roles auto-derive; the preview is what keeps them from surprising).
"""

import unittest

from launch.cluster.legoset import ClusterTemplate
from launch.cluster.member import Member
from launch.gui.cluster_form import (
    add_pick, prefill_picks, preview_ids, prompt_members, remove_last,
)


def a_template(*members: Member) -> ClusterTemplate:
    return ClusterTemplate(name="team", members=members)


class TestPrefill(unittest.TestCase):
    def test_template_roles_ride_in_verbatim(self):
        picks = prefill_picks(a_template(Member.of("researcher", "primary"),
                                         Member.of("researcher", "adversarial")))
        self.assertEqual(picks, [("researcher", "primary"),
                                 ("researcher", "adversarial")])

    def test_a_defaulted_role_comes_back_as_none(self):
        # `Member.of("golem")` stores role="golem" (the collapse default). The
        # form must treat that as UNROLED: if the user then adds a second
        # golem, both renumber to golem__1/golem__2 — a kept literal "golem"
        # role would pin the first id while its twin got a number.
        picks = prefill_picks(a_template(Member.of("golem")))
        self.assertEqual(picks, [("golem", None)])

    def test_order_is_kept(self):
        picks = prefill_picks(a_template(Member.of("b"), Member.of("a")))
        self.assertEqual([agent for agent, _ in picks], ["b", "a"])


class TestAddRemove(unittest.TestCase):
    def test_picking_appends_an_entry(self):
        picks = [("golem", None)]
        add_pick(picks, "golem")
        add_pick(picks, "poet")
        self.assertEqual(picks, [("golem", None), ("golem", None), ("poet", None)])

    def test_remove_takes_that_agents_last_entry_only(self):
        picks = [("golem", None), ("poet", None), ("golem", None)]
        remove_last(picks, "golem")
        self.assertEqual(picks, [("golem", None), ("poet", None)])

    def test_remove_at_zero_is_a_no_op(self):
        picks = [("poet", None)]
        remove_last(picks, "golem")
        self.assertEqual(picks, [("poet", None)])

    def test_template_entries_are_not_protected(self):
        # Prefills are a starting point, never a lock: shrinking devteam's two
        # researchers drops the most recently listed one first.
        picks = [("researcher", "primary"), ("researcher", "adversarial")]
        remove_last(picks, "researcher")
        self.assertEqual(picks, [("researcher", "primary")])


class TestPreview(unittest.TestCase):
    """The live id panel — what makes invisible auto-roles acceptable: every
    id the confirm will create is on screen before it happens."""

    def test_shows_the_exact_ids_confirm_would_create(self):
        picks = [("researcher", "primary"), ("researcher", None),
                 ("golem", None)]
        self.assertEqual(preview_ids(picks),
                         ["researcher__primary", "researcher__1", "golem"])

    def test_duplicates_render_numbered_the_moment_the_second_is_picked(self):
        picks = [("golem", None)]
        self.assertEqual(preview_ids(picks), ["golem"])
        add_pick(picks, "golem")
        self.assertEqual(preview_ids(picks), ["golem__1", "golem__2"])


class TestPromptMembersGuards(unittest.TestCase):
    def test_no_agents_is_a_programming_error(self):
        # An empty pick list is a user state the form handles; an empty AGENT
        # list means the caller scanned nothing — fail loud, not a blank form.
        with self.assertRaises(ValueError):
            prompt_members([], [], title="t")


if __name__ == "__main__":
    unittest.main()
