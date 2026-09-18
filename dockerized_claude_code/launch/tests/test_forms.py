"""Tests for launch.gui.forms — what each concrete form ASKS: the tag form's
sectioned rows, its cluster-wide sibling (no engines, locked pair), and the
merged preferences form's sections.

Split out of test_tag_form 2026-09-03; the machinery those forms run on is
tested in test_form_core.py."""

import dataclasses
import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import forms
from launch.gui.forms import (
    _form_requires, _harness_warnings, _pairing_warnings, _tag_form_options, _tag_row,
    _toolkit_form_options, prompt_tags,
)
from launch.gui.form_core import active_warnings
from launch.gui.styles import STYLE_UNDERLINE, _plain
from launch.paths import AGENTS_DIR
from launch.tags import AgentBuild, Budget, is_standard, scan_all
from launch.tags.profession import ToolkitEntry

REGISTRY = scan_all(AGENTS_DIR)


class TestTagFormOptions(unittest.TestCase):
    """_tag_form_options — the pure assembly behind the tag form: a header
    row per kind, the AI then the engines as radio groups at the top, then
    every profession/specialty/policy (keyed by full name), pre-checked from
    the given build, with requires parentheticals and short descriptions."""

    def test_every_kind_member_appears_as_selectable_row(self):
        keys = {o.key for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if not o.header}
        expected = (set(REGISTRY.ais) | set(REGISTRY.harnesses) | set(REGISTRY.engines) | set(REGISTRY.professions)
                    | set(REGISTRY.specialties) | set(REGISTRY.policies))
        self.assertEqual(keys, expected)

    def test_one_header_per_kind_in_order(self):
        headers = [o.key for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.header]
        self.assertEqual(headers, ["#ai", "#harness", "#engine", "#profession", "#specialty", "#policy"])

    def test_the_ai_section_leads_the_form_then_the_harness_then_the_engines(self):
        # The AI decides what every engine standard below it means, so it is
        # asked first; the harness — the CLI around it — second; between the
        # headers sit exactly each kind's members.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        self.assertEqual(rows[0].key, "#ai")
        self.assertTrue(rows[0].header)
        harness_header = next(i for i, o in enumerate(rows) if o.key == "#harness")
        engine_header = next(i for i, o in enumerate(rows) if o.key == "#engine")
        self.assertEqual({o.key for o in rows[1:harness_header]}, set(REGISTRY.ais))
        self.assertEqual({o.key for o in rows[harness_header + 1:engine_header]}, set(REGISTRY.harnesses))

    def test_harnesses_form_a_radio_group_dotted_from_the_build(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(ai="gemini", harness="gemini-cli"), scope="solo")
        harness_rows = [o for o in rows if o.key in REGISTRY.harnesses]
        self.assertTrue(all(o.group == "harness" for o in harness_rows))
        self.assertEqual({o.key for o in harness_rows if o.checked}, {"gemini-cli"})

    def test_the_ais_default_harness_is_dotted_when_the_build_names_none(self):
        for build, expected in ((AgentBuild(), REGISTRY.default_ai.harness), (AgentBuild(ai="grok"), "grok-build")):
            rows = _tag_form_options(REGISTRY, build, scope="solo")
            self.assertEqual({o.key for o in rows if o.key in REGISTRY.harnesses and o.checked}, {expected})

    def test_harness_rows_say_which_ai_they_run(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        for option in (o for o in rows if o.key in REGISTRY.harnesses):
            with self.subTest(harness=option.key):
                note = "runs " + " ".join(REGISTRY.ais[a].label for a in REGISTRY.harnesses[option.key].ais)
                self.assertIn(note, _plain(option.label))

    def test_ais_form_a_radio_group_dotted_from_the_build(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(ai="gemini"), scope="solo")
        ai_rows = [o for o in rows if o.key in REGISTRY.ais]
        self.assertTrue(all(o.group == "ai" for o in ai_rows))
        self.assertEqual({o.key for o in ai_rows if o.checked}, {"gemini"})

    def test_the_default_ai_is_dotted_when_the_build_names_none(self):
        # `.lego` / instances.toml leave `ai` unset for "the default"; the form
        # shows what that resolves to rather than an undotted radio group.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        self.assertEqual({o.key for o in rows if o.key in REGISTRY.ais and o.checked},
                         {REGISTRY.default_ai.name})

    def test_ai_rows_lead_with_the_default_then_go_by_name(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        ai_keys = [o.key for o in rows if o.key in REGISTRY.ais]
        self.assertEqual(ai_keys[0], REGISTRY.default_ai.name)
        self.assertEqual(ai_keys[1:], sorted(ai_keys[1:]))

    def test_ai_rows_wear_their_own_logo_colours(self):
        for option in (o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key in REGISTRY.ais):
            with self.subTest(ai=option.key):
                self.assertIn(REGISTRY.ais[option.key].style, [style for style, _ in option.label])

    def test_engines_form_a_radio_group(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(engine="poet"), scope="solo")
        engine_rows = [o for o in rows if o.key in REGISTRY.engines]
        self.assertTrue(all(o.group == "engine" for o in engine_rows))
        self.assertEqual({o.key for o in engine_rows if o.checked}, {"poet"})

    def test_engines_ordered_by_standard_then_output_budget(self):
        # The CONTRACT, asserted as invariants rather than as a literal list:
        # capability standard first (strongest first — AI-neutral), then
        # max_output_tokens descending within a standard, then name. Derived so
        # that adding or deleting an engine — including a throwaway probe
        # tier — cannot break a test about ORDERING.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        engine_keys = [o.key for o in rows if o.key in REGISTRY.engines]
        self.assertGreater(len(engine_keys), 1)          # the ordering must have something to order

        def rank(key: str) -> int:
            return -REGISTRY.engines[key].budget.rank

        def budget(key: str) -> int:
            return REGISTRY.engines[key].budget.max_output_tokens or 0

        ranks = [rank(k) for k in engine_keys]
        self.assertEqual(ranks, sorted(ranks), "standards must not interleave")
        for r in set(ranks):
            block = [k for k in engine_keys if rank(k) == r]
            budgets = [budget(k) for k in block]
            self.assertEqual(budgets, sorted(budgets, reverse=True), "budgets must descend within a standard")
            for earlier, later in zip(block, block[1:]):
                if budget(earlier) == budget(later):
                    self.assertLess(earlier, later, "equal budgets tiebreak by name")

    def test_engine_rows_show_their_effort_tier(self):
        # The tier's words live in tag.info and name neither model nor tier;
        # the effort_tier shown beside them is the engine's own budget value —
        # a dated quarter, or one of the two ends.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        for option in (o for o in rows if o.key in REGISTRY.engines):
            with self.subTest(engine=option.key):
                effort_tier = REGISTRY.engines[option.key].budget.effort_tier
                label = "".join(text for _, text in option.label)
                self.assertTrue(is_standard(effort_tier))    # a quarter, `cheapest`, or `best`
                self.assertTrue(label.rstrip().endswith(f" {effort_tier}"), label)

    def test_the_form_names_no_model_and_no_effort(self):
        # THE point of the change (operator, 2026-09-17): the form chooses an
        # engine, so it shows the engine's own word. Which model answers that
        # standard, and at what effort, is the AI's business — the F8 legend,
        # the preview and the banner are where the model belongs, and they
        # still show it.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        labels = " ".join(text for o in rows for _, text in o.label)
        for ai in REGISTRY.ais.values():
            for standard, tier in ai.tiers:
                with self.subTest(ai=ai.name, standard=standard):
                    self.assertNotIn(tier.model, labels)
        # "effort: <word>" was the shape this rendered for one day; the word
        # itself is legal prose in an engine's own description (the default
        # engine's reads "baseline max-effort"), so the pin is the shape.
        engine_labels = " ".join(text for o in rows if o.key in REGISTRY.engines for _, text in o.label)
        self.assertNotIn("effort: ", engine_labels)

    def test_an_engine_without_a_standard_shows_nothing_extra(self):
        # A nesting-only engine inherits its parent's standard at scan time; one
        # whose effective budget still has none must render nothing after its
        # description — not "None", not an empty parenthesis.
        bare = dataclasses.replace(REGISTRY.engines["quick"], budget=Budget())
        label = "".join(text for _, text in _tag_row(bare, checked=False, group="engine").label)
        self.assertEqual(label.rstrip(), f"{bare.label} {bare.short_description}")

    def test_non_radio_rows_are_not_grouped(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        radios = set(REGISTRY.ais) | set(REGISTRY.harnesses) | set(REGISTRY.engines)
        self.assertTrue(all(o.group is None for o in rows if not o.header and o.key not in radios))

    def test_build_prechecks_boxes(self):
        # Locked always-on rows (<-su>) are checked regardless of the build;
        # the AI and harness radios always show one dot each (here the
        # default AI's and its harness's).
        build = AgentBuild(professions=("code",), specialties=("auto",))
        checked = {o.key for o in _tag_form_options(REGISTRY, build, scope="solo") if o.checked and not o.locked}
        self.assertEqual(checked, {"code", "auto", REGISTRY.default_ai.name, REGISTRY.default_ai.harness})

    def test_nothing_prechecked_for_empty_build(self):
        # ...except the locked always-on rows, which are always checked, and
        # the AI and harness radios, which always have a dot (tested above).
        # Locked but UNCHECKED: the tags a solo build cannot carry ({clstr},
        # {cc} — cluster creation applies them), greyed with their note.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        radios = set(REGISTRY.ais) | set(REGISTRY.harnesses)
        self.assertFalse(any(o.checked for o in rows if not o.locked and o.key not in radios))
        self.assertEqual({o.key for o in rows if o.locked}, {"no-sudo", "cluster", "cluster-cowork"})
        self.assertEqual({o.key for o in rows if o.locked and o.checked}, {"no-sudo"})

    def test_always_on_policy_row_is_locked_checked_and_marked(self):
        no_sudo = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "no-sudo")
        self.assertTrue(no_sudo.locked)
        self.assertTrue(no_sudo.checked)
        self.assertIn("(always-on)", _plain(no_sudo.label))

    def test_labels_show_short_description(self):
        fw = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "firewall")
        self.assertIn("outbound whitelist", _plain(fw.label))
        self.assertIn("<frwl>".replace("<", "{").replace(">", "}"), _plain(fw.label))
        # ...and the full description only in the focused-row body panel.
        self.assertNotIn(REGISTRY.specialties["firewall"].full_description.splitlines()[0],
                         _plain(fw.label))

    def test_body_leads_with_the_underlined_fullname(self):
        # The label shows an abbreviation ({frwl}, {dood}, (🧠)); focusing the
        # row must spell out what it stands for — underlined, then ": ".
        fw = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "firewall")
        self.assertEqual(fw.body[0], (STYLE_UNDERLINE, "firewall"))
        self.assertTrue(fw.body[1][1].startswith(": "))
        dood = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "dood")
        self.assertEqual(dood.body[0], (STYLE_UNDERLINE, "Docker-outside-of-Docker"))

    def test_policies_grouped_by_shortname_symbol(self):
        # `!` < `+` < `-` in ASCII — demands, then grants, then denials. The
        # boundaries are derived rather than hardcoded so adding a policy to a
        # group cannot break the test while the GROUPING (the actual invariant)
        # still holds.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        policy_keys = [o.key for o in rows if o.key in REGISTRY.policies]
        shortnames = [REGISTRY.policies[k].shortname for k in policy_keys]
        self.assertEqual(shortnames, sorted(shortnames))
        symbols = [s[0] for s in shortnames]
        self.assertEqual(symbols, sorted(symbols, key="!+-".index))   # never interleaved
        self.assertLessEqual({"!", "+", "-"}, set(symbols))           # all three stances present

    def test_requires_parenthetical_present(self):
        # webdev's tree position (profession/code/webdev) makes code a prerequisite;
        # the label must say so.
        webdev = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "webdev")
        self.assertIn("(requires: code)", _plain(webdev.label))

    def test_no_parenthetical_without_requires(self):
        code = next(o for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.key == "code")
        self.assertNotIn("requires", _plain(code.label))

    def test_labels_carry_kind_punctuation(self):
        labels = {o.key: _plain(o.label) for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo")}
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
                               instance="poet__verse", workspace="/tmp/ws", scope="solo")

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

    def test_picked_harness_lands_in_the_build(self):
        self.assertEqual(self._run(["gemini", "gemini-cli"]).harness, "gemini-cli")

    def test_a_harness_that_cannot_run_the_picked_ai_falls_back_to_the_ais_own(self):
        # The form's warning zone said so while both were dotted; the stored
        # build names no harness, so the AI's default applies at resolve time.
        self.assertIsNone(self._run(["claude", "gemini-cli"]).harness)
        self.assertIsNone(self._run(["gemini-cli"]).harness)                 # the default AI cannot run in it either
        self.assertEqual(self._run(["gemini", "gemini-cli"], current=AgentBuild(engine="poet", harness="claude-code")).harness, "gemini-cli")

    def test_the_form_is_given_the_pairing_warnings_too(self):
        self._run([])
        self.assertIn(frozenset({"claude", "opencode"}), self.form.call_args.kwargs["warnings"])

    def test_harness_warnings_name_every_pair_that_cannot_run_and_no_pair_that_can(self):
        warnings = _harness_warnings(REGISTRY)
        self.assertIn(frozenset({"claude", "gemini-cli"}), warnings)
        self.assertNotIn(frozenset({"claude", "claude-code"}), warnings)
        first, rest = warnings[frozenset({"claude", "gemini-cli"})]
        self.assertIn("⟦GeminiCLI⟧", first)
        self.assertIn("⟪Claude⟫", first)
        self.assertIn("⟦ClaudeCode⟧", " ".join(rest))                        # what the launch falls back to
        for ai in REGISTRY.ais.values():
            for harness in REGISTRY.harnesses.values():
                self.assertEqual(frozenset({ai.name, harness.name}) in warnings, not harness.runs(ai.name))

    def test_ai_preserved_from_current(self):
        self.assertEqual(self._run([], current=AgentBuild(engine="poet", ai="grok")).ai, "grok")

    def test_picked_ai_overrides_current(self):
        self.assertEqual(self._run(["gemini"], current=AgentBuild(engine="poet", ai="grok")).ai, "gemini")

    def test_preamble_names_instance_and_workspace(self):
        self._run([])
        preamble = self.form.call_args.kwargs["preamble"]
        self.assertEqual(preamble, ["# instance:  poet__verse",
                                    "# workspace: /tmp/ws"])

    def test_empty_selection_yields_bare_build(self):
        build = self._run([])
        self.assertEqual((build.ai, build.professions, build.specialties, build.policies),
                         (None, (), (), ()))     # ai None: "the default", as the store spells it


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

