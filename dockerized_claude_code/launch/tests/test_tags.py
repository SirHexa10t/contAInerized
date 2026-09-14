"""Tests for the launch.tags package (P0 of the tags rewrite).

Everything here runs against **fixture trees** built in temp dirs — the
scanners take an `agents_dir` argument, so no test touches the real repo
`agents/` (which still holds the pre-rewrite layout until P1). Coverage:
per-kind discovery, tree-derived requirements, hidden-layer claiming, engine
conf inheritance, policy-fragment merge rules, `.lego` parsing/validation,
manifest parsing edge cases, and the registry's cross-cutting validation.
"""

import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from launch import tags
from launch.paths import COMMANDS_DIR_NAME
from launch.tags import (
    AgentBuild, Budget, is_standard, rank_of, sorted_standards, Engine, Instance, Policy, PolicyStance, Profession, Registry,
    Specialty, TagError, ToolkitEntry, addendums, image_chain, load_lego, merge_fragments,
    migrations, resolve_build, scan_all, sorted_engines, store,
)
from launch.tags.ai import Ai
from launch.tags.identity import FORBIDDEN_IN_LABELS, label_error, suggested_label


class TestSuggestedLabel(unittest.TestCase):
    """`suggested_label` — the rule as a FIXER, for a form's auto-filled default
    only: a dotted project dir must never pre-fill a name the field then
    refuses. Typed names are never bent (`label_error` refuses them)."""

    def test_illegal_characters_become_dashes(self):
        self.assertEqual(suggested_label("my.app"), "my-app")
        self.assertEqual(suggested_label("a b/c:d"), "a-b-c-d")
        self.assertEqual(suggested_label("my–app"), "my-app")     # an en dash, not a hyphen

    def test_letters_with_an_ascii_spelling_get_it_rather_than_a_dash(self):
        # Accents decompose (NFKD) and the mark is dropped; the letters that
        # do not decompose have their spelling in _TRANSLITERATIONS.
        for text, expected in (("café", "cafe"), ("Zürich.app", "Zurich-app"),
                               ("naïve résumé", "naive-resume"), ("Straße", "Strasse"),
                               ("Ærø", "AEro"), ("Łódź", "Lodz"), ("ﬁle²", "file2")):
            with self.subTest(text=text):
                self.assertEqual(suggested_label(text), expected)

    def test_scripts_without_an_ascii_spelling_still_dash(self):
        # No guessing: a CJK or Cyrillic name is not transliterated, it is
        # dropped like any other illegal character.
        self.assertEqual(suggested_label("日本-app"), "app")
        self.assertEqual(suggested_label("Москва"), "")

    def test_runs_collapse_and_ends_trim(self):
        self.assertEqual(suggested_label("..hidden.."), "hidden")
        self.assertEqual(suggested_label("a...b"), "a-b")

    def test_a_legal_name_is_untouched(self):
        self.assertEqual(suggested_label("my-app_2"), "my-app_2")

    def test_nothing_legal_leaves_the_caller_its_fallback(self):
        self.assertEqual(suggested_label("..."), "")

    def test_the_result_always_passes_the_rule_it_was_bent_by(self):
        for text in ("my.app", "a b/c", "weird~^?*[\\name", "café!", "Straße", "\x1bx"):
            with self.subTest(text=text):
                bent = suggested_label(text)
                if bent:
                    self.assertIsNone(label_error(bent))


# The dated standards a fixture tree declares (the kind's shared file) and the
# full key list a member's efforts.tiers must answer: the ends around them.
FIXTURE_STANDARDS = ("2025Q1", "2026Q1")
FIXTURE_KEYS = ("cheapest", *FIXTURE_STANDARDS, "best")
FIXTURE_STANDARDS_FILE = (
    '[2025Q1]\nset_by = "Model A"\nindex = 20.0\nestimated = true\n'
    '[2026Q1]\nset_by = "Model B"\nindex = 40.0\n')


# A complete, valid AI member for fixture trees: a tier for every fixture
# standard on one test model, a two-word scale, and knobs enough to render an
# engine's budget. The shared standards file rides along (one per tree; two
# members merge to the same key).
def ai_member(name="claude", *, default=True, model="claude-test", fg="#ff8700", bg="#3a3a3a"):
    tiers = "".join(f'[{s}]\nmodel = "{model}"\neffort = "{"low" if s == "cheapest" else "high"}"\n' for s in FIXTURE_KEYS)
    return {
        "ai/capability.standards": FIXTURE_STANDARDS_FILE,
        f"ai/{name}/tag.info": (f'full_description = "{name} by Vendor"\nvendor = "Vendor"\nharness = "{name} CLI"\n'
                                f'default = {str(default).lower()}\nfg = "{fg}"\nbg = "{bg}"\n'),
        f"ai/{name}/efforts.tiers": '[scale]\nefforts = ["low", "high"]\n' + tiers,
        f"ai/{name}/knobs.mapping": (
            '[model]\nMODEL = "{value}"\n[effort]\nEFFORT = "{value}"\n'
            '[thinking.on]\nTHINK = "1"\n[thinking.off]\nTHINK = "0"\n'
            '[max_output_tokens]\nOUT = "{value}"\n[compact_at_percent]\nPCT = "{value/100}"\n'
            '[tool_output_tokens]\nCHARS = "{value*4}"\n'),
    }


class TagTreeTestCase(unittest.TestCase):
    """Base: `tree({relpath: contents})` writes a fixture `agents/` dir under
    a per-test temp dir and returns its path. `dedent` is applied so tests can
    indent heredocs naturally."""

    def tree(self, spec: dict[str, str]) -> Path:
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "agents"
        for rel, contents in spec.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(contents))
        return root

    # A small, valid, full-coverage tree reused by several tests.
    def full_tree(self) -> Path:
        return self.tree({
            **ai_member(),
            "engine/default/tag.info": 'full_description = "baseline"\n',
            "engine/default/tag.budget": 'standard = "best"\nthinking = true\n',
            "profession/code/tag.info": 'full_description = "coding toolchains"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/web/tag.info": 'full_description = "browser"\n',
            "profession/code/web/Dockerfile": "FROM code\n",
            "profession/code/_dood/Dockerfile": "FROM code\n",
            "specialty/auto/tag.info": (
                'full_description = "work nonstop"\nwarn = true\n'
                'claude_args = ["--dangerously-skip-permissions"]\n'
                '[wants]\nfirewall = "open network!"\n'
            ),
            "specialty/dood/tag.info": 'full_description = "host docker"\nwarn = true\n',
            "specialty/firewall/tag.info": 'full_description = "whitelist"\n',
            "specialty/combos.info": '[warnings]\n"dood + auto" = "both = danger"\n',
            "policy/no-sudo/tag.info": 'full_description = "no sudo"\nshortname = "-su"\n',
            "policy/no-sudo/policy.json": '{"permissions": {"deny": ["Bash(sudo *)"]}}',
        })


# ============================================================
# Engine — discovery + conf inheritance
# ============================================================

