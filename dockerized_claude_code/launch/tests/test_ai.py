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
    ADAPTERS, CLAUDE_CODE, DEFAULT_HARNESS_KEY, active_adapter, active_harness_key, adapter_for, refusal_for,
    set_active_harness,
)
from launch.paths import AGENTS_DIR
from launch.tags import BEST, CHEAPEST, scan_all, sorted_ais, sorted_engines, sorted_harnesses, sorted_standards
from launch.tags.ai import load_standards
from launch.tags import engine as engine_module

REGISTRY = scan_all(paths.AGENTS_DIR)


class TestAiMembers(unittest.TestCase):
    """agents/ai/ — the four launch partners, each a complete member."""

    def test_the_four_launch_partners_are_members(self):
        self.assertEqual(set(REGISTRY.ais), {"claude", "gemini", "chatgpt", "grok"})
        self.assertEqual({ai.shortname for ai in REGISTRY.ais.values()}, {"Claude", "Gemini", "ChatGPT", "Grok"})

    def test_claude_is_the_default_and_the_code_agrees(self):
        # The tree marks the default AI; the code's default harness must be
        # that AI's default harness, or `active_harness_key()` would run a CLI
        # the tree does not default to.
        self.assertEqual(REGISTRY.default_ai.name, "claude")
        self.assertEqual(DEFAULT_HARNESS_KEY, REGISTRY.default_ai.harness)
        self.assertEqual(active_harness_key(), DEFAULT_HARNESS_KEY)

    def test_every_member_names_its_vendor_harness_and_colours(self):
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertTrue(ai.vendor)
                self.assertIn(ai.harness, REGISTRY.harnesses)                 # its default harness is a member …
                self.assertTrue(REGISTRY.harnesses[ai.harness].runs(ai.name))  # … that runs it
                self.assertRegex(ai.fg, r"^#[0-9a-f]{6}$")
                self.assertRegex(ai.bg, r"^#[0-9a-f]{6}$")
                self.assertEqual(ai.style, f"fg:{ai.fg} bg:{ai.bg}")
                # searchable by company: the vendor's name is in the prose too
                company = ai.vendor.split(" ")[0].lower()
                self.assertIn(company, ai.full_description.lower())

    def test_labels_wear_the_kinds_parentheses(self):
        for ai in REGISTRY.ais.values():
            self.assertEqual(ai.label, f"⟪{ai.shortname}⟫")

    def test_every_member_names_its_vendors_key_variable(self):
        # The variable every harness reads for that AI — what credentials/keys/<ai>.env must define.
        expected = {"claude": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY", "chatgpt": "OPENAI_API_KEY", "grok": "XAI_API_KEY"}
        self.assertEqual({ai.name: ai.key_env for ai in REGISTRY.ais.values()}, expected)

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
                self.assertIn(engine.budget.effort_tier, REGISTRY.default_ai.standards)


class TestAiOrder(unittest.TestCase):
    def test_the_default_leads_then_by_name(self):
        ordered = [ai.name for ai in sorted_ais(REGISTRY.ais.values())]
        self.assertEqual(ordered[0], REGISTRY.default_ai.name)
        self.assertEqual(ordered[1:], sorted(ordered[1:]))
        self.assertEqual(set(ordered), set(REGISTRY.ais))


class TestRendering(unittest.TestCase):
    """An engine's budget × an AI's tier × a harness's knobs → that CLI's native settings."""

    def test_every_engine_renders_on_every_ai_in_every_harness_that_runs_it(self):
        for engine in REGISTRY.engines.values():
            for ai in REGISTRY.ais.values():
                for name in REGISTRY.harnesses_running(ai.name):
                    with self.subTest(engine=engine.name, ai=ai.name, harness=name):
                        rendering = REGISTRY.harnesses[name].render(engine.budget, ai)
                        self.assertTrue(rendering.settings)
                        model = ai.tier(engine.budget.effort_tier).model
                        self.assertTrue(any(model in value for value in rendering.map.values()), rendering.map)

    def test_claude_renders_the_budgets_as_the_former_env_files_did(self):
        # Behaviour parity with the claude.conf files the budgets replaced
        # (2026-09-13): the same keys, the same values — with one deliberate
        # deviation: golem sets no CLAUDE_CODE_EFFORT_LEVEL any more, because
        # Haiku 4.5 takes no effort level (Anthropic's models overview, checked
        # by the researcher 2026-09-14); its thinking switch is the knob.
        claude = REGISTRY.ais["claude"]
        render = lambda name: REGISTRY.harnesses["claude-code"].render(REGISTRY.engines[name].budget, claude).map
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
        codex = REGISTRY.harnesses["codex-cli"].render(researcher, REGISTRY.ais["chatgpt"])
        self.assertIn("max_output_tokens", codex.unmapped)       # Codex has no output cap
        self.assertIn("compact_at_percent", codex.unmapped)      # absolute tokens only
        self.assertFalse(any("output" in key.lower() and "tool" not in key for key in codex.map))
        gemini = REGISTRY.harnesses["gemini-cli"].render(REGISTRY.engines["poet"].budget, REGISTRY.ais["gemini"])
        self.assertEqual(gemini.unmapped, ("tool_search.on",))

    def test_unit_conversions_happen_once_at_the_boundary(self):
        gemini = REGISTRY.harnesses["gemini-cli"].render(REGISTRY.engines["researcher"].budget, REGISTRY.ais["gemini"]).map
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
        self.assertEqual(ordered[0].budget.effort_tier, "best")
        self.assertEqual(ordered[-1].name, "golem")
        self.assertEqual(engine_module.effort_tier_rank(None), -1)


class TestHarnessMembers(unittest.TestCase):
    """agents/harness/ — the four launch partners' CLIs, each a complete
    member wearing ⟦ ⟧, each running its AI."""

    VENDOR_CLIS = {"claude-code", "gemini-cli", "codex-cli", "grok-build"}
    OPEN_HARNESSES = {"opencode", "openclaw", "hermes"}

    def test_the_seven_clis_are_members(self):
        self.assertEqual(set(REGISTRY.harnesses), self.VENDOR_CLIS | self.OPEN_HARNESSES)
        self.assertEqual({h.shortname for h in REGISTRY.harnesses.values()},
                         {"ClaudeCode", "GeminiCLI", "CodexCLI", "GrokBuild", "OpenCode", "OpenClaw", "Hermes"})

    def test_a_vendors_cli_runs_its_ai_and_an_open_harness_runs_all_four(self):
        for name in self.VENDOR_CLIS:
            self.assertEqual(len(REGISTRY.harnesses[name].ais), 1, name)
        for name in self.OPEN_HARNESSES:
            with self.subTest(harness=name):
                self.assertEqual(set(REGISTRY.harnesses[name].ais), set(REGISTRY.ais))
                self.assertTrue(REGISTRY.harnesses[name].needs_providers, "a multi-AI CLI spells the model with a provider slug")
                self.assertEqual(set(dict(REGISTRY.harnesses[name].providers)), set(REGISTRY.ais))

    def test_labels_wear_the_kinds_parentheses(self):
        for harness in REGISTRY.harnesses.values():
            self.assertEqual(harness.label, f"⟦{harness.shortname}⟧")

    def test_every_member_names_its_vendor_ais_binary_and_package(self):
        for harness in REGISTRY.harnesses.values():
            with self.subTest(harness=harness.name):
                self.assertTrue(harness.vendor and harness.binary and harness.package)
                self.assertTrue(harness.ais)
                for ai in harness.ais:
                    self.assertIn(ai, REGISTRY.ais)
                company = harness.vendor.split(" ")[0].lower()
                self.assertIn(company, harness.full_description.lower())    # searchable by company, like the AIs

    def test_each_ai_declares_where_its_plan_may_be_spent(self):
        # Vendors differ, so this is per-AI data: Anthropic gates Pro/Max to
        # Claude Code while OpenAI and xAI let their sign-ins into harnesses
        # they did not write (plans/credentials.md carries the sources). Every
        # named harness must run that AI, and its own CLI is always among them.
        self.assertEqual(REGISTRY.ais["claude"].plan_harnesses, ("claude-code",))
        self.assertLess(len(REGISTRY.ais["claude"].plan_harnesses), len(REGISTRY.ais["grok"].plan_harnesses))
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertIn(ai.harness, ai.plan_harnesses)
                for name in ai.plan_harnesses:
                    self.assertTrue(REGISTRY.harnesses[name].runs(ai.name))

    def test_the_published_fallback_floors_are_declared_and_the_others_left_empty(self):
        # Where a vendor publishes what an API key alone gets, the AI carries
        # its words; where none does, the field stays empty rather than
        # guessing (checked 2026-09-17 — plans/credentials.md has the four
        # rows, including the two negatives and why they are negative).
        self.assertIn("no free tier", REGISTRY.ais["claude"].key_free_tier)
        self.assertIn("250 req/day", REGISTRY.ais["gemini"].key_free_tier)
        for ai in REGISTRY.ais.values():   # phrase-sized: the form quotes them inline
            self.assertLess(len(ai.key_free_tier), 60, ai.name)
        self.assertEqual(REGISTRY.ais["chatgpt"].key_free_tier, "")
        self.assertEqual(REGISTRY.ais["grok"].key_free_tier, "")
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertNotRegex(ai.key_free_tier, r"orders of magnitude")   # no folklore in the tree

    def test_only_claude_carries_a_field_report_and_it_names_its_provenance(self):
        # The one line in the AI shelf that is field evidence rather than a
        # vendor's published term (operator, 2026-09-17: two first-hand
        # accounts). It must say who saw it and when, so the tree never holds
        # a bare number — and no other AI has one to declare.
        report = REGISTRY.ais["claude"].foreign_harness_report
        self.assertIn("~50x", report)
        # It is quoted VERBATIM as the warning's first line, so the sentence
        # must hedge and date itself — the scan refuses one that does not.
        self.assertRegex(report, r"report|alleg|observ|unverified|anecdot")
        self.assertRegex(report, r"20\d\d")
        self.assertLess(len(report), 130)
        for name in ("gemini", "chatgpt", "grok"):
            self.assertEqual(REGISTRY.ais[name].foreign_harness_report, "", name)

    def test_every_ais_default_harness_is_its_vendors_cli(self):
        # The vendor's own CLI is the default; the open harnesses are choices.
        for ai in REGISTRY.ais.values():
            with self.subTest(ai=ai.name):
                self.assertIn(ai.harness, self.VENDOR_CLIS)
                self.assertIn(ai.harness, REGISTRY.harnesses_running(ai.name))
                self.assertEqual(set(REGISTRY.harnesses_running(ai.name)), {ai.harness} | self.OPEN_HARNESSES)

    def test_the_default_ais_harnesses_lead_the_order(self):
        # Every harness that runs the default AI comes first, by name — the
        # vendor's CLI among them — then the rest by name.
        ordered = sorted_harnesses(REGISTRY.harnesses.values(), REGISTRY.default_ai.name)
        running = [h.name for h in ordered if h.runs(REGISTRY.default_ai.name)]
        self.assertEqual([h.name for h in ordered[:len(running)]], sorted(running))
        self.assertEqual([h.name for h in ordered[len(running):]], sorted(h.name for h in ordered[len(running):]))


class TestAdapterRecord(unittest.TestCase):
    """The adapter's data half: one record per harness the launcher can run,
    keyed by the harness member it implements, reached through the call-time
    accessors, never through a fallback to Claude Code's names."""

    def test_claude_code_defines_every_field(self):
        for field in dataclasses.fields(CLAUDE_CODE):
            with self.subTest(field=field.name):
                value = getattr(CLAUDE_CODE, field.name)
                self.assertIsNotNone(value)
                self.assertTrue(value, f"{field.name} is empty")

    def test_the_registry_is_keyed_by_the_harness_member_each_record_implements(self):
        for key, adapter in ADAPTERS.items():
            self.assertEqual(adapter.key, key)
            self.assertIn(key, REGISTRY.harnesses, "an adapter must implement a tree member")
            member = REGISTRY.harnesses[key]
            self.assertEqual(adapter.name, member.fullname, "the tree names the same CLI")
            self.assertEqual(adapter.binary, member.binary, "the tree names the same executable")
        self.assertIs(adapter_for("claude-code"), CLAUDE_CODE)

    def test_active_adapter_follows_the_active_key(self):
        self.assertEqual(active_adapter().key, active_harness_key())
        try:
            set_active_harness("gemini-cli")
            with self.assertRaises(LookupError) as caught:
                active_adapter()
            self.assertIn("gemini-cli", str(caught.exception))
        finally:
            set_active_harness(None)
        self.assertEqual(active_harness_key(), DEFAULT_HARNESS_KEY)

    def test_a_harness_without_an_adapter_raises_rather_than_borrowing_claude_codes_names(self):
        unadapted = [name for name in REGISTRY.harnesses if name not in ADAPTERS]
        self.assertTrue(unadapted, "every harness has an adapter now — retire this test's premise")
        for name in unadapted:
            with self.subTest(harness=name), self.assertRaises(LookupError):
                adapter_for(name)


class TestLoginKeys(unittest.TestCase):
    def test_the_login_keys_are_claude_codes_private_shape(self):
        # The CLI documents neither: `.credentials.json` carries the OAuth
        # tokens under `claudeAiOauth`, `.claude.json` the account under
        # `oauthAccount` (probed on Claude Code 2.1.266). Every fixture in the
        # suite writes these, and the migration, the launch notice and the
        # audit key on them — so a rename here is a decision, not a drift.
        self.assertEqual({f.role: f.login_key for f in CLAUDE_CODE.auth_files},
                         {"credentials": "claudeAiOauth", "account": "oauthAccount"})


class TestRefusal(unittest.TestCase):
    """refusal_for — the one message for an instance (run.py, the quickie) or
    a cluster member (launching.refusal) whose harness has no adapter: it
    names the harness, says the launcher can describe but not run it, and
    points at the fix."""

    def test_an_adapted_harness_is_not_refused(self):
        for key in ADAPTERS:
            self.assertIsNone(refusal_for(key, REGISTRY.harnesses[key].label))

    def test_an_unadapted_harness_is_refused_by_label_with_the_way_out(self):
        for harness in (h for h in REGISTRY.harnesses.values() if h.name not in ADAPTERS):
            with self.subTest(harness=harness.name):
                reason = refusal_for(harness.name, harness.label)
                self.assertIsNotNone(reason)
                self.assertIn(harness.label, reason)
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
        self.assertEqual(offenders, [], "read these from launch.ai (active_adapter) or the tree files instead")


class TestConsumersReadTheAdapter(unittest.TestCase):
    """The re-pointed call sites produce the same values they always did, now
    from the record — so a switch of AI moves them all at once."""

    def test_effort_args_take_the_instances_effort_word(self):
        from launch.docker_config import effort_args
        self.assertEqual(effort_args("max", []), [active_adapter().effort_flag, "max"])
        self.assertEqual(effort_args("max", [f"{active_adapter().effort_flag}=low"]), [])
        self.assertEqual(effort_args(None, []), [])

    def test_the_cluster_member_command_is_the_harness_binary(self):
        from launch.cluster import launch_plan
        self.assertEqual(launch_plan.default_member_command(), (active_adapter().binary,))

    def test_the_firewalls_critical_hosts_are_the_harnesss(self):
        from launch.firewall import resolver
        self.assertEqual(resolver._critical_hosts(), active_adapter().critical_hosts)

    def test_paths_carry_the_harnesss_filenames(self):
        # The login files come from the adapter's auth_files, mounted per
        # launch shape: a solo instance keeps the default config root, so the
        # `config`-anchored file goes there and the `account`-anchored one to
        # HOME; a cluster member's relocated root takes both.
        solo = dict(paths.auth_file_mounts(CLAUDE_CODE, config="/home/claude/.claude", relocated=False))
        member = dict(paths.auth_file_mounts(CLAUDE_CODE, config="/cluster/members/x", relocated=True))
        names = {f.name for f in CLAUDE_CODE.auth_files}
        self.assertEqual({Path(s).name for s in solo}, names)
        self.assertTrue(all(Path(s).parent == paths.credentials_dir(CLAUDE_CODE.key) for s in solo))
        creds, account = CLAUDE_CODE.auth_file("credentials"), CLAUDE_CODE.auth_file("account")
        self.assertEqual(solo[str(paths.credentials_dir(CLAUDE_CODE.key) / creds.name)], f"/home/claude/.claude/{creds.name}")
        self.assertEqual(solo[str(paths.credentials_dir(CLAUDE_CODE.key) / account.name)], f"/home/claude/{account.name}")
        self.assertEqual(member[str(paths.credentials_dir(CLAUDE_CODE.key) / account.name)], f"/cluster/members/x/{account.name}")
        self.assertTrue(all(f.mode == "rw" for f in CLAUDE_CODE.auth_files), "the CLI refreshes both in place")
        self.assertEqual(paths.auth_file_path(CLAUDE_CODE, "account"), paths.credentials_dir(CLAUDE_CODE.key) / account.name)
        self.assertIsNone(paths.auth_file_path(CLAUDE_CODE, "env"))
        self.assertEqual(paths.CLAUDE_CONFIG_IN_CONTAINER.name, CLAUDE_CODE.config_dir_name)
        self.assertEqual(paths.state_md_path(Path("/s")).name, CLAUDE_CODE.persona_filename)
        self.assertEqual(paths.state_history_path(Path("/s")).name, CLAUDE_CODE.history_filename)


if __name__ == "__main__":
    unittest.main()
