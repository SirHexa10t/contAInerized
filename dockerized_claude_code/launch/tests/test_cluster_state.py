"""Tests for launch.cluster.state — `cluster.toml` and discovery.

Two contracts carry the weight. **Round-tripping**: what is written must read
back identically, including member ORDER (which is tmux window order, and cannot
be recovered from the key-sorted tables). And **discovery-by-scan**: the
directory is the record, so a cluster exists exactly when its `cluster.toml`
does — no registry to disagree with it.

`AGENTS_STATE` is redirected for every test, which moves the whole feature at
once because every cluster path is composed from `clusters_dir()` at call time.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from launch import paths
from launch.cluster import state
from launch.cluster.member import ClusterError, Member
from launch.tags.lego import AgentBuild


class ClusterTmp(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(paths, "AGENTS_STATE", self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)

    def a_cluster(self, session: str = "poc", *members: Member) -> state.Cluster:
        chosen = members or (
            Member.of("refactorer"),
            Member.of("researcher", "primary",
                      AgentBuild(engine="researcher", professions=("code",),
                                 policies=("all-actions",))),
            Member.of("researcher", "adversarial"),
        )
        return state.from_template(session, Path("/tmp/project"), chosen,
                                   template="devteam")


class TestClusterModel(ClusterTmp):
    def test_ids_follow_definition_order(self):
        self.assertEqual(self.a_cluster().ids,
                         ("refactorer", "researcher__primary",
                          "researcher__adversarial"))

    def test_member_lookup(self):
        cluster = self.a_cluster()
        self.assertEqual(cluster.member("researcher__primary").role, "primary")
        self.assertIsNone(cluster.member("nobody"))

    def test_a_cluster_with_no_members_is_refused(self):
        # Nothing to launch; better to refuse than to produce an empty tmux
        # session the user has to diagnose.
        with self.assertRaises(ClusterError):
            state.from_template("poc", Path("/tmp/p"), ())

    def test_duplicate_member_ids_refused(self):
        with self.assertRaises(ClusterError):
            state.from_template("poc", Path("/tmp/p"),
                                (Member.of("golem"), Member.of("golem")))

    def test_illegal_session_name_refused(self):
        # The session name is also the tmux session name and a directory.
        for bad in ("has:colon", "has.dot", "has/slash", "has space"):
            with self.subTest(session=bad), self.assertRaises(ClusterError):
                state.from_template(bad, Path("/tmp/p"), (Member.of("golem"),))

    def test_with_member_rejects_a_duplicate(self):
        cluster = self.a_cluster()
        with self.assertRaises(ClusterError):
            cluster.with_member(Member.of("refactorer"))

    def test_with_member_appends(self):
        cluster = self.a_cluster().with_member(Member.of("golem"))
        self.assertEqual(cluster.ids[-1], "golem")

    def test_without_member_is_idempotent(self):
        # Removal's end state is unambiguous, unlike addition's, so a repeat is
        # a no-op rather than an error.
        cluster = self.a_cluster().without_member("refactorer")
        self.assertEqual(cluster.without_member("refactorer").ids, cluster.ids)

    def test_worktree_path_is_derived_not_stored(self):
        cluster = self.a_cluster()
        self.assertEqual(cluster.worktree("refactorer"),
                         paths.cluster_worktree_path("poc", "refactorer"))


class TestForcedTags(ClusterTmp):
    """Every member carries {muxer} and {cluster}, whatever its agent's .lego
    says — a member unaware it is one would introduce itself wrongly and address
    nobody, and a cluster created programmatically must not depend on the form's
    interactive auto-tick to be launchable."""

    def test_both_tags_are_applied_at_creation(self):
        cluster = state.from_template("poc", Path("/tmp/p"), (Member.of("golem"),))
        self.assertEqual(cluster.members[0].build.specialties, ("muxer", "cluster"))

    def test_existing_specialties_are_preserved(self):
        member = Member.of("feature-identifier", build=AgentBuild(
            specialties=("auto", "firewall")))
        cluster = state.from_template("poc", Path("/tmp/p"), (member,))
        self.assertEqual(cluster.members[0].build.specialties,
                         ("auto", "firewall", "muxer", "cluster"))

    def test_no_duplicate_when_an_agent_already_carries_one(self):
        member = Member.of("golem", build=AgentBuild(specialties=("muxer",)))
        cluster = state.from_template("poc", Path("/tmp/p"), (member,))
        self.assertEqual(cluster.members[0].build.specialties, ("muxer", "cluster"))

    def test_other_axes_are_untouched(self):
        member = Member.of("researcher", build=AgentBuild(
            engine="researcher", professions=("code",), policies=("all-actions",)))
        built = state.from_template("poc", Path("/tmp/p"), (member,)).members[0]
        self.assertEqual(built.build.engine, "researcher")
        self.assertEqual(built.build.professions, ("code",))
        self.assertEqual(built.build.policies, ("all-actions",))

    def test_forced_tags_survive_a_round_trip(self):
        state.save(state.from_template("poc", Path("/tmp/p"), (Member.of("golem"),)))
        self.assertEqual(state.load("poc").members[0].build.specialties,
                         ("muxer", "cluster"))

    def test_the_forced_names_are_real_tags(self):
        # A typo here would produce clusters that fail tag validation at launch.
        from launch import paths as real_paths
        from launch.tags import scan_all
        registry = scan_all(real_paths.AGENTS_DIR)
        for name in state.FORCED_SPECIALTIES:
            with self.subTest(tag=name):
                self.assertIn(name, registry.specialties)


class TestWithBuild(ClusterTmp):
    """`Cluster.with_build` — the picker's F2 edit, persisted through one
    method so its guarantees live in one place."""

    def test_replaces_exactly_one_members_tags(self):
        cluster = self.a_cluster()
        edited = cluster.with_build("researcher__primary",
                                    AgentBuild(engine="thinker"))
        self.assertEqual(edited.member("researcher__primary").build.engine,
                         "thinker")
        # The sibling with the same agent is untouched.
        self.assertEqual(edited.member("researcher__adversarial").build,
                         cluster.member("researcher__adversarial").build)

    def test_order_is_untouched_because_it_is_window_order(self):
        cluster = self.a_cluster()
        self.assertEqual(cluster.with_build("refactorer", AgentBuild()).ids,
                         cluster.ids)

    def test_the_forced_specialties_survive_an_edit_that_unticked_them(self):
        # The tag form lets a user untick anything; an edit is the SECOND place
        # a member's build enters the file, so it gets the same guarantee
        # from_template gives the first — no path produces a member unaware it
        # is one.
        cluster = self.a_cluster()
        edited = cluster.with_build("refactorer", AgentBuild(professions=("code",)))
        self.assertEqual(edited.member("refactorer").build.specialties,
                         ("muxer", "cluster"))

    def test_an_unknown_member_is_a_loud_stop(self):
        # The edit came from a row naming a member; missing means the file
        # changed underneath.
        with self.assertRaises(ClusterError):
            self.a_cluster().with_build("nobody", AgentBuild())


class TestDestroy(ClusterTmp):
    def test_a_shared_workspace_cluster_is_removed_without_touching_git(self):
        # No worktrees were ever made, and the project may not even be a git
        # repo — destroy must not shell out to git at all in that case.
        from unittest.mock import patch as mock_patch
        cluster = state.save(self.a_cluster())
        self.assertTrue(paths.cluster_state_path("poc").is_file())
        with mock_patch.object(state, "shell_returncode",
                               side_effect=AssertionError("git was called")) :
            state.destroy(cluster)
        self.assertFalse(paths.cluster_path("poc").exists())

    def test_destroy_is_what_makes_exists_false(self):
        cluster = state.save(self.a_cluster())
        self.assertTrue(state.exists("poc"))
        state.destroy(cluster)
        self.assertFalse(state.exists("poc"))
        self.assertEqual(state.discover(), [])


class TestPickerOrder(ClusterTmp):
    """`picker_order` — THE member ordering, derived from the same logic that
    sorts the picker's agent rows. Every sequence consumer (windows, rows,
    previews, summaries) goes through it, so `^b 3` and the third row can
    never name different members."""

    def order(self, *members: Member) -> list[str]:
        from launch.tags import scan_all
        from launch.paths import AGENTS_DIR
        registry = scan_all(AGENTS_DIR)
        return [m.id for m in state.picker_order(tuple(members), registry)]

    def test_members_follow_the_agent_rows_order(self):
        # Same agents, same order as the picker's + Agent rows — verified
        # against the REAL ordering function, not a re-derivation of its rules.
        from launch.agents_crud import creatable_agents
        from launch.tags import scan_all
        from launch.paths import AGENTS_DIR
        registry = scan_all(AGENTS_DIR)
        agent_names = [a.name for a in creatable_agents(registry)]
        members = tuple(Member.of(name) for name in reversed(agent_names))
        self.assertEqual(self.order(*members), agent_names)

    def test_same_agent_members_group_and_sort_by_id(self):
        ordered = self.order(Member.of("researcher", "primary"),
                             Member.of("golem"),
                             Member.of("researcher", "adversarial"))
        researchers = [i for i in ordered if i.startswith("researcher")]
        self.assertEqual(researchers,
                         ["researcher__adversarial", "researcher__primary"])
        # Grouped: nothing sits between two members of one agent.
        first = ordered.index(researchers[0])
        self.assertEqual(ordered[first:first + 2], researchers)

    def test_an_unknown_agent_sinks_last_instead_of_crashing(self):
        # A stale member is a problem the ROWS display; ordering is not the
        # place to die.
        ordered = self.order(Member.of("ghost-agent"), Member.of("golem"))
        self.assertEqual(ordered[-1], "ghost-agent")


class TestRoundTrip(ClusterTmp):
    def test_a_round_trip_is_lossless_up_to_canonical_order(self):
        # Loading is CANONICAL (members id-sorted) — nothing else may change.
        saved = state.save(self.a_cluster())
        loaded = state.load("poc")
        self.assertEqual({m.id: m for m in loaded.members},
                         {m.id: m for m in saved.members})
        self.assertEqual((loaded.session, loaded.project, loaded.template),
                         (saved.session, saved.project, saved.template))
        # And canonical means a second trip changes nothing at all.
        self.assertEqual(state.load(state.save(loaded).session), loaded)

    def test_authored_order_is_deliberately_not_stored(self):
        # DECIDED: no `order` field — window/display order is DERIVED by
        # picker-sort at use time (see picker_order below), "one less small
        # decision for the user". Storage is id-sorted, so an order sorting
        # would destroy… gets destroyed, and the file carries no order key.
        state.save(state.from_template(
            "poc", Path("/tmp/p"),
            (Member.of("zebra"), Member.of("alpha"), Member.of("mid"))))
        self.assertEqual(state.load("poc").ids, ("alpha", "mid", "zebra"))
        self.assertNotIn("order", paths.cluster_state_path("poc").read_text())

    def test_tags_survive_per_member(self):
        loaded = state.load(state.save(self.a_cluster()).session)
        primary = loaded.member("researcher__primary")
        self.assertEqual(primary.build.engine, "researcher")
        self.assertEqual(primary.build.professions, ("code",))
        self.assertEqual(primary.build.policies, ("all-actions",))

    def test_agent_and_role_are_recovered_from_the_table_name(self):
        # Not stored twice: the key IS the identity.
        state.save(self.a_cluster())
        text = paths.cluster_state_path("poc").read_text()
        self.assertIn("[researcher__primary]", text)
        self.assertNotIn("agent =", text)
        self.assertNotIn("role =", text)

    def test_a_resave_produces_no_diff(self):
        # Canonical form: the file is stable under repeated saves, so a diff
        # means a real change.
        first = state.dumps(state.save(self.a_cluster()))
        self.assertEqual(state.dumps(state.load("poc")), first)

    def test_template_is_optional(self):
        cluster = state.from_template("poc", Path("/tmp/p"), (Member.of("golem"),))
        self.assertIsNone(state.save(cluster) and state.load("poc").template)

    def test_a_path_with_spaces_survives(self):
        # The emitter quotes via json.dumps; a project path with a space is the
        # ordinary case on macOS.
        state.save(state.from_template("poc", Path("/tmp/my project"),
                                       (Member.of("golem"),)))
        self.assertEqual(state.load("poc").project, Path("/tmp/my project"))


class TestCorruption(ClusterTmp):
    def test_missing_project_key_raises(self):
        paths.cluster_state_path("poc").parent.mkdir(parents=True)
        paths.cluster_state_path("poc").write_text("[golem]\nengine = \"golem\"\n")
        with self.assertRaises(ClusterError):
            state.load("poc")

    def test_a_legacy_order_key_is_ignored_not_validated(self):
        # Files written before the field was dropped carry `order` — including
        # one naming ids that no longer match. The MEMBERS are the truth now,
        # so such a file loads from its tables and the stale key means nothing.
        paths.cluster_state_path("poc").parent.mkdir(parents=True)
        paths.cluster_state_path("poc").write_text(
            'project = "/tmp/p"\norder = ["golem", "ghost"]\n\n[golem]\n')
        self.assertEqual(state.load("poc").ids, ("golem",))

    def test_absent_cluster_loads_as_none(self):
        # "No cluster called that" is an answer a CLI prints, not a fault.
        self.assertIsNone(state.load("never-made"))


class TestDiscovery(ClusterTmp):
    def test_finds_saved_clusters_sorted(self):
        state.save(self.a_cluster("beta"))
        state.save(self.a_cluster("alpha"))
        self.assertEqual([c.session for c in state.discover()], ["alpha", "beta"])

    def test_a_dir_without_cluster_toml_is_not_a_cluster(self):
        (paths.clusters_dir() / "stray").mkdir(parents=True)
        self.assertEqual(state.discover(), [])

    def test_no_clusters_dir_yields_nothing(self):
        self.assertEqual(state.discover(), [])

    def test_one_corrupt_cluster_does_not_hide_the_healthy_ones(self):
        state.save(self.a_cluster("good"))
        bad = paths.cluster_state_path("bad")
        bad.parent.mkdir(parents=True)
        bad.write_text("{{{ not toml")
        self.assertEqual([c.session for c in state.discover()], ["good"])

    def test_exists_tracks_the_file(self):
        self.assertFalse(state.exists("poc"))
        state.save(self.a_cluster())
        self.assertTrue(state.exists("poc"))


if __name__ == "__main__":
    unittest.main()
