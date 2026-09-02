"""Tests for launch.tag_form — the sectioned tag form's pure parts (option
assembly, radio grouping, attached_to ordering, requires cascade, warning
computation, prompt_tags post-processing) plus the shared display coercers.
Assembled against the real shipped agents/ tree. The prompt_toolkit
Application itself (checkbox_form) is interactive and stays out of unit
scope."""

import unittest
from pathlib import Path
from unittest.mock import patch

from launch.gui import tag_form
from launch.paths import AGENTS_DIR
from launch.gui.tag_form import (
    STYLE_UNDERLINE, FormOption, _combo_warnings, _form_requires, _normalize,
    _plain, _tag_form_options, active_warnings, ordered_form_options,
    _toolkit_form_options, prompt_tags, requires_closure,
)
from launch.tags import AgentBuild, scan_all
from launch.tags.profession import ToolkitEntry

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

    def test_real_tree_webdev_requires_code(self):
        self.assertEqual(requires_closure("webdev", _form_requires(REGISTRY)), {"code"})


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


class TestRefreshAuto(unittest.TestCase):
    """The auto-fill contract: a derived field FOLLOWS its source until the
    user touches it, then never again — 'the name follows the path until you
    type your own'."""

    def fields(self):
        path = tag_form.TextField(key="path", label="path", value="/code/thing")
        name = tag_form.TextField(
            key="name", label="name", value="",
            auto=lambda values: f"golem__{values['path'].rsplit('/', 1)[-1]}")
        return path, name

    def test_untouched_fields_follow_their_source(self):
        path, name = self.fields()
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing")
        path.insert("2")
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing2")

    def test_one_keystroke_in_the_field_stops_the_following(self):
        path, name = self.fields()
        tag_form.refresh_auto([path, name])
        name.insert("!")                      # the user typed their own
        path.insert("2")
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing!")

    def test_backspace_counts_as_touching_too(self):
        # Deleting part of the suggestion IS choosing a name.
        path, name = self.fields()
        tag_form.refresh_auto([path, name])
        name.backspace()
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thin")

    def test_cursor_motion_does_not_count_as_touching(self):
        # Arrowing around a suggestion is LOOKING, not choosing — the field
        # keeps following its source, and the rewrite snaps the cursor back
        # to the end (there was no user cursor position worth preserving).
        path, name = self.fields()
        tag_form.refresh_auto([path, name])
        name.home()
        name.word_right()
        path.insert("2")
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "golem__thing2")
        self.assertEqual(name.cursor, len("golem__thing2"))

    def test_fields_without_auto_never_move(self):
        path, name = self.fields()
        name.auto = None
        name.value = "pinned"
        tag_form.refresh_auto([path, name])
        self.assertEqual(name.value, "pinned")


