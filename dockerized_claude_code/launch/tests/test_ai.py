"""Tests for the AI kind's shipped members and the launch.ai package — the
AIs as tag members of agents/ai/ (manifests, capability standards, settings
vocabulary), the rendering of every engine's budget in every AI's words, the
AI-neutral engine order, and the code half: the active AI key, the harness
adapters, and the rule that no production module outside launch/ai spells a
harness's words. Fixture-tree tests of the kind's validation live in
test_tags (TestAiKind)."""

import ast
import dataclasses
import unittest
from pathlib import Path

from launch import paths
from launch.ai import (
    CLAUDE_CODE, DEFAULT_AI_KEY, HARNESSES, active_ai_key, active_harness, harness_for, refusal_for,
    set_active_ai,
)
from launch.paths import AGENTS_DIR
from launch.tags import BEST, CHEAPEST, scan_all, sorted_ais, sorted_engines, sorted_standards
from launch.tags.ai import load_standards
from launch.tags import engine as engine_module

REGISTRY = scan_all(paths.AGENTS_DIR)


class TestAiMembers(unittest.TestCase):
    """agents/ai/ — the four launch partners, each a complete member."""

    def test_the_four_launch_partners_are_members(self):
        self.assertEqual(set(REGISTRY.ais), {"claude", "gemini", "chatgpt", "grok"})
        self.assertEqual({ai.shortname for ai in REGISTRY.ais.values()}, {"Claude", "Gemini", "ChatGPT", "Grok"})

    def test_claude_is_the_default_and_the_code_agrees(self):
        # The tree marks the default; the code's constant must name the same
        # member, or `active_ai_key()` would run an AI the tree does not default to.
        self.assertEqual(REGISTRY.default_ai.name, "claude")
        self.assertEqual(DEFAULT_AI_KEY, REGISTRY.default_ai.name)
        self.assertEqual(active_ai_key(), DEFAULT_AI_KEY)

    def test_every_member_names_its_vendor_harness_and_colours(self):
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertTrue(ai.vendor and ai.harness)
                self.assertRegex(ai.fg, r"^#[0-9a-f]{6}$")
                self.assertRegex(ai.bg, r"^#[0-9a-f]{6}$")
                self.assertEqual(ai.style, f"fg:{ai.fg} bg:{ai.bg}")
                # searchable by company: the vendor's name is in the prose too
                company = ai.vendor.split(" ")[0].lower()
                self.assertIn(company, ai.full_description.lower())

    def test_labels_wear_the_kinds_parentheses(self):
        for ai in REGISTRY.ais.values():
            self.assertEqual(ai.label, f"⟪{ai.shortname}⟫")

    def test_every_member_answers_every_standard_cheapest_first(self):
        # The shared file's quarters between the two ends, in rising order —
        # the same tuple for every member.
        declared = (CHEAPEST, *(s.key for s in load_standards(AGENTS_DIR / "ai")), BEST)
        self.assertEqual(list(declared), sorted_standards(declared))
        self.assertGreaterEqual(len(declared), 4, "at least two dated standards between the ends")
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertEqual(ai.standards, declared)
                for key, tier in ai.tiers:
                    self.assertTrue(tier.model, f"{ai.name} {key} has no model")
                    if tier.effort is not None and ai.scale:
                        self.assertIn(tier.effort, ai.scale)

    def test_the_dated_standards_are_quarters_whose_index_rises(self):
        standards = load_standards(AGENTS_DIR / "ai")
        self.assertEqual([s.key for s in standards], sorted_standards(s.key for s in standards))
        for earlier, later in zip(standards, standards[1:]):
            self.assertLess(earlier.index, later.index, f"{later.key} must beat {earlier.key}")
        for s in standards:
            self.assertRegex(s.key, r"^\d{4}Q[1-4]$")
            self.assertTrue(s.set_by)
        self.assertFalse(standards[-1].estimated, "the newest standard is a measured record, not an estimate")

    def test_every_engine_names_a_standard_the_ais_answer(self):
        for engine in REGISTRY.engines.values():
            with self.subTest(engine=engine.name):
                self.assertIn(engine.budget.standard, REGISTRY.default_ai.standards)


class TestAiOrder(unittest.TestCase):
    def test_the_default_leads_then_by_name(self):
        ordered = [ai.name for ai in sorted_ais(REGISTRY.ais.values())]
        self.assertEqual(ordered[0], REGISTRY.default_ai.name)
        self.assertEqual(ordered[1:], sorted(ordered[1:]))
        self.assertEqual(set(ordered), set(REGISTRY.ais))


