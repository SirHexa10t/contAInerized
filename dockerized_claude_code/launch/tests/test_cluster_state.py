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
        for bad in ("has:colon", "has.dot", "has/slash", "has space",
                    "has~tilde", "has*star", "has\\backslash", "has\x1bescape"):
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


class TestClusterTags(ClusterTmp):
    """Cluster-level tags (2026-09-02): the set every member inherits, stored
    ONCE on the cluster instead of copied into every member table — which is
    what lets the picker show a member's own tags without duplicating the
    cluster's, and lets `{cc}` be set once for the whole team."""

    def test_the_locked_pair_is_always_there(self):
        cluster = state.from_template("poc", Path("/tmp/p"), (Member.of("golem"),))
        self.assertEqual(cluster.tags.specialties, ("muxer", "cluster"))

    def test_a_cluster_engine_is_refused_silently(self):
        # A thinking budget is per member — the cluster form has no engine
        # section, and a hand-edited file must not smuggle one in either.
        cluster = state.Cluster(
            session="poc", project=Path("/tmp/p"), members=(Member.of("golem"),),
            tags=AgentBuild(engine="thinker", specialties=("muxer", "cluster")))
        self.assertIsNone(cluster.tags.engine)

    def test_members_store_only_their_OWN_tags(self):
        member = Member.of("feature-identifier", build=AgentBuild(
            specialties=("auto", "muxer")))       # muxer is the cluster's
        cluster = state.from_template("poc", Path("/tmp/p"), (member,))
        self.assertEqual(cluster.members[0].build.specialties, ("auto",))

    def test_member_build_unions_cluster_and_own(self):
        member = Member.of("researcher", build=AgentBuild(
            engine="researcher", specialties=("auto",), professions=("code",)))
        cluster = state.from_template(
            "poc", Path("/tmp/p"), (member,),
            tags=AgentBuild(specialties=("muxer", "cluster", "cluster-cowork")))
        build = cluster.member_build(cluster.members[0])
        self.assertEqual(build.specialties,
                         ("muxer", "cluster", "cluster-cowork", "auto"))
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.engine, "researcher")   # the member's own

    def test_other_axes_are_untouched(self):
        member = Member.of("researcher", build=AgentBuild(
            engine="researcher", professions=("code",), policies=("all-actions",)))
        built = state.from_template("poc", Path("/tmp/p"), (member,)).members[0]
        self.assertEqual(built.build.engine, "researcher")
        self.assertEqual(built.build.professions, ("code",))
        self.assertEqual(built.build.policies, ("all-actions",))

    def test_cluster_tags_survive_a_round_trip(self):
        state.save(state.from_template(
            "poc", Path("/tmp/p"), (Member.of("golem"),),
            tags=AgentBuild(specialties=("muxer", "cluster", "cluster-cowork"),
                            policies=("free-bash",))))
        loaded = state.load("poc")
        self.assertEqual(loaded.tags.specialties,
                         ("muxer", "cluster", "cluster-cowork"))
        self.assertEqual(loaded.tags.policies, ("free-bash",))
        self.assertEqual(loaded.members[0].build.specialties, ())

    def test_member_tables_carry_ai_and_harness_as_scalars(self):
        # Per member, like the engine — the cluster forces neither.
        cluster = state.from_template(
            "poc", Path("/tmp/p"),
            (Member("researcher", "alien", build=AgentBuild(ai="grok", harness="grok-build", engine="golem")),),
            template="devteam")
        text = state.dumps(cluster)
        self.assertIn('ai = "grok"', text)
        self.assertIn('harness = "grok-build"', text)
        loaded = state.loads("poc", text)
        self.assertEqual((loaded.members[0].build.ai, loaded.members[0].build.harness), ("grok", "grok-build"))
        self.assertEqual((loaded.member_build(loaded.members[0]).ai, loaded.member_build(loaded.members[0]).harness), ("grok", "grok-build"))
        self.assertIsNone(loaded.tags.harness)

    def test_a_legacy_file_without_cluster_tags_still_loads(self):
        # Pre-2026-09-02 files repeat the forced pair in every member table
        # and carry no cluster-level keys. They must load into the new shape
        # (locked pair at cluster level, members stripped) — no migration.
        legacy = ('project = "/tmp/p"\n'
                  '[golem]\nengine = "golem"\n'
                  'professions = []\nspecialties = ["muxer", "cluster"]\n'
                  'policies = []\n')
        cluster = state.loads("poc", legacy)
        self.assertEqual(cluster.tags.specialties, ("muxer", "cluster"))
        self.assertEqual(cluster.members[0].build.specialties, ())
        # ...and the member still LAUNCHES with them.
        self.assertEqual(
            cluster.member_build(cluster.members[0]).specialties,
            ("muxer", "cluster"))

    def test_the_locked_names_are_real_tags(self):
        # A typo here would produce clusters that fail tag validation at launch.
        from launch import paths as real_paths
        from launch.tags import scan_all
        registry = scan_all(real_paths.AGENTS_DIR)
        for name in state.LOCKED_SPECIALTIES:
            with self.subTest(tag=name):
                self.assertIn(name, registry.specialties)


