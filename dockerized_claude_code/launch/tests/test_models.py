"""Tests for launch.tags.models — an AI's models.list: the options the tag form
offers an instance as its model and its effort, how each line is read and
refused, how a model reads in the picker (⟪Claude:Opus-5.5⟫), which level a
stored effort pin runs at, and why a stored pick cannot run once its AI's
list stops carrying it.

The shipped lists are held here too: every id's label is rendered and held
to the label rule's promises, so a bad label fails the suite rather than
surfacing in a picker screenshot (bug-investigator, gate model-picker)."""

import re
import unittest
from pathlib import Path

from launch.paths import AGENTS_DIR
from launch.tags import Ai, TagError, scan_all
from launch.tags.models import NO_EFFORT, Model, StaleModel, model_label, parse_models

PATH = Path("agents/ai/x/models.list")
VERIFIED = "# Ids verified 2026-09-28 against the vendor's page\n"
SCALE = ("low", "medium", "high", "xhigh", "max")        # Claude's, the widest a fixture needs


def parse(body: str) -> tuple[Model, ...]:
    return parse_models(VERIFIED + body, PATH, SCALE)


class TestParseModels(unittest.TestCase):
    """One model per line: its id, then `efforts=` (required: its levels, or
    `-`), `alias=` (repeatable) and `display=` — the whole vocabulary."""

    def test_ids_come_back_in_file_order(self):
        models = parse("x-strong-2  efforts=high\nx-strong-1  efforts=high\nx-light-1  efforts=-\n")
        self.assertEqual([m.id for m in models], ["x-strong-2", "x-strong-1", "x-light-1"])

    def test_every_key_is_read(self):
        (model,) = parse("x-a-1-20250101  alias=x-a-1-latest  alias=x-a  display=A-One  efforts=low,max\n")
        self.assertEqual(model, Model(id="x-a-1-20250101", aliases=("x-a-1-latest", "x-a"), display="A-One",
                                      efforts=("low", "max")))

    def test_levels_come_back_in_the_scales_order_whatever_the_file_says(self):
        # Order is free in the file, so a diff of two ranges reads the same
        # either way (strict-reviewer, gate model-picker-3); a gap is kept.
        (model,) = parse("x-a-1  efforts=max,low,high\n")
        self.assertEqual(model.efforts, ("low", "high", "max"))

    def test_a_dash_is_a_model_that_takes_no_effort(self):
        (model,) = parse(f"x-a-1  efforts={NO_EFFORT}\n")
        self.assertEqual((model.efforts, model.effortless, model.top_effort), ((), True, None))

    def test_comments_and_blank_lines_are_not_models(self):
        models = parse("\n# a comment line\nx-a-1  efforts=high   # a trailing comment\n\n   \n")
        self.assertEqual([m.id for m in models], ["x-a-1"])

    def test_the_verified_line_may_sit_anywhere(self):
        models = parse_models("x-a-1  efforts=high\n# verified 2026-01-02 against the vendor\n", PATH, SCALE)
        self.assertEqual([m.id for m in models], ["x-a-1"])


