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
    _effort_key, _follow_key, _form_requires, _harness_warnings, _model_defaults, _model_key, _model_keys,
    _pairing_warnings, _tag_form_options, _tag_row, _toolkit_form_options, prompt_tags,
)
from launch.gui.form_core import FormResult, active_warnings
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
        # Every tag is a row keyed by its name; every other row sits under an
        # AI — its follow bullet, its model bullets, its effort row (their own
        # tests below).
        keys = {o.key for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
                if not o.header and o.key not in _under_ai_keys()}
        expected = (set(REGISTRY.ais) | set(REGISTRY.harnesses) | set(REGISTRY.engines) | set(REGISTRY.professions)
                    | set(REGISTRY.specialties) | set(REGISTRY.policies))
        self.assertEqual(keys, expected)

    def test_one_header_per_kind_in_order(self):
        headers = [o.key for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo") if o.header]
        self.assertEqual(headers, ["#engine", "#ai", "#harness", "#profession", "#specialty", "#policy"])

    def test_the_engine_leads_the_form_then_the_ai_then_the_harness(self):
        # The engine is chosen first and the AI fine-tunes it (operator,
        # 2026-09-28; the AI led until then): the engine's standard picks a
        # model for whichever AI is dotted. The harness — the CLI around the
        # AI — follows it. Between the headers sit exactly each kind's members.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        self.assertEqual(rows[0].key, "#engine")
        self.assertTrue(rows[0].header)
        ai_header = next(i for i, o in enumerate(rows) if o.key == "#ai")
        harness_header = next(i for i, o in enumerate(rows) if o.key == "#harness")
        profession_header = next(i for i, o in enumerate(rows) if o.key == "#profession")
        self.assertEqual({o.key for o in rows[1:ai_header]}, set(REGISTRY.engines))
        ai_section = rows[ai_header + 1:harness_header]
        self.assertEqual({o.key for o in ai_section if o.group == "ai"}, set(REGISTRY.ais))
        self.assertEqual({o.key for o in ai_section if o.group != "ai"}, _under_ai_keys())   # the rows under each AI, and nothing else
        self.assertEqual({o.key for o in rows[harness_header + 1:profession_header]}, set(REGISTRY.harnesses))

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

    def test_every_harness_the_launcher_cannot_run_says_so_and_stays_selectable(self):
        # A NOTE, not a lock (bug-investigator and agent-writer, gate
        # gemini-adapter): describing an instance in a harness whose adapter
        # is still coming is the design, and the launch refuses it with the
        # way out. Grey would claim "never" for what is "not this month".
        from launch.ai import readiness_note
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        for option in (o for o in rows if o.key in REGISTRY.harnesses):
            with self.subTest(harness=option.key):
                note = readiness_note(option.key)
                text = _plain(option.label)
                if note is None:
                    self.assertNotIn("yet", text)
                else:
                    self.assertIn(note, text)
                self.assertFalse(option.locked, "a roadmap state is not a lock")

    def test_a_harness_that_cannot_start_yet_still_lands_in_the_build(self):
        # The row is a real choice: the build stores it, and the launch is
        # what refuses it — the promise the adapter registry's comment makes.
        rows = _tag_form_options(REGISTRY, AgentBuild(ai="gemini", harness="gemini-cli"), scope="solo")
        self.assertEqual({o.key for o in rows if o.key in REGISTRY.harnesses and o.checked}, {"gemini-cli"})
        with patch.object(forms, "checkbox_form",
                          return_value=FormResult(checked=["gemini", "gemini-cli"])):
            build = prompt_tags(REGISTRY, AgentBuild(ai="gemini"),
                                instance="poet__verse", workspace="/tmp/ws", scope="solo")
        self.assertEqual(build.harness, "gemini-cli")

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
        # Since 2026-09-28 the form also offers each AI's models, as bullets
        # under it — an instance's own pick, not the engine's answer — so the
        # pin is that no OTHER row names a tier's model.
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
        labels = " ".join(text for o in rows if o.group != "model" for _, text in o.label)
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
        radios = (set(REGISTRY.ais) | set(REGISTRY.harnesses) | set(REGISTRY.engines) | set(_model_keys(REGISTRY))
                  | {_follow_key(ai) for ai in REGISTRY.ais.values()})
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
        labels = {o.key: _plain(o.label) for o in _tag_form_options(REGISTRY, AgentBuild(), scope="solo")
                  if not callable(o.label)}                   # a follow bullet's words depend on the dotted engine
        self.assertIn("[code]", labels["code"])
        self.assertIn("{auto}", labels["auto"])
        self.assertIn("<+qry>", labels["web-research"])   # policies render their shortname


class TestPromptTags(unittest.TestCase):
    """prompt_tags' post-form processing: the flat key list splits back into
    axes (registry order) and the engine rides through untouched. The form
    itself is patched — its interactive behavior is out of unit scope."""

    def _run(self, checked, current=AgentBuild(engine="poet")):
        # `checked` is the key list a confirmed form comes back with, or None
        # for Esc — the fixture wraps it in the record the real form returns,
        # so a caller below still reads as the tick list it means.
        result = None if checked is None else FormResult(checked=checked)
        with patch.object(forms, "checkbox_form", return_value=result) as self.form:
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

    # --- what the dotted rows store (gate model-picker-3): follow stores no
    # model, a model bullet its id, the effort row's pick its level. poet's
    # standard lands Claude on claude-sonnet-5 at medium. ---

    def _run_with(self, checked, choices, current):
        result = FormResult(checked=checked, choices=choices)
        with patch.object(forms, "checkbox_form", return_value=result) as self.form:
            return prompt_tags(REGISTRY, current, instance="poet__verse", workspace="/tmp/ws", scope="solo")

    def test_follow_stores_no_model_and_a_default_effort_stores_none(self):
        build = self._run_with(["poet", "claude", "claude:(follow)"], {"claude:(follow):(effort)": None},
                               AgentBuild(engine="poet", ai="claude"))
        self.assertEqual((build.model, build.effort), (None, None))

    def test_a_model_bullet_pins_that_model_even_the_engines_current_one(self):
        # A pin of today's engine model is now expressible (operator, 2026-09-29).
        for model in ("claude-opus-5-5", "claude-sonnet-5"):
            with self.subTest(model=model):
                build = self._run_with(["poet", "claude", f"claude:{model}"], {f"claude:{model}:(effort)": None},
                                       AgentBuild(engine="poet", ai="claude"))
                self.assertEqual(build.model, model)

    def test_the_pick_under_the_dotted_bullet_is_stored(self):
        build = self._run_with(["poet", "claude", "claude:claude-opus-5-5"], {"claude:claude-opus-5-5:(effort)": "low"},
                               AgentBuild(engine="poet", ai="claude"))
        self.assertEqual((build.model, build.effort), ("claude-opus-5-5", "low"))

    def test_only_the_dotted_bullets_effort_row_counts(self):
        # Each bullet carries its own row (operator, 2026-09-29: "below the
        # selected model"); a pick left under another bullet is not the answer.
        build = self._run_with(["poet", "claude", "claude:(follow)"],
                               {"claude:(follow):(effort)": None, "claude:claude-opus-5-5:(effort)": "low",
                                "gemini:(follow):(effort)": "LOW"}, AgentBuild(engine="poet", ai="claude"))
        self.assertIsNone(build.effort)

    def test_stale_picks_are_replaced_and_the_form_said_so(self):
        current = AgentBuild(engine="poet", ai="claude", model="claude-opus-4-1", effort="high")
        build = self._run_with(["poet", "claude", "claude:(follow)"], {"claude:(follow):(effort)": None}, current)
        self.assertEqual((build.model, build.effort), (None, None))
        header, body = self.form.call_args.kwargs["warnings"][frozenset({"claude"})]
        self.assertEqual(header, "Its model claude-opus-4-1 is not among ⟪Claude⟫'s models.")
        self.assertEqual(body, ["Confirming stores what is dotted in their place."])

    def test_an_effort_the_running_model_lacks_is_warned_about(self):
        current = AgentBuild(engine="poet", ai="claude", model="claude-opus-4-5-20251101", effort="max")   # no max on Opus 4.5
        self._run_with(["poet", "claude", "claude:claude-opus-4-5-20251101"], {"claude:claude-opus-4-5-20251101:(effort)": None}, current)
        header, _ = self.form.call_args.kwargs["warnings"][frozenset({"claude"})]
        self.assertEqual(header, "Its effort max is not one claude-opus-4-5-20251101 takes.")

    def test_live_picks_raise_no_warning(self):
        self._run_with(["poet", "claude", "claude:claude-opus-5"], {"claude:claude-opus-5:(effort)": "low"},
                       AgentBuild(engine="poet", ai="claude", model="claude-opus-5", effort="low"))
        self.assertNotIn(frozenset({"claude"}), self.form.call_args.kwargs["warnings"])

    def test_the_form_is_given_the_model_defaults(self):
        self._run([])
        self.assertEqual(self.form.call_args.kwargs["defaults"], _model_defaults(REGISTRY))

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


def _bullets() -> set[str]:
    """Every bullet under an AI: its follow bullet and its model bullets."""
    return {*_model_keys(REGISTRY)} | {_follow_key(ai) for ai in REGISTRY.ais.values()}


def _under_ai_keys() -> set[str]:
    """Every row key under an AI: its bullets, and the effort row under each."""
    return _bullets() | {_effort_key(bullet) for bullet in _bullets()}


class TestModelRows(unittest.TestCase):
    """What sits under each AI's row (operators' words, 2026-09-28 and 29):
    FOLLOW THE ENGINE first, then its models.list as bullets that pin, then
    its horizontal EFFORT pick. The bullets are one MANDATORY radio group —
    exactly one, always — the engine's and the AI's dots returning it to
    follow (`_model_defaults`); every row folds while its AI is not dotted,
    and the effort row also while the dotted model takes no level."""

    @staticmethod
    def rows(build=AgentBuild()):
        return _tag_form_options(REGISTRY, build, scope="solo")

    @staticmethod
    def text(option, dotted=frozenset()):
        label = option.label(frozenset(dotted)) if callable(option.label) else option.label
        return "".join(t for _, t in label)

    def test_each_ai_has_follow_then_its_models_each_with_its_effort_row_beneath(self):
        rows, keys = self.rows(), _model_keys(REGISTRY)
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                bullets = [o for o in rows if o.attached_to == ai.name]
                self.assertEqual(bullets[0].key, _follow_key(ai))
                self.assertEqual([keys[o.key][1] for o in bullets[1:]], list(ai.models))
                self.assertTrue(all(o.group == "model" and o.folds_with_anchor for o in bullets))
                for bullet in bullets:
                    (effort,) = [o for o in rows if o.attached_to == bullet.key]
                    self.assertEqual(effort.key, _effort_key(bullet.key))
                    self.assertTrue(effort.folds_with_anchor)
                    self.assertIsNotNone(effort.choices)

    def test_each_effort_row_sits_directly_below_its_bullet(self):
        rows = self.rows()
        for index, option in enumerate(rows):
            if option.choices is not None:
                with self.subTest(row=option.key):
                    self.assertEqual(rows[index - 1].key, option.attached_to)

    def test_the_follow_bullet_names_the_model_the_dotted_engine_means(self):
        follow = next(o for o in self.rows() if o.key == "claude:(follow)")
        self.assertIn("follow the engine", self.text(follow))
        self.assertIn("Sonnet-5", self.text(follow, {"poet", "claude"}))          # poet → 2025Q3 → claude-sonnet-5
        self.assertIn("Haiku-4.5", self.text(follow, {"golem", "claude"}))        # golem → the Haiku ALIAS, resolved
        self.assertIn("Fable-5.1", self.text(follow, {"thinker", "claude"}))

    def test_a_live_stored_pin_is_prefilled_by_id_or_alias(self):
        for spelling in ("claude-haiku-4-5-20251001", "claude-haiku-4-5"):
            with self.subTest(stored=spelling):
                dotted = [o.key for o in self.rows(AgentBuild(ai="claude", model=spelling)) if o.group == "model" and o.checked]
                self.assertEqual(dotted, ["claude:claude-haiku-4-5-20251001"])

    def test_no_pin_a_stale_pin_or_another_ais_pin_prefills_nothing(self):
        # The form's `defaults` then dot follow at open (the next tests).
        for build in (AgentBuild(ai="claude"), AgentBuild(ai="claude", model="claude-opus-4-1"),
                      AgentBuild(ai="gemini", model="claude-opus-5")):
            with self.subTest(build=build):
                self.assertEqual([o.key for o in self.rows(build) if o.group == "model" and o.checked], [])

    def test_the_bullets_and_the_effort_rows_say_what_they_do(self):
        rows = {o.key: "".join(t for _, t in o.body) for o in self.rows()}
        self.assertIn("the tier moves, the instance moves with it", rows["claude:(follow)"])
        self.assertIn("replacing the engine's rated pairing", rows["claude:claude-opus-5-5"])
        self.assertIn("runs at the model's highest level", rows["claude:claude-opus-5-5"])
        self.assertIn("The one marked (default) stores nothing", rows["claude:claude-opus-5-5:(effort)"])

    def test_an_effort_row_offers_its_models_levels_with_the_default_tagged_not_added(self):
        # No "default" position (operator, 2026-09-29): the levels alone, the
        # default among them tagged — and pre-selected (choice_default).
        rows = {o.key: o for o in self.rows()}
        follow = rows["claude:(follow):(effort)"]
        poet = frozenset({"poet", "claude", "claude:(follow)"})
        self.assertEqual(follow.choices(poet), [("low", "low"), ("medium", "medium (default)"), ("high", "high"),
                                                ("xhigh", "xhigh"), ("max", "max")])
        self.assertEqual(follow.choice_default(poet), "medium")                  # poet's rated level
        opus = rows["claude:claude-opus-4-6:(effort)"]
        dotted = frozenset({"poet", "claude", "claude:claude-opus-4-6"})
        self.assertEqual([value for value, _ in opus.choices(dotted)], ["low", "medium", "high", "max"])   # the gap, kept
        self.assertEqual(opus.choice_default(dotted), "max")                     # a pin's default is its top

    def test_the_default_tag_moves_with_the_dotted_bullet(self):
        # bug-investigator, gate effort-row: a stale tag pointing at the old
        # default is the failure a user would trust.
        rows = {o.key: o for o in self.rows()}
        def tagged(key, dotted):
            return [value for value, text in rows[key].choices(frozenset(dotted)) if text.endswith("(default)")]
        self.assertEqual(tagged("claude:(follow):(effort)", {"quick", "claude", "claude:(follow)"}), ["high"])
        self.assertEqual(tagged("claude:(follow):(effort)", {"thinker", "claude", "claude:(follow)"}), ["max"])
        self.assertEqual(tagged("claude:claude-sonnet-5:(effort)", {"quick", "claude", "claude:claude-sonnet-5"}), ["max"])

    def test_the_effort_row_has_nothing_to_offer_for_a_model_that_takes_none(self):
        # So the row folds (bug-investigator, gate model-picker-3) — for every
        # shipped `efforts=-` model, all four AIs.
        rows = {o.key: o for o in self.rows()}
        for ai in REGISTRY.ais.values():
            for model in ai.models:
                if model.effortless:
                    bullet = _model_key(ai, model)
                    with self.subTest(ai=ai.name, model=model.id):
                        self.assertEqual(rows[_effort_key(bullet)].choices(frozenset({"poet", ai.name, bullet})), [])

    def test_a_stored_effort_starts_on_the_row_under_its_stored_bullet_only(self):
        follows = {o.key: o.choice for o in self.rows(AgentBuild(ai="claude", effort="low")) if o.choices is not None}
        self.assertEqual(follows["claude:(follow):(effort)"], "low")
        self.assertIsNone(follows["claude:claude-opus-5:(effort)"])
        pinned = {o.key: o.choice for o in self.rows(AgentBuild(ai="claude", model="claude-opus-5", effort="low"))
                  if o.choices is not None}
        self.assertEqual((pinned["claude:claude-opus-5:(effort)"], pinned["claude:(follow):(effort)"]), ("low", None))

    def test_an_ais_ids_line_up_down_its_bullets(self):
        rows, keys = self.rows(), _model_keys(REGISTRY)
        for ai in REGISTRY.ais.values():
            starts = {self.text(o).index(keys[o.key][1].id) for o in rows if o.key in keys and o.attached_to == ai.name}
            with self.subTest(ai=ai.name):
                self.assertEqual(len(starts), 1)

    def test_every_bullet_requires_its_ai_and_no_mandatory_radio_requires_anything(self):
        requires = _form_requires(REGISTRY)
        for key, (ai, _) in _model_keys(REGISTRY).items():
            with self.subTest(bullet=key):
                self.assertEqual(requires[key], frozenset({ai.name}))
        for ai in REGISTRY.ais.values():
            self.assertEqual(requires[_follow_key(ai)], frozenset({ai.name}))
        for name in (*REGISTRY.ais, *REGISTRY.harnesses, *REGISTRY.engines):
            self.assertNotIn(name, requires)

    def test_the_cluster_form_has_nothing_under_an_ai(self):
        rows = _tag_form_options(REGISTRY, AgentBuild(), scope="cluster", engines=False)
        self.assertFalse([o for o in rows if o.attached_to is not None])

    def test_the_defaults_send_every_engine_and_ai_to_that_ais_follow_bullet(self):
        defaults = _model_defaults(REGISTRY)
        self.assertEqual(len(defaults), len(REGISTRY.engines) * len(REGISTRY.ais))    # TOTAL (bug-investigator)
        for engine in REGISTRY.engines.values():
            for ai in REGISTRY.ais.values():
                self.assertEqual(defaults[frozenset({engine.name, ai.name})], _follow_key(ai))

    def _drive(self, build, keys):
        """The REAL checkbox_form over the tag form's rows, keystrokes
        through a pipe, then prompt_tags' reading of what it returned."""
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from launch.gui import form_core
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            pipe.send_text(keys + "\x03\x03")
            result = form_core.checkbox_form("t", self.rows(build), requires=_form_requires(REGISTRY),
                                             defaults=_model_defaults(REGISTRY))
        self.assertIsNotNone(result)
        with patch.object(forms, "checkbox_form", return_value=result):
            return prompt_tags(REGISTRY, build, instance="poet__verse", workspace="/tmp/ws", scope="solo")

    def _to(self, build, key):
        """The downs that reach the row keyed `key`, counting only the rows
        the form shows at open (by KEY: labels repeat words — the default
        engine's "baseline max-effort" holds "effort")."""
        rows = [o for o in form_core_order(self.rows(build)) if not o.header and _shown_at_open(o, build)]
        return "\x1b[B" * next(i for i, o in enumerate(rows) if o.key == key)

    def test_an_untouched_confirm_keeps_a_stored_effort_that_is_not_the_default(self):
        # strict-reviewer's baseline trap: the at-open fill must not reset it.
        build = self._drive(AgentBuild(engine="poet", ai="claude", effort="low"), "\r")
        self.assertEqual((build.model, build.effort), (None, "low"))

    def test_choosing_another_engine_returns_the_effort_to_its_default(self):
        # The engine selects the model AND its default level: start on poet
        # with low pinned under follow, dot quick, confirm — no effort stored.
        start = AgentBuild(engine="poet", ai="claude", effort="low")
        build = self._drive(start, self._to(start, "quick") + " \r")
        self.assertEqual((build.engine, build.model, build.effort), ("quick", None, None))

    def test_landing_back_on_the_default_level_stores_nothing(self):
        start = AgentBuild(engine="poet", ai="claude")
        down = self._to(start, "claude:(follow):(effort)")
        build = self._drive(start, down + "\x1b[D\x1b[C\r")                    # medium (default) → low → medium
        self.assertIsNone(build.effort)
        build = self._drive(start, down + "\x1b[C\r")                           # medium → high: a pin
        self.assertEqual(build.effort, "high")

    def test_the_form_opens_with_exactly_one_bullet_dotted(self):
        # strict-reviewer's opening cases, through the real checkbox_form,
        # whose `defaults` fill at open.
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from launch.gui import form_core
        cases = {
            "no pin": (AgentBuild(engine="poet", ai="claude"), "claude:(follow)"),
            "a live pin": (AgentBuild(engine="poet", ai="claude", model="claude-opus-5-5"), "claude:claude-opus-5-5"),
            "a stale pin": (AgentBuild(engine="poet", ai="claude", model="claude-opus-4-1"), "claude:(follow)"),
            "another AI's pin": (AgentBuild(engine="golem", ai="gemini", model="claude-opus-5"), "gemini:(follow)"),
        }
        bullets = {*_model_keys(REGISTRY), *(_follow_key(ai) for ai in REGISTRY.ais.values())}
        for label, (build, expected) in cases.items():
            with self.subTest(case=label), create_pipe_input() as pipe, \
                    create_app_session(input=pipe, output=DummyOutput()):
                pipe.send_text("\r\x03\x03")
                result = form_core.checkbox_form("t", self.rows(build), requires=_form_requires(REGISTRY),
                                                 defaults=_model_defaults(REGISTRY))
                self.assertEqual([key for key in result.checked if key in bullets], [expected])


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
            return None if result is None else FormResult(checked=result)

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


def form_core_order(options):
    from launch.gui.form_core import ordered_form_options
    return ordered_form_options(options)


def _shown_at_open(option, build):
    """Whether a row shows when the tag form opens on `build` with no pin:
    everything not under an AI, the dotted AI's follow bullet and bullets,
    and the effort row under follow — the stops ↓ walks through."""
    if option.attached_to is None:
        return True
    ai = REGISTRY.ai_for(build)
    if option.attached_to == ai.name:
        return True
    return option.attached_to == _follow_key(ai) and _effort_key(_follow_key(ai)) == option.key