class TestScopedRows(unittest.TestCase):
    """The form reads `forbid_on` (gate tag-scopes): a tag the build's scope
    cannot carry renders locked, UNCHECKED and grey with its scope note; a
    tag the CLUSTER gave a member renders locked, CHECKED, `(from the
    cluster)` — two states kept apart so an inherited {dood} still completes
    the dood+auto combo warning on a member's form."""

    def _rows(self, build, scope, locked=frozenset()):
        return {o.key: o for o in _tag_form_options(REGISTRY, build, scope=scope, locked=locked)}

    def test_a_tag_forbidden_as_the_builds_own_is_locked_unchecked_with_the_note(self):
        dood = self._rows(AgentBuild(specialties=("dood",)), "member")["dood"]   # even when the stored build names it
        self.assertTrue(dood.locked)
        self.assertFalse(dood.checked)
        self.assertIn("(cluster-wide only: F2 on the cluster row)", _plain(dood.label))
        frwl = self._rows(AgentBuild(), "cluster")["firewall"]
        self.assertTrue(frwl.locked and not frwl.checked)
        self.assertIn("(not on a cluster)", _plain(frwl.label))
        clstr = self._rows(AgentBuild(), "solo")["cluster"]
        self.assertTrue(clstr.locked and not clstr.checked)
        self.assertIn("(clusters only: cluster creation applies it)", _plain(clstr.label))

    def test_where_a_tag_is_allowed_its_row_is_an_ordinary_checkbox(self):
        self.assertFalse(self._rows(AgentBuild(), "solo")["dood"].locked)
        self.assertFalse(self._rows(AgentBuild(), "cluster")["dood"].locked)
        self.assertFalse(self._rows(AgentBuild(), "solo")["firewall"].locked)

    def test_an_inherited_tag_stays_checked_so_the_combo_warning_it_completes_fires(self):
        # strict-reviewer (gate tag-scopes): cluster-wide {dood} + this member's own {auto}.
        rows = self._rows(AgentBuild(professions=("code",), specialties=("muxer", "cluster", "dood", "auto")),
                          "member", locked=frozenset({"muxer", "cluster", "dood"}))
        self.assertTrue(rows["dood"].locked and rows["dood"].checked)
        self.assertIn("(from the cluster)", _plain(rows["dood"].label))
        self.assertNotIn("cluster-wide only", _plain(rows["dood"].label))
        checked = {o.key for o in rows.values() if o.checked}
        fired = active_warnings(checked, forms._combo_warnings(REGISTRY))
        self.assertTrue(any("{dood}" in " ".join([header, *body]) for header, body in fired))

    def test_the_cluster_form_judges_combos_with_the_members_own_tags(self):
        warnings = {frozenset({"dood", "auto"}): ("h", ["b"]), frozenset({"x", "y"}): ("h2", ["b2"]),
                    frozenset({"p", "q"}): ("h3", ["b3"])}
        self.assertEqual(forms._warnings_given(warnings, frozenset({"auto", "x", "y"})),
                         {frozenset({"dood"}): ("h", ["b"]),        # ticking {dood} completes what a member started
                          frozenset({"p", "q"}): ("h3", ["b3"])})  # untouched combos stay; x+y is the members' alone
        captured = {}

        def fake_form(title, options, **kwargs):
            captured.update(kwargs)
            return None

        with patch("launch.gui.forms.checkbox_form", side_effect=fake_form):
            forms.prompt_cluster_tags(REGISTRY, AgentBuild(specialties=("muxer", "cluster")), session="team",
                                      locked=frozenset({"muxer", "cluster"}), member_tags=frozenset({"auto"}))
        self.assertIn(frozenset({"dood"}), captured["warnings"])