class TestFormDrivenHeadless(unittest.TestCase):
    """checkbox_form driven for real — keystrokes through a pipe input, no
    terminal. This is where the interactive-only guarantees live: the confirm
    gate on invalid fields is unreachable from pure helpers, and a mutation
    run proved it was unpinned until these."""

    def drive(self, keys, fields=None, options=None):
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                # Trailing double ctrl-C is a TRIPWIRE, not part of any test's
                # script: a form still open after its keys (e.g. the really-
                # done? question arming when it shouldn't) cancels to None and
                # FAILS loudly — without it such a regression hangs the suite.
                # Two, because the first may be consumed as the question's
                # answer. A correctly-exited form never reads them.
                pipe.send_text(keys + "\x03\x03")
                return tag_form.checkbox_form(
                    "t", options or [tag_form.FormOption(key="o", label="opt")],
                    fields=fields)

    def test_confirm_refuses_while_a_field_is_invalid(self):
        # First Enter must be REFUSED (field empty + validator says so); the
        # typed x then makes it valid and the second Enter lands. If the gate
        # dies, the first Enter exits with the empty value instead.
        result = self.drive("\rx\r", fields=[
            tag_form.TextField(key="f", label="name", value="",
                               validate=lambda v: "empty" if not v else None)])
        self.assertEqual(result, ({"f": "x"}, []))

    def test_space_is_a_literal_in_a_field(self):
        result = self.drive("a b\r", fields=[
            tag_form.TextField(key="f", label="name", value="")])
        self.assertEqual(result, ({"f": "a b"}, []))

    def test_backspace_edits_the_field(self):
        result = self.drive("ab\x7f\r", fields=[
            tag_form.TextField(key="f", label="name", value="")])
        self.assertEqual(result, ({"f": "a"}, []))

    def test_options_below_the_fields_still_toggle(self):
        # Down-arrow onto the option row, Space toggles it, Enter confirms —
        # the field rows must not have broken the row offset arithmetic.
        result = self.drive("\x1b[B \r", fields=[
            tag_form.TextField(key="f", label="name", value="ok")])
        self.assertEqual(result, ({"f": "ok"}, ["o"]))

    def test_arrows_never_edit_a_field(self):
        # ← once ate a character in a live form (a remove handler fell through
        # to backspace). Left and right on a focused field must change nothing
        # — which makes this form UNCHANGED, so Enter asks and `y` closes it.
        result = self.drive("\x1b[D\x1b[C\ry", fields=[
            tag_form.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abc"}, []))

    def test_left_arrow_moves_the_cursor_so_typing_lands_mid_string(self):
        result = self.drive("\x1b[Dx\r", fields=[
            tag_form.TextField(key="f", label="name", value="ab")])
        self.assertEqual(result, ({"f": "axb"}, []))

    def test_ctrl_left_jumps_a_word_in_a_path(self):
        # ctrl+← from the end of /tmp/proj lands before `proj` (path
        # separators end a word), so the x goes in front of the basename.
        result = self.drive("\x1b[1;5Dx\r", fields=[
            tag_form.TextField(key="f", label="path", value="/tmp/proj")])
        self.assertEqual(result, ({"f": "/tmp/xproj"}, []))

    def test_home_and_the_delete_key_erase_at_the_cursor(self):
        # Home to column 0, Delete eats the char AT the cursor (not before).
        result = self.drive("\x1b[H\x1b[3~\r", fields=[
            tag_form.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "bc"}, []))

    def test_an_unchanged_confirm_asks_and_any_other_key_stays(self):
        # Enter on an untouched form arms the really-done? question. The next
        # key ANSWERS it and is consumed — the `n` here must not land in the
        # field as text; the x afterwards proves the form is still live.
        result = self.drive("\rnx\r", fields=[
            tag_form.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abcx"}, []))

    def test_a_changed_confirm_never_asks(self):
        # One real edit and Enter closes directly — no `y` is queued, so if
        # the question wrongly armed, the tripwire would cancel to None.
        result = self.drive("x\r", fields=[
            tag_form.TextField(key="f", label="name", value="abc")])
        self.assertEqual(result, ({"f": "abcx"}, []))


class TestWantsWarnings(unittest.TestCase):
    """wants_warnings — the advisory zone. Keys stay manifest NAMES (they must
    match the checked set); `labels` is display-only, and it exists because a
    header naming 'cowork' and 'free-bash' points the user at two strings that
    appear nowhere on screen — the rows say {cowork} and <+bash>."""

    WANTS = {"cowork": (("free-bash", "coworkers get auto-denied without it"),)}

    def test_fires_only_while_the_wanted_tag_is_unchecked(self):
        self.assertEqual(len(tag_form.wants_warnings({"cowork"}, self.WANTS)), 1)
        self.assertEqual(tag_form.wants_warnings({"cowork", "free-bash"},
                                                 self.WANTS), [])
        self.assertEqual(tag_form.wants_warnings(set(), self.WANTS), [])

    def test_the_header_shows_labels_when_given(self):
        labels = {"cowork": "{cowrk}", "free-bash": "<+bash>"}
        (header, _), = tag_form.wants_warnings({"cowork"}, self.WANTS, labels)
        self.assertEqual(header, "'{cowrk}' wants '<+bash>':")

    def test_a_key_missing_from_the_map_falls_back_to_itself(self):
        # Non-tag callers of checkbox_form lose nothing by omitting labels.
        (header, _), = tag_form.wants_warnings({"cowork"}, self.WANTS,
                                               {"cowork": "{cowrk}"})
        self.assertEqual(header, "'{cowrk}' wants 'free-bash':")

    def test_the_forms_label_map_is_punctuated_for_every_kind(self):
        # The real registry, all four kinds — a want may point at any tag.
        labels = tag_form._form_labels(REGISTRY)
        self.assertEqual(labels["cowork"], "{cowrk}")
        self.assertEqual(labels["free-bash"], "<+bash>")
        self.assertEqual(labels["code"], "[code]")
        # Engines included (a want may point at one) — asserted via the tag's
        # own label because engine shortnames are expressive (thinker is 🧠).
        self.assertEqual(labels["thinker"], REGISTRY.engines["thinker"].label)


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


class TestCascadeInForm(unittest.TestCase):
    """The check-cascade wiring: simulate what the form's Space handler does
    (toggle + cascade) using the pure pieces, against the real tree's
    webdev→code edge."""

    def test_checking_dependent_checks_requirement(self):
        req = _form_requires(REGISTRY)
        checked = {"webdev"} | requires_closure("webdev", req)
        self.assertIn("code", checked)

    def test_unchecking_requirement_identifies_dependents(self):
        req = _form_requires(REGISTRY)
        dependents = {k for k in ("webdev",) if "code" in requires_closure(k, req)}
        self.assertEqual(dependents, {"webdev"})


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

        with patch("launch.gui.tag_form.toolkit_profile_path",
                   return_value=Path("/nonexistent/code_profile.toml")), \
             patch("launch.gui.tag_form.ui_profile_path",
                   return_value=Path("/nonexistent/ui_profile.toml")), \
             patch("launch.gui.tag_form.checkbox_form", side_effect=fake_form):
            tag_form.edit_profiles_menu(scan_all(AGENTS_DIR))
        return captured

    def test_size_note_passed_as_preamble(self):
        # The size disclaimer belongs to the toolkit rows and rides along
        # whenever a toolkit section is present.
        self.assertIn(tag_form.TOOLKIT_SIZE_NOTE,
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

        with patch("launch.gui.tag_form.checkbox_form", side_effect=fake_form):
            build = tag_form.prompt_cluster_tags(
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


if __name__ == "__main__":
    unittest.main()