class TestEngine(TagTreeTestCase):
    def test_flat_engine_budget(self):
        root = self.tree({
            "engine/golem/tag.info": 'full_description = "cheap"\n',
            "engine/golem/tag.budget": 'standard = "cheapest"\nthinking = false\nmemory = false\n',
        })
        (golem,) = Engine.scan(root)
        self.assertEqual(golem.name, "golem")
        self.assertEqual(golem.label, "(golem)")
        self.assertEqual(golem.budget, Budget(standard="cheapest", thinking=False, memory=False))
        self.assertEqual(golem.budget.rank, 0)

    def test_nested_engine_inherits_and_overrides(self):
        root = self.tree({
            "engine/thinker/tag.info": 'full_description = "t"\n',
            "engine/thinker/tag.budget": 'standard = "2026Q1"\nmax_output_tokens = 30000\n',
            "engine/thinker/breakthrough/tag.info": 'full_description = "b"\n',
            "engine/thinker/breakthrough/tag.budget": 'max_output_tokens = 40000\n',
        })
        by_name = {e.name: e for e in Engine.scan(root)}
        # child inherits parent's standard, overrides the output budget
        self.assertEqual(by_name["breakthrough"].budget, Budget(standard="2026Q1", max_output_tokens=40000))
        # parent untouched
        self.assertEqual(by_name["thinker"].budget.max_output_tokens, 30000)

    def test_engine_without_budget_is_empty(self):
        root = self.tree({"engine/bare/tag.info": 'full_description = "no budget"\n'})
        (bare,) = Engine.scan(root)
        self.assertEqual(bare.budget, Budget())
        self.assertEqual(bare.budget.rank, -1)

    def test_budget_typos_fail_loudly(self):
        for body in ('standardd = "best"\n', 'standard = "ultra"\n', 'standard = "2025Q5"\n', 'thinking = "yes"\n',
                     'max_output_tokens = -1\n', 'compact_at_percent = 150\n'):
            with self.subTest(body=body), self.assertRaises(TagError):
                Engine.scan(self.tree({"engine/x/tag.info": 'full_description = "x"\n',
                                       "engine/x/tag.budget": body}))

    def test_sorted_engines_rank_by_standard_then_output_then_name(self):
        root = self.tree({
            "engine/a/tag.info": 'full_description = "a"\n', "engine/a/tag.budget": 'standard = "2025Q4"\nmax_output_tokens = 1000\n',
            "engine/b/tag.info": 'full_description = "b"\n', "engine/b/tag.budget": 'standard = "best"\n',
            "engine/c/tag.info": 'full_description = "c"\n', "engine/c/tag.budget": 'standard = "2025Q4"\nmax_output_tokens = 2000\n',
            "engine/d/tag.info": 'full_description = "d"\n', "engine/d/tag.budget": 'standard = "2025Q4"\nmax_output_tokens = 1000\n',
        })
        self.assertEqual([e.name for e in sorted_engines(Engine.scan(root))], ["b", "c", "a", "d"])

    def test_missing_engine_root_yields_nothing(self):
        self.assertEqual(Engine.scan(self.tree({"profession/code/tag.info": 'full_description="c"\n'})), [])


# ============================================================
# Ai — the fifth kind: manifests, standards, tiers, knobs, rendering
# ============================================================


class TestStandardVocabulary(unittest.TestCase):
    """tags/budget — the shape and order of a capability standard, without any
    tree: the ends, dated quarters between them, rising with the date."""

    def test_spelling(self):
        for good in ("cheapest", "best", "2024Q4", "2025Q1", "2031Q3"):
            self.assertTrue(is_standard(good), good)
        for bad in ("low", "2025Q5", "2025Q0", "25Q1", "2025-Q1", "Best", "", None, 3):
            self.assertFalse(is_standard(bad), repr(bad))

    def test_order_is_in_the_key(self):
        self.assertEqual(rank_of(None), -1)
        self.assertEqual(rank_of("low"), -1)
        self.assertLess(rank_of("cheapest"), rank_of("2024Q4"))
        self.assertLess(rank_of("2024Q4"), rank_of("2025Q1"))      # the year outranks the quarter
        self.assertLess(rank_of("2025Q1"), rank_of("2025Q4"))
        self.assertLess(rank_of("2025Q4"), rank_of("2026Q1"))
        self.assertLess(rank_of("9999Q4"), rank_of("best"))         # best tops every quarter there will be
        self.assertEqual(sorted_standards(["best", "2026Q1", "cheapest", "2025Q3"]), ["cheapest", "2025Q3", "2026Q1", "best"])

    def test_budget_rank_follows(self):
        self.assertEqual(Budget().rank, -1)
        self.assertEqual(Budget(standard="cheapest").rank, 0)
        self.assertGreater(Budget(standard="best").rank, Budget(standard="2026Q3").rank)


class TestAiKind(TagTreeTestCase):
    def test_scan_reads_manifest_tiers_and_knobs(self):
        (claude,) = Ai.scan(self.tree(ai_member()))
        self.assertEqual((claude.name, claude.label, claude.vendor, claude.harness), ("claude", "⟪claude⟫", "Vendor", "claude CLI"))
        self.assertTrue(claude.default)
        self.assertEqual(claude.style, "fg:#ff8700 bg:#3a3a3a")
        self.assertEqual(claude.standards, FIXTURE_KEYS)
        self.assertEqual(claude.tier("cheapest").effort, "low")
        self.assertEqual(claude.tier("2026Q1").model, "claude-test")
        self.assertEqual(claude.knob("model"), (("MODEL", "{value}"),))
        self.assertIsNone(claude.knob("telemetry.off"))

    def test_render_fills_templates_and_converts_units(self):
        (claude,) = Ai.scan(self.tree(ai_member()))
        rendering = claude.render(Budget(standard="best", thinking=True, max_output_tokens=1000,
                                         compact_at_percent=60, tool_output_tokens=100))
        self.assertEqual(rendering.map, {"MODEL": "claude-test", "EFFORT": "high", "THINK": "1",
                                         "OUT": "1000", "CHARS": "400", "PCT": "0.6"})
        self.assertEqual(rendering.unmapped, ())

    def test_render_reports_what_the_ai_cannot_say(self):
        (claude,) = Ai.scan(self.tree(ai_member()))
        rendering = claude.render(Budget(standard="2025Q1", memory=False, telemetry=False))
        self.assertEqual(rendering.unmapped, ("memory.off", "telemetry.off"))
        self.assertEqual(rendering.map, {"MODEL": "claude-test", "EFFORT": "high"})

    def test_render_needs_a_standard(self):
        (claude,) = Ai.scan(self.tree(ai_member()))
        with self.assertRaises(TagError):
            claude.render(Budget())

    def test_exactly_one_default(self):
        with self.assertRaises(TagError):
            Ai.scan(self.tree({**ai_member("a"), **ai_member("b", fg="#000000", bg="#ffffff")}))   # two defaults
        with self.assertRaises(TagError):
            Ai.scan(self.tree(ai_member("a", default=False)))                                    # none
        two = Ai.scan(self.tree({**ai_member("a"), **ai_member("b", default=False)}))
        self.assertEqual([ai.name for ai in two if ai.default], ["a"])

    def test_manifest_faults_fail_loudly(self):
        base = ai_member()
        faults = {
            "bad colour": {"ai/claude/tag.info": base["ai/claude/tag.info"].replace('#ff8700', 'orange')},
            "no harness": {"ai/claude/tag.info": base["ai/claude/tag.info"].replace('harness = "claude CLI"', 'harness = ""')},
            "missing standard": {"ai/claude/efforts.tiers": base["ai/claude/efforts.tiers"].replace("[2026Q1]", "[2026Q2]")},
            "undeclared standard": {"ai/claude/efforts.tiers": base["ai/claude/efforts.tiers"] + '[2027Q1]\nmodel = "x"\n'},
            "no standards file": {"ai/capability.standards": None},
            "an end written as a standard": {"ai/capability.standards": FIXTURE_STANDARDS_FILE + '[best]\nset_by = "x"\nindex = 99\n'},
            "standards that do not rise": {"ai/capability.standards": FIXTURE_STANDARDS_FILE.replace("index = 40.0", "index = 19.0")},
            "a standard that is not a quarter": {"ai/capability.standards": FIXTURE_STANDARDS_FILE.replace("[2026Q1]", "[2026Q5]")},
            "a standard without its setter": {"ai/capability.standards": FIXTURE_STANDARDS_FILE.replace('set_by = "Model B"\n', "")},
            "effort outside scale": {"ai/claude/efforts.tiers": base["ai/claude/efforts.tiers"].replace('effort = "low"', 'effort = "ultra"')},
            "unknown purpose": {"ai/claude/knobs.mapping": base["ai/claude/knobs.mapping"] + '[colour]\nX = "1"\n'},
            "bad template": {"ai/claude/knobs.mapping": base["ai/claude/knobs.mapping"].replace('"{value/100}"', '"{value^2}"')},
            "missing knobs file": {"ai/claude/knobs.mapping": None},
        }
        for label, change in faults.items():
            spec = {**base}
            for path, contents in change.items():
                if contents is None:
                    spec.pop(path)
                else:
                    spec[path] = contents
            with self.subTest(fault=label), self.assertRaises(TagError):
                Ai.scan(self.tree(spec))

    def test_members_do_not_nest(self):
        spec = {**ai_member(), "ai/claude/mini/tag.info": 'full_description = "nested"\n'}
        with self.assertRaises(TagError):
            Ai.scan(self.tree(spec))

    def test_missing_ai_root_yields_nothing(self):
        self.assertEqual(Ai.scan(self.tree({"profession/code/tag.info": 'full_description="c"\n'})), [])


