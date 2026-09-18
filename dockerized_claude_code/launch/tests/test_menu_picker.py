"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the row factories (continuable_instances / cluster_entries — sorting, the
cwd-relation hint, tag display), the row anatomy every existing thing wears,
and the --stop selector. The tag form's tests live in test_form_core.py; the
prompt_toolkit Applications themselves are interactive and stay out of unit
scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker, picker_widget
from launch.gui.menu_picker import (
    RUNNING_HINT, STYLE_RUNNING_NAME, continuable_instances,
)
from launch.gui.picker_widget import PickerCwdHint
from launch.paths import DEFAULT_WORKSPACE, DEFAULTING_DIRS
from launch.gui.styles import STYLE_TAG_HARNESS
from launch.tags import AgentBuild, Instance
from launch.tests.fixtures import REGISTRY, make_inst


class TestInstanceBuild(unittest.TestCase):
    def test_round_trips_axis_names(self):
        inst = make_inst(professions=["code", "webdev"], specialties=["auto"],
                         policies=["no-sudo"])
        build = inst.build
        self.assertEqual(build.professions, ("code", "webdev"))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("no-sudo",))

    def test_engine_name_captured(self):
        self.assertEqual(make_inst(agent="poet").build.engine, "poet")


class TestContinuableInstances(unittest.TestCase):
    """continuable_instances turns store-backed Instances into sorted,
    flagged Cont rows. Real repo tags/engines provide the resolution side
    (poet — sonnet, golem — haiku); the store factory + listing + cwd are
    patched."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ws = self.tmpdir.name
        # No instance has a history.jsonl in these fixtures.
        patcher = patch("launch.tags.identity.last_history_mtime", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _entries(self, insts, cwd=None, running=None):
        # `running` mirrors docker_running_instances_subprocess: a frozenset of
        # running instance ids, or None for "couldn't determine". Default None
        # keeps every other test docker-free.
        by_id = {i.instance: i for i in insts}
        with patch.object(menu_picker, "list_all_instances", return_value=list(by_id) + ["ghost__x"]), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: by_id.get(name)), \
             patch.object(menu_picker, "docker_running_instances_subprocess", return_value=running), \
             patch.object(menu_picker, "resolved_cwd", return_value=cwd or Path("/nowhere")):
            return continuable_instances(REGISTRY)

    def test_orphan_instances_skipped(self):
        # instance_from_store returns None for ghost__x (no .md) — row dropped.
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertEqual([e.identity.instance for e in entries], ["golem__a"])

    def test_preview_expands_the_tags(self):
        # The row column may show one-char chips; the preview is where a tag
        # can be READ — label plus description, not just the name.
        entries = self._entries([make_inst("golem", "a", self.ws, specialties=["auto"])])
        preview = entries[0].preview
        self.assertIn("{auto}", preview)
        self.assertIn(REGISTRY.specialties["auto"].short_description, preview)

    def test_preview_lists_the_ai_before_the_tags(self):
        # Which AI runs the instance is the first thing the pane says about
        # its tags (operator, 2026-09-13); the fixture resolves to the default.
        entries = self._entries([make_inst("golem", "a", self.ws, specialties=["auto"])])
        preview = entries[0].preview
        ai = REGISTRY.default_ai
        self.assertIn(ai.label, preview)
        self.assertLess(preview.index(ai.label), preview.index("{auto}"))
        self.assertIn(ai.fullname, preview)

    def test_preview_without_history_has_no_last_prompt_field(self):
        # The field drops out entirely rather than showing an empty label.
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertNotIn("Last prompt", entries[0].preview)


    def test_tagless_sorts_before_tagged_and_family_orders_within(self):
        # poet (sonnet) outranks golem (haiku); golem__b carries a specialty
        # so it sinks below both tag-less rows regardless of family.
        entries = self._entries([
            make_inst("golem", "a", self.ws),
            make_inst("golem", "b", self.ws, specialties=["auto"]),
            make_inst("poet", "p", self.ws),
        ])
        self.assertEqual([e.identity.instance for e in entries],
                         ["poet__p", "golem__a", "golem__b"])

    def test_current_dir_flagged(self):
        entries = self._entries([make_inst("golem", "a", self.ws)],
                                cwd=Path(self.ws).resolve())
        self.assertIs(entries[0].workspace.hint, PickerCwdHint.CURRENT)

    def test_invalid_workspace_flagged_but_shown(self):
        entries = self._entries([make_inst("golem", "a", "/no/such/dir")])
        self.assertIs(entries[0].workspace.hint, PickerCwdHint.INVALID)
        self.assertEqual(entries[0].workspace.display, "/no/such/dir")   # stored value still shown

    def test_missing_workspace_shows_placeholder(self):
        entries = self._entries([make_inst("golem", "a", None)])
        self.assertEqual(entries[0].workspace, menu_picker.WorkspaceView("?", None))
        self.assertIsNone(entries[0].identity.workspace)

    def test_never_used_renders_never(self):
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertEqual(entries[0].last_used_display, "(never)")


class TestRunningFlag(TestContinuableInstances):
    """continuable_instances marks which instances have a live container, and
    the picker renders those rows greyed + tagged instead of blue."""

    def test_running_instance_flagged(self):
        entries = self._entries([make_inst("golem", "a", self.ws), make_inst("poet", "b", self.ws)],
                                running=frozenset({"golem__a"}))
        flags = {e.identity.instance: e.is_running for e in entries}
        self.assertTrue(flags["golem__a"])
        self.assertFalse(flags["poet__b"])

    def test_nothing_running_flags_nothing(self):
        entries = self._entries([make_inst("golem", "a", self.ws)], running=frozenset())
        self.assertFalse(entries[0].is_running)

    def test_undeterminable_docker_state_flags_nothing(self):
        # None = `docker ps` failed / docker absent. Marking every row RUNNING
        # would wrongly lock instances the user can actually launch.
        entries = self._entries([make_inst("golem", "a", self.ws)], running=None)
        self.assertFalse(entries[0].is_running)

    def test_running_row_greyed_and_tagged_and_locked(self):
        (entry,) = self._entries([make_inst("golem", "a", self.ws)], running=frozenset({"golem__a"}))
        self.assertTrue(entry.is_running)
        self.assertEqual(STYLE_RUNNING_NAME, "fg:ansibrightblack")   # grey, not STYLE_AGENT_NAME blue
        self.assertEqual(RUNNING_HINT[1].strip(), "(RUNNING)")
        self.assertIn("red", RUNNING_HINT[0])                        # the tag itself is the red part


class TestRowAssembly(unittest.TestCase):
    """select_agent's entry building, run for real with the TUI stubbed out.
    The markers are multi-fragment now and SPLATTED into each display list —
    a call site still treating one as a single fragment would only blow up
    when the picker opens interactively, which no other test does."""

    def entries(self):
        captured = {}

        def fake_pick(title, entries, **kw):
            captured["entries"] = entries
            return (None, None)

        # One real store-backed instance rides along, so the instance-row
        # assertions below can never pass vacuously — without this, the test
        # environment has no instances and `inst_rows` would be empty (the
        # exact silent-guard failure a mutation run caught once already).
        inst = make_inst("golem", "assembly", "/tmp")
        with patch.object(menu_picker, "pick_with_preview", fake_pick), \
             patch.object(menu_picker, "list_all_instances",
                          return_value=[inst.instance]), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: inst), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=None), \
             patch("launch.tags.identity.last_history_mtime", return_value=None):
            self.assertIsNone(menu_picker.select_agent(REGISTRY))
        return captured["entries"]

    def test_every_display_fragment_is_a_style_text_pair(self):
        for entry in self.entries():
            for fragment in entry.display:
                with self.subTest(fragment=fragment):
                    style, text = fragment          # unpacking IS the assertion
                    self.assertIsInstance(style, str)
                    self.assertIsInstance(text, str)

    def test_agent_rows_open_with_the_tab_and_its_tip(self):
        agent_rows = [e for e in self.entries()
                      if isinstance(e.value, menu_picker.Agent)]
        self.assertTrue(agent_rows)
        for row in agent_rows:
            with self.subTest(agent=row.value.name):
                self.assertEqual(tuple(row.display[:2]),
                                 menu_picker.PickerRowMarker.NEW.lead)

    def test_instance_rows_open_with_the_nested_marker(self):
        inst_rows = [e for e in self.entries()
                     if isinstance(e.value, Instance)]
        self.assertTrue(inst_rows, "fixture must yield at least one Cont row")
        for row in inst_rows:
            with self.subTest(instance=row.value.instance):
                self.assertEqual((row.display[0],),
                                 menu_picker.PickerRowMarker.CONT.lead)

    def test_instance_rows_wear_the_ai_and_harness_before_the_name_and_agent_rows_none(self):
        # The runtime column sits between the tags and the name on INSTANCE
        # rows: the AI in its own colours (operator, 2026-09-13), then the
        # harness (operator, 2026-09-14). An agent row carries neither: both
        # are decided when an instance is created, they are not properties of
        # the agent — nor does the Create pane show them.
        ai = REGISTRY.default_ai
        harness = REGISTRY.harnesses[ai.harness]
        entries = self.entries()
        inst_rows = [e for e in entries if isinstance(e.value, Instance)]
        agent_rows = [e for e in entries if isinstance(e.value, menu_picker.Agent)]
        self.assertTrue(inst_rows and agent_rows)
        for row in inst_rows:
            with self.subTest(instance=row.value.instance):
                text = "".join(t for _, t in row.display)
                self.assertLess(text.index(ai.label), text.index(harness.label))
                self.assertLess(text.index(harness.label), text.index(row.value.instance))
                self.assertIn((ai.style, ai.label), row.display)
                self.assertIn((STYLE_TAG_HARNESS, harness.label), row.display)
        for row in agent_rows:
            with self.subTest(agent=row.value.name):
                text = "".join(t for _, t in row.display)
                self.assertNotIn(ai.label, text)
                self.assertNotIn(harness.label, text)
                self.assertNotIn("Tags:", row.preview)      # the pane is the persona alone (which may itself mention a tag)

    def test_each_shipped_template_gets_a_cluster_row(self):
        # The real tree ships devteam.legoset; its row opens the creation flow
        # (the value carries the template path for the dispatcher).
        rows = [e for e in self.entries()
                if isinstance(e.value, menu_picker._ClusterTemplateRow)]
        self.assertEqual([r.value.name for r in rows], ["devteam"])
        (row,) = rows
        self.assertEqual(tuple(row.display[:2]),
                         menu_picker.PickerRowMarker.CLUSTER.lead)
        # Agent-row anatomy: member COUNT (in creation-green) where agents
        # show tags, then the name, then " — description" — which describes
        # what the TEAM does (the .legoset's description key), never a member
        # list; the enumeration lives in the preview.
        text = "".join(t for _, t in row.display)
        self.assertIn("(5 members)", text)
        count_style = next(style for style, t in row.display if "members)" in t)
        self.assertEqual(count_style, menu_picker.STYLE_MEMBER_COUNT)
        self.assertIn("green", menu_picker.STYLE_MEMBER_COUNT)
        self.assertIn(" — Builds features end to end", text)
        self.assertNotIn("researcher__primary", text)

    def test_the_template_name_sits_in_the_agents_name_column(self):
        # "indented the same distance as agents' entries": the cluster tab is
        # wider than the agent tab, so the count column is PADDED to land the
        # template name exactly where agent names start — measured per row
        # text, not trusted from the arithmetic that produced it.
        entries = self.entries()
        agent_row = next(e for e in entries
                         if isinstance(e.value, menu_picker.Agent))
        cluster_row = next(e for e in entries
                           if isinstance(e.value, menu_picker._ClusterTemplateRow))
        agent_text = "".join(t for _, t in agent_row.display)
        cluster_text = "".join(t for _, t in cluster_row.display)
        self.assertEqual(cluster_text.index(cluster_row.value.name),
                         agent_text.index(agent_row.value.name))

    def test_every_marked_row_starts_with_its_markers_lead(self):
        # `PickerEntry.marker` drives the accent bar; the lead the producer
        # splats into `display` must be that same marker's, or the bar and
        # the row would disagree about what kind of row this is.
        for entry in self.entries():
            if entry.marker is None:
                continue
            with self.subTest(marker=entry.marker.name):
                lead = entry.marker.lead
                self.assertEqual(tuple(entry.display[:len(lead)]), lead)

    def test_a_broken_template_renders_unselectable_not_a_crash(self):
        # Templates are hand-authored; the picker is where the author IS, so a
        # parse error must become a red info row naming the fault.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "oops.legoset"
            bad.write_text("members = []")
            with patch.object(menu_picker, "discover_templates",
                              return_value={"oops": bad}):
                rows = [e for e in self.entries() if e.selectable is False
                        and "broken template" in "".join(t for _, t in e.display)]
        self.assertEqual(len(rows), 1)


class TestPromptStop(unittest.TestCase):
    """prompt_stop — the `--stop` flag's selector. Running rows only, wearing
    the picker's Cont-row anatomy WITHOUT the (RUNNING) hint (everything here
    runs by definition), `{muxer}` emphasized wherever present, clusters and
    stray container ids included, and the checked keys returned verbatim in
    the running-snapshot's prefix-stripped spelling."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.ws = self.tmpdir.name
        patcher = patch("launch.tags.identity.last_history_mtime", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Cluster rows read their members' state dirs; keep that off the
        # real ~/.ai-agents.
        from launch import paths as launch_paths
        redirect = patch.object(launch_paths, "AGENTS_STATE", Path(self.ws))
        redirect.start()
        self.addCleanup(redirect.stop)

    def _run(self, insts, running, clusters=(), picked=None):
        captured = {}

        def fake_form(title, options, **kwargs):
            captured["title"] = title
            captured["options"] = options
            return picked

        by_id = {i.instance: i for i in insts}
        with patch.object(menu_picker, "list_all_instances",
                          return_value=list(by_id)), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: by_id.get(name)), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=frozenset(running)), \
             patch.object(menu_picker, "resolved_cwd",
                          return_value=Path("/nowhere")), \
             patch.object(menu_picker.cluster_state, "discover",
                          return_value=list(clusters)), \
             patch.object(menu_picker, "checkbox_form",
                          side_effect=fake_form), \
             patch("builtins.print"):
            result = menu_picker.prompt_stop(REGISTRY)
        return result, captured

    @staticmethod
    def _row_text(option):
        return "".join(text for _, text in option.label)

    def test_only_running_instances_are_offered(self):
        insts = [make_inst("golem", "up", self.ws),
                 make_inst("golem", "down", self.ws)]
        _, captured = self._run(insts, running={"golem__up"})
        self.assertEqual([o.key for o in captured["options"]], ["golem__up"])

    def test_no_running_hint_and_the_row_keeps_the_picker_anatomy(self):
        # tags · AI · name · workspace — but never "(RUNNING)": in this list
        # it would say nothing.
        insts = [make_inst("golem", "up", self.ws, specialties=["auto"])]
        _, captured = self._run(insts, running={"golem__up"})
        row = self._row_text(captured["options"][0])
        self.assertIn("golem__up", row)
        self.assertIn(self.ws, row)
        self.assertNotIn("(RUNNING)", row)
        ai = REGISTRY.default_ai
        self.assertLess(row.index("{auto}"), row.index(ai.label))
        self.assertLess(row.index(ai.label), row.index("golem__up"))

    def test_muxer_is_emphasized_other_tags_are_not(self):
        insts = [make_inst("golem", "up", self.ws,
                           specialties=["auto", "muxer"])]
        _, captured = self._run(insts, running={"golem__up"})
        styles = {text: style for style, text in captured["options"][0].label}
        self.assertIn(picker_widget.TAG_EMPHASIS, styles["{mux}"])
        self.assertNotIn(picker_widget.TAG_EMPHASIS, styles["{auto}"])

    def test_running_clusters_get_a_row_keyed_by_container_id(self):
        # A REAL cluster (unsaved — the row factory only needs the record):
        # its rows are built through member_instance, so a stand-in object
        # would not do any more.
        from launch.cluster import state
        from launch.cluster.member import Member
        cluster = state.from_template("team", Path("/proj"),
                                      (Member.of("golem"), Member.of("poet")))
        _, captured = self._run([], running={"cluster-team"},
                                clusters=[cluster])
        (row,) = captured["options"]
        self.assertEqual(row.key, "cluster-team")
        text = self._row_text(row)
        self.assertIn("team", text)
        self.assertIn("/proj", text)
        self.assertIn("(2 members)", text)          # the cluster column carries the count
        self.assertIn("(INVALID DIR)", text)        # and the cwd hint, like an instance row
        self.assertIn("last used", "".join(t for _, t in row.body))

    def test_a_stray_running_id_still_gets_a_stoppable_row(self):
        # A container with no store entry and no cluster is exactly what
        # someone reaching for --stop most needs to be able to stop.
        _, captured = self._run([], running={"mystery__leftover"})
        (row,) = captured["options"]
        self.assertEqual(row.key, "mystery__leftover")

    def test_esc_stops_nothing(self):
        insts = [make_inst("golem", "up", self.ws)]
        result, _ = self._run(insts, running={"golem__up"}, picked=None)
        self.assertEqual(result, [])

    def test_picked_keys_come_back_verbatim(self):
        insts = [make_inst("golem", "up", self.ws)]
        result, _ = self._run(insts, running={"golem__up"},
                              picked=["golem__up"])
        self.assertEqual(result, ["golem__up"])

    def test_nothing_running_skips_the_form_entirely(self):
        result, captured = self._run([make_inst("golem", "s", self.ws)],
                                     running=set())
        self.assertEqual(result, [])
        self.assertNotIn("options", captured)   # checkbox_form never opened