class TestPlanWarnings(unittest.TestCase):
    """_plan_warnings — the pairing is legal and the model is the same, but
    the BILL changes: a vendor's subscription is spendable only in the clients
    that vendor allows, and anywhere else the same work goes through an API
    key, metered per token (operator, 2026-09-17). Per-AI data, because the
    vendors genuinely differ."""

    def test_it_covers_exactly_the_pairs_with_something_to_say(self):
        warnings = _pairing_warnings(REGISTRY)
        for ai in REGISTRY.ais.values():
            for harness in REGISTRY.harnesses.values():
                outside_plan = (bool(ai.plan_harnesses) and harness.runs(ai.name)
                                and harness.name not in ai.plan_harnesses)
                reported = (bool(ai.foreign_harness_report) and harness.runs(ai.name)
                            and harness.name != ai.harness)
                with self.subTest(ai=ai.name, harness=harness.name):
                    self.assertEqual(frozenset({ai.name, harness.name}) in warnings,
                                     outside_plan or reported)

    def test_claude_elsewhere_leads_with_the_report_and_labels_it(self):
        # The operator's own reason for this warning: two first-hand accounts
        # of Claude burning vastly more tokens outside Claude Code. It LEADS
        # (the header is the line that always reads first) and it is labelled
        # as what it is — field evidence, unreproduced here — so it can never
        # be mistaken for something a vendor published.
        first, rest = _pairing_warnings(REGISTRY)[frozenset({"claude", "opencode"})]
        self.assertIn("⟦OpenCode⟧", first)
        self.assertIn("⟪Claude⟫", first)
        self.assertIn(REGISTRY.ais["claude"].foreign_harness_report, first)   # quoted as written
        self.assertIn("~50x", first)
        body = " ".join(rest)
        self.assertIn("⟦ClaudeCode⟧", body)                      # where the plan CAN be spent
        self.assertLessEqual(len(rest), 2)                       # a header and two short lines, no more
        self.assertIn("ANTHROPIC_API_KEY", body)      # what it costs you instead
        # No pointer into plans/ — that tree is the maintainers' record, not
        # documentation for whoever is picking tags (operator, 2026-09-17).
        self.assertNotIn("plans/", " ".join([first, body]))
        # THE anti-folklore clause, in four words: no vendor prices by client
        # (checked across all four, 2026-09-17), so the header's number cannot
        # be read as a surcharge.
        self.assertIn("per token at the usual rate", body)

    def test_a_multiplier_may_appear_only_inside_a_declared_report(self):
        # The anti-folklore invariant: a number lives ONLY inside a sentence
        # the tree declares as a report (and the scan makes every such
        # sentence hedge itself), never in the explanatory body, which speaks
        # for verified facts.
        import re
        for pair, (header, rest) in _pairing_warnings(REGISTRY).items():
            with self.subTest(pair=sorted(pair)):
                self.assertNotRegex(" ".join(rest), r"\d+x")
                if re.search(r"\d+x", header):
                    (ai,) = [name for name in pair if name in REGISTRY.ais]
                    report = REGISTRY.ais[ai].foreign_harness_report
                    self.assertTrue(report)
                    self.assertIn(report, header)

    def test_the_warning_names_the_floor_where_a_vendor_publishes_one(self):
        # The fact a reader decides on: an unpaid Gemini key keeps a recurring
        # free tier, an Anthropic one does not. This is how the AIs differ —
        # by published fact, not by a severity dial (researcher, 2026-09-17).
        warnings = _pairing_warnings(REGISTRY)
        claude = " ".join(warnings[frozenset({"claude", "opencode"})][1])
        gemini = " ".join(warnings[frozenset({"gemini", "hermes"})][1])
        self.assertIn("no free tier", claude)
        self.assertIn("250 req/day", gemini)

    def test_an_unestablished_floor_is_simply_not_mentioned(self):
        # chatgpt and grok have no vendor page establishing one, so the
        # warning guesses nothing — silence is the honest default here too.
        body = " ".join(_pairing_warnings(REGISTRY)[frozenset({"grok", "openclaw"})][1])
        self.assertNotIn("free tier", body)
        self.assertNotIn("(", body.split("XAI_API_KEY")[1])   # no floor parenthetical where none is known
        self.assertEqual("", REGISTRY.ais["grok"].key_free_tier)
        self.assertEqual("", REGISTRY.ais["chatgpt"].key_free_tier)

    def test_a_vendor_that_permits_the_harness_is_not_warned_about(self):
        # The answer to "do the others do the same?": no. OpenAI's ChatGPT
        # sign-in reaches OpenCode and xAI's SuperGrok login reaches three
        # harnesses, so those pairings are silent — which is why the permitted
        # set is per-AI data rather than "the AI's own CLI".
        warnings = _pairing_warnings(REGISTRY)
        self.assertNotIn(frozenset({"chatgpt", "opencode"}), warnings)
        self.assertNotIn(frozenset({"grok", "hermes"}), warnings)
        self.assertIn(frozenset({"claude", "opencode"}), warnings)

    def test_an_ai_with_nothing_known_warns_about_nothing(self):
        # Silence is the honest default: no gating known AND nothing reported
        # means nothing to say. Both must be cleared — they are independent
        # claims, and claude carries one of each.
        quiet = dataclasses.replace(REGISTRY.ais["claude"], plan_harnesses=(), foreign_harness_report="")
        registry = dataclasses.replace(REGISTRY, ais={**REGISTRY.ais, "claude": quiet})
        self.assertFalse([pair for pair in _pairing_warnings(registry) if "claude" in pair])

    def test_every_warning_is_short_enough_to_read(self):
        # A warning nobody finishes reading warns nobody (operator,
        # 2026-09-17). The long version lives one pointer away.
        for pair, (header, body) in _pairing_warnings(REGISTRY).items():
            with self.subTest(pair=sorted(pair)):
                self.assertLessEqual(len(body), 2)
                for line in (header, *body):
                    self.assertLess(len(line), 160)

    def test_the_two_warning_maps_never_collide(self):
        # Both are merged into one dict for the form; a shared key would drop
        # one message silently. Disjoint by construction — one covers pairs
        # that cannot run at all, the other pairs that can.
        self.assertEqual(set(_pairing_warnings(REGISTRY)) & set(_harness_warnings(REGISTRY)), set())
