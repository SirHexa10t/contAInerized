"""Tests for launch.tag_form — the sectioned tag form's pure parts (option
assembly, radio grouping, attached_to ordering, requires cascade, warning
computation, prompt_tags post-processing) plus the shared display coercers.
Assembled against the real shipped agents/ tree. The prompt_toolkit
Application itself (checkbox_form) is interactive and stays out of unit
scope."""

import unittest
from unittest.mock import patch

from launch import tag_form
from launch.paths import AGENTS_DIR
from launch.tag_form import (
    STYLE_UNDERLINE, FormOption, _combo_warnings, _form_requires, _normalize,
    _plain, _tag_form_options, active_warnings, ordered_form_options,
    prompt_tags, requires_closure,
)
from launch.tags import AgentBuild, scan_all

REGISTRY = scan_all(AGENTS_DIR)


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




# ============================================================
# Checkbox form — pure assembly / ordering / cascade / warning logic
# ============================================================


class TestTagFormOptions(unittest.TestCase):
    """_tag_form_options — the pure assembly behind the tag form: a header
    row per kind, engines as a radio group at the top, then every
    profession/specialty/policy (keyed by full name), pre-checked from the
    given build, with requires parentheticals and short descriptions."""

    def test_every_kind_member_appears_as_selectable_row(self):
        keys = {o.key for o in _tag_form_options(REGISTRY, AgentBuild()) if not o.header}
        expected = (set(REGISTRY.engines) | set(REGISTRY.professions)
                    | set(REGISTRY.specialties) | set(REGISTRY.policies))
        self.assertEqual(keys, expected)

    def test_one_header_per_kind_in_order(self):
        headers = [o.key for o in _tag_form_options(REGISTRY, AgentBuild()) if o.header]
        self.assertEqual(headers, ["#engine", "#profession", "#specialty", "#policy"])

    def test_engine_section_leads_the_form(self):
        rows = _tag_form_options(REGISTRY, AgentBuild())
        self.assertEqual(rows[0].key, "#engine")
        self.assertTrue(rows[0].header)

    def test_engines_form_a_radio_group(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(engine="poet"))
        engine_rows = [o for o in rows if o.key in REGISTRY.engines]
        self.assertTrue(all(o.group == "engine" for o in engine_rows))
        self.assertEqual({o.key for o in engine_rows if o.checked}, {"poet"})

    def test_engines_ordered_by_model_then_output_budget(self):
        # Model first (fable → sonnet → haiku); among the fable tiers,
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS descending, then name. breakthrough +
        # researcher set 40000 (→ ahead, name-tiebroken); thinker sits at
        # 36000 (its premium bump over default); default inherits the 32000
        # default. golem (haiku) last; poet (sonnet) second-last.
        rows = _tag_form_options(REGISTRY, AgentBuild())
        engine_keys = [o.key for o in rows if o.key in REGISTRY.engines]
        self.assertEqual(
            engine_keys,
            ["breakthrough", "researcher", "thinker", "default", "poet", "golem"],
        )

    def test_non_engine_rows_are_not_grouped(self):
        rows = _tag_form_options(REGISTRY, AgentBuild())
        self.assertTrue(all(o.group is None for o in rows
                            if not o.header and o.key not in REGISTRY.engines))

    def test_build_prechecks_boxes(self):
        build = AgentBuild(professions=("code",), specialties=("auto",))
        checked = {o.key for o in _tag_form_options(REGISTRY, build) if o.checked}
        self.assertEqual(checked, {"code", "auto"})

    def test_nothing_prechecked_for_empty_build(self):
        self.assertFalse(any(o.checked for o in _tag_form_options(REGISTRY, AgentBuild())))

    def test_labels_show_short_description(self):
        fw = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "firewall")
        self.assertIn("outbound whitelist", _plain(fw.label))
        self.assertIn("<frwl>".replace("<", "{").replace(">", "}"), _plain(fw.label))
        # ...and the full description only in the focused-row body panel.
        self.assertNotIn(REGISTRY.specialties["firewall"].full_description.splitlines()[0],
                         _plain(fw.label))

    def test_body_leads_with_the_underlined_fullname(self):
        # The label shows an abbreviation ({frwl}, {dood}, (🧠)); focusing the
        # row must spell out what it stands for — underlined, then ": ".
        fw = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "firewall")
        self.assertEqual(fw.body[0], (STYLE_UNDERLINE, "firewall"))
        self.assertTrue(fw.body[1][1].startswith(": "))
        dood = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "dood")
        self.assertEqual(dood.body[0], (STYLE_UNDERLINE, "Docker-outside-of-Docker"))

    def test_policies_grouped_by_shortname_symbol(self):
        # `!` < `+` < `-` in ASCII — obligations, then grants, then denials.
        rows = _tag_form_options(REGISTRY, AgentBuild())
        policy_keys = [o.key for o in rows if o.key in REGISTRY.policies]
        shortnames = [REGISTRY.policies[k].shortname for k in policy_keys]
        self.assertEqual(shortnames, sorted(shortnames))
        self.assertEqual(shortnames[0][0], "!")   # obligations first
        self.assertTrue(all(s[0] == "+" for s in shortnames[1:3]))
        self.assertTrue(all(s[0] == "-" for s in shortnames[3:]))

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
        self.assertIn("<+qry>", labels["web-research"])   # policies render their shortname


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
        with patch.object(tag_form, "checkbox_form", return_value=form_result) as self.form:
            return prompt_tags(REGISTRY, current,
                               instance="poet__verse", workspace="/tmp/ws")

    def test_cancel_propagates_none(self):
        self.assertIsNone(self._run(None))

    def test_keys_split_into_axes(self):
        build = self._run(["code", "auto", "no-sudo"])
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("no-sudo",))

    def test_engine_preserved_from_current(self):
        self.assertEqual(self._run([]).engine, "poet")

    def test_picked_engine_overrides_current(self):
        self.assertEqual(self._run(["golem"]).engine, "golem")

    def test_preamble_names_instance_and_workspace(self):
        self._run([])
        preamble = self.form.call_args.kwargs["preamble"]
        self.assertEqual(preamble, ["# instance:  poet__verse",
                                    "# workspace: /tmp/ws"])

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