# ============================================================
# Profession — discovery + requires-from-nesting + hidden layers
# ============================================================

class TestProfession(TagTreeTestCase):
    def test_nesting_becomes_requires(self):
        root = self.full_tree()
        by_name = {p.name: p for p in Profession.scan(root)}
        self.assertEqual(by_name["code"].requires, frozenset())
        self.assertEqual(by_name["web"].requires, frozenset({"code"}))
        self.assertEqual(by_name["web"].label, "[web]")

    def test_underscore_dir_not_offered_as_profession(self):
        names = {p.name for p in Profession.scan(self.full_tree())}
        self.assertEqual(names, {"code", "web"})   # _dood excluded

    def test_shortname_defaults_to_name(self):
        (code,) = [p for p in Profession.scan(self.full_tree()) if p.name == "code"]
        self.assertEqual(code.shortname, "code")

    def test_stray_dir_without_taginfo_raises(self):
        # STRICT: a non-tag, non-underscore dir is a structural error — a
        # forgotten/misnamed tag.info would otherwise silently drop the tag
        # and sever any requirement edge routed through it.
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/stray/notes.txt": "not a tag\n",
        })
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Profession.scan(root)

    def test_stray_intermediate_breaks_nesting(self):
        # The inverse of the old "grouping" allowance: an intermediate dir
        # meant to hold [web] must itself be a tag (carry tag.info), else error.
        root = self.tree({
            "profession/grp/web/tag.info": 'full_description = "w"\n',
            "profession/grp/web/Dockerfile": "FROM base\n",
        })
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Profession.scan(root)

    def test_underscore_dir_elsewhere_is_skipped_not_layer(self):
        # A `_`-dir OUTSIDE the profession tree is just an ignored asset dir —
        # no layer semantics (layers are a profession-tree concept).
        root = self.tree({
            "specialty/auto/tag.info": 'full_description = "a"\n',
            "specialty/_shared/helper.sh": "#!/bin/sh\n",
        })
        self.assertEqual({s.name for s in Specialty.scan(root, {}, {})}, {"auto"})

    def test_discover_layers(self):
        layers = Profession.discover_layers(self.full_tree())
        self.assertIn("dood", layers)
        self.assertEqual(layers["dood"].requires, frozenset({"code"}))

    def test_duplicate_hidden_layer_raises(self):
        root = self.tree({
            "profession/a/tag.info": 'full_description="a"\n', "profession/a/Dockerfile": "x\n",
            "profession/a/_dup/Dockerfile": "x\n",
            "profession/b/tag.info": 'full_description="b"\n', "profession/b/Dockerfile": "x\n",
            "profession/b/_dup/Dockerfile": "x\n",
        })
        with self.assertRaisesRegex(TagError, "duplicate hidden layer 'dup'"):
            Profession.discover_layers(root)

    def test_hidden_dir_with_taginfo_raises(self):
        root = self.tree({
            "profession/code/tag.info": 'full_description="c"\n', "profession/code/Dockerfile": "x\n",
            "profession/code/_bad/tag.info": 'full_description="oops"\n',
        })
        with self.assertRaisesRegex(TagError, "must not contain a tag"):
            Profession.discover_layers(root)

class TestProfessionToolkit(TagTreeTestCase):
    """`template.form` — a profession's optional, sibling-file-declared set of
    configurable installs (ToolkitEntry / Profession.load_toolkit)."""

    def test_no_manifest_yields_none_and_empty_toolkit(self):
        (code,) = [p for p in Profession.scan(self.full_tree()) if p.name == "code"]
        self.assertIsNone(code.toolkit_path)
        self.assertEqual(code.load_toolkit(), {})

    def test_manifest_present_is_discovered_and_parsed(self):
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/template.form": (
                '[rust]\ndescription = "Rust toolchain"\nrun_command = "cargo"\nlanguage = "compiled"\napprox_size_mb = 613\ndefault = true\nbuild_arg = "INSTALL_RUST"\n'
                '[node]\ndescription = "Node.js LTS"\nrun_command = "node"\nlanguage = "interpreted"\napprox_size_mb = 196\ndefault = false\nbuild_arg = "INSTALL_NODE"\n'
            ),
        })
        (code,) = Profession.scan(root)
        self.assertIsNotNone(code.toolkit_path)
        entries = code.load_toolkit()
        self.assertEqual(set(entries), {"rust", "node"})
        self.assertEqual(entries["rust"], ToolkitEntry(key="rust", description="Rust toolchain",
                                                        run_command="cargo", language="compiled",
                                                        approx_size_mb=613, default=True,
                                                        build_arg="INSTALL_RUST"))
        self.assertFalse(entries["node"].default)

    def test_missing_required_field_raises(self):
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/template.form": '[rust]\ndescription = "Rust toolchain"\nrun_command = "cargo"\nlanguage = "compiled"\napprox_size_mb = 613\nbuild_arg = "INSTALL_RUST"\n',   # no default
        })
        (code,) = Profession.scan(root)
        with self.assertRaisesRegex(TagError, "'rust'.*'default'"):
            code.load_toolkit()

    def test_malformed_build_arg_raises(self):
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/template.form": '[rust]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "install-rust"\n',
        })
        (code,) = Profession.scan(root)
        with self.assertRaisesRegex(TagError, "not a valid ARG name"):
            code.load_toolkit()

    def test_duplicate_build_arg_raises(self):
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/template.form": (
                '[rust]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_X"\n'
                '[node]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_X"\n'
            ),
        })
        (code,) = Profession.scan(root)
        with self.assertRaisesRegex(TagError, "both claim build_arg"):
            code.load_toolkit()

    def test_load_toolkit_reflects_live_edits(self):
        # Parsed fresh each call (small file, no cache) — the picker's "Edit
        # Toolkits" menu must see a manifest edited since the last scan.
        root = self.tree({
            "profession/code/tag.info": 'full_description = "c"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/template.form": '[rust]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_RUST"\n',
        })
        (code,) = Profession.scan(root)
        self.assertEqual(set(code.load_toolkit()), {"rust"})
        code.toolkit_path.write_text('[rust]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_RUST"\n'
                                     '[node]\ndescription = "d"\nrun_command = "x"\nlanguage = "y"\napprox_size_mb = 1\ndefault = true\nbuild_arg = "INSTALL_NODE"\n')
        self.assertEqual(set(code.load_toolkit()), {"rust", "node"})



class TestResolveStoreBuild(TagTreeTestCase):
    """Registry.resolve_store_build — the non-raising partition used for
    (user-editable) instances.toml entries: keep names that resolve to their
    axis's kind, and report the rest as `TagProblem`s with same-kind
    alternatives. (Shipped `.lego` files use the raising validate_build.)"""

    def setUp(self):
        self.reg = scan_all(self.full_tree())   # code, web(→requires code), auto/dood/firewall, no-sudo

    def test_all_valid_yields_no_problems(self):
        clean, problems = self.reg.resolve_store_build(
            AgentBuild(engine="default", professions=("code", "web"), specialties=("auto",)))
        self.assertEqual(problems, [])
        self.assertEqual(clean.professions, ("code", "web"))

    def test_unknown_name_is_dropped_and_reported(self):
        clean, problems = self.reg.resolve_store_build(AgentBuild(professions=("code", "ghost")))
        self.assertEqual(clean.professions, ("code",))              # good one kept
        (prob,) = problems
        self.assertEqual((prob.name, prob.axis, prob.kind, prob.reason), ("ghost", "professions", "profession", "unknown"))
        self.assertEqual(prob.label, "[ghost]")                     # expected-kind punctuation
        self.assertEqual(prob.options, ("code", "web"))             # only professions offered

    def test_wrong_axis_is_reported_with_actual_kind(self):
        _, problems = self.reg.resolve_store_build(AgentBuild(specialties=("no-sudo",)))
        (prob,) = problems
        self.assertEqual(prob.reason, "wrong_axis")
        self.assertEqual(prob.actual_kind, "policy")
        self.assertEqual(prob.kind, "specialty")

    def test_unknown_ai_is_reported_on_its_own_axis(self):
        reg = scan_all(self.full_tree())
        cleaned, problems = reg.resolve_store_build(AgentBuild(ai="mistral", engine="default"))
        self.assertIsNone(cleaned.ai)
        self.assertEqual([(p.axis, p.name, p.reason) for p in problems], [("ai", "mistral", "unknown")])
        self.assertEqual(problems[0].options, ("claude",))

    def test_unknown_engine_reported_options_are_engines(self):
        _, problems = self.reg.resolve_store_build(AgentBuild(engine="ghost"))
        (prob,) = problems
        self.assertEqual((prob.axis, prob.kind, prob.label), ("engine", "engine", "(ghost)"))
        self.assertEqual(prob.options, ("default",))

    def test_never_raises_on_bad_input(self):
        # The whole point vs validate_build: a stale store entry must not crash.
        clean, problems = self.reg.resolve_store_build(
            AgentBuild(engine="x", professions=("y",), specialties=("z",), policies=("w",)))
        self.assertEqual(len(problems), 4)
        self.assertEqual((clean.professions, clean.specialties, clean.policies), ((), (), ()))



