"""Tests for launch.gui.picker_flows — what each picker key MEANS once a row
is chosen: the cluster create/edit flows (two forms in sequence), member
re-tagging, and the removals. Split out of test_menu_picker 2026-09-03 with
the code.

The forms themselves are stubbed; everything they return is applied for real
against a redirected AGENTS_STATE, and the dispatch tests drive the picker's
own loop to prove each key routes here."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker, picker_flows, picker_prompts
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Instance, resolve_build, scan_all

REGISTRY = scan_all(AGENTS_DIR)


def make_inst(agent="poet", session="s", workspace="/tmp", *,
              professions=(), specialties=(), policies=()):
    """A real Instance resolved against the real registry (engine falls back
    agent-name → default, exactly like a launch)."""
    build = AgentBuild(engine=None, professions=tuple(professions),
                       specialties=tuple(specialties), policies=tuple(policies))
    return Instance(agent=agent, md_path=Path(f"/fake/{agent}.md"), session=session,
                    workspace=workspace, is_brand_new=False,
                    **resolve_build(build, agent, REGISTRY))



class TestCreateClusterFlow(unittest.TestCase):
    """_create_cluster_flow — prompts and form stubbed, persistence real (into
    a redirected AGENTS_STATE). What must hold: a confirm SAVES exactly the
    picked members with template roles and auto-numbering applied, and a
    cancel saves nothing."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # The whole cluster feature composes its paths from AGENTS_STATE at
        # call time, so one patch on launch.paths moves it (the design
        # test_cluster_state relies on too).
        from launch import paths as launch_paths
        patcher = patch.object(launch_paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def flow(self, picks):
        # The form now carries name + path as TEXT FIELDS; its return is
        # (field values, picks) — None still means cancel.
        template_path = AGENTS_DIR / "devteam.legoset"
        answer = None if picks is None else (
            {"session": "myteam", "project": "/tmp/project"}, picks)
        # Step 1 is the cluster-tag form (stubbed to the locked pair, as
        # confirming it unchanged would return); step 2 is this form.
        with patch.object(picker_flows, "prompt_cluster_tags",
                          return_value=AgentBuild(
                              specialties=("muxer", "cluster"))), \
             patch.object(picker_flows, "prompt_members",
                          return_value=answer) as form, \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            picker_flows._create_cluster_flow(REGISTRY, template_path)
        return form

    def test_confirming_creates_the_cluster_with_previewed_ids(self):
        self.flow([("golem", None), ("golem", None), ("researcher", "primary")])
        from launch.cluster import state
        cluster = state.load("myteam")
        self.assertEqual(cluster.ids, ("golem__1", "golem__2",
                                       "researcher__primary"))
        self.assertEqual(str(cluster.project), "/tmp/project")
        # Members carry their agents' .lego defaults, not empty builds.
        self.assertEqual(cluster.member("researcher__primary").build.engine,
                         "researcher")

    def test_the_form_opens_prefilled_with_the_template(self):
        form = self.flow([("golem", None)])
        prefill = form.call_args.args[1]
        self.assertEqual(prefill[2:4], [("researcher", "primary"),
                                        ("researcher", "adversarial")])
        # Unroled template entries arrive as None so duplicates can renumber.
        self.assertEqual(prefill[0], ("project-starter", None))
        # And the fields ride in prefilled: template name, default workspace.
        # Project FIRST — it feeds the name's auto-fill; the name derives
        # <template>__<basename> live once the form opens.
        fields = form.call_args.kwargs["fields"]
        self.assertEqual([(f.key, f.value) for f in fields],
                         [("project", menu_picker.DEFAULT_WORKSPACE),
                          ("session", "devteam")])
        self.assertIsNotNone(fields[1].auto)
        self.assertEqual(fields[1].auto({"project": "/code/thing"}),
                         "devteam__thing")

    def test_cancelling_the_form_saves_nothing(self):
        self.flow(None)
        from launch.cluster import state
        self.assertEqual(state.discover(), [])


class TestClusterRowsAndEditing(unittest.TestCase):
    """Existing clusters in the picker: the rows, and the three verbs on them —
    F2 re-tags a member, Del removes one (guarding the last), Del on the
    cluster destroys it. Flows run against real persistence in a redirected
    AGENTS_STATE; only the interactive pieces (form, prompts, confirm) are
    stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from launch import paths as launch_paths
        patcher = patch.object(launch_paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        from launch.cluster import state
        from launch.cluster.member import Member
        self.state = state
        self.cluster = state.save(state.from_template(
            "team", Path("/tmp/project"),
            (Member.of("golem"), Member.of("researcher", "primary")),
            template="devteam"))

    def entries(self, running=None):
        captured = {}
        with patch.object(menu_picker, "pick_with_preview",
                          lambda t, entries, **kw:
                          captured.update(entries=entries) or (None, None)), \
             patch.object(menu_picker, "list_all_instances", return_value=[]), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=running):
            menu_picker.select_agent(REGISTRY)
        return captured["entries"]

    def test_a_cluster_row_then_its_members_nested_beneath(self):
        entries = self.entries()
        kinds = [type(e.value).__name__ for e in entries
                 if isinstance(e.value, (menu_picker._ClusterRow,
                                         menu_picker._MemberRow))]
        self.assertEqual(kinds, ["_ClusterRow", "_MemberRow", "_MemberRow"])
        cluster_row = next(e for e in entries
                           if isinstance(e.value, menu_picker._ClusterRow))
        self.assertEqual(tuple(cluster_row.display[:1]),
                         menu_picker.PickerRowMarker.CLSTR.lead)
        self.assertTrue(cluster_row.modifiable)   # F2: rename / repoint / membership
        self.assertTrue(cluster_row.deletable)

    def test_member_rows_are_the_editing_unit(self):
        member_rows = [e for e in self.entries()
                       if isinstance(e.value, menu_picker._MemberRow)]
        self.assertEqual([r.value.member_id for r in member_rows],
                         ["golem", "researcher__primary"])
        for row in member_rows:
            with self.subTest(member=row.value.member_id):
                self.assertTrue(row.modifiable)
                self.assertTrue(row.deletable)
                # The row shows the member's OWN tags only: the cluster's
                # tags moved to the CLUSTER row (2026-09-02), because
                # repeating {mux}{clstr} on every member was noise —
                # "visual duplication", in the operator's words.
                text = "".join(t for _, t in row.display)
                self.assertNotIn("{clstr}", text)
                self.assertNotIn("{mux}", text)

    def test_a_running_cluster_locks_its_row_and_its_members(self):
        # The live container is named claude-code_cluster-team, so the probe's
        # stripped set carries "cluster-team" — the same detection instances
        # get. All three keys go dark on the cluster row (Enter → docker name
        # conflict; F2 rename / Del would move or delete the state dir the
        # container has mounted) and on every member row (their edits write
        # into that same dir), and the row wears the red RUNNING tag.
        rows = [e for e in self.entries(running=frozenset({"cluster-team"}))
                if isinstance(e.value, (menu_picker._ClusterRow,
                                        menu_picker._MemberRow))]
        self.assertEqual(len(rows), 3)   # the cluster and both members
        for row in rows:
            with self.subTest(value=row.value):
                self.assertFalse(row.selectable)
                self.assertFalse(row.deletable)
                self.assertFalse(row.modifiable)
        self.assertIn(menu_picker.RUNNING_HINT, rows[0].display)

    def test_a_different_running_cluster_locks_nothing_here(self):
        cluster_row = next(e for e in self.entries(
            running=frozenset({"cluster-other"}))
            if isinstance(e.value, menu_picker._ClusterRow))
        self.assertTrue(cluster_row.selectable)
        self.assertNotIn(menu_picker.RUNNING_HINT, cluster_row.display)

    def test_f2_persists_only_the_members_OWN_tags(self):
        # The member form shows the cluster's tags locked-and-checked, so a
        # confirm returns them — persistence subtracts them again (they live
        # on the cluster, once) while the member still LAUNCHES with them.
        with patch.object(picker_flows, "prompt_tags",
                          return_value=AgentBuild(
                              professions=("code",),
                              specialties=("muxer", "cluster", "auto"))):
            picker_flows._edit_member_flow(REGISTRY, "team", "golem")
        cluster = self.state.load("team")
        edited = cluster.member("golem")
        self.assertEqual(edited.build.professions, ("code",))
        self.assertEqual(edited.build.specialties, ("auto",))
        self.assertEqual(cluster.member_build(edited).specialties,
                         ("muxer", "cluster", "auto"))

    def test_f2_shows_the_clusters_tags_locked_so_a_member_sees_its_real_build(self):
        # What the member form is HANDED matters: the cluster's tags must
        # arrive checked AND named as locked, or the member row's form would
        # imply the member isn't carrying them.
        with patch.object(picker_flows, "prompt_tags",
                          return_value=None) as form:
            picker_flows._edit_member_flow(REGISTRY, "team", "golem")
        current = form.call_args.args[1]
        self.assertEqual(current.specialties, ("muxer", "cluster"))
        self.assertEqual(form.call_args.kwargs["locked"],
                         frozenset({"muxer", "cluster"}))

    def test_cancelling_the_tag_form_changes_nothing(self):
        with patch.object(picker_flows, "prompt_tags", return_value=None):
            picker_flows._edit_member_flow(REGISTRY, "team", "golem")
        self.assertEqual(self.state.load("team"), self.cluster)

    def test_del_removes_the_member_after_confirmation(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=True), \
             patch.object(picker_flows, "confirm_dialog", return_value=True):
            picker_flows._remove_member_flow("team", "researcher__primary")
        self.assertEqual(self.state.load("team").ids, ("golem",))

    def test_an_unconfirmed_removal_changes_nothing(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=False), \
             patch.object(picker_flows, "confirm_dialog", return_value=False):
            picker_flows._remove_member_flow("team", "golem")
        self.assertEqual(self.state.load("team").ids,
                         ("golem", "researcher__primary"))

    def test_the_last_member_cannot_be_removed(self):
        # An empty cluster is unrepresentable; the honest gesture is destroying
        # the cluster, which the guard message points at.
        with patch.object(menu_picker, "confirm_dialog", return_value=True), \
             patch.object(picker_flows, "confirm_dialog", return_value=True), \
             patch("builtins.input", return_value=""), \
             patch("builtins.print") as told:
            picker_flows._remove_member_flow("team", "researcher__primary")
            picker_flows._remove_member_flow("team", "golem")
        self.assertEqual(self.state.load("team").ids, ("golem",))
        self.assertIn("only member", str(told.call_args_list))

    def test_del_on_the_cluster_row_destroys_it(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=True), \
             patch.object(picker_flows, "confirm_dialog", return_value=True):
            picker_flows._destroy_cluster_flow("team")
        self.assertFalse(self.state.exists("team"))

    def test_an_unconfirmed_destroy_keeps_everything(self):
        with patch.object(menu_picker, "confirm_dialog", return_value=False), \
             patch.object(picker_flows, "confirm_dialog", return_value=False):
            picker_flows._destroy_cluster_flow("team")
        self.assertTrue(self.state.exists("team"))

    def dispatch(self, action, value):
        """Drive select_agent's dispatch once: the stubbed picker returns the
        given (action, value), then cancels on the next loop iteration."""
        answers = iter([(action, value), (None, None)])
        with patch.object(menu_picker, "pick_with_preview",
                          lambda *a, **kw: next(answers)), \
             patch.object(menu_picker, "list_all_instances", return_value=[]), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=None), \
             patch.object(picker_flows, "prompt_cluster_tags",
                          return_value=AgentBuild(
                              specialties=("muxer", "cluster"))), \
             patch.object(menu_picker, "confirm_dialog", return_value=True), \
             patch.object(picker_flows, "confirm_dialog", return_value=True):
            self.assertIsNone(menu_picker.select_agent(REGISTRY))

    def test_the_delete_key_routes_a_member_row_to_member_removal(self):
        # The DELETE branch used to assume Instance (`value.instance`) — a
        # member row reaching it must shrink the cluster, not crash or, worse,
        # destroy the whole cluster.
        self.dispatch(menu_picker.PickerAction.DELETE,
                      menu_picker._MemberRow("team", "golem"))
        self.assertEqual(self.state.load("team").ids, ("researcher__primary",))

    def test_the_modify_key_routes_a_member_row_to_the_tag_form(self):
        with patch.object(picker_flows, "prompt_tags",
                          return_value=AgentBuild(engine="thinker")):
            self.dispatch(menu_picker.PickerAction.MODIFY,
                          menu_picker._MemberRow("team", "golem"))
        self.assertEqual(self.state.load("team").member("golem").build.engine,
                         "thinker")

    def test_the_delete_key_routes_a_cluster_row_to_destruction(self):
        self.dispatch(menu_picker.PickerAction.DELETE,
                      menu_picker._ClusterRow("team"))
        self.assertFalse(self.state.exists("team"))


class TestEditClusterFlow(TestClusterRowsAndEditing):
    """F2 on the cluster row — one form edits name, project, and membership.
    Inherits the fixture (cluster 'team': golem + researcher__primary in a
    redirected AGENTS_STATE); the form itself is stubbed, everything it
    returns is applied for real."""

    def edit(self, session="team", values=None, picks=None, tags=None):
        answer = None if values is None else (values, picks)
        with patch.object(picker_flows, "prompt_cluster_tags",
                          return_value=tags or AgentBuild(
                              specialties=("muxer", "cluster"))), \
             patch.object(picker_flows, "prompt_members",
                          return_value=answer) as form, \
             patch("builtins.input", return_value=""), \
             patch("builtins.print"):
            picker_flows._edit_cluster_flow(REGISTRY, session)
        return form

    def keep_picks(self):
        return [("golem", None), ("researcher", "primary")]

    def test_the_form_opens_prefilled_with_the_cluster(self):
        form = self.edit(values={"session": "team", "project": "/tmp/project"},
                         picks=self.keep_picks())
        self.assertEqual(form.call_args.args[1],
                         [("golem", None), ("researcher", "primary")])
        fields = form.call_args.kwargs["fields"]
        self.assertEqual([(f.key, f.value) for f in fields],
                         [("project", "/tmp/project"), ("session", "team")])
        # Editing pins the name — no auto derivation on a rename form.
        self.assertIsNone(fields[1].auto)
        # Keeping your own name must not read as a collision.
        self.assertIsNone(fields[1].validate("team"))
        self.assertIsNotNone(picker_prompts._session_field_error("team", None))

    def test_renaming_moves_the_whole_directory(self):
        # Member state dirs ride the move — a rename must not orphan them.
        from launch import paths as launch_paths
        member_file = (launch_paths.cluster_member_dir("team", "golem")
                       / "CLAUDE.md")
        member_file.parent.mkdir(parents=True)
        member_file.write_text("persona")
        self.edit(values={"session": "crew", "project": "/tmp/project"},
                  picks=self.keep_picks())
        self.assertFalse(self.state.exists("team"))
        renamed = self.state.load("crew")
        self.assertEqual(renamed.ids, ("golem", "researcher__primary"))
        self.assertEqual((launch_paths.cluster_member_dir("crew", "golem")
                          / "CLAUDE.md").read_text(), "persona")

    def test_repointing_the_project(self):
        self.edit(values={"session": "team", "project": "/tmp"},
                  picks=self.keep_picks())
        self.assertEqual(str(self.state.load("team").project), "/tmp")

    def test_surviving_members_keep_their_edited_builds(self):
        # THE reassemble guarantee: an unrelated edit (rename, membership
        # change) must not wipe a member's F2-edited tags back to .lego.
        self.state.save(self.cluster.with_build(
            "golem", AgentBuild(engine="thinker")))
        self.edit(values={"session": "team", "project": "/tmp/project"},
                  picks=self.keep_picks() + [("poet", None)])
        edited = self.state.load("team")
        self.assertEqual(edited.member("golem").build.engine, "thinker")
        # The newcomer stores its .lego minus the cluster's tags — and still
        # launches with them, which is what member_build is for.
        newcomer = edited.member("poet")
        self.assertEqual(newcomer.build.specialties, ())
        self.assertEqual(edited.member_build(newcomer).specialties,
                         ("muxer", "cluster"))

    def test_membership_shrinks_when_a_pick_is_dropped(self):
        self.edit(values={"session": "team", "project": "/tmp/project"},
                  picks=[("golem", None)])
        self.assertEqual(self.state.load("team").ids, ("golem",))

    def test_cancel_changes_nothing(self):
        self.edit(values=None)
        self.assertEqual(self.state.load("team"), self.cluster)

    def test_the_modify_key_routes_a_cluster_row_here(self):
        with patch.object(picker_flows, "prompt_members",
                          return_value=({"session": "crew",
                                         "project": "/tmp/project"},
                                        self.keep_picks())), \
             patch("builtins.input", return_value=""):
            self.dispatch(menu_picker.PickerAction.MODIFY,
                          menu_picker._ClusterRow("team"))
        self.assertTrue(self.state.exists("crew"))


if __name__ == "__main__":
    unittest.main()
