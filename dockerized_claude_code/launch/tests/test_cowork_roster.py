"""Tests for launch.cowork.roster — who a manager could recruit.

Instances are built as REAL store entries plus real state dirs, and rehydrated
through `agents_crud.instance_from_store`, because the whole module rests on
`Instance.is_cowork` resolving from a stored tag list. A stubbed identity would
prove only that the stub said what the test told it to.

The `{cowork}` tag is read from the actual `agents/` tree, so the fixture uses the
real specialty name rather than a made-up one — if that tag were renamed, these
tests should fail rather than keep passing against a fiction.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cowork import group as grp
from launch.cowork import roster
from launch.cowork.roster import Candidate, Roster, describe, reachable, survey
from launch.tags import scan_all
from launch.tags import store
from launch.tags.identity import COWORK_SPECIALTY

MANAGER = "refactorer__proj"


class RosterHarness(unittest.TestCase):
    """A tmp state dir with real instance dirs, store entries, and tag resolution.

    Liveness is patched at `roster`'s own import of the docker probe: the point
    under test is what the roster concludes, not that `docker ps` parses."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.state = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._patch(patch.object(paths, "AGENTS_STATE", self.state))
        # paths.INSTANCES_FILE is a CONSTANT computed at import, so patching
        # AGENTS_STATE does not move it — without this, store.save() in a test
        # writes into the REAL ~/.claude-agents/instances.toml (found the hard
        # way: the audit flagged this suite's fixture names as ghost entries).
        self._patch(patch.object(store, "INSTANCES_FILE",
                                 self.state / "instances.toml"))
        self.registry = scan_all(paths.AGENTS_DIR)
        self.agent = self._an_agent_name()
        self.live: set[str] | None = set()
        self._patch(patch.object(roster, "docker_running_instances_subprocess",
                                 lambda: None if self.live is None
                                 else frozenset(self.live)))

    def _patch(self, p):
        p.start()
        self.addCleanup(p.stop)

    def _an_agent_name(self) -> str:
        """Any real agent, so `instance_from_store` can find its `.md`."""
        from launch.file_access import agent_md_index
        return sorted(agent_md_index())[0]

    def add_instance(self, session: str, *specialties: str, running: bool = True,
                     workspace: str = "/work") -> str:
        """Create a real instance: state dir + instances.toml entry."""
        instance_id = f"{self.agent}__{session}"
        paths.instance_state_dir_path(instance_id).mkdir(parents=True, exist_ok=True)
        mapping = store.load()
        mapping[instance_id] = {"workspace": workspace,
                                "specialties": list(specialties)}
        store.save(mapping)
        if running:
            assert self.live is not None
            self.live.add(instance_id)
        return instance_id

    def add_coworker(self, session: str, **kwargs) -> str:
        return self.add_instance(session, COWORK_SPECIALTY, **kwargs)

    def ids(self, candidates) -> list[str]:
        return [c.instance for c in candidates]


class TestCapability(RosterHarness):
    def test_a_cowork_tagged_instance_is_a_candidate(self):
        peer = self.add_coworker("peer")
        self.assertEqual(self.ids(survey(MANAGER, self.registry).candidates), [peer])

    def test_an_untagged_instance_is_not_a_candidate(self):
        self.add_instance("plain")
        self.assertEqual(survey(MANAGER, self.registry).candidates, ())

    def test_a_running_untagged_instance_is_reported_as_needing_a_relaunch(self):
        # "Nobody is available" is a confusing answer when the truth is "nobody
        # is tagged yet" — and the tag only arrives at launch.
        plain = self.add_instance("plain")
        self.assertEqual(survey(MANAGER, self.registry).needs_relaunch, (plain,))

    def test_a_stopped_untagged_instance_is_not_worth_mentioning(self):
        # It would need a launch anyway, so naming it adds noise, not options.
        self.add_instance("plain", running=False)
        self.assertEqual(survey(MANAGER, self.registry).needs_relaunch, ())

    def test_an_orphaned_state_dir_is_skipped_entirely(self):
        # No agent .md, so it cannot be rehydrated, let alone recruited.
        paths.instance_state_dir_path("ghostagent__x").mkdir(parents=True)
        result = survey(MANAGER, self.registry)
        self.assertEqual(result.candidates, ())
        self.assertEqual(result.needs_relaunch, ())

    def test_no_instances_at_all_is_an_empty_roster_not_a_crash(self):
        result = survey(MANAGER, self.registry)
        self.assertEqual((result.candidates, result.needs_relaunch), ((), ()))


