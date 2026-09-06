"""Tests for launch.gui.forms — what each concrete form ASKS: the tag form's
sectioned rows, its cluster-wide sibling (no engines, locked pair), and the
merged preferences form's sections.

Split out of test_tag_form 2026-09-03; the machinery those forms run on is
tested in test_form_core.py."""

import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import forms
from launch.gui.forms import (
    _form_requires, _tag_form_options, _toolkit_form_options, prompt_tags,
)
from launch.gui.styles import STYLE_UNDERLINE, _plain
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, scan_all
from launch.tags.profession import ToolkitEntry

REGISTRY = scan_all(AGENTS_DIR)


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
        # The CONTRACT, asserted as invariants rather than as a literal list:
        # model family first (fable → opus → sonnet → haiku), then
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS descending within a family, then name.
        # Derived so that adding or deleting an engine — including a throwaway
        # probe tier — cannot break a test about ORDERING.
        families = ["fable", "opus", "sonnet", "haiku"]
        rows = _tag_form_options(REGISTRY, AgentBuild())
        engine_keys = [o.key for o in rows if o.key in REGISTRY.engines]
        self.assertGreater(len(engine_keys), 1)          # the ordering must have something to order

        def family_rank(key: str) -> int:
            model = REGISTRY.engines[key].conf_map.get("ANTHROPIC_MODEL", "")
            return next((i for i, f in enumerate(families) if f in model), len(families))

        def budget(key: str) -> int:
            return int(REGISTRY.engines[key].conf_map.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", 0))

        ranks = [family_rank(k) for k in engine_keys]
        self.assertEqual(ranks, sorted(ranks), "model families must not interleave")
        for rank in set(ranks):
            block = [k for k in engine_keys if family_rank(k) == rank]
            budgets = [budget(k) for k in block]
            self.assertEqual(budgets, sorted(budgets, reverse=True),
                             f"budgets must descend within the {families[rank]} block")
            for earlier, later in zip(block, block[1:]):
                if budget(earlier) == budget(later):
                    self.assertLess(earlier, later, "equal budgets tiebreak by name")

    def test_non_engine_rows_are_not_grouped(self):
        rows = _tag_form_options(REGISTRY, AgentBuild())
        self.assertTrue(all(o.group is None for o in rows
                            if not o.header and o.key not in REGISTRY.engines))

    def test_build_prechecks_boxes(self):
        # Locked always-on rows (<-su>) are checked regardless of the build.
        build = AgentBuild(professions=("code",), specialties=("auto",))
        checked = {o.key for o in _tag_form_options(REGISTRY, build) if o.checked and not o.locked}
        self.assertEqual(checked, {"code", "auto"})

    def test_nothing_prechecked_for_empty_build(self):
        # ...except the locked always-on rows, which are always checked.
        rows = _tag_form_options(REGISTRY, AgentBuild())
        self.assertFalse(any(o.checked for o in rows if not o.locked))
        self.assertEqual({o.key for o in rows if o.locked}, {"no-sudo"})

    def test_always_on_policy_row_is_locked_checked_and_marked(self):
        no_sudo = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "no-sudo")
        self.assertTrue(no_sudo.locked)
        self.assertTrue(no_sudo.checked)
        self.assertIn("(always-on)", _plain(no_sudo.label))

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
        # `!` < `+` < `-` in ASCII — demands, then grants, then denials. The
        # boundaries are derived rather than hardcoded so adding a policy to a
        # group cannot break the test while the GROUPING (the actual invariant)
        # still holds.
        rows = _tag_form_options(REGISTRY, AgentBuild())
        policy_keys = [o.key for o in rows if o.key in REGISTRY.policies]
        shortnames = [REGISTRY.policies[k].shortname for k in policy_keys]
        self.assertEqual(shortnames, sorted(shortnames))
        symbols = [s[0] for s in shortnames]
        self.assertEqual(symbols, sorted(symbols, key="!+-".index))   # never interleaved
        self.assertLessEqual({"!", "+", "-"}, set(symbols))           # all three stances present

    def test_requires_parenthetical_present(self):
        # webdev's tree position (profession/code/webdev) makes code a prerequisite;
        # the label must say so.
        webdev = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "webdev")
        self.assertIn("(requires: code)", _plain(webdev.label))

    def test_no_parenthetical_without_requires(self):
        code = next(o for o in _tag_form_options(REGISTRY, AgentBuild()) if o.key == "code")
        self.assertNotIn("requires", _plain(code.label))

    def test_labels_carry_kind_punctuation(self):
        labels = {o.key: _plain(o.label) for o in _tag_form_options(REGISTRY, AgentBuild())}
        self.assertIn("[code]", labels["code"])
        self.assertIn("{auto}", labels["auto"])
        self.assertIn("<+qry>", labels["web-research"])   # policies render their shortname