class TestAlwaysOnPolicy(TagTreeTestCase):
    """`always_on = true` — a STATIC policy: applied to every instance,
    locked in the form, and never listed in .lego / instances.toml (shipped
    .lego listing it is a repo bug → validate_build raises; a store entry
    listing it is harmless staleness → resolve_store_build drops silently)."""

    def _tree_with_static(self):
        return self.tree({
            "engine/default/tag.info": 'full_description = "d"\n',
            "policy/no-sudo/tag.info": 'full_description = "no sudo"\nstance = "deny"\nalways_on = true\n',
            "policy/no-sudo/policy.json": '{"permissions": {"deny": ["Bash(sudo *)"]}}',
            "policy/open/tag.info": 'full_description = "o"\n',
            "policy/open/policy.json": "{}",
        })

    def test_scan_parses_always_on(self):
        reg = scan_all(self._tree_with_static())
        self.assertTrue(reg.policies["no-sudo"].always_on)
        self.assertFalse(reg.policies["open"].always_on)   # default False

    def test_lego_listing_always_on_raises(self):
        reg = scan_all(self._tree_with_static())
        with self.assertRaisesRegex(TagError, "always-on"):
            reg.validate_build(AgentBuild(policies=("no-sudo",)), Path("x.lego"))

    def test_store_listing_always_on_dropped_silently(self):
        reg = scan_all(self._tree_with_static())
        clean, problems = reg.resolve_store_build(AgentBuild(policies=("no-sudo", "open")))
        self.assertEqual(problems, [])                    # not a fault — just stale
        self.assertEqual(clean.policies, ("open",))       # static name dropped, rest kept


# ============================================================
# Specialty — discovery + layer claim + wants + combos
# ============================================================

class TestSpecialty(TagTreeTestCase):
    def _specialties(self, root):
        layers = Profession.discover_layers(root)
        return {s.name: s for s in Specialty.scan(root, layers, {})}

    def test_fields_and_label(self):
        s = self._specialties(self.full_tree())["auto"]
        self.assertTrue(s.warn)
        self.assertEqual(s.claude_args, ("--dangerously-skip-permissions",))
        self.assertEqual(s.label, "{auto}")
        self.assertEqual(s.wants_map, {"firewall": "open network!"})

    def test_layer_claim_supplies_requires(self):
        dood = self._specialties(self.full_tree())["dood"]
        self.assertIsNotNone(dood.layer)
        self.assertEqual(dood.requires, frozenset({"code"}))

    def test_specialty_without_layer_has_no_requires(self):
        auto = self._specialties(self.full_tree())["auto"]
        self.assertIsNone(auto.layer)
        self.assertEqual(auto.requires, frozenset())

    def test_squash_glyph_skips_stance_symbols_and_punctuation(self):
        # The one-char form for crowded rows: first letter/digit of what the
        # label shows — never the policy stance symbol, never the brackets.
        self.assertEqual(Policy(name="vcs-safe", path=Path("/x"), shortname="-gpush").squash_glyph, "g")
        self.assertEqual(Specialty(name="cowork", path=Path("/x"), shortname="cowrk").squash_glyph, "c")
        self.assertEqual(Profession(name="code", path=Path("/x")).squash_glyph, "c")

    def test_squash_glyph_of_a_symbol_only_name_is_a_placeholder(self):
        self.assertEqual(Policy(name="x", path=Path("/x"), shortname="!!").squash_glyph, "?")

    def test_nested_specialty_requires_its_ancestors(self):
        # The specialty tree nests like the profession tree: {manager} inside
        # cowork/ requires {cowork}, and the form's check-cascade comes free.
        root = self.tree({
            "specialty/cowork/tag.info": 'full_description = "recruitable"\n',
            "specialty/cowork/manager/tag.info": 'full_description = "hosts groups"\n',
        })
        specs = self._specialties(root)
        self.assertEqual(specs["manager"].requires, frozenset({"cowork"}))
        self.assertEqual(specs["cowork"].requires, frozenset())

    def test_nesting_and_layer_claim_requires_merge(self):
        # Two sources, two shapes: tree ancestors (specialties) and a claimed
        # layer's ancestors (professions). Neither may shadow the other.
        root = self.tree({
            "profession/code/tag.info": 'full_description = "code"\n',
            "profession/code/Dockerfile": "FROM base\n",
            "profession/code/_inner/Dockerfile": "FROM code\n",
            "specialty/outer/tag.info": 'full_description = "outer"\n',
            "specialty/outer/inner/tag.info": 'full_description = "claims a layer"\n',
        })
        self.assertEqual(self._specialties(root)["inner"].requires,
                         frozenset({"outer", "code"}))

    def test_combos_parse(self):
        (combo,) = tags.scan_combos(self.full_tree())
        self.assertEqual(combo.tags, frozenset({"dood", "auto"}))
        self.assertEqual(combo.message, "both = danger")

    def test_combo_single_name_raises(self):
        root = self.tree({"specialty/combos.info": '[warnings]\n"lonely" = "x"\n'})
        with self.assertRaisesRegex(TagError, "≥2 tag names"):
            tags.scan_combos(root)

    def test_absent_combos_file_is_empty(self):
        self.assertEqual(tags.scan_combos(self.tree({"specialty/auto/tag.info": 'full_description="a"\n'})), [])


# ============================================================
# Policy — discovery + fragment validation + merge
# ============================================================

class TestPolicy(TagTreeTestCase):
    def test_fields_and_fragment(self):
        (p,) = Policy.scan(self.tree({
            "policy/web-research/tag.info": 'full_description = "no ask"\nshortname = "+query"\nstance = "allow"\n',
            "policy/web-research/policy.json": '{"permissions": {"allow": ["WebSearch"]}}',
        }))
        self.assertEqual(p.label, "<+query>")
        self.assertIs(p.stance, PolicyStance.ALLOW)
        self.assertEqual(p.load_fragment(), {"permissions": {"allow": ["WebSearch"]}})

    def test_stance_parsed(self):
        (p,) = Policy.scan(self.tree({
            "policy/no-sudo/tag.info": 'full_description = "deny sudo"\nstance = "deny"\n',
            "policy/no-sudo/policy.json": '{"permissions": {"deny": ["Bash(sudo *)"]}}',
        }))
        self.assertIs(p.stance, PolicyStance.DENY)

    def test_unknown_stance_raises(self):
        with self.assertRaisesRegex(TagError, "stance must be one of"):
            Policy.scan(self.tree({
                "policy/x/tag.info": 'full_description = "x"\nstance = "sideways"\n',
                "policy/x/policy.json": '{}',
            }))

    def test_stance_defaults_to_allow(self):
        (p,) = Policy.scan(self.tree({
            "policy/x/tag.info": 'full_description = "x"\n',
            "policy/x/policy.json": '{}',
        }))
        self.assertIs(p.stance, PolicyStance.ALLOW)

    def test_missing_json_raises(self):
        with self.assertRaisesRegex(TagError, "missing policy.json"):
            Policy.scan(self.tree({"policy/x/tag.info": 'full_description="x"\n'}))

    def test_non_object_fragment_raises(self):
        with self.assertRaisesRegex(TagError, "must be a JSON object"):
            Policy.scan(self.tree({
                "policy/x/tag.info": 'full_description="x"\n',
                "policy/x/policy.json": '["not", "an", "object"]',
            }))

    def test_bad_json_raises(self):
        with self.assertRaisesRegex(TagError, "invalid JSON"):
            Policy.scan(self.tree({
                "policy/x/tag.info": 'full_description="x"\n',
                "policy/x/policy.json": '{not valid',
            }))

    def test_stray_dir_under_policy_raises(self):
        # STRICT applies to every kind subtree, not just professions.
        with self.assertRaisesRegex(TagError, r"needs tag\.info"):
            Policy.scan(self.tree({
                "policy/real/tag.info": 'full_description = "r"\n',
                "policy/real/policy.json": "{}",
                "policy/junk/readme.txt": "notes\n",
            }))


