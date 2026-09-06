"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
tag display), and the shared session prompt. The tag form's tests live in
test_form_core.py; the prompt_toolkit Applications themselves are
interactive and stay out of unit scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import menu_picker, picker_widget
from launch.gui.menu_picker import (
    RUNNING_HINT, STYLE_RUNNING_NAME, continuable_instances,
)
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
        self.assertTrue(entries[0].is_current_dir)
        self.assertFalse(entries[0].is_invalid_dir)

    def test_invalid_workspace_flagged_but_shown(self):
        entries = self._entries([make_inst("golem", "a", "/no/such/dir")])
        self.assertTrue(entries[0].is_invalid_dir)
        self.assertFalse(entries[0].is_current_dir)
        self.assertEqual(entries[0].workspace_display, "/no/such/dir")   # stored value still shown

    def test_missing_workspace_shows_placeholder(self):
        entries = self._entries([make_inst("golem", "a", None)])
        self.assertEqual(entries[0].workspace_display, "?")
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
        # tags · name · workspace — but never "(RUNNING)": in this list it
        # would say nothing.
        insts = [make_inst("golem", "up", self.ws, specialties=["auto"])]
        _, captured = self._run(insts, running={"golem__up"})
        row = self._row_text(captured["options"][0])
        self.assertIn("golem__up", row)
        self.assertIn(self.ws, row)
        self.assertNotIn("(RUNNING)", row)

    def test_muxer_is_emphasized_other_tags_are_not(self):
        insts = [make_inst("golem", "up", self.ws,
                           specialties=["auto", "muxer"])]
        _, captured = self._run(insts, running={"golem__up"})
        styles = {text: style for style, text in captured["options"][0].label}
        self.assertIn(picker_widget.TAG_EMPHASIS, styles["{mux}"])
        self.assertNotIn(picker_widget.TAG_EMPHASIS, styles["{auto}"])

    def test_running_clusters_get_a_row_keyed_by_container_id(self):
        from types import SimpleNamespace
        cluster = SimpleNamespace(session="team", members=[1, 2],
                                  project=Path("/proj"), ids=("a", "b"))
        _, captured = self._run([], running={"cluster-team"},
                                clusters=[cluster])
        (row,) = captured["options"]
        self.assertEqual(row.key, "cluster-team")
        self.assertIn("team", self._row_text(row))
        self.assertIn("/proj", self._row_text(row))

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


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