class TestMemberInstance(ClusterTmp):
    """`Cluster.member_instance` — a member as the ordinary Instance it launches
    as, resolved against the REAL registry. Its two failure encodings are the
    contract: None for a vanished agent, `invalid_tags` for a stale tag name —
    never a raise, because the picker needs both as visible rows and
    `launching.member_instances` adds the loud stop on top."""

    def setUp(self):
        super().setUp()
        from launch.paths import AGENTS_DIR
        from launch.tags import scan_all
        self.registry = scan_all(AGENTS_DIR)

    def test_a_member_is_an_instance_in_its_own_dir(self):
        cluster = self.a_cluster()
        inst = cluster.member_instance(cluster.member("researcher__primary"),
                                       self.registry)
        self.assertEqual(inst.state_dir,
                         paths.cluster_member_dir("poc", "researcher__primary"))
        self.assertEqual(inst.workspace, "/tmp/project")
        self.assertEqual(inst.engine.name, "researcher")   # its own .lego engine
        self.assertTrue(inst.is_startable)

    def test_the_instance_carries_the_clusters_tags_as_well_as_its_own(self):
        member = Member.of("researcher", build=AgentBuild(specialties=("auto",)))
        cluster = state.from_template(
            "poc", Path("/tmp/p"), (member,),
            tags=AgentBuild(specialties=("muxer", "cluster", "cluster-cowork")))
        inst = cluster.member_instance(cluster.members[0], self.registry)
        self.assertEqual([s.name for s in inst.specialties],
                         ["muxer", "cluster", "cluster-cowork", "auto"])

    def test_a_stale_tag_lands_on_invalid_tags_instead_of_raising(self):
        member = Member.of("poet", build=AgentBuild(specialties=("ghost-tag",)))
        cluster = state.from_template("poc", Path("/tmp/p"), (member,))
        inst = cluster.member_instance(cluster.members[0], self.registry)
        self.assertEqual([p.name for p in inst.invalid_tags], ["ghost-tag"])
        self.assertFalse(inst.is_startable)
        # The resolvable tags still resolved — the member renders, red chip and all.
        self.assertEqual([s.name for s in inst.specialties], ["muxer", "cluster"])

    def test_a_cluster_wide_container_tag_is_inherited_unflagged_while_a_members_own_is_flagged(self):
        # The two halves resolve in their own scopes (bug-investigator, gate
        # tag-scopes): the union at scope member would flag a legal
        # cluster-wide {dood} on every member.
        member = Member.of("researcher", build=AgentBuild(professions=("code",)))
        cluster = state.from_template("poc", Path("/tmp/p"), (member,),
                                      tags=AgentBuild(professions=("code",), specialties=("muxer", "cluster", "dood")))
        inst = cluster.member_instance(cluster.members[0], self.registry)
        self.assertTrue(inst.is_startable)
        self.assertIn("dood", [s.name for s in inst.specialties])
        own = state.from_template("poc2", Path("/tmp/p"), (Member.of(
            "researcher", build=AgentBuild(professions=("code",), specialties=("dood",))),))
        inst = own.member_instance(own.members[0], self.registry)
        (problem,) = inst.invalid_tags
        self.assertEqual((problem.name, problem.reason), ("dood", "forbidden"))
        self.assertEqual(problem.hint, "cluster-wide only: F2 on the cluster row")
        self.assertNotIn("dood", [s.name for s in inst.specialties])
        self.assertFalse(inst.is_startable)

    def test_forbidden_tags_speaks_for_the_creation_flows(self):
        # feature-identifier's .lego defaults carry {firewall}: it can be a
        # solo instance and no cluster member, and the line says THAT.
        from launch.cluster.legoset import assemble
        from launch.paths import AGENTS_DIR
        cluster = state.from_template("poc", Path("/tmp/p"), assemble([("feature-identifier", None)], AGENTS_DIR))
        (line,) = state.forbidden_tags(cluster, self.registry)
        self.assertIn("feature-identifier", line)
        self.assertIn("{frwl}", line)
        self.assertIn("not in a cluster", line)
        self.assertNotIn(".lego", line)
        self.assertEqual(state.forbidden_tags(self.a_cluster(), self.registry), [])
        wide = state.from_template("poc3", Path("/tmp/p"), (Member.of("golem"),),
                                   tags=AgentBuild(specialties=("muxer", "cluster", "firewall")))
        (line,) = state.forbidden_tags(wide, self.registry)
        self.assertIn("cluster-wide {frwl}: not on a cluster", line)

    def test_a_vanished_agent_is_none_not_a_raise(self):
        cluster = state.from_template("poc", Path("/tmp/p"), (Member.of("nobody"),))
        self.assertIsNone(cluster.member_instance(cluster.members[0], self.registry))