class TestSelfExclusion(RosterHarness):
    def test_the_asker_is_never_a_candidate(self):
        asker = self.add_coworker("self")
        self.assertEqual(survey(asker, self.registry).candidates, ())

    def test_another_instance_of_the_same_agent_is_still_a_candidate(self):
        asker = self.add_coworker("self")
        other = self.add_coworker("other")
        self.assertEqual(self.ids(survey(asker, self.registry).candidates), [other])

    def test_no_asker_excludes_nobody(self):
        # A human at the CLI is not in the list to begin with.
        peer = self.add_coworker("peer")
        self.assertEqual(self.ids(survey(None, self.registry).candidates), [peer])

    def test_the_asker_is_not_reported_as_needing_a_relaunch_either(self):
        asker = self.add_instance("self")
        self.assertEqual(survey(asker, self.registry).needs_relaunch, ())


class TestLiveness(RosterHarness):
    def test_a_running_peer_is_marked_reachable(self):
        self.add_coworker("peer")
        self.assertTrue(survey(MANAGER, self.registry).candidates[0].running)

    def test_a_stopped_peer_is_listed_but_not_reachable(self):
        self.add_coworker("peer", running=False)
        result = survey(MANAGER, self.registry)
        self.assertEqual(len(result.candidates), 1)
        self.assertEqual(reachable(result), ())

    def test_running_peers_sort_before_stopped_ones(self):
        # An agent acts on what it reads first, so the wakeable peer belongs top.
        self.add_coworker("aaa_stopped", running=False)
        live = self.add_coworker("zzz_live")
        self.assertEqual(self.ids(survey(MANAGER, self.registry).candidates)[0], live)

    def test_an_unreachable_docker_is_admitted_not_hidden(self):
        # "Everyone is offline" and "we could not tell" lead a manager to
        # opposite conclusions.
        self.add_coworker("peer")
        self.live = None
        result = survey(MANAGER, self.registry)
        self.assertFalse(result.liveness_known)
        self.assertEqual(len(result.candidates), 1)
        self.assertFalse(result.candidates[0].running)

    def test_liveness_is_known_when_docker_answers_with_nothing_running(self):
        self.add_coworker("peer", running=False)
        self.assertTrue(survey(MANAGER, self.registry).liveness_known)


class TestCommitments(RosterHarness):
    def _group(self, manager: str, project: str, *coworkers: str):
        session = grp.create_session(manager, project, "task")
        for coworker in coworkers:
            session = session.with_coworker(coworker)
        return grp.save_session(session)

    def test_an_uncommitted_peer_reports_no_groups(self):
        self.add_coworker("peer")
        candidate = survey(MANAGER, self.registry).candidates[0]
        self.assertEqual(candidate.groups, ())
        self.assertFalse(candidate.committed)

    def test_a_peers_active_group_is_reported(self):
        peer = self.add_coworker("peer")
        session = self._group(MANAGER, "widget", peer)
        candidate = survey(MANAGER, self.registry).candidates[0]
        self.assertEqual(candidate.groups, (session.key,))
        self.assertTrue(candidate.committed)

    def test_a_closed_group_is_not_a_commitment(self):
        peer = self.add_coworker("peer")
        grp.save_session(self._group(MANAGER, "widget", peer).closed())
        self.assertEqual(survey(MANAGER, self.registry).candidates[0].groups, ())

    def test_a_peer_in_two_groups_reports_both(self):
        peer = self.add_coworker("peer")
        self._group(MANAGER, "one", peer)
        self._group(MANAGER, "two", peer)
        self.assertEqual(len(survey(MANAGER, self.registry).candidates[0].groups), 2)

    def test_the_least_committed_peer_sorts_first(self):
        busy = self.add_coworker("aaa_busy")
        free = self.add_coworker("zzz_free")
        self._group(MANAGER, "widget", busy)
        self.assertEqual(self.ids(survey(MANAGER, self.registry).candidates)[0], free)


