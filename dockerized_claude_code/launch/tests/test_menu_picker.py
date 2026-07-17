"""Tests for launch.menu_picker's non-TUI logic: the pure display helpers,
the Cont-row factory (continuable_instances — sorting, cwd-relation flags,
tag display), the shared session prompt, and the checkbox form's pure parts
(option assembly, attached_to ordering, requires cascade, live-warning
computation — all against the real shipped agents/ tree).
The prompt_toolkit Applications themselves (pick_with_preview,
checkbox_form) are interactive and stay out of unit scope."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import menu_picker
from launch.menu_picker import (
    FormOption, _agent_description, _combo_warnings, _form_requires,
    _normalize, _plain, _tag_form_options, _tags_column, active_warnings,
    build_of, continuable_instances, ordered_form_options, prompt_session,
    prompt_tags, requires_closure,
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


class TestAgentDescription(unittest.TestCase):
    def test_plain_first_line(self):
        self.assertEqual(_agent_description("A fast simpleton.\nMore text."), "A fast simpleton.")

    def test_heading_marker_stripped(self):
        self.assertEqual(_agent_description("# Poet\nbody"), "Poet")

    def test_deep_heading_marker_stripped(self):
        self.assertEqual(_agent_description("### Deep heading"), "Deep heading")

    def test_empty_md_yields_empty_string(self):
        # Regression: splitlines()[0] raised IndexError on a zero-byte agent
        # .md, crashing the picker before it could even render.
        self.assertEqual(_agent_description(""), "")

    def test_whitespace_only_md_yields_empty_string(self):
        self.assertEqual(_agent_description("   \n\n"), "")


class TestDisplayCoercion(unittest.TestCase):
    """_normalize/_plain back the picker's filter matching — every accepted
    display shape must round-trip to comparable plain text."""

    def test_normalize_plain_string(self):
        self.assertEqual(_normalize("hello"), [("", "hello")])

    def test_normalize_fragment_list_passthrough(self):
        frags = [("bold", "a"), ("", "b")]
        self.assertEqual(_normalize(frags), frags)

    def test_plain_joins_fragment_text(self):
        self.assertEqual(_plain([("bold", "a"), ("", "b")]), "ab")

    def test_plain_of_string(self):
        self.assertEqual(_plain("hello"), "hello")


class TestTagsColumn(unittest.TestCase):
    """_tags_column renders tag labels as warn-aware pt fragments — the one
    rendering source for both Create-row and Cont-row tag columns."""

    def test_empty_input(self):
        self.assertEqual(_tags_column([]), ([], 0))

    def test_safe_tag_renders_green(self):
        fragments, width = _tags_column([REGISTRY.professions["code"]])
        styles = {style for style, _ in fragments}
        self.assertIn(menu_picker.STYLE_TAG_SAFE, styles)
        self.assertIn("[code]", "".join(text for _, text in fragments))
        self.assertEqual(width, len("[code]") + 1)   # +1 trailing separator space

    def test_warn_specialty_renders_red(self):
        fragments, _ = _tags_column([REGISTRY.specialties["dood"]])
        self.assertIn(menu_picker.STYLE_TAG_WARN, {style for style, _ in fragments})

    def test_multiple_tags_space_separated(self):
        fragments, width = _tags_column([REGISTRY.professions["code"],
                                         REGISTRY.specialties["auto"]])
        text = "".join(t for _, t in fragments)
        self.assertEqual(text, "[code] {auto} ")
        self.assertEqual(width, len(text))


class TestBuildOf(unittest.TestCase):
    def test_round_trips_axis_names(self):
        inst = make_inst(professions=["code", "web"], specialties=["auto"],
                         policies=["no-sudo"])
        build = build_of(inst)
        self.assertEqual(build.professions, ("code", "web"))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("no-sudo",))

    def test_engine_name_captured(self):
        self.assertEqual(build_of(make_inst(agent="poet")).engine, "poet")


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

    def _entries(self, insts, cwd=None):
        by_id = {i.instance: i for i in insts}
        with patch.object(menu_picker, "list_all_instances", return_value=list(by_id) + ["ghost__x"]), \
             patch.object(menu_picker, "instance_from_store",
                          side_effect=lambda name, registry: by_id.get(name)), \
             patch.object(menu_picker, "resolved_cwd", return_value=cwd or Path("/nowhere")):
            return continuable_instances(REGISTRY)

    def test_orphan_instances_skipped(self):
        # instance_from_store returns None for ghost__x (no .md) — row dropped.
        entries = self._entries([make_inst("golem", "a", self.ws)])
        self.assertEqual([e.identity.instance for e in entries], ["golem__a"])

    def test_tags_display_names(self):
        entries = self._entries([make_inst("golem", "a", self.ws, specialties=["auto"])])
        self.assertEqual(entries[0].tags_display, "auto")

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


class TestPromptSession(unittest.TestCase):
    """One shared collision loop for both flows: create (default = workspace
    basename) and modify (default = current name, which is always accepted)."""

    def _run(self, answers, existing=(), **kwargs):
        answer_iter = iter(answers)
        with patch("builtins.input", side_effect=lambda _prompt: next(answer_iter)), \
             patch.object(menu_picker, "path_exists",
                          side_effect=lambda p: any(str(p).endswith(f"golem__{e}") for e in existing)), \
             patch("builtins.print"):
            return prompt_session("golem", "/some/workspace/myproj", **kwargs)

    def test_enter_accepts_workspace_basename_default(self):
        self.assertEqual(self._run([""]), "myproj")

    def test_collision_reprompts_until_free(self):
        self.assertEqual(self._run(["taken", "free"], existing=["taken"]), "free")

    def test_modify_flow_defaults_to_current_name(self):
        # Enter keeps the existing session even though its state dir exists.
        self.assertEqual(self._run([""], existing=["mysess"], current="mysess"), "mysess")

    def test_modify_flow_rejects_other_existing_names(self):
        self.assertEqual(self._run(["other", "free"], existing=["mysess", "other"], current="mysess"), "free")

    def test_rename_to_fresh_name_accepted(self):
        self.assertEqual(self._run(["newname"], existing=["mysess"], current="mysess"), "newname")


# ============================================================
# Checkbox form — pure assembly / ordering / cascade / warning logic
# ============================================================


class TestTagFormOptions(unittest.TestCase):
    """_tag_form_options — the pure assembly behind the tag form: every
    discovered profession/specialty/policy appears (keyed by full name),
    pre-checked from the given build, with requires parentheticals."""

    def test_every_form_kind_member_appears(self):
        keys = {o.key for o in _tag_form_options(REGISTRY, AgentBuild())}
        expected = (set(REGISTRY.professions) | set(REGISTRY.specialties)
                    | set(REGISTRY.policies))
        self.assertEqual(keys, expected)

    def test_engines_not_in_form(self):
        keys = {o.key for o in _tag_form_options(REGISTRY, AgentBuild())}
        self.assertFalse(keys & set(REGISTRY.engines))

    def test_build_prechecks_boxes(self):
        build = AgentBuild(professions=("code",), specialties=("auto",))
        checked = {o.key for o in _tag_form_options(REGISTRY, build) if o.checked}
        self.assertEqual(checked, {"code", "auto"})

    def test_nothing_prechecked_for_empty_build(self):
        self.assertFalse(any(o.checked for o in _tag_form_options(REGISTRY, AgentBuild())))

    def test_requires_parenthetical_present(self):
        # web's tree position (profession/code/web) makes code a prerequisite;
        # the label must say so.
        web = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "web")
        self.assertIn("(requires: code)", _plain(web.label))

    def test_no_parenthetical_without_requires(self):
        code = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "code")
        self.assertNotIn("requires", _plain(code.label))

    def test_labels_carry_kind_punctuation(self):
        labels = {o.key: _plain(o.label) for o in _tag_form_options(REGISTRY, AgentBuild())}
        self.assertIn("[code]", labels["code"])
        self.assertIn("{auto}", labels["auto"])
        self.assertIn("<+query>", labels["web-research"])   # policies render their shortname


class TestRequiresClosure(unittest.TestCase):
    def test_no_requires_yields_empty(self):
        self.assertEqual(requires_closure("code", {}), set())

    def test_direct_requirement(self):
        self.assertEqual(requires_closure("web", {"web": frozenset({"code"})}), {"code"})

    def test_transitive_requirement(self):
        req = {"c": frozenset({"b"}), "b": frozenset({"a"})}
        self.assertEqual(requires_closure("c", req), {"a", "b"})

    def test_self_not_included(self):
        self.assertNotIn("web", requires_closure("web", {"web": frozenset({"code"})}))

    def test_cycle_terminates(self):
        req = {"a": frozenset({"b"}), "b": frozenset({"a"})}
        self.assertEqual(requires_closure("a", req), {"a", "b"})

    def test_real_tree_web_requires_code(self):
        self.assertEqual(requires_closure("web", _form_requires(REGISTRY)), {"code"})


class TestOrderedFormOptions(unittest.TestCase):
    """ordered_form_options — the attached_to proximity layout: attached
    options tuck directly beneath their anchor; no dependency semantics."""

    @staticmethod
    def _opt(key: str, attached_to: str | None = None) -> FormOption:
        return FormOption(key=key, label=key, attached_to=attached_to)

    def test_anchor_order_preserved_without_attachments(self):
        out = ordered_form_options([self._opt("a"), self._opt("b"), self._opt("c")])
        self.assertEqual([o.key for o in out], ["a", "b", "c"])

    def test_attached_tucks_directly_after_anchor(self):
        # The future firewall⇄auto shape: firewall declared last still
        # renders right beneath auto.
        out = ordered_form_options([self._opt("auto"), self._opt("dood"),
                                    self._opt("firewall", attached_to="auto")])
        self.assertEqual([o.key for o in out], ["auto", "firewall", "dood"])

    def test_multiple_attachments_keep_relative_order(self):
        out = ordered_form_options([self._opt("auto"),
                                    self._opt("f1", attached_to="auto"),
                                    self._opt("f2", attached_to="auto")])
        self.assertEqual([o.key for o in out], ["auto", "f1", "f2"])

    def test_unknown_anchor_appends_at_end(self):
        out = ordered_form_options([self._opt("a"), self._opt("x", attached_to="ghost")])
        self.assertEqual([o.key for o in out], ["a", "x"])


class TestActiveWarnings(unittest.TestCase):
    """active_warnings against the real combos.info copy (re-keyed by tag
    name via _combo_warnings) — the form's live warning zone."""

    def setUp(self):
        self.warnings = _combo_warnings(REGISTRY)

    def test_dood_plus_auto_combo_shipped(self):
        self.assertIn(frozenset({"dood", "auto"}), self.warnings)

    def test_auto_plus_dood_fires(self):
        self.assertEqual(len(active_warnings({"auto", "dood"}, self.warnings)), 1)

    def test_superset_still_fires(self):
        self.assertEqual(len(active_warnings({"auto", "dood", "web"}, self.warnings)), 1)

    def test_singles_dont_fire(self):
        self.assertEqual(active_warnings({"auto"}, self.warnings), [])
        self.assertEqual(active_warnings({"dood"}, self.warnings), [])

    def test_empty_selection_no_warnings(self):
        self.assertEqual(active_warnings(set(), self.warnings), [])


