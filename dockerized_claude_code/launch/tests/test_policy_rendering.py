"""Tests for the policy words and their rendering: launch/tags/rules.py (a
policy's rules in the launcher's words), launch/tags/policy_mapping.py (one
harness's spelling of them) and `Harness.render_policy`.

The Claude Code rendering is pinned against the policy.json fragments it
replaced on 2026-09-26: every policy renders to exactly its former rules,
compared as sets per settings key with NO exemption list, which the gate that
designed the words (policy-mapping, on the cluster queue) made the condition
of replacing hand-written fragments at all. The Gemini CLI rendering was run
once through that CLI's own policy engine (v0.61.0, plans/ISSUES.md says
how); here its shape is pinned."""

import tempfile
import tomllib
import unittest
from pathlib import Path, PurePosixPath

from launch.ai import CLAUDE_CODE, GEMINI_CLI
from launch.tags import TagError
from launch.tags.policy_mapping import POLICY_MAPPING_FILE, parse_policy_mapping, rules_file_text
from launch.tags.rules import CAPABILITIES, Actions, Rules, parse_rules
from launch.tests.fixtures import REGISTRY

CLAUDE = REGISTRY.harnesses[CLAUDE_CODE.key]
GEMINI = REGISTRY.harnesses[GEMINI_CLI.key]

# The fragments tag.rules replaced, verbatim (agents/policy/*/policy.json as
# of 2026-09-26). Frozen on purpose: a mapping or rule change that alters what
# Claude Code receives has to change this table too, deliberately.
FORMER_CLAUDE_SETTINGS = {
    "all-actions": {"permissions": {"allow": ["Bash", "WebFetch", "WebSearch", "Read", "Glob", "Grep",
                                              "Write", "Edit", "NotebookEdit", "Task"]}},
    "free-bash": {"permissions": {"allow": ["Bash"]}},
    "no-git": {"permissions": {"deny": ["Bash(git *)", "Bash(git:*)"]}},
    "no-net": {"permissions": {"deny": ["WebSearch", "WebFetch", "Bash(curl *)", "Bash(curl:*)",
                                        "Bash(wget *)", "Bash(wget:*)"]}},
    "no-sudo": {"permissions": {"deny": ["Bash(sudo *)", "Bash(sudo:*)"]}},
    "plan-first": {"permissions": {"defaultMode": "plan"}},
    "vcs-safe": {"permissions": {"deny": ["Bash(git push*)", "Bash(git push:*)",
                                          "Bash(git reset --hard*)", "Bash(git reset --hard:*)"]}},
    "web-research": {"permissions": {"allow": ["WebSearch", "WebFetch"]}},
    "read-only": {"permissions": {"deny": ["Write", "Edit", "NotebookEdit"]}},   # {ro}'s claimed policy/_read-only
}


def claude_pairs(*, words=(), stems=()) -> list[str]:
    """A command's two Bash spellings, as harness/claude-code/policy.mapping
    [shell] writes them — the templates the fragments above already pin."""
    return ([rule for w in words for rule in (f"Bash({w} *)", f"Bash({w}:*)")]
            + [rule for s in stems for rule in (f"Bash({s}*)", f"Bash({s}:*)")])