class TestMergeFragments(unittest.TestCase):
    def test_disjoint_keys_union(self):
        self.assertEqual(
            merge_fragments([("a", {"x": 1}), ("b", {"y": 2})]),
            {"x": 1, "y": 2})

    def test_nested_dicts_recurse(self):
        self.assertEqual(
            merge_fragments([("a", {"p": {"allow": ["X"]}}), ("b", {"p": {"deny": ["Y"]}})]),
            {"p": {"allow": ["X"], "deny": ["Y"]}})

    def test_lists_concat_and_dedupe(self):
        self.assertEqual(
            merge_fragments([("a", {"allow": ["X", "Y"]}), ("b", {"allow": ["Y", "Z"]})]),
            {"allow": ["X", "Y", "Z"]})

    def test_equal_scalars_coexist(self):
        self.assertEqual(merge_fragments([("a", {"k": 1}), ("b", {"k": 1})]), {"k": 1})

    def test_scalar_conflict_names_both(self):
        with self.assertRaisesRegex(TagError, r"conflict at 'k'.*<a>.*<b>"):
            merge_fragments([("a", {"k": 1}), ("b", {"k": 2})])

    def test_shape_clash_is_conflict(self):
        with self.assertRaisesRegex(TagError, "conflict"):
            merge_fragments([("a", {"k": {"nested": 1}}), ("b", {"k": [1, 2]})])

    def test_sources_not_mutated(self):
        a = {"p": {"allow": ["X"]}}
        merge_fragments([("a", a), ("b", {"p": {"allow": ["Y"]}})])
        self.assertEqual(a, {"p": {"allow": ["X"]}})   # deepcopy protected the input


# ============================================================
# .lego — parse + reference validation
# ============================================================

class TestLego(TagTreeTestCase):
    def test_missing_file_is_empty_build(self):
        self.assertEqual(load_lego(Path("/nonexistent/x.lego")), AgentBuild())

    def test_parse(self):
        root = self.tree({"researcher.lego":
            'engine = "researcher"\nprofessions = ["code"]\nspecialties = ["auto", "firewall"]\npolicies = ["no-sudo"]\n'})
        build = load_lego(root / "researcher.lego")
        self.assertEqual(build.engine, "researcher")
        self.assertEqual(build.professions, ("code",))
        self.assertEqual(build.selected(), {"researcher", "code", "auto", "firewall", "no-sudo"})

    def test_ai_must_be_string(self):
        root = self.tree({"x.lego": "ai = 3\n"})
        with self.assertRaises(TagError):
            load_lego(root / "x.lego")

    def test_engine_must_be_string(self):
        root = self.tree({"x.lego": 'engine = ["nope"]\n'})
        with self.assertRaisesRegex(TagError, "'engine' must be a string"):
            load_lego(root / "x.lego")

    def test_axis_must_be_string_list(self):
        root = self.tree({"x.lego": 'professions = [1, 2]\n'})
        with self.assertRaisesRegex(TagError, "'professions' must be a list of strings"):
            load_lego(root / "x.lego")

    def test_validate_build_unknown_tag(self):
        reg = scan_all(self.full_tree())
        with self.assertRaisesRegex(TagError, "unknown tag 'ghost'"):
            reg.validate_build(AgentBuild(professions=("ghost",)), Path("x.lego"))

    def test_validate_build_wrong_axis(self):
        reg = scan_all(self.full_tree())
        # 'auto' is a specialty; listing it under professions is a wrong-axis error.
        with self.assertRaisesRegex(TagError, "'auto' is a specialty, not a profession"):
            reg.validate_build(AgentBuild(professions=("auto",)), Path("x.lego"))

    def test_validate_build_accepts_valid(self):
        reg = scan_all(self.full_tree())
        reg.validate_build(AgentBuild(engine="default", professions=("code", "web"),
                                      specialties=("auto", "firewall"), policies=("no-sudo",)),
                           Path("ok.lego"))   # no raise


# ============================================================
# tag.docker + tag.info parsing edge cases
# ============================================================

class TestManifestParsing(TagTreeTestCase):
    def test_docker_mount_relative_resolved_absolute_kept(self):
        root = self.tree({
            "specialty/firewall/tag.info": 'full_description = "fw"\n',
            "specialty/firewall/init.sh": "#!/bin/sh\n",
            "specialty/firewall/tag.docker": (
                '[run]\ncap_add = ["NET_ADMIN"]\nentrypoint = "init.sh"\n'
                'mounts = ["init.sh -> /usr/local/bin/init.sh:ro", "/var/run/docker.sock -> /var/run/docker.sock"]\n'
                'env_forward = ["WHITELIST_ADDRESSES"]\n'
            ),
        })
        (fw,) = Specialty.scan(root, {}, {})
        d = fw.docker
        self.assertEqual(d.cap_add, ("NET_ADMIN",))
        self.assertEqual(d.env_forward, ("WHITELIST_ADDRESSES",))
        rel_src, rel_tgt = d.mounts[0]
        self.assertEqual(rel_src, root / "specialty/firewall/init.sh")   # relative → resolved to tag dir
        self.assertEqual(d.mounts[1][0], Path("/var/run/docker.sock"))   # absolute → kept
        self.assertEqual(rel_tgt, "/usr/local/bin/init.sh:ro")

    def test_docker_missing_mount_source_raises(self):
        root = self.tree({
            "specialty/x/tag.info": 'full_description="x"\n',
            "specialty/x/tag.docker": '[run]\nmounts = ["ghost.sh -> /bin/ghost.sh"]\n',
        })
        with self.assertRaisesRegex(TagError, "mount source 'ghost.sh' not found"):
            Specialty.scan(root, {}, {})

    def test_docker_malformed_mount_raises(self):
        root = self.tree({
            "specialty/x/tag.info": 'full_description="x"\n',
            "specialty/x/tag.docker": '[run]\nmounts = ["no arrow here"]\n',
        })
        with self.assertRaisesRegex(TagError, "not 'source -> target'"):
            Specialty.scan(root, {}, {})

    def test_docker_bare_entrypoint_must_exist(self):
        root = self.tree({
            "specialty/x/tag.info": 'full_description="x"\n',
            "specialty/x/tag.docker": '[run]\nentrypoint = "ghost.sh"\n',
        })
        with self.assertRaisesRegex(TagError, "entrypoint 'ghost.sh' not found"):
            Specialty.scan(root, {}, {})

    def test_wants_non_string_message_raises(self):
        root = self.tree({"specialty/x/tag.info": 'full_description="x"\n[wants]\nfirewall = 5\n'})
        with self.assertRaisesRegex(TagError, "wants.firewall must be a string"):
            Specialty.scan(root, {}, {})

    def test_malformed_toml_names_file(self):
        root = self.tree({"specialty/x/tag.info": 'full_description = "unterminated\n'})
        with self.assertRaisesRegex(TagError, r"tag\.info: cannot read TOML"):
            Specialty.scan(root, {}, {})


# ============================================================
# Registry — cross-cutting validation
# ============================================================