class TestParseModelsRefuses(unittest.TestCase):
    """Anything else fails the scan, naming the file and the line — the tree's
    strict rule: a typo fails the suite, not a launch."""

    def assertRefused(self, text: str, message: str) -> None:
        with self.assertRaisesRegex(TagError, message):
            parse_models(text, PATH, SCALE)

    def test_a_list_that_never_says_when_it_was_checked(self):
        self.assertRefused("x-a-1  efforts=high\n", "needs a line saying when it was checked")

    def test_a_list_with_no_model(self):
        self.assertRefused(VERIFIED + "# nothing yet\n", "lists no model")

    def test_a_line_without_its_effort_range(self):
        self.assertRefused(VERIFIED + "x-a-1\n", r":2: x-a-1 needs efforts=")

    def test_a_level_outside_the_ais_scale(self):
        # The vendors' own `none` is a level no scale here carries: `-` is the
        # spelling for "takes none" (strict-reviewer, gate model-picker-3).
        for level in ("ultra", "none", "MINIMAL"):
            with self.subTest(level=level):
                self.assertRefused(VERIFIED + f"x-a-1  efforts=low,{level}\n", f"efforts= {level} — not in this AI's scale")

    def test_a_level_named_twice(self):
        self.assertRefused(VERIFIED + "x-a-1  efforts=low,low\n", "names a level twice")

    def test_an_id_that_is_not_one(self):
        self.assertRefused(VERIFIED + "-x-a  efforts=high\n", r"models.list:2: '-x-a' is not a model id")

    def test_an_unknown_key(self):
        for token in ("retired=2027-01-01", "effort=none"):   # both retired with the formats they belonged to
            with self.subTest(token=token):
                self.assertRefused(VERIFIED + f"x-a-1  efforts=high  {token}\n", rf":2: '{token}' is not one of")

    def test_a_token_without_a_key(self):
        self.assertRefused(VERIFIED + "x-a-1  x-a-2  efforts=high\n", r":2: 'x-a-2' is not one of")

    def test_a_key_without_a_value(self):
        self.assertRefused(VERIFIED + "x-a-1  alias=  efforts=high\n", r":2: 'alias=' is not one of")

    def test_a_single_valued_key_given_twice(self):
        for key, value in (("display", "A"), ("efforts", "high")):
            with self.subTest(key=key):
                self.assertRefused(VERIFIED + f"x-a-1  {key}={value}  {key}={value}\n", f"{key}= appears twice")

    def test_an_alias_that_is_not_an_id(self):
        self.assertRefused(VERIFIED + "x-a-1  alias=/x  efforts=high\n", r"'/x' is not a model id")

    def test_a_spelling_used_twice(self):
        cases = {
            "an id repeated": "x-a-1  efforts=high\nx-a-1  efforts=high\n",
            "an alias naming another id": "x-a-1  efforts=high\nx-b-1  alias=x-a-1  efforts=high\n",
            "one alias on two models": "x-a-1  alias=x-a  efforts=high\nx-b-1  alias=x-a  efforts=high\n",
            "an alias repeating its own id": "x-a-1  alias=x-a-1  efforts=high\n",
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                self.assertRefused(VERIFIED + body, "already names")


class TestEffortFor(unittest.TestCase):
    """Which level a stored pin runs at on a model. No pin: the model's top
    (a PINNED model's default). A pin it takes: the pin. A pin it does not
    take falls back PRESERVING DIRECTION — the nearest level at or below it,
    else the lowest — because a person pins to go BELOW the top, and landing
    on it would do the opposite (bug-investigator, strict-reviewer, gate
    model-picker-3)."""

    OPUS_4_6 = Model(id="x-opus-4-6", efforts=("low", "medium", "high", "max"))     # takes max but not xhigh

    def test_no_pin_runs_the_top(self):
        self.assertEqual(self.OPUS_4_6.effort_for(None, SCALE), "max")

    def test_a_pin_the_model_takes_runs_as_pinned(self):
        self.assertEqual(self.OPUS_4_6.effort_for("medium", SCALE), "medium")

    def test_a_pin_in_a_gap_falls_to_the_nearest_level_below(self):
        self.assertEqual(self.OPUS_4_6.effort_for("xhigh", SCALE), "high")      # never up to max

    def test_a_pin_below_every_level_falls_to_the_lowest(self):
        narrow = Model(id="x-a", efforts=("high", "max"))
        self.assertEqual(narrow.effort_for("low", SCALE), "high")               # the lowest, not the top

    def test_a_pin_no_scale_knows_has_no_direction_so_runs_the_top(self):
        self.assertEqual(self.OPUS_4_6.effort_for("ultra", SCALE), "max")

    def test_a_model_that_takes_none_sends_nothing_whatever_the_pin(self):
        # Never the string "none", never an error on an empty range
        # (researcher, gate model-picker-3).
        effortless = Model(id="x-haiku", efforts=())
        for pin in (None, "low", "ultra"):
            with self.subTest(pin=pin):
                self.assertIsNone(effortless.effort_for(pin, SCALE))


class TestModelSpells(unittest.TestCase):
    def test_its_id_and_each_alias_but_nothing_else(self):
        model = Model(id="x-a-1-20250101", aliases=("x-a-1",))
        self.assertTrue(model.spells("x-a-1-20250101"))
        self.assertTrue(model.spells("x-a-1"))
        self.assertFalse(model.spells("x-a"))           # a prefix is not a spelling
        self.assertFalse(model.spells("X-A-1"))         # nor another case


class TestModelLabel(unittest.TestCase):
    """How a model reads after its AI's name in the picker: best-effort from
    the id, `display=` overriding."""

    def test_the_prefix_goes_words_capitalise_versions_dot(self):
        self.assertEqual(model_label(Model(id="claude-opus-5-5"), "claude-"), "Opus-5.5")

    def test_a_single_version_group_stays_bare(self):
        self.assertEqual(model_label(Model(id="claude-opus-5"), "claude-"), "Opus-5")

    def test_an_already_dotted_version_is_left_as_written(self):
        self.assertEqual(model_label(Model(id="gemini-3.8-flash"), "gemini-"), "3.8-Flash")

    def test_a_date_stamp_is_dropped_so_a_snapshot_reads_as_its_alias(self):
        self.assertEqual(model_label(Model(id="claude-haiku-4-5-20251001"), "claude-"), "Haiku-4.5")
        self.assertEqual(model_label(Model(id="grok-4.20-0309-non-reasoning"), "grok-"), "4.20-Non-Reasoning")
        self.assertEqual(model_label(Model(id="grok-4.20-multi-agent-0309"), "grok-"), "4.20-Multi-Agent")

    def test_a_display_wins_over_the_derived_label(self):
        self.assertEqual(model_label(Model(id="claude-opus-5-5", display="Opus 5.5"), "claude-"), "Opus 5.5")

    def test_an_id_without_the_prefix_is_labelled_whole(self):
        self.assertEqual(model_label(Model(id="o5-mini"), "gpt-"), "O5-Mini")

    def test_an_id_that_is_all_date_labels_as_itself(self):
        self.assertEqual(model_label(Model(id="20250101"), ""), "20250101")


class TestStaleModel(unittest.TestCase):
    """A stored pick its AI's list does not carry — deleted when the vendor
    retired it (operator, 2026-09-29), or never there — in the words every
    notice of it uses."""

    def test_it_says_the_list_does_not_carry_it_and_is_labelled_off_its_spelling(self):
        stale = StaleModel("y-b-2")
        self.assertEqual(stale.why("⟪X⟫"), "not among ⟪X⟫'s models")
        self.assertEqual(stale.shown, Model(id="y-b-2"))


class TestShippedLists(unittest.TestCase):
    """Every agents/ai/*/models.list, as the scan reads it — held to what the
    label rule promises, over every shipped id rather than a chosen few."""

    ais: list[Ai]

    @classmethod
    def setUpClass(cls) -> None:
        cls.ais = list(scan_all(AGENTS_DIR).ais.values())

    def test_every_ai_offers_models(self):
        for ai in self.ais:
            with self.subTest(ai=ai.name):
                self.assertTrue(ai.models)

    def test_every_range_lies_in_its_ais_scale_in_its_order(self):
        for ai in self.ais:
            for model in ai.models:
                with self.subTest(ai=ai.name, model=model.id):
                    self.assertTrue(set(model.efforts) <= set(ai.scale))
                    self.assertEqual(list(model.efforts), sorted(model.efforts, key=ai.scale.index))

    def test_the_known_gaps_and_empties_are_as_the_vendors_publish(self):
        # Spot checks against researcher's 2026-09-28 reads: a gap (4.6 takes
        # max, not xhigh), a model that takes none, and a launcher-withheld one.
        by_id = {m.id: m for ai in self.ais for m in ai.models}
        self.assertEqual(by_id["claude-opus-4-6"].efforts, ("low", "medium", "high", "max"))
        self.assertEqual(by_id["claude-haiku-4-5-20251001"].efforts, ())
        self.assertEqual(by_id["gemini-3.8-flash"].efforts, ("LOW", "MEDIUM", "HIGH"))     # no MINIMAL
        self.assertEqual(by_id["gemini-2.5-pro"].efforts, ())                              # the launcher sends none
        self.assertEqual(by_id["gpt-5.5"].top_effort, "xhigh")

    def test_every_label_is_a_clean_version_of_its_id(self):
        shape = re.compile(r"[A-Z0-9][A-Za-z0-9.]*(-[A-Z0-9][A-Za-z0-9.]*)*")
        for ai in self.ais:
            for model in ai.models:
                label = model_label(model, ai.model_prefix)
                with self.subTest(ai=ai.name, model=model.id, label=label):
                    self.assertRegex(label, shape)                              # capitalised words, dotted versions
                    self.assertFalse(label.lower().startswith(ai.model_prefix.rstrip("-")))   # the vendor's prefix is gone
                    self.assertNotRegex(label, r"(^|-)\d{4,}(-|$)")             # no date stamp
                    self.assertNotIn("..", label)

    def test_labels_tell_an_ais_models_apart(self):
        for ai in self.ais:
            labels = [model_label(m, ai.model_prefix) for m in ai.models]
            with self.subTest(ai=ai.name):
                self.assertEqual(len(labels), len(set(labels)))

    def test_representative_labels_read_as_a_person_would_write_them(self):
        # One id per shape the rule handles, on real data: dash-grouped
        # versions, a dated snapshot, a dotted version, a family-less id, a
        # date stamp mid-id.
        by_id = {m.id: (ai, m) for ai in self.ais for m in ai.models}
        expected = {
            "claude-opus-5-5": "⟪Claude:Opus-5.5⟫",
            "claude-haiku-4-5-20251001": "⟪Claude:Haiku-4.5⟫",
            "gemini-3.8-flash": "⟪Gemini:3.8-Flash⟫",
            "gpt-6-astra": "⟪ChatGPT:6-Astra⟫",
            "grok-4.20-0309-non-reasoning": "⟪Grok:4.20-Non-Reasoning⟫",
        }
        for model_id, label in expected.items():
            ai, model = by_id[model_id]
            with self.subTest(model=model_id):
                self.assertEqual(ai.label_with(model), label)


if __name__ == "__main__":
    unittest.main()