class TestRunningManagers(RosterHarness):
    """The hub's exit condition reads this; the distinction that matters is
    manager-tagged-and-running vs everything else, with docker-unknown kept
    apart from "none"."""

    def test_a_running_manager_is_reported(self):
        boss = self.add_instance("boss", COWORK_SPECIALTY, "manager")
        self.assertEqual(roster.running_managers(self.registry), frozenset({boss}))

    def test_a_running_plain_coworker_is_not_a_manager(self):
        self.add_coworker("peer")
        self.assertEqual(roster.running_managers(self.registry), frozenset())

    def test_a_stopped_manager_does_not_count(self):
        self.add_instance("boss", COWORK_SPECIALTY, "manager", running=False)
        self.assertEqual(roster.running_managers(self.registry), frozenset())

    def test_docker_unknown_is_none_not_empty(self):
        # "Every manager is gone" and "we could not tell" must stay different
        # answers — the hub exits on the first and keeps serving on the second.
        self.add_instance("boss", COWORK_SPECIALTY, "manager")
        self.live = None
        self.assertIsNone(roster.running_managers(self.registry))

    def test_a_running_container_with_no_store_entry_is_not_a_manager(self):
        assert self.live is not None
        self.live.add("ghost__x")            # container up, instance deleted
        self.assertEqual(roster.running_managers(self.registry), frozenset())


class TestDescription(RosterHarness):
    def test_names_each_candidate_and_its_state(self):
        peer = self.add_coworker("peer")
        text = describe(survey(MANAGER, self.registry))
        self.assertIn(peer, text)
        self.assertIn("running", text)

    def test_says_so_plainly_when_there_are_none(self):
        self.assertIn("No cowork-capable peers", describe(survey(MANAGER, self.registry)))

    def test_a_stopped_peer_is_marked_as_unwakeable(self):
        self.add_coworker("peer", running=False)
        self.assertIn("cannot be woken", describe(survey(MANAGER, self.registry)))

    def test_leads_with_the_warning_when_liveness_is_unknown(self):
        self.add_coworker("peer")
        self.live = None
        self.assertTrue(describe(survey(MANAGER, self.registry)).startswith("!"))

    def test_explains_what_a_relaunch_would_buy(self):
        plain = self.add_instance("plain")
        text = describe(survey(MANAGER, self.registry))
        self.assertIn(plain, text)
        self.assertIn("relaunch", text)

    def test_reports_existing_commitments_so_a_manager_can_avoid_overloading(self):
        peer = self.add_coworker("peer")
        grp.save_session(grp.create_session(MANAGER, "widget", "t").with_coworker(peer))
        self.assertIn("already in 1 group", describe(survey(MANAGER, self.registry)))

    def test_mentions_the_workspace_so_two_sessions_are_distinguishable(self):
        self.add_coworker("peer", workspace="/srv/api")
        self.assertIn("/srv/api", describe(survey(MANAGER, self.registry)))


class TestShape(unittest.TestCase):
    """Pure-data guards that need no filesystem."""

    def test_committed_is_derived_not_stored(self):
        base = dict(instance="a__b", running=True, workspace="/w", tags=())
        self.assertFalse(Candidate(**base, groups=()).committed)
        self.assertTrue(Candidate(**base, groups=("g",)).committed)

    def test_reachable_of_an_empty_roster_is_empty(self):
        self.assertEqual(reachable(Roster((), (), True)), ())


if __name__ == "__main__":
    unittest.main()