class TestRegistryValidation(TagTreeTestCase):
    def test_full_tree_scans_clean(self):
        reg = scan_all(self.full_tree())
        self.assertIsInstance(reg, Registry)
        self.assertEqual(set(reg.engines), {"default"})
        self.assertEqual(set(reg.professions), {"code", "web"})
        self.assertEqual(set(reg.specialties), {"auto", "dood", "firewall"})
        self.assertEqual(set(reg.policies), {"no-sudo"})
        self.assertEqual(reg.kind_of("web"), "profession")
        self.assertIsNone(reg.get("nonexistent"))

    def test_cross_kind_name_collision_raises(self):
        root = self.tree({
            "profession/dup/tag.info": 'full_description="p"\n', "profession/dup/Dockerfile": "x\n",
            "specialty/dup/tag.info": 'full_description="s"\n',
        })
        with self.assertRaisesRegex(TagError, "'dup' used by both .* names must be unique"):
            scan_all(root)

    def test_orphan_hidden_layer_raises(self):
        # A `_ghost` layer with no matching specialty is an error.
        root = self.tree({
            "profession/code/tag.info": 'full_description="c"\n', "profession/code/Dockerfile": "x\n",
            "profession/code/_ghost/Dockerfile": "x\n",
        })
        with self.assertRaisesRegex(TagError, "hidden layer '_ghost' has no matching specialty"):
            scan_all(root)

    def test_unknown_wants_reference_raises(self):
        root = self.tree({"specialty/auto/tag.info": 'full_description="a"\n[wants]\nnosuchthing = "msg"\n'})
        with self.assertRaisesRegex(TagError, "wants unknown tag 'nosuchthing'"):
            scan_all(root)

    def test_unknown_combo_reference_raises(self):
        root = self.tree({
            "specialty/auto/tag.info": 'full_description="a"\n',
            "specialty/combos.info": '[warnings]\n"auto + ghost" = "x"\n',
        })
        with self.assertRaisesRegex(TagError, "combo references unknown tag 'ghost'"):
            scan_all(root)

    def test_within_kind_duplicate_name_raises(self):
        # Two engines resolving to the same name via nesting (parents are
        # valid tags, so strict is satisfied — the clash is the leaf name).
        root = self.tree({
            "engine/a/tag.info": 'full_description="a"\n',
            "engine/a/dup/tag.info": 'full_description="d1"\n',
            "engine/b/tag.info": 'full_description="b"\n',
            "engine/b/dup/tag.info": 'full_description="d2"\n',
        })
        with self.assertRaisesRegex(TagError, "duplicate engine 'dup'"):
            scan_all(root)

    def test_empty_tree_is_valid(self):
        reg = scan_all(self.tree({"placeholder.md": "x\n"}))
        self.assertEqual(reg.all_names(), set())


class TestTagCommands(TagTreeTestCase):
    """The `commands = [...]` tag.info key: a tag GRANTS slash commands by name;
    the files live centrally in `<agents>/_commands/`. Split validation on
    purpose — shape errors surface at parse time naming the manifest, while a
    dangling name surfaces at registry time, because only the registry knows
    the tree root the central dir hangs off."""

    def test_any_kind_may_declare_and_the_names_are_sorted(self):
        root = self.tree({
            f"{COMMANDS_DIR_NAME}/deploy.md": "---\ndescription: d\n---\nbody\n",
            f"{COMMANDS_DIR_NAME}/audit.md": "---\ndescription: a\n---\nbody\n",
            "profession/ops/tag.info": 'full_description="o"\ncommands = ["deploy", "audit"]\n',
            "profession/ops/Dockerfile": "x\n",
            "policy/guarded/tag.info": 'full_description="g"\ncommands = ["audit"]\n',
            "policy/guarded/policy.json": '{"permissions": {}}',
        })
        reg = scan_all(root)
        # Sorted at parse time, so assembly and legend order never depend on
        # authoring order.
        self.assertEqual(reg.professions["ops"].commands, ("audit", "deploy"))
        self.assertEqual(reg.policies["guarded"].commands, ("audit",))

    def test_two_tags_may_share_one_command_file(self):
        # The point of central files: no duplication when a second tag grants
        # the same command.
        root = self.tree({
            f"{COMMANDS_DIR_NAME}/shared.md": "---\ndescription: s\n---\nbody\n",
            "specialty/one/tag.info": 'full_description="1"\ncommands = ["shared"]\n',
            "specialty/two/tag.info": 'full_description="2"\ncommands = ["shared"]\n',
        })
        reg = scan_all(root)
        self.assertEqual(reg.specialties["one"].commands,
                         reg.specialties["two"].commands)

    def test_no_declaration_means_no_commands(self):
        root = self.tree({"specialty/plain/tag.info": 'full_description="p"\n'})
        self.assertEqual(scan_all(root).specialties["plain"].commands, ())

    def test_a_dangling_name_aborts_the_scan_naming_tag_and_options(self):
        # A typo'd name would otherwise assemble an instance MISSING the
        # command it was tagged for, silently.
        root = self.tree({
            f"{COMMANDS_DIR_NAME}/real.md": "---\ndescription: r\n---\nbody\n",
            "specialty/oops/tag.info": 'full_description="o"\ncommands = ["reall"]\n',
        })
        with self.assertRaisesRegex(TagError, r"oops.*unknown command 'reall'.*real"):
            scan_all(root)

    def test_a_missing_commands_dir_reads_as_no_commands_available(self):
        # Same failure as a dangling name — with an empty "available" list
        # rather than a crash on the absent dir.
        root = self.tree({"specialty/oops/tag.info": 'full_description="o"\ncommands = ["x"]\n'})
        with self.assertRaisesRegex(TagError, r"available: \[\]"):
            scan_all(root)

    def test_shape_errors_surface_at_parse_time(self):
        for label, manifest in [
            ("not a list", 'full_description="x"\ncommands = "deploy"\n'),
            ("non-string entry", 'full_description="x"\ncommands = [7]\n'),
        ]:
            with self.subTest(shape=label):
                root = self.tree({"specialty/bad/tag.info": manifest})
                with self.assertRaisesRegex(TagError, "list of command-name strings"):
                    scan_all(root)

    def test_a_duplicate_entry_is_its_own_error_not_a_dangling_name(self):
        # The file EXISTS here, so the only fault left is the repetition —
        # without that, this test would pass on the dangling-name error instead
        # and the duplicate guard could die unnoticed (it did, in a mutation
        # run, when this used a tree with no commands/ dir).
        root = self.tree({
            f"{COMMANDS_DIR_NAME}/a.md": "---\ndescription: a\n---\nbody\n",
            "specialty/bad/tag.info": 'full_description="x"\ncommands = ["a", "a"]\n',
        })
        with self.assertRaisesRegex(TagError, "more than once"):
            scan_all(root)


# ============================================================
# Identity — image chain + Instance + resolve_build
# ============================================================