class TestLastUsed(ClusterTmp):
    """`Cluster.last_used_mtime` — a cluster goes by its LATEST member: the
    newest member history.jsonl, via the same probe an instance uses."""

    def _touch_history(self, member_id: str, mtime: float) -> None:
        import os
        history = paths.state_history_path(paths.cluster_member_dir("poc", member_id))
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text("{}\n")
        os.utime(history, (mtime, mtime))

    def test_no_member_history_means_never(self):
        self.assertIsNone(self.a_cluster().last_used_mtime)

    def test_the_newest_member_history_wins(self):
        self._touch_history("refactorer", 1_000_000.0)
        self._touch_history("researcher__primary", 2_000_000.0)
        self.assertEqual(self.a_cluster().last_used_mtime, 2_000_000.0)

    def test_one_member_with_history_is_enough(self):
        self._touch_history("researcher__adversarial", 1_500_000.0)
        self.assertEqual(self.a_cluster().last_used_mtime, 1_500_000.0)


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

    def test_an_edit_cannot_shed_the_clusters_tags_nor_duplicate_them(self):
        # The member form shows the cluster's tags locked-and-CHECKED, so they
        # come back in the result. with_build subtracts them — they live on the
        # cluster, once — while member_build proves the member still launches
        # with them. Unticking is impossible in the form and moot here.
        cluster = self.a_cluster()
        edited = cluster.with_build("refactorer", AgentBuild(
            professions=("code",), specialties=("muxer", "cluster", "auto")))
        member = edited.member("refactorer")
        self.assertEqual(member.build.specialties, ("auto",))       # own only
        self.assertEqual(edited.member_build(member).specialties,
                         ("muxer", "cluster", "auto"))              # real build

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


class TestRename(ClusterTmp):
    def test_rename_moves_the_state_and_returns_the_new_identity(self):
        renamed = state.rename(state.save(self.a_cluster()), "crew")
        self.assertEqual(renamed.session, "crew")
        self.assertFalse(state.exists("poc"))
        # Loading is canonical (id-sorted) — compare membership, not sequence.
        self.assertEqual(set(state.load("crew").ids), set(renamed.ids))

    def test_rename_refuses_a_collision_rather_than_merging(self):
        # The form validator blocks this upstream, but rename is a public
        # function — its own guard must hold without the form in front of it.
        cluster = state.save(self.a_cluster())
        state.save(state.from_template("other", Path("/tmp/p"),
                                       (Member.of("golem"),)))
        with self.assertRaises(ClusterError):
            state.rename(cluster, "other")
        self.assertTrue(state.exists("poc"))       # nothing moved
        self.assertEqual(state.load("other").ids, ("golem",))   # nothing merged

    def test_renaming_to_the_same_name_is_a_plain_save(self):
        cluster = state.save(self.a_cluster())
        self.assertEqual(state.rename(cluster, "poc"), cluster)


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