# Policies added since the migration, and the rendering each is meant to get.
# Written out on purpose, like the fragments above: a change to what Claude
# Code receives has to change this table too, deliberately.
ADDED_CLAUDE_SETTINGS = {
    "no-git-write": {"permissions": {"deny": claude_pairs(words=["git merge"], stems=[
    "git -", "git add", "git am", "git apply", "git archimport", "git bisect", "git branch", "git bundle",
    "git checkout", "git cherry-pick", "git citool", "git clean", "git clone", "git commit", "git config",
    "git credential", "git cvsimport", "git fast-import", "git filter-branch", "git filter-repo", "git gc",
    "git gui", "git hash-object", "git http-push", "git index-pack", "git init", "git lfs checkout",
    "git lfs dedup", "git lfs install", "git lfs lock", "git lfs migrate", "git lfs prune", "git lfs push",
    "git lfs track", "git lfs uninstall", "git lfs unlock", "git lfs untrack", "git lfs update",
    "git maintenance", "git merge-file", "git merge-index", "git merge-octopus", "git merge-one-file",
    "git merge-ours", "git merge-recursive", "git merge-resolve", "git merge-subtree", "git mergetool",
    "git mktag", "git mktree", "git multi-pack-index", "git mv", "git notes", "git p4", "git pack-objects",
    "git pack-refs", "git prune", "git push", "git quiltimport", "git read-tree", "git rebase",
    "git receive-pack", "git reflog", "git remote add", "git remote prune", "git remote remove",
    "git remote rename", "git remote rm", "git remote set-branches", "git remote set-head",
    "git remote set-url", "git repack", "git replace", "git replay", "git rerere", "git reset", "git restore",
    "git revert", "git rm", "git send-email", "git send-pack", "git sparse-checkout", "git stage",
    "git stash", "git submodule", "git subtree", "git svn", "git switch", "git symbolic-ref", "git tag",
    "git unpack-objects", "git update", "git worktree", "git write-tree",
    ])}},
}
PINNED_CLAUDE_SETTINGS = FORMER_CLAUDE_SETTINGS | ADDED_CLAUDE_SETTINGS


def every_shipped_rules() -> dict[str, Rules]:
    """Every set of rules the tree ships: each offered policy's, and each
    specialty's claimed fragment's — iterated, never listed, so a new policy
    cannot slip past the equivalence below."""
    rules = {name: policy.rules for name, policy in REGISTRY.policies.items()}
    rules |= {name: s.fragment.rules for name, s in REGISTRY.specialties.items() if s.fragment and s.fragment.rules}
    return rules


def as_sets(settings: dict, prefix: str = "") -> dict:
    """A settings fragment flattened to dotted keys, lists as sets: content
    only, since order within a permission list is not part of its meaning."""
    out: dict = {}
    for key, value in settings.items():
        dotted = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out |= as_sets(value, dotted)
        else:
            out[dotted] = set(value) if isinstance(value, list) else value
    return out


def rules(text: str) -> Rules:
    return parse_rules(tomllib.loads(text), Path("tag.rules"))


class TestRulesVocabulary(unittest.TestCase):
    """tag.rules — what a policy allows, denies or demands, in the launcher's
    words. Every table and key is checked, so a typo cannot render a rule
    that silently covers nothing."""

    def test_each_table_parses(self):
        self.assertEqual(rules('[allow]\ntools = ["web", "read"]\nshell = ["make"]\nshell_stems = ["npm run"]\n'),
                         Rules(allow=Actions(tools=("web", "read"), shell=("make",), shell_stems=("npm run",))))
        self.assertEqual(rules('[deny]\nshell = ["sudo"]\n'), Rules(deny=Actions(shell=("sudo",))))
        self.assertEqual(rules('[demand]\nmode = "plan"\n'), Rules(mode="plan"))

    def test_tables_names_what_the_file_holds_in_order(self):
        self.assertEqual(rules('[demand]\nmode = "plan"\n[deny]\nshell = ["git"]\n').tables, ("deny", "demand"))

    def test_all_is_an_allow_word_and_stands_alone(self):
        self.assertEqual(rules("[allow]\nall = true\n"), Rules(allow=Actions(all=True)))
        for text, message in (("[deny]\nall = true\n", r"`all` is an \[allow\] word"),
                              ("[allow]\nall = false\n", "takes only true"),
                              ('[allow]\nall = true\ntools = ["web"]\n', "already allows every tool — drop tools")):
            with self.subTest(text=text), self.assertRaisesRegex(TagError, message):
                rules(text)

    def test_unknown_words_are_refused_naming_the_vocabulary(self):
        for text, message in (('[grant]\ntools = ["web"]\n', r"unknown table \[grant\]"),
                              ('[deny]\ncommands = ["git"]\n', "unknown key 'commands'"),
                              ('[deny]\ntools = ["network"]\n', "tools names 'network' — the capabilities are: shell, web"),
                              ('[demand]\nmode = "yolo"\n', "mode must be one of: plan"),
                              ('[demand]\nmode = "plan"\nstrict = true\n', "takes exactly one key, mode")):
            with self.subTest(text=text), self.assertRaisesRegex(TagError, message):
                rules(text)

    def test_shell_entries_are_single_spaced_command_words(self):
        # Quotes, globs, parentheses and backslashes each mean something
        # different to each CLI's matcher; a rule nobody can predict is out.
        for word in ("git reset --hard", "npm run build", "python3.12", "./configure", "a=1", "x@y", "50%"):
            with self.subTest(good=word):
                self.assertEqual(rules(f'[deny]\nshell = ["{word}"]\n').deny.shell, (word,))
        for word in ("git  push", " git", "git ", "git*", "a(b)", "echo \\\"x\\\"", "rm $(x)", "a\\\\b"):
            with self.subTest(bad=word), self.assertRaisesRegex(TagError, "must be command words, single-spaced"):
                rules(f'[deny]\nshell = ["{word}"]\n')

    def test_a_word_named_twice_or_as_both_word_and_stem_is_refused(self):
        with self.assertRaisesRegex(TagError, "names 'git' twice"):
            rules('[deny]\nshell = ["git", "git"]\n')
        with self.assertRaisesRegex(TagError, "both a shell word and a stem"):
            rules('[deny]\nshell = ["git push"]\nshell_stems = ["git push"]\n')

    def test_a_file_or_table_that_names_nothing_is_refused(self):
        with self.assertRaisesRegex(TagError, "names no rule"):
            rules("")
        with self.assertRaisesRegex(TagError, r"\[deny\] names nothing"):
            rules("[deny]\n")