class TestPromptTags(unittest.TestCase):
    """prompt_tags' post-form processing: the flat key list splits back into
    axes (registry order) and the engine rides through untouched. The form
    itself is patched — its interactive behavior is out of unit scope."""

    def _run(self, form_result, current=AgentBuild(engine="poet")):
        with patch.object(forms, "checkbox_form", return_value=form_result) as self.form:
            return prompt_tags(REGISTRY, current,
                               instance="poet__verse", workspace="/tmp/ws")

    def test_cancel_propagates_none(self):
        self.assertIsNone(self._run(None))

    def test_keys_split_into_axes(self):
        build = self._run(["code", "auto", "web-research"])
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.specialties, ("auto",))
        self.assertEqual(build.policies, ("web-research",))

    def test_always_on_policy_never_lands_in_the_build(self):
        # <-su> is static: its locked row comes back checked from the form,
        # but it must not be persisted onto the instance.
        build = self._run(["code", "no-sudo", "web-research"])
        self.assertEqual(build.policies, ("web-research",))

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


class TestFormRequires(unittest.TestCase):
    def test_only_tags_with_requires_present(self):
        req = _form_requires(REGISTRY)
        self.assertIn("webdev", req)       # tree-nested under code
        self.assertNotIn("code", req)      # top-level profession — no requires

    def test_dood_layer_requires_code(self):
        # dood's `_dood` image layer lives under profession/code/ — the
        # specialty inherits the code requirement from its claimed layer.
        self.assertEqual(_form_requires(REGISTRY).get("dood"), frozenset({"code"}))


class TestToolkitFormOptions(unittest.TestCase):
    """_toolkit_form_options — the pure assembly behind the "(Edit Preferences)"
    menu: one row per manifest entry, key-sorted; toggleable rows checked from
    the current profile (falling back to the entry's own default for a key the
    profile doesn't mention); locked rows grayed + fixed to their default;
    each row's flavor (run command + language type) in its body; `—`
    separators aligned into columns."""

    ENTRIES = {
        "python": ToolkitEntry(key="python", description="Python 3", run_command="python3", language="interpreted", default=True, locked=True),
        "rust":   ToolkitEntry(key="rust",   description="Rust toolchain", run_command="cargo", language="compiled", approx_size_mb=613, default=True,  build_arg="INSTALL_RUST"),
        "cmake":  ToolkitEntry(key="cmake",  description="CMake", run_command="cmake", language="build-system", approx_size_mb=66, default=False, build_arg="INSTALL_CMAKE"),
    }

    def test_one_row_per_entry_key_sorted(self):
        options = _toolkit_form_options(self.ENTRIES, {})
        self.assertEqual([o.key for o in options], ["cmake", "python", "rust"])

    def test_toggleable_checked_from_profile(self):
        options = {o.key: o for o in _toolkit_form_options(self.ENTRIES, {"rust": False, "cmake": True})}
        self.assertFalse(options["rust"].checked)
        self.assertTrue(options["cmake"].checked)

    def test_missing_profile_key_falls_back_to_entry_default(self):
        options = {o.key: o for o in _toolkit_form_options(self.ENTRIES, {})}
        self.assertTrue(options["rust"].checked)     # default True
        self.assertFalse(options["cmake"].checked)   # default False

    def test_locked_row_is_grayed_and_fixed(self):
        # Python: locked=True → the row is flagged locked (grayed, inert to
        # Space) and shows its fixed default, ignoring any profile value.
        (python,) = [o for o in _toolkit_form_options(self.ENTRIES, {"python": False}) if o.key == "python"]
        self.assertTrue(python.locked)
        self.assertTrue(python.checked)   # default True wins over the profile's False

    def test_locked_row_shows_included_not_a_size(self):
        (python,) = [o for o in _toolkit_form_options(self.ENTRIES, {}) if o.key == "python"]
        self.assertIn("included", "".join(t for _, t in python.label))

    def test_label_carries_size_and_description(self):
        (rust,) = [o for o in _toolkit_form_options(self.ENTRIES, {}) if o.key == "rust"]
        text = "".join(t for _, t in rust.label)
        self.assertIn("~613MB", text)
        self.assertIn("Rust toolchain", text)

    def test_body_carries_run_command_and_language(self):
        (rust,) = [o for o in _toolkit_form_options(self.ENTRIES, {}) if o.key == "rust"]
        body = "".join(t for _, t in rust.body)
        self.assertIn("cargo", body)
        self.assertIn("compiled", body)

    def test_dash_separators_align_across_rows(self):
        # Key + size columns are padded, so both `—` separators sit at the
        # same index in every label — the form reads as a table.
        labels = ["".join(t for _, t in o.label) for o in _toolkit_form_options(self.ENTRIES, {})]
        first = {label.index("—") for label in labels}
        second = {label.rindex("—") for label in labels}
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)