class TestPromptTags(unittest.TestCase):
    """prompt_tags' post-form processing: the flat key list splits back into
    axes (registry order) and the engine rides through untouched. The form
    itself is patched — its interactive behavior is out of unit scope."""

    def _run(self, form_result, current=AgentBuild(engine="poet")):
        with patch.object(menu_picker, "checkbox_form", return_value=form_result):
            return prompt_tags(REGISTRY, current)

    def test_cancel_propagates_none(self):
        self.assertIsNone(self._run(None))

    def test_keys_split_into_axes(self):
        build = self._run(["code", "auto", "no-sudo"])
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("no-sudo",))

    def test_engine_preserved_from_current(self):
        self.assertEqual(self._run([]).engine, "poet")

    def test_empty_selection_yields_bare_build(self):
        build = self._run([])
        self.assertEqual((build.professions, build.specialties, build.policies),
                         ((), (), ()))


class TestCascadeInForm(unittest.TestCase):
    """The check-cascade wiring: simulate what the form's Space handler does
    (toggle + cascade) using the pure pieces, against the real tree's
    web→code edge."""

    def test_checking_dependent_checks_requirement(self):
        req = _form_requires(REGISTRY)
        checked = {"web"} | requires_closure("web", req)
        self.assertIn("code", checked)

    def test_unchecking_requirement_identifies_dependents(self):
        req = _form_requires(REGISTRY)
        dependents = {k for k in ("web",) if "code" in requires_closure(k, req)}
        self.assertEqual(dependents, {"web"})


class TestFormRequires(unittest.TestCase):
    def test_only_tags_with_requires_present(self):
        req = _form_requires(REGISTRY)
        self.assertIn("web", req)          # tree-nested under code
        self.assertNotIn("code", req)      # top-level profession — no requires

    def test_dood_layer_requires_code(self):
        # dood's `_dood` image layer lives under profession/code/ — the
        # specialty inherits the code requirement from its claimed layer.
        self.assertEqual(_form_requires(REGISTRY).get("dood"), frozenset({"code"}))


if __name__ == "__main__":
    unittest.main()