class TestClaudeRenderingKeepsEveryFormerFragment(unittest.TestCase):
    """Every shipped rule set, rendered for Claude Code, is exactly the
    fragment it replaced — five word pairs and two stem pairs included — or,
    for a policy added since, exactly the rendering pinned for it."""

    def test_every_shipped_rule_set_has_a_pinned_rendering(self):
        self.assertEqual(set(every_shipped_rules()), set(PINNED_CLAUDE_SETTINGS))

    def test_each_renders_to_its_pinned_settings_with_no_exemption(self):
        for name, policy_rules in every_shipped_rules().items():
            with self.subTest(policy=name):
                rendering = CLAUDE.render_policy(name, policy_rules)
                self.assertEqual(rendering.unmapped, ())
                self.assertEqual(rendering.rules, ())   # Claude Code keeps no rules file
                self.assertEqual(as_sets(rendering.settings), as_sets(PINNED_CLAUDE_SETTINGS[name]))

    def test_a_word_keeps_its_boundary_and_a_stem_does_not(self):
        # The distinction the hand-written fragments drew: `git` must never
        # deny gitk, `git push` must also deny git pushall.
        word = CLAUDE.render_policy("w", rules('[deny]\nshell = ["git"]\n')).settings["permissions"]["deny"]
        stem = CLAUDE.render_policy("s", rules('[deny]\nshell_stems = ["git push"]\n')).settings["permissions"]["deny"]
        self.assertEqual(word, ["Bash(git *)", "Bash(git:*)"])
        self.assertEqual(stem, ["Bash(git push*)", "Bash(git push:*)"])