class TestProfilesFormAssembly(unittest.TestCase):
    """The merged preferences form (the "middle-handler"): any number of
    profile sections concatenated into ONE checkbox form — the first
    section's title is the form title, every later section a header row —
    each saving to its own file on confirm, none on Esc."""

    def _captured(self):
        captured = {}

        def fake_form(title, options, **kwargs):
            captured["title"] = title
            captured["options"] = options
            captured.update(kwargs)
            return None   # cancel — nothing persisted

        with patch("launch.gui.forms.toolkit_profile_path",
                   return_value=Path("/nonexistent/code_profile.toml")), \
             patch("launch.gui.forms.ui_profile_path",
                   return_value=Path("/nonexistent/ui_profile.toml")), \
             patch("launch.gui.forms.checkbox_form", side_effect=fake_form):
            forms.edit_profiles_menu(scan_all(AGENTS_DIR))
        return captured

    def test_size_note_passed_as_preamble(self):
        # The size disclaimer belongs to the toolkit rows and rides along
        # whenever a toolkit section is present.
        self.assertIn(forms.TOOLKIT_SIZE_NOTE,
                      self._captured().get("preamble", []))

    def test_first_section_titles_the_form_and_later_ones_become_headers(self):
        captured = self._captured()
        code = scan_all(AGENTS_DIR).professions["code"]
        self.assertEqual(captured["title"],
                         f"Edit {code.label} toolkit  (Space to toggle):")
        headers = ["".join(text for _, text in option.label)
                   for option in captured["options"] if option.header]
        self.assertIn("Edit UI configs  (Space to toggle):", headers)

    def test_three_newlines_separate_sections(self):
        # The next section's title lands three newlines after the previous
        # section's last row — two blank header rows, then the title row
        # (operator's spec, 2026-08-30). Blanks are headers, so navigation
        # skips straight across the gap.
        options = self._captured()["options"]

        def text(option):
            return option.label if isinstance(option.label, str) \
                else "".join(part for _, part in option.label)

        ui_at = next(index for index, option in enumerate(options)
                     if option.header and "Edit UI configs" in text(option))
        for blank in (options[ui_at - 1], options[ui_at - 2]):
            self.assertTrue(blank.header)
            self.assertEqual(text(blank), "")
        self.assertFalse(options[ui_at - 3].header)   # the toolkit's last row

    def test_the_muxer_toggle_rides_the_ui_section_checked_by_default(self):
        # The UI section is ALWAYS present (profession-independent), its keys
        # namespaced per section so profile files may reuse a name; with no
        # profile on disk the manifest default (herdr) shows checked.
        captured = self._captured()
        (muxer,) = [option for option in captured["options"]
                    if option.key.endswith(":herdr_instead_of_tmux")]
        self.assertTrue(muxer.checked)
        self.assertIn("tmux", "".join(text for _, text in muxer.body))


class TestClusterTagForm(unittest.TestCase):
    """prompt_cluster_tags — the cluster-wide tag step (2026-09-02). Three
    departures from the instance form, each asked for: no engines, locked
    rows for what makes a cluster a cluster, and a preamble that SAYS the
    selection is forced on every member (a tag list cannot imply that)."""

    def _captured(self, current=None, locked=frozenset({"muxer", "cluster"}),
                  result=None):
        captured = {}

        def fake_form(title, options, **kwargs):
            captured["title"] = title
            captured["options"] = options
            captured.update(kwargs)
            return result

        with patch("launch.gui.forms.checkbox_form", side_effect=fake_form):
            build = forms.prompt_cluster_tags(
                REGISTRY, current or AgentBuild(specialties=tuple(locked)),
                session="team", locked=locked)
        return build, captured

    def test_no_engine_section_at_all(self):
        # A thinking budget is per member; offering one cluster-wide would
        # silently override every member's own engine.
        _, captured = self._captured()
        keys = {option.key for option in captured["options"]}
        self.assertTrue(keys.isdisjoint(set(REGISTRY.engines)))
        self.assertNotIn("#engine", keys)

    def test_the_locked_pair_is_checked_and_inert(self):
        _, captured = self._captured()
        rows = {option.key: option for option in captured["options"]}
        for name in ("muxer", "cluster"):
            with self.subTest(tag=name):
                self.assertTrue(rows[name].checked)
                self.assertTrue(rows[name].locked)
        # ...while an ordinary row stays freely toggleable.
        self.assertFalse(rows["cluster-cowork"].locked)

    def test_the_preamble_states_that_every_member_is_forced(self):
        _, captured = self._captured()
        text = " ".join(captured["preamble"])
        self.assertIn("EVERY member", text)
        self.assertIn("FORCED", text)
        self.assertIn("F2", text)          # where per-member tags still live

    def test_the_result_carries_the_picked_tags_and_never_an_engine(self):
        build, _ = self._captured(
            result=["muxer", "cluster", "cluster-cowork", "code", "free-bash"])
        self.assertIsNone(build.engine)
        self.assertEqual(build.professions, ("code",))
        self.assertIn("cluster-cowork", build.specialties)
        self.assertEqual(build.policies, ("free-bash",))

    def test_esc_returns_none(self):
        build, _ = self._captured(result=None)
        self.assertIsNone(build)