class TestRendering(unittest.TestCase):
    """An engine's budget × an AI's files → that AI's native settings."""

    def test_every_engine_renders_on_every_ai_with_its_model(self):
        for engine in REGISTRY.engines.values():
            for ai in REGISTRY.ais.values():
                with self.subTest(engine=engine.name, ai=ai.name):
                    rendering = ai.render(engine.budget)
                    self.assertTrue(rendering.settings)
                    self.assertIn(ai.tier(engine.budget.standard).model, rendering.map.values())

    def test_claude_renders_the_budgets_as_the_former_env_files_did(self):
        # Behaviour parity with the claude.conf files the budgets replaced
        # (2026-09-13): the same keys, the same values — with one deliberate
        # deviation: golem sets no CLAUDE_CODE_EFFORT_LEVEL any more, because
        # Haiku 4.5 takes no effort level (Anthropic's models overview, checked
        # by the researcher 2026-09-14); its thinking switch is the knob.
        claude = REGISTRY.ais["claude"]
        render = lambda name: claude.render(REGISTRY.engines[name].budget).map
        self.assertEqual(render("default"), {"ANTHROPIC_MODEL": "claude-fable-5-1", "CLAUDE_CODE_EFFORT_LEVEL": "max",
                                             "CLAUDE_CODE_ENABLE_THINKING": "1"})
        self.assertEqual(render("golem"), {
            "ANTHROPIC_MODEL": "claude-haiku-4-5",
            "CLAUDE_CODE_ENABLE_THINKING": "0", "MAX_THINKING_TOKENS": "0",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "DISABLE_PROMPT_CACHING": "1",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1", "CLAUDE_CODE_DISABLE_CRON": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        self.assertEqual(render("poet"), {"ANTHROPIC_MODEL": "claude-sonnet-5", "CLAUDE_CODE_EFFORT_LEVEL": "medium",
                                          "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0", "ENABLE_TOOL_SEARCH": "true",
                                          "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "18000"})
        self.assertEqual(render("reliable")["ANTHROPIC_MODEL"], "claude-opus-5")
        self.assertEqual(render("quick")["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "21600")

    def test_unmapped_purposes_are_reported_never_invented(self):
        researcher = REGISTRY.engines["researcher"].budget
        codex = REGISTRY.ais["chatgpt"].render(researcher)
        self.assertIn("max_output_tokens", codex.unmapped)       # Codex has no output cap
        self.assertIn("compact_at_percent", codex.unmapped)      # absolute tokens only
        self.assertFalse(any("output" in key.lower() and "tool" not in key for key in codex.map))
        gemini = REGISTRY.ais["gemini"].render(REGISTRY.engines["poet"].budget)
        self.assertEqual(gemini.unmapped, ("tool_search.on",))

    def test_unit_conversions_happen_once_at_the_boundary(self):
        gemini = REGISTRY.ais["gemini"].render(REGISTRY.engines["researcher"].budget).map
        self.assertEqual(gemini["tools.truncateToolOutputThreshold"], "400000")   # 100000 tokens × 4 characters
        self.assertEqual(gemini["model.compressionThreshold"], "0.6")             # 60 % → a fraction


class TestEngineOrder(unittest.TestCase):
    def test_engines_sort_by_standard_then_output_then_name(self):
        # The contract as invariants — AI-neutral: rank never rises down the
        # list; equal rank → bigger output budget first; equal both → name.
        ordered = sorted_engines(REGISTRY.engines.values())
        keys = [(e.budget.rank, e.budget.max_output_tokens or 0, e.name) for e in ordered]
        for (r1, o1, n1), (r2, o2, n2) in zip(keys, keys[1:]):
            self.assertGreaterEqual(r1, r2)
            if r1 == r2:
                self.assertGreaterEqual(o1, o2)
                if o1 == o2:
                    self.assertLess(n1, n2)
        self.assertEqual(ordered[0].budget.standard, "best")
        self.assertEqual(ordered[-1].name, "golem")
        self.assertEqual(engine_module.standard_rank(None), -1)


class TestHarnessRecord(unittest.TestCase):
    """The adapter's data half: one record per harness, reached through the
    call-time accessors, never through a fallback to Claude's names."""

    def test_claude_code_defines_every_field(self):
        for field in dataclasses.fields(CLAUDE_CODE):
            with self.subTest(field=field.name):
                value = getattr(CLAUDE_CODE, field.name)
                self.assertIsNotNone(value)
                self.assertTrue(value, f"{field.name} is empty")

    def test_the_registry_is_keyed_by_the_ai_each_record_serves(self):
        for key, harness in HARNESSES.items():
            self.assertEqual(harness.ai_key, key)
            self.assertIn(key, REGISTRY.ais, "a harness must serve a tree member")
            self.assertEqual(harness.name, REGISTRY.ais[key].harness, "the tree names the same CLI")
        self.assertIs(harness_for("claude"), CLAUDE_CODE)

    def test_active_harness_follows_the_active_key(self):
        self.assertEqual(active_harness().ai_key, active_ai_key())
        try:
            set_active_ai("gemini")
            with self.assertRaises(LookupError) as caught:
                active_harness()
            self.assertIn("gemini", str(caught.exception))
        finally:
            set_active_ai(None)
        self.assertEqual(active_ai_key(), DEFAULT_AI_KEY)

    def test_an_ai_without_an_adapter_raises_rather_than_borrowing_claudes_names(self):
        unadapted = [name for name in REGISTRY.ais if name not in HARNESSES]
        self.assertTrue(unadapted, "every AI has an adapter now — retire this test's premise")
        for name in unadapted:
            with self.subTest(ai=name), self.assertRaises(LookupError):
                harness_for(name)


class TestRefusal(unittest.TestCase):
    """refusal_for — the one message for an instance (run.py) or a cluster
    member (launching.refusal) whose AI has no adapter: it names the AI, says
    the launcher can describe but not run it, and points at the fix."""

    def test_an_adapted_ai_is_not_refused(self):
        for key in HARNESSES:
            self.assertIsNone(refusal_for(key, REGISTRY.ais[key].label))

    def test_an_unadapted_ai_is_refused_by_label_with_the_way_out(self):
        for ai in (a for a in REGISTRY.ais.values() if a.name not in HARNESSES):
            with self.subTest(ai=ai.name):
                reason = refusal_for(ai.name, ai.label)
                self.assertIsNotNone(reason)
                self.assertIn(ai.label, reason)
                self.assertIn("plans/adding_an_ai.md", reason)
                self.assertIn("F2", reason)


# Claude Code's words, each of which the tree once spelled at its point of use
# and now reads from the adapter record (launch/ai/claude_code.py) or from
# agents/ai/claude/*. A production module spelling one again would be a second
# definition that a switch of AI leaves behind. Whole-literal matches only, so
# `.claude` does not flag `.claude-agents` (a launcher name, §14).
HARNESS_WORDS = {
    "claude", "--continue", "--effort", "stream-json", ".claude", "CLAUDE.md", "history.jsonl",
    "projects", ".credentials.json", ".claude.json", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_NAME",
    "CLAUDE_CODE_EFFORT_LEVEL", "ANTHROPIC_MODEL", "api.anthropic.com", "console.anthropic.com",
}


def _code_string_literals(path: Path) -> list[tuple[int, str]]:
    """Every string literal in a module's CODE — docstrings excluded, comments
    never parsed — as (line, text). f-string pieces count as literals."""
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    return [(node.lineno, node.value) for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings]


class TestTheAdapterIsTheOnlyDefinition(unittest.TestCase):
    def test_no_production_module_spells_a_harness_word(self):
        root = Path(engine_module.__file__).resolve().parents[1]
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if {"tests", "benchmark"} & set(path.relative_to(root).parts[:-1]) or path.parent == root / "ai":
                continue
            offenders += [f"{path.relative_to(root)}:{line} {text!r}"
                          for line, text in _code_string_literals(path) if text in HARNESS_WORDS]
        self.assertEqual(offenders, [], "read these from launch.ai (active_harness) or the AI's tree files instead")


class TestConsumersReadTheAdapter(unittest.TestCase):
    """The re-pointed call sites produce the same values they always did, now
    from the record — so a switch of AI moves them all at once."""

    def test_effort_args_take_the_instances_effort_word(self):
        from launch.docker_config import effort_args
        self.assertEqual(effort_args("max", []), [active_harness().effort_flag, "max"])
        self.assertEqual(effort_args("max", [f"{active_harness().effort_flag}=low"]), [])
        self.assertEqual(effort_args(None, []), [])

    def test_the_cluster_member_command_is_the_harness_binary(self):
        from launch.cluster import launch_plan
        self.assertEqual(launch_plan.default_member_command(), (active_harness().binary,))

    def test_the_firewalls_critical_hosts_are_the_harnesss(self):
        from launch.firewall import resolver
        self.assertEqual(resolver._critical_hosts(), active_harness().critical_hosts)

    def test_paths_carry_the_harnesss_filenames(self):
        self.assertEqual(paths.CLAUDE_CONFIG_IN_CONTAINER.name, CLAUDE_CODE.config_dir_name)
        self.assertEqual(paths.state_md_path(Path("/s")).name, CLAUDE_CODE.persona_filename)
        self.assertEqual(paths.state_history_path(Path("/s")).name, CLAUDE_CODE.history_filename)
        self.assertEqual(paths.ACCOUNT_FILE.name, CLAUDE_CODE.account_filename)
        self.assertEqual(paths.CREDENTIALS_FILE.name, CLAUDE_CODE.credentials_filename)


if __name__ == "__main__":
    unittest.main()