class TestGeminiRendering(unittest.TestCase):
    """Gemini CLI's Policy Engine records, one policy at a time."""

    def records(self, name):
        return list(GEMINI.render_policy(name, every_shipped_rules()[name]).rules)

    def test_a_word_becomes_a_command_prefix(self):
        self.assertEqual(self.records("no-git"), [{
            "toolName": "run_shell_command", "commandPrefix": "git", "decision": "deny",
            "priority": 999, "denyMessage": "The launcher's no-git policy denies this.",
        }])

    def test_a_stem_becomes_an_escaped_regex(self):
        # The engine anchors a commandRegex at the command's start, so the
        # stem itself is the whole pattern, with its metacharacters escaped.
        self.assertEqual([r["commandRegex"] for r in self.records("vcs-safe")], ["git push", "git reset --hard"])
        escaped = GEMINI.render_policy("x", rules('[deny]\nshell_stems = ["make.bat", "g++"]\n')).rules
        self.assertEqual([r["commandRegex"] for r in escaped], [r"make\.bat", r"g\+\+"])

    def test_a_capability_is_one_record_naming_its_tools(self):
        (record,) = self.records("read-only")
        self.assertEqual(record["toolName"], ["write_file", "replace"])

    def test_all_covers_every_capability_the_mapping_lists(self):
        named = [tool for record in self.records("all-actions") for tool in record["toolName"]]
        self.assertEqual(sorted(named), sorted(tool for _, tools in GEMINI.policy_mapping.tools for tool in tools))
        self.assertEqual([capability for capability, _ in GEMINI.policy_mapping.tools], list(CAPABILITIES))

    def test_allows_never_reach_plan_mode_and_denies_reach_every_mode(self):
        # Plan mode's own limits are default-tier rules; an allow ranked
        # above them in plan mode would lift them. A deny names no modes, so
        # it holds in all of them, YOLO included.
        for name, policy_rules in every_shipped_rules().items():
            for record in GEMINI.render_policy(name, policy_rules).rules:
                with self.subTest(policy=name, decision=record["decision"]):
                    if record["decision"] == "allow":
                        self.assertEqual(record["modes"], ["default", "autoEdit", "yolo"])
                    else:
                        self.assertNotIn("modes", record)

    def test_a_deny_outranks_every_allow_and_a_persisted_always_allow(self):
        # Gemini ranks a rule at TIER + priority/1000 and takes the highest
        # match. Its constants (v0.61.0, packages/core/src/policy/config.ts):
        # ADMIN_POLICY_TIER 5, USER_POLICY_TIER 4, and an "always allow" the
        # CLI saves for itself lands at the user tier + 950/1000. The margin
        # is theirs, not ours: pinned so a lower deny priority cannot slip in.
        admin_tier, user_tier, always_allow = 5, 4, 4 + 950 / 1000
        fields = dict(GEMINI.policy_mapping.rule_fields)
        deny, allow = dict(fields["deny"])["priority"], dict(fields["allow"])["priority"]
        self.assertGreater(deny, allow)
        self.assertGreater(admin_tier + deny / 1000, always_allow)   # the fixed tier's always-on copy
        self.assertGreater(user_tier + deny / 1000, always_allow)    # the per-instance file: the margin that decides if --policy is ever dropped

    def test_a_demanded_mode_renders_into_settings(self):
        rendering = GEMINI.render_policy("plan-first", every_shipped_rules()["plan-first"])
        self.assertEqual((rendering.settings, rendering.rules), ({"general": {"defaultApprovalMode": "plan"}}, ()))

    def test_the_args_name_the_file_inside_the_config_root(self):
        self.assertEqual(GEMINI.policy_args("/home/claude/.gemini"),
                         ("--policy", "/home/claude/.gemini/policies/launcher.toml"))
        self.assertEqual(CLAUDE.policy_args("/home/claude/.claude"), ())
        self.assertIsNone(CLAUDE.policy_file)

    def test_the_file_sits_in_the_clis_own_user_policies_dir(self):
        # Storage.getUserPoliciesDir = <config root>/policies: if the flag is
        # ever ignored, the CLI still loads the file, one tier down.
        self.assertEqual(PurePosixPath(GEMINI.policy_file).parent, PurePosixPath("policies"))


class TestRulesFileText(unittest.TestCase):
    """The policy-engine rules file: JSON literals TOML reads identically."""

    def test_every_shipped_policy_round_trips_through_toml(self):
        sections = [(name, GEMINI.render_policy(name, r).rules) for name, r in every_shipped_rules().items()]
        loaded = tomllib.loads(rules_file_text(sections))
        self.assertEqual(loaded["rule"], [dict(record) for _, records in sections for record in records])

    def test_each_policy_is_introduced_by_its_name(self):
        text = rules_file_text([("no-git", GEMINI.render_policy("no-git", every_shipped_rules()["no-git"]).rules)])
        self.assertIn("# no-git\n[[rule]]\n", text)
        self.assertTrue(text.startswith("# Written by the launcher"))

    def test_no_rules_is_still_a_valid_file(self):
        self.assertEqual(tomllib.loads(rules_file_text([("none", ())])), {})

    def test_booleans_and_unsupported_values(self):
        self.assertIn("interactive = true", rules_file_text([("p", ({"interactive": True},))]))
        with self.assertRaises(TypeError):
            rules_file_text([("p", ({"priority": 1.5},))])