class TestCwdContext(unittest.TestCase):
    """`_CwdContext` — the ONE reading of a workspace's relation to the launch
    site, shared by instance rows, cluster rows and --stop (it was inline in
    the instance factory, and the hint chain was copied per row kind). The
    precedence is the contract: CURRENT beats DEFAULT, INVALID is exclusive,
    and DEFAULT applies only when the launcher runs from a neutral dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.here = Path(self.tmp.name).resolve()
        self.other = Path(tempfile.mkdtemp(dir=self.tmp.name)).resolve()

    def test_the_cwd_itself_reads_current_even_when_it_is_the_default(self):
        ctx = menu_picker._CwdContext(cwd=self.here, default_workspace=self.here,
                                      defaulting=True)
        self.assertIs(ctx.view(str(self.here)).hint, PickerCwdHint.CURRENT)

    def test_the_default_workspace_reads_default_only_from_a_neutral_dir(self):
        neutral = menu_picker._CwdContext(cwd=self.here, default_workspace=self.other,
                                          defaulting=True)
        self.assertIs(neutral.view(str(self.other)).hint, PickerCwdHint.DEFAULT)
        # From a project dir the "default" is just another path — no hint,
        # because being in a project under $HOME does not make /ai_workspace
        # your default workspace.
        project = menu_picker._CwdContext(cwd=self.here, default_workspace=self.other,
                                          defaulting=False)
        self.assertIsNone(project.view(str(self.other)).hint)

    def test_a_vanished_path_reads_invalid_but_is_still_shown(self):
        ctx = menu_picker._CwdContext(cwd=self.here, default_workspace=self.here,
                                      defaulting=False)
        view = ctx.view("/no/such/dir")
        self.assertIs(view.hint, PickerCwdHint.INVALID)
        self.assertEqual(view.display, "/no/such/dir")

    def test_nothing_stored_shows_the_placeholder_with_no_hint(self):
        ctx = menu_picker._CwdContext(cwd=self.here, default_workspace=self.here,
                                      defaulting=False)
        self.assertEqual(ctx.view(None), menu_picker.WorkspaceView("?", None))

    def test_here_knows_whether_the_launch_site_is_neutral(self):
        with patch.object(menu_picker, "resolved_cwd",
                          return_value=Path(DEFAULTING_DIRS[0]).resolve()):
            self.assertTrue(menu_picker._CwdContext.here().defaulting)
        with patch.object(menu_picker, "resolved_cwd", return_value=self.here):
            self.assertFalse(menu_picker._CwdContext.here().defaulting)


class TestClusterRows(unittest.TestCase):
    """Cluster rows wear the instance rows' anatomy (`_session_row`): their
    project paths line up in one column, they carry the cwd hints, and their
    members are rows with the instance rows' deferred previews. Real clusters
    in a redirected AGENTS_STATE; the TUI is stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        from launch import paths as launch_paths
        patcher = patch.object(launch_paths, "AGENTS_STATE", Path(self._tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        from launch.cluster import state
        from launch.cluster.member import Member
        self.state, self.Member = state, Member

    def save(self, session, members, project="/tmp/project", tags=None):
        return self.state.save(self.state.from_template(
            session, Path(project), members, template="devteam", tags=tags))

    def entries(self, cwd=None, running=None):
        captured = {}
        with patch.object(menu_picker, "pick_with_preview",
                          lambda t, entries, **kw:
                          captured.update(entries=entries) or (None, None)), \
             patch.object(menu_picker, "list_all_instances", return_value=[]), \
             patch.object(menu_picker, "docker_running_instances_subprocess",
                          return_value=running), \
             patch.object(menu_picker, "resolved_cwd",
                          return_value=cwd or Path("/nowhere")):
            menu_picker.select_agent(REGISTRY)
        return captured["entries"]

    @staticmethod
    def text(entry):
        return "".join(t for _, t in entry.display)

    def cluster_rows(self, **kw):
        return [e for e in self.entries(**kw)
                if isinstance(e.value, menu_picker._ClusterRow)]

    def test_a_break_row_sits_between_cluster_blocks_and_nowhere_else(self):
        # One rule per boundary: two clusters means one rule, and it sits
        # between the last row of the first block and the first row of the
        # second — never before the first block or after the last.
        self.save("aa", (self.Member.of("golem"),))
        self.save("bb", (self.Member.of("poet"),))
        entries = self.entries()
        breaks = [i for i, e in enumerate(entries) if e.separator]
        rows_of = lambda session: [i for i, e in enumerate(entries)
                                   if isinstance(e.value, (menu_picker._ClusterRow, menu_picker._MemberRow))
                                   and e.value.session == session]
        (rule,) = breaks
        self.assertEqual(max(rows_of("aa")) + 1, rule)
        self.assertEqual(min(rows_of("bb")), rule + 1)

    def test_the_break_is_a_rule_no_wider_than_the_rows_it_separates(self):
        self.save("aa", (self.Member.of("golem"),))
        self.save("bb", (self.Member.of("poet"),))
        entries = self.entries()
        rule = next(e for e in entries if e.separator)
        text = self.text(rule)
        self.assertEqual(set(text), {picker_widget.BREAK_CHAR})
        widest_cluster_row = max(len(self.text(r)) for r in self.cluster_rows())
        self.assertLessEqual(len(text), widest_cluster_row)
        self.assertFalse(rule.selectable or rule.pickable)

    def test_one_cluster_gets_no_break_at_all(self):
        self.save("solo", (self.Member.of("golem"),))
        self.assertEqual([e for e in self.entries() if e.separator], [])

    def test_filtering_a_word_two_clusters_share_keeps_the_break_between_them(self):
        # The reason the rules exist: both members match "researcher", and
        # without the kept rule they would read as one cluster's roster.
        self.save("aa", (self.Member.of("researcher", "primary"),))
        self.save("bb", (self.Member.of("researcher", "other"),))
        entries = self.entries()
        shown = picker_widget._visible_indices(entries, "researcher__")
        kinds = [entries[i] for i in shown]
        # The member rows carry their AI and harness after the name, so match
        # on the id rather than the whole row.
        names = [self.text(e) for e in kinds if not e.separator]
        self.assertEqual(len(names), 2)
        self.assertIn("researcher__primary", names[0])
        self.assertIn("researcher__other", names[1])
        self.assertTrue(kinds[1].separator)           # exactly one rule, between the two
        self.assertEqual(sum(e.separator for e in kinds), 1)

    def test_project_paths_line_up_across_cluster_rows(self):
        # Different name lengths AND different tag sets — the two things the
        # column pads — must still land the path at one index (the same
        # measured-not-trusted check the template rows get).
        self.save("aa", (self.Member.of("golem"),))
        self.save("a-much-longer-cluster-name", (self.Member.of("golem"), self.Member.of("poet")),
                  tags=AgentBuild(professions=("code",),
                                  specialties=("muxer", "cluster", "cluster-cowork")))
        rows = self.cluster_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({self.text(r).index("/tmp/project") for r in rows}), 1)

    def test_the_column_carries_the_forced_tags_and_the_member_count(self):
        self.save("team", (self.Member.of("golem"), self.Member.of("poet")),
                  tags=AgentBuild(specialties=("muxer", "cluster", "cluster-cowork")))
        (row,) = self.cluster_rows()
        text = self.text(row)
        self.assertIn("{cc}", text)
        self.assertIn("(2 members)", text)
        count_style = next(style for style, t in row.display if "members)" in t)
        self.assertEqual(count_style, menu_picker.STYLE_MEMBER_COUNT)
        self.assertLess(text.index("(2 members)"), text.index("team"))   # column, then name

    def test_a_cluster_row_gets_the_cwd_hints_instance_rows_have(self):
        # Launched from a neutral dir with the project AT the default
        # workspace: the yellow (DEFAULT DIR) — the hint the operator asked
        # cluster rows to get "like instances do".
        self.save("team", (self.Member.of("golem"),), project=DEFAULT_WORKSPACE)
        (row,) = self.cluster_rows(cwd=Path(DEFAULTING_DIRS[0]).resolve())
        self.assertIn(PickerCwdHint.DEFAULT.fragment, row.display)
        # The project as the cwd → (CURRENT DIR); a vanished project → (INVALID DIR).
        self.save("here", (self.Member.of("golem"),), project=self._tmp.name)
        self.save("gone", (self.Member.of("golem"),), project="/no/such/dir")
        rows = {r.value.session: r for r in self.cluster_rows(cwd=Path(self._tmp.name).resolve())}
        self.assertIn(PickerCwdHint.CURRENT.fragment, rows["here"].display)
        self.assertIn(PickerCwdHint.INVALID.fragment, rows["gone"].display)

    def test_member_rows_defer_their_previews_like_instance_rows(self):
        self.save("team", (self.Member.of("golem"),))
        (row,) = [e for e in self.entries()
                  if isinstance(e.value, menu_picker._MemberRow)]
        self.assertFalse(row.preview_ready)              # a callable: the transcript read waits for the first highlight
        self.assertIsNotNone(row.preview_quick)          # and the instant form stands in meanwhile
        self.assertIs(row.marker, menu_picker.PickerRowMarker.MEMBER)

    def test_member_rows_wear_their_own_ai_and_harness_after_the_name(self):
        # A member's AI and harness are its own (the cluster forces neither):
        # the row shows them right after the member id, before its own tags;
        # a cluster row shows none.
        self.save("team", (self.Member.of("golem"),
                           self.Member("researcher", "alien", build=AgentBuild(ai="grok"))))
        rows = {e.value.member_id: e for e in self.entries()
                if isinstance(e.value, menu_picker._MemberRow)}
        golem, alien = self.text(rows["golem"]), self.text(rows["researcher__alien"])
        default, grok = REGISTRY.default_ai, REGISTRY.ais["grok"]
        default_harness, grok_harness = REGISTRY.harnesses[default.harness], REGISTRY.harnesses[grok.harness]
        self.assertLess(golem.index("golem"), golem.index(default.label))
        self.assertLess(golem.index(default.label), golem.index(default_harness.label))
        self.assertIn((grok.style, grok.label), rows["researcher__alien"].display)
        self.assertIn((STYLE_TAG_HARNESS, grok_harness.label), rows["researcher__alien"].display)   # its AI's default harness, resolved
        self.assertNotIn(default.label, alien)
        self.assertNotIn(default_harness.label, alien)
        (cluster_row,) = self.cluster_rows()
        self.assertNotIn(default.label, self.text(cluster_row))
        self.assertNotIn(default_harness.label, self.text(cluster_row))

    def test_enter_is_inert_on_every_member_row_but_f2_and_del_are_not(self):
        # A member launches with its cluster: Enter must do nothing there —
        # not even explain itself — while the row stays the editing unit.
        # Both member kinds: a healthy one and one whose agent is gone.
        self.save("team", (self.Member.of("golem"), self.Member.of("nobody")))
        rows = [e for e in self.entries()
                if isinstance(e.value, menu_picker._MemberRow)]
        self.assertEqual(len(rows), 2)
        for row in rows:
            with self.subTest(member=row.value.member_id):
                self.assertFalse(row.pickable)
                self.assertTrue(row.selectable)
                self.assertTrue(row.deletable)

    def test_a_member_whose_agent_is_gone_stays_listed_in_red(self):
        # Never dropped: a member the listing hides is the silently-degraded
        # peer the launch refuses. Del still removes it; F2 has nothing to edit.
        self.save("team", (self.Member.of("golem"), self.Member.of("nobody")))
        rows = {e.value.member_id: e for e in self.entries()
                if isinstance(e.value, menu_picker._MemberRow)}
        self.assertEqual(set(rows), {"golem", "nobody"})
        ghost = rows["nobody"]
        self.assertIn("no agent 'nobody'", self.text(ghost))
        self.assertIn((menu_picker.STYLE_TAG_INVALID, "nobody"), ghost.display)
        self.assertTrue(ghost.selectable)
        self.assertTrue(ghost.deletable)
        self.assertFalse(ghost.modifiable)
        # The cluster pane names the fault too, and still lists the healthy member.
        (cluster_row,) = self.cluster_rows()
        self.assertIn("no agent 'nobody'", cluster_row.preview)
        self.assertIn("golem", cluster_row.preview)

    def test_every_template_row_precedes_every_cluster_row(self):
        # Templates together, then the clusters — a second template must land
        # after devteam, never after devteam's clusters, because a cluster is
        # not nested under the template it was created from.
        self.save("aa", (self.Member.of("golem"),))
        self.save("zz", (self.Member.of("golem"),))
        kinds = [type(e.value).__name__ for e in self.entries()
                 if isinstance(e.value, (menu_picker._ClusterTemplateRow,
                                         menu_picker._ClusterRow))]
        self.assertEqual(kinds, sorted(kinds, key=kinds.index))   # no interleaving...
        self.assertEqual(kinds, ["_ClusterTemplateRow"] * kinds.count("_ClusterTemplateRow")
                         + ["_ClusterRow"] * kinds.count("_ClusterRow"))

    def test_a_running_cluster_shows_the_running_hint_in_the_shared_anatomy(self):
        self.save("team", (self.Member.of("golem"),))
        (row,) = self.cluster_rows(running=frozenset({"cluster-team"}))
        self.assertIn(RUNNING_HINT, row.display)
        self.assertFalse(row.selectable)


if __name__ == "__main__":
    unittest.main()

class TestLegendScopes(unittest.TestCase):
    def test_the_legend_names_where_a_tag_may_not_go(self):
        # An author writing a .lego by hand reads the legend and the tree,
        # not the form (agent-writer, gate tag-scopes).
        import re
        text = re.sub(r"\x1b\[[0-9;]*m", "", menu_picker._build_composition_legend(REGISTRY))
        flat = re.sub(r"\s+", " ", text)
        self.assertIn("not on: member", flat)
        self.assertIn("not on: cluster, member", flat)   # {frwl}, in SCOPES order
        self.assertIn("not on: solo", flat)