class TestImageChain(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def test_base_only(self):
        self.assertEqual(image_chain((), ()), ["base"])

    def test_requirement_ordered_before_dependent(self):
        code, web = self.reg.professions["code"], self.reg.professions["web"]
        self.assertEqual(image_chain((web, code), ()), ["base", "code", "web"])   # code first despite input order

    def test_specialties_follow_professions(self):
        code, auto = self.reg.professions["code"], self.reg.specialties["auto"]
        self.assertEqual(image_chain((code,), (auto,)), ["base", "code", "auto"])

    def test_specialties_sorted_by_name(self):
        auto, dood = self.reg.specialties["auto"], self.reg.specialties["dood"]
        self.assertEqual(image_chain((), (dood, auto)), ["base", "auto", "dood"])

    def test_a_nested_specialty_follows_its_ancestor_despite_the_alphabet(self):
        # 'a_child' < 'z_parent' alphabetically — the topo order must win, or a
        # nested specialty's addendum would compose before the one it extends.
        reg = scan_all(self.tree({
            "specialty/z_parent/tag.info": 'full_description = "parent"\n',
            "specialty/z_parent/a_child/tag.info": 'full_description = "child"\n',
        }))
        child, parent = reg.specialties["a_child"], reg.specialties["z_parent"]
        self.assertEqual(image_chain((), (child, parent)),
                         ["base", "z_parent", "a_child"])


class TestInstance(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def _inst(self, **kw) -> Instance:
        base = dict(agent="researcher", md_path=Path("/x/researcher.md"),
                    session="proj", workspace="/tmp/ws", is_brand_new=True,
                    engine=self.reg.engines["default"], ai=self.reg.default_ai)
        base.update(kw)
        return Instance(**base)

    def test_instance_id_and_paths(self):
        i = self._inst()
        self.assertEqual(i.instance, "researcher__proj")
        self.assertTrue(str(i.state_dir).endswith("researcher__proj"))
        self.assertEqual(i.state_md.name, "CLAUDE.md")

    def test_chain_and_conf(self):
        i = self._inst(professions=(self.reg.professions["code"],),
                       specialties=(self.reg.specialties["auto"],))
        self.assertEqual(i.chain, ["base", "code", "auto"])
        # the engine's budget rendered by the instance's AI
        self.assertEqual(i.conf, {"MODEL": "claude-test", "EFFORT": "high", "THINK": "1"})
        self.assertEqual(i.model, "claude-test")
        self.assertEqual(i.effort, "high")
        self.assertEqual(i.build.ai, "claude")

    def test_claude_args_from_specialties(self):
        i = self._inst(specialties=(self.reg.specialties["auto"],))
        self.assertIn("--dangerously-skip-permissions", i.claude_args)

    def test_is_cowork_and_is_manager_match_by_name(self):
        # The two launcher-recognised specialties; both matched by NAME (see
        # identity.py's constants for why), so a fixture with those names is
        # exactly what the real tree provides.
        reg = scan_all(self.tree({
            "specialty/cowork/tag.info": 'full_description = "recruitable"\n',
            "specialty/cowork/manager/tag.info": 'full_description = "hosts"\n',
        }))
        plain = self._inst()
        coworker = self._inst(specialties=(reg.specialties["cowork"],))
        manager = self._inst(specialties=(reg.specialties["cowork"],
                                          reg.specialties["manager"]))
        self.assertFalse(plain.is_cowork or plain.is_manager)
        self.assertTrue(coworker.is_cowork)
        self.assertFalse(coworker.is_manager)     # recruitable, but cannot command the hub
        self.assertTrue(manager.is_cowork and manager.is_manager)


class TestResolveBuild(TagTreeTestCase):
    def setUp(self):
        self.reg = scan_all(self.full_tree())

    def test_names_resolve_to_objects(self):
        kw = resolve_build(AgentBuild(engine="default", professions=("code",),
                                      specialties=("auto",)), "x", self.reg)
        self.assertIs(kw["engine"], self.reg.engines["default"])
        self.assertEqual(kw["professions"], (self.reg.professions["code"],))
        self.assertEqual(kw["specialties"], (self.reg.specialties["auto"],))

    def test_engine_falls_back_to_default(self):
        # no engine named, agent name isn't an engine either → default
        kw = resolve_build(AgentBuild(), "poet", self.reg)
        self.assertIs(kw["engine"], self.reg.engines["default"])

    def test_ai_falls_back_to_the_trees_default_member(self):
        self.assertIs(resolve_build(AgentBuild(), "poet", self.reg)["ai"], self.reg.default_ai)
        self.assertIs(resolve_build(AgentBuild(ai="claude"), "poet", self.reg)["ai"], self.reg.ais["claude"])


# ============================================================
# Store — instances.toml load/save + legacy-map migration
# ============================================================


class TestStore(TagTreeTestCase):
    def test_ai_is_a_scalar_field_omitted_when_unset(self):
        text = store.dumps({"x__s": store.build_entry(AgentBuild(ai="gemini", engine="quick"), "/w")})
        self.assertIn('ai = "gemini"', text)
        self.assertEqual(store.entry_to_build({"ai": "gemini"}).ai, "gemini")
        self.assertNotIn("ai =", store.dumps({"y__s": store.build_entry(AgentBuild(), "/w")}))

    def test_load_missing_is_empty(self):
        self.assertEqual(store.load(Path("/nonexistent/instances.toml")), {})

    def test_save_load_roundtrip(self):
        p = self.tree({"placeholder": ""}) / "instances.toml"
        m = {"golem__x": {"workspace": "/w", "engine": "golem",
                          "professions": [], "specialties": [], "policies": []}}
        store.save(m, p)
        self.assertEqual(store.load(p), m)

    def test_saved_file_is_toml(self):
        p = self.tree({"placeholder": ""}) / "instances.toml"
        store.save({"golem__x": {"workspace": "/w", "professions": ["code"],
                                 "specialties": [], "policies": []}}, p)
        text = p.read_text()
        self.assertIn("[golem__x]", text)
        self.assertIn('workspace = "/w"', text)
        self.assertIn('professions = ["code"]', text)

    def test_none_values_omitted_and_read_back_absent(self):
        # TOML has no null — build_entry keeps None in the dict, dumps drops
        # it, and load simply doesn't have the key (readers .get() → None).
        p = self.tree({"placeholder": ""}) / "instances.toml"
        store.save({"golem__x": {"workspace": None, "engine": None,
                                 "professions": [], "specialties": [], "policies": []}}, p)
        entry = store.load(p)["golem__x"]
        self.assertNotIn("workspace", entry)
        self.assertNotIn("engine", entry)

    def test_non_bare_instance_id_quoted(self):
        # A future dotted agent name must not corrupt the file — the emitter
        # quotes any key that isn't a TOML bare key.
        p = self.tree({"placeholder": ""}) / "instances.toml"
        m = {"agent.v2__x": {"workspace": "/w", "professions": [],
                             "specialties": [], "policies": []}}
        store.save(m, p)
        self.assertIn('["agent.v2__x"]', p.read_text())
        self.assertEqual(store.load(p), m)

    def test_migrate_translates_modes_onto_axes(self):
        agents = self.tree({"researcher.lego": 'engine = "researcher"\nprofessions = ["code"]\n'})
        out = migrations.migrate_from_maps(
            {"researcher__proj": "/home/u/proj"},
            {"researcher__proj": ["auto", "DooD"]},
            agents,
        )
        self.assertEqual(out["researcher__proj"], {
            "workspace": "/home/u/proj", "engine": "researcher",
            # Legacy `auto` bundled the firewall → both specialties post-split.
            "professions": ["code"], "specialties": ["auto", "firewall", "dood"], "policies": [],
        })

    def test_migrate_web_mode_becomes_profession(self):
        agents = self.tree({"researcher.lego": 'engine = "researcher"\nprofessions = ["code"]\n'})
        out = migrations.migrate_from_maps({}, {"researcher__x": ["web"]}, agents)
        self.assertEqual(sorted(out["researcher__x"]["professions"]), ["code", "webdev"])
        self.assertEqual(out["researcher__x"]["specialties"], [])

    def test_migrate_engine_defaults_to_agent_when_lego_absent(self):
        agents = self.tree({"placeholder": ""})   # no .lego for 'poet'
        out = migrations.migrate_from_maps({"poet__d": "/w"}, {}, agents)
        self.assertEqual(out["poet__d"]["engine"], "poet")


# ============================================================
# Addendums — chain-keyed CLAUDE.md section composer
# ============================================================


def _tag_with_addendum(title, body):
    """Stand-in for compose() input — it only reads `.addendum`."""
    return SimpleNamespace(addendum=(title, body))


_NO_ADDENDUM = SimpleNamespace(addendum=None)


class TestAddendums(unittest.TestCase):
    def test_base_notices_always_present(self):
        out = addendums.compose([])
        self.assertIn(f"## {addendums.ADDENDUM_SECTION_TITLE}", out)
        self.assertIn(addendums.SEEK_SUMMARY.body, out)
        self.assertIn(addendums.MAINTAIN_PRIVACY.body, out)

    def test_tag_addendum_rendered_after_base(self):
        out = addendums.compose([_tag_with_addendum("Firewall", "watch the wall")])
        self.assertIn("### Firewall\n\nwatch the wall", out)
        self.assertLess(out.index("### Project summary"), out.index("### Firewall"))

    def test_addendumless_tags_contribute_nothing(self):
        base_only = addendums.compose([])
        self.assertEqual(addendums.compose([_NO_ADDENDUM, _NO_ADDENDUM]), base_only)

    def test_placeholder_interpolated(self):
        out = addendums.compose([_tag_with_addendum("Firewall", "status: {domain_resolve_status}")])
        self.assertNotIn("{domain_resolve_status}", out)
        self.assertIn("domains_pending_resolve", out)

    def test_empty_placeholder_drops_the_addendum(self):
        # The Credentials case: no optional creds on the host → the whole
        # section disappears rather than rendering a dangling sentence.
        with patch("launch.tags.addendums.installed_cred_clis", return_value=""):
            out = addendums.compose([_tag_with_addendum("Credentials", "tools: {cred_clis}")])
        self.assertNotIn("Credentials", out)

    def test_populated_placeholder_keeps_the_addendum(self):
        with patch("launch.tags.addendums.installed_cred_clis", return_value="gh jira"):
            out = addendums.compose([_tag_with_addendum("Credentials", "tools: {cred_clis}")])
        self.assertIn("tools: gh jira", out)

    def test_nothing_active_renders_empty(self):
        with patch.object(addendums, "BASE_ADDENDUMS", []):
            self.assertEqual(addendums.compose([]), "")

    def test_real_tree_addendums_wired(self):
        # The shipped tree carries the moved notices: code → Credentials,
        # webdev → Headless browser, firewall → Firewall; auto has none.
        from launch.paths import AGENTS_DIR
        reg = scan_all(AGENTS_DIR)
        self.assertEqual(reg.professions["code"].addendum[0], "Credentials")
        self.assertEqual(reg.professions["webdev"].addendum[0], "Headless browser")
        self.assertEqual(reg.specialties["firewall"].addendum[0], "Firewall")
        self.assertIsNone(reg.specialties["auto"].addendum)


class TestWorkspaceReadonly(TagTreeTestCase):
    """The `workspace_readonly` specialty field ({ro}) → Instance property
    that docker_config reads for the /workspace mount mode."""

    def test_field_parsed_from_tag_info(self):
        (s,) = Specialty.scan(self.tree({
            "specialty/read-only/tag.info": 'full_description = "ro"\nworkspace_readonly = true\n',
        }), {}, {})
        self.assertTrue(s.workspace_readonly)

    def test_field_defaults_false(self):
        (s,) = Specialty.scan(self.tree({
            "specialty/plain/tag.info": 'full_description = "x"\n',
        }), {}, {})
        self.assertFalse(s.workspace_readonly)

    def test_instance_property_true_when_any_specialty_asks(self):
        reg = scan_all(_REAL_AGENTS_DIR())
        from launch.tags import Instance, resolve_build, AgentBuild
        from pathlib import Path as _P
        inst = Instance(agent="x", md_path=_P("/fake/x.md"), session="s",
                        workspace="/w", is_brand_new=False,
                        **resolve_build(AgentBuild(specialties=("read-only",)), "x", reg))
        self.assertTrue(inst.workspace_readonly)

    def test_instance_property_false_without_it(self):
        reg = scan_all(_REAL_AGENTS_DIR())
        from launch.tags import Instance, resolve_build, AgentBuild
        from pathlib import Path as _P
        inst = Instance(agent="x", md_path=_P("/fake/x.md"), session="s",
                        workspace="/w", is_brand_new=False,
                        **resolve_build(AgentBuild(specialties=("auto",)), "x", reg))
        self.assertFalse(inst.workspace_readonly)


def _REAL_AGENTS_DIR():
    from launch.paths import AGENTS_DIR
    return AGENTS_DIR


class TestPolicyFragments(TagTreeTestCase):
    """Hidden `policy/_<name>` fragments — a settings fragment a same-named
    specialty claims (the policy-tree twin of `_<name>` image layers). How
    `{ro}` bundles its Write/Edit deny."""

    def test_discover_finds_underscore_fragments(self):
        agents = self.tree({"policy/_read-only/policy.json": '{"permissions": {"deny": ["Write"]}}'})
        frags = Policy.discover_fragments(agents)
        self.assertEqual(set(frags), {"read-only"})

    def test_offered_policies_exclude_underscore_dirs(self):
        agents = self.tree({"policy/_read-only/policy.json": '{"permissions": {"deny": ["Write"]}}'})
        self.assertEqual(Policy.scan(agents), [])   # hidden — not offered

    def test_fragment_with_tag_info_raises(self):
        agents = self.tree({
            "policy/_bad/policy.json": "{}",
            "policy/_bad/tag.info": 'full_description = "no"\n',
        })
        with self.assertRaisesRegex(TagError, "must not contain tag.info"):
            Policy.discover_fragments(agents)

    def test_fragment_missing_json_raises(self):
        agents = self.tree({"policy/_bad/placeholder": ""})
        with self.assertRaisesRegex(TagError, "missing policy.json"):
            Policy.discover_fragments(agents)

    def test_specialty_claims_same_named_fragment(self):
        agents = self.tree({
            "specialty/read-only/tag.info": 'full_description = "ro"\nworkspace_readonly = true\n',
            "policy/_read-only/policy.json": '{"permissions": {"deny": ["Write", "Edit"]}}',
        })
        reg = scan_all(agents)
        ro = reg.specialties["read-only"]
        self.assertEqual(ro.policy_dir.name, "_read-only")
        self.assertEqual(ro.load_fragment(), {"permissions": {"deny": ["Write", "Edit"]}})

    def test_unclaimed_fragment_fails_scan(self):
        agents = self.tree({"policy/_orphan/policy.json": "{}"})   # no specialty 'orphan'
        with self.assertRaisesRegex(TagError, "no matching specialty"):
            scan_all(agents)

    def test_specialty_without_fragment_loads_empty(self):
        agents = self.tree({"specialty/auto/tag.info": 'full_description = "a"\n'})
        (s,) = Specialty.scan(agents, {}, Policy.discover_fragments(agents))
        self.assertEqual(s.load_fragment(), {})

    def test_real_tree_read_only_bundles_both(self):
        # The shipped {ro} specialty mounts :ro AND claims the _read-only
        # fragment that denies the edit tools — one tag, defense in depth.
        reg = scan_all(_REAL_AGENTS_DIR())
        ro = reg.specialties["read-only"]
        self.assertTrue(ro.workspace_readonly)
        self.assertEqual(ro.load_fragment(), {"permissions": {"deny": ["Write", "Edit", "NotebookEdit"]}})
        self.assertNotIn("read-only", reg.policies)   # the fragment is not an offered policy


class TestLabelError(unittest.TestCase):
    """`label_error` — the ONE legality rule every name a person types is held
    to (an instance's session, a cluster's session, a member's role). A name
    lands as a directory, a docker `--name`, a tmux address, a herdr label, a
    git branch component and a cowork token; each rejected character names the
    place it would misbehave, in the shape a form validator returns."""

    def test_a_legal_name_is_none(self):
        for name in ("golem", "bug-investigator", "my_project", "a__b", "v2", "X"):
            with self.subTest(name=name):
                self.assertIsNone(label_error(name))

    def test_dockers_charset_is_the_catch_all(self):
        # Anything the table and the printable check let through still has
        # to survive `docker run --name claude-code_<label>`: an accent, a
        # `!`, a quote would otherwise fail there, at the end of a build.
        for name in ("café", "a!b", "a#b", "a'b", "a=b", "a,b"):
            with self.subTest(name=name):
                self.assertIn("docker", label_error(name))
        # ...while the table's own characters keep their more specific reason.
        self.assertNotIn("docker", label_error("a.b"))

    def test_empty_is_the_first_complaint(self):
        self.assertEqual(label_error(""), "cannot be empty")

    def test_every_forbidden_character_is_refused_with_its_reason(self):
        # Iterates the real table, so a character added there is tested by
        # construction — no list to keep in step.
        for char, reason in FORBIDDEN_IN_LABELS.items():
            with self.subTest(char=repr(char)):
                error = label_error(f"a{char}b")
                self.assertIsNotNone(error)
                self.assertIn(reason, error)

    def test_whitespace_is_named_as_such(self):
        # `' '` in a message reads as a typo; the word does not.
        self.assertIn("whitespace", label_error("a b"))
        self.assertNotIn("whitespace", label_error("a:b"))

    def test_control_and_invisible_characters_are_refused_as_a_class(self):
        for bad in ("a\x1bb", "a\x00b", "a\u200bb", "a\rb"):
            with self.subTest(label=bad):
                self.assertIn("control or invisible", label_error(bad))

    def test_the_first_problem_is_reported_not_all(self):
        # A str-or-None contract: ONE message a field can show, never a list.
        error = label_error("a:b/c")
        self.assertIsInstance(error, str)
        self.assertIn("':'", error)
        self.assertNotIn("'/'", error)


if __name__ == "__main__":
    unittest.main()