class TestUnmapped(unittest.TestCase):
    """What a harness cannot say is reported, never invented."""

    def test_a_harness_without_a_mapping_maps_nothing(self):
        codex = REGISTRY.harnesses["codex-cli"]
        self.assertIsNone(codex.policy_mapping)
        rendering = codex.render_policy("mix", rules('[deny]\ntools = ["web"]\nshell = ["git"]\nshell_stems = ["git push"]\n'))
        self.assertEqual([str(word) for word in rendering.unmapped],
                         ["[deny] tools 'web'", "[deny] shell 'git'", "[deny] shell_stems 'git push'"])
        self.assertEqual((rendering.settings, rendering.rules), ({}, ()))

    def test_an_unmapped_mode_is_a_demand(self):
        codex = REGISTRY.harnesses["codex-cli"]
        (word,) = codex.render_policy("plan-first", rules('[demand]\nmode = "plan"\n')).unmapped
        self.assertEqual(str(word), "[demand] mode 'plan'")


class TestPolicyMappingFile(unittest.TestCase):
    """policy.mapping — checked in full, so a mapping cannot quietly render a
    rule narrower than it reads."""

    SETTINGS = ('format = "settings"\n[tools]\nshell = ["Bash"]\n'
                '[shell]\nword = ["Bash({command} *)"]\nstem = ["Bash({command}*)"]\n'
                '[lists]\nallow = "permissions.allow"\ndeny = "permissions.deny"\n'
                '[fixed]\npath = "/etc/cli/managed.json"\n')
    ENGINE = ('format = "policy-engine"\n[tools]\nshell = ["run_shell_command"]\n'
              '[shell]\ntool = "run_shell_command"\n'
              '[file]\npath = "policies/launcher.toml"\nargs = ["--policy", "{path}"]\n'
              '[rules.deny]\npriority = 999\n[rules.allow]\npriority = 100\n'
              '[fixed]\npath = "/etc/cli/policies/always.toml"\n')

    def parse(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / POLICY_MAPPING_FILE).write_text(text)
            return parse_policy_mapping(Path(tmp))

    def test_no_file_is_no_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(parse_policy_mapping(Path(tmp)))

    def test_the_minimal_forms_parse(self):
        self.assertEqual(self.parse(self.SETTINGS).lists, (("allow", "permissions.allow"), ("deny", "permissions.deny")))
        self.assertEqual(self.parse(self.ENGINE).file_path, "policies/launcher.toml")
        self.assertEqual(self.parse(self.ENGINE).fixed_path, "/etc/cli/policies/always.toml")

    def test_a_stated_absence_of_a_fixed_tier_parses_to_none(self):
        # "a harness that lacks one is a None, never a guess" — but stated:
        # the reason sits in the file, beside the data.
        without = self.SETTINGS.replace('path = "/etc/cli/managed.json"', 'none = "this CLI reads no system path"')
        self.assertIsNone(self.parse(without).fixed_path)

    def test_the_policy_engine_flag_follows_the_fixed_tier(self):
        # With a fixed tier the system dir holds a .toml and --admin-policy
        # is ignored; without one, --admin-policy is the stronger tier.
        no_fixed = self.ENGINE.replace('path = "/etc/cli/policies/always.toml"', 'none = "no system dir"')
        admin = no_fixed.replace('"--policy"', '"--admin-policy"')
        self.assertEqual(self.parse(admin).args("/c"), ("--admin-policy", "/c/policies/launcher.toml"))
        with self.assertRaisesRegex(TagError, "use --policy with no fixed tier"):
            self.parse(no_fixed)
        with self.assertRaisesRegex(TagError, "use --admin-policy, which Gemini CLI ignores"):
            self.parse(self.ENGINE.replace('"--policy"', '"--admin-policy"'))

    def test_malformed_mappings_are_refused(self):
        cases = {
            'format = "yaml"\n': "format must be one of: settings, policy-engine",
            self.SETTINGS + '[file]\npath = "x.toml"\nargs = ["{path}"]\n': r"\[file\] is not a table of the settings format",
            self.SETTINGS.replace('shell = ["Bash"]', 'files = ["Read"]'): "names capability 'files'",
            self.SETTINGS.replace('shell = ["Bash"]', 'shell = ["Bash"]\nwrite = ["Bash"]'): "sits under both shell and write",
            self.SETTINGS.replace('stem = ["Bash({command}*)"]\n', ""): "takes exactly word and stem",
            self.SETTINGS.replace("Bash({command} *)", "Bash(git *)"): r"must use \{command\} and no other placeholder",
            self.SETTINGS.replace("Bash({command} *)", "Bash({command} {flags})"): r"must use \{command\} and no other placeholder",
            self.SETTINGS.replace('deny = "permissions.deny"\n', ""): "must name the settings key for both allow and deny",
            self.SETTINGS + '[mode.yolo]\n"x" = "y"\n': r"\[mode\.yolo\] is not a mode",
            self.SETTINGS + '[mode.plan]\n"permissions.defaultMode" = 1\n': r"\[mode\.plan\] must be a non-empty table",
            self.SETTINGS + '[mode.plan]\n"permissions.deny" = "x"\n': "collides with the rule list",
            self.ENGINE.replace('tool = "run_shell_command"', 'tool = "bash"'): r"takes exactly tool, one of \[tools\] shell",
            self.ENGINE.replace('"policies/launcher.toml"', '"/etc/x.toml"'): "must be a .toml path inside the config root",
            self.ENGINE.replace('"policies/launcher.toml"', '"../x.toml"'): "must be a .toml path inside the config root",
            self.ENGINE.replace('"policies/launcher.toml"', '"policies/launcher.json"'): "must be a .toml path",
            self.ENGINE.replace('["--policy", "{path}"]', '["--policy"]'): r"names the file with \{path\}",
            self.SETTINGS.replace('[fixed]\npath = "/etc/cli/managed.json"\n', ""): r"\[fixed\] must state the fixed tier",
            self.SETTINGS.replace('path = "/etc/cli/managed.json"', 'path = "/etc/a.json"\nnone = "x"'): r"\[fixed\] must state",
            self.SETTINGS.replace('path = "/etc/cli/managed.json"', 'none = " "'): "none must say why",
            self.SETTINGS.replace('"/etc/cli/managed.json"', '"etc/cli/managed.json"'): "must be an absolute .json path",
            self.SETTINGS.replace('"/etc/cli/managed.json"', '"/etc/cli/managed.toml"'): "must be an absolute .json path",
            self.ENGINE.replace('"/etc/cli/policies/always.toml"', '"/etc/cli/../always.toml"'): "must be an absolute .toml path",
            self.ENGINE.replace("[rules.allow]\npriority = 100\n", ""): r"must hold \[rules\.allow\] and \[rules\.deny\]",
            self.ENGINE + '[rules.deny.x]\n': "sets 'x'",
            self.ENGINE.replace("[rules.deny]\n", '[rules.deny]\ndecision = "deny"\n'): "the decision is the stance's own word",
            self.ENGINE.replace("priority = 999", "priority = 1000"): "priority must be an integer from 0 to 999",
            self.ENGINE.replace("priority = 999", "priority = 50"): "a deny has to outrank every allow",
            self.ENGINE.replace("priority = 100", 'priority = 100\nmodes = "plan"'): "modes must be list",
        }
        for text, message in cases.items():
            with self.subTest(expect=message), self.assertRaisesRegex(TagError, message):
                self.parse(text)


if __name__ == "__main__":
    unittest.main()
