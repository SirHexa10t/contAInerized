"""Tests for the FIXED tier: the always-on policies' denies, baked into the
image as root at a system path each CLI reads whatever config root or flags
it is started with (gate fixed-tier, 2026-09-26) — so a second copy of the
CLI an agent starts itself still obeys them.

The pieces, each pinned here: every policy.mapping STATES its fixed tier
(`[fixed] path`, or `none` with the reason); `Policy.scan` holds always-on
policies to deny, because a fixed allow would outrank every per-instance
deny; `agents_crud.fixed_policy` renders what gets baked, from the same
renderings every instance gets; `container_env` carries it to the build as
two build args (tested in test_container_env); and each harness Dockerfile
writes it as root, FAILING the build rather than baking nothing — its shell
is run here for real, against a scratch path."""

import base64
import json
import os
import re
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

from launch.agents_crud import fixed_policy
from launch.ai import ADAPTERS
from launch.paths import AGENTS_DIR
from launch.tags import Policy, TagError
from launch.tags.base import read_toml
from launch.tests.fixtures import REGISTRY

HARNESS_DIR = AGENTS_DIR / "harness"
FIXED_ARGS = ("FIXED_POLICY_PATH", "FIXED_POLICY_B64", "FIXED_POLICY_COUNT")


def dockerfile_harnesses() -> list[str]:
    """Every harness the launcher can build — found on disk, never listed."""
    return sorted(h for h, tag in REGISTRY.harnesses.items() if tag.dockerfile is not None)


def fixed_block(harness: str) -> str:
    """The Dockerfile text from the fixed tier's comment to its closing
    `USER claude` — the block both harness Dockerfiles must carry verbatim."""
    text = (HARNESS_DIR / harness / "Dockerfile").read_text()
    match = re.search(r"# The ALWAYS-ON policies' denies at this CLI's FIXED tier.*?\nUSER claude\n", text, re.S)
    assert match, f"{harness}'s Dockerfile has no fixed-tier block"
    return match.group(0)


def fixed_run(harness: str) -> str:
    """The block's RUN command, as the shell the build hands it to."""
    block = fixed_block(harness)
    return block[block.index("RUN ") + len("RUN "):block.rindex("\nUSER claude")]


class TestWhatGetsBaked(unittest.TestCase):
    """fixed_policy: the always-on denies in each format's own shape."""

    def test_claude_code_gets_managed_settings(self):
        fixed = fixed_policy(REGISTRY.harnesses["claude-code"], REGISTRY)
        self.assertEqual(fixed.path, "/etc/claude-code/managed-settings.json")
        self.assertEqual(json.loads(fixed.text), {"permissions": {"deny": ["Bash(sudo *)", "Bash(sudo:*)"]}})
        self.assertEqual(fixed.rules, 2)   # what the build counts back out of the baked file

    def test_gemini_cli_gets_a_system_policy_file(self):
        fixed = fixed_policy(REGISTRY.harnesses["gemini-cli"], REGISTRY)
        self.assertEqual(fixed.path, "/etc/gemini-cli/policies/launcher-always-on.toml")
        self.assertTrue(fixed.text.startswith("# Baked into the image by the launcher"))
        (rule,) = tomllib.loads(fixed.text)["rule"]
        self.assertEqual((rule["commandPrefix"], rule["decision"], rule["priority"]), ("sudo", "deny", 999))
        self.assertEqual(fixed.rules, 1)

    def test_a_harness_without_a_mapping_has_no_fixed_tier(self):
        self.assertIsNone(fixed_policy(REGISTRY.harnesses["codex-cli"], REGISTRY))

    def test_the_baked_copy_is_the_always_on_part_of_every_instances_rendering(self):
        # Two renderings of one source now exist; this is what stops them
        # drifting apart (agent-writer, gate fixed-tier).
        always_on = [p for p in REGISTRY.policies.values() if p.always_on]
        self.assertTrue(always_on)
        claude = REGISTRY.harnesses["claude-code"]
        per_instance = {rule for p in always_on
                        for rule in claude.render_policy(p.name, p.rules).settings["permissions"]["deny"]}
        self.assertEqual(set(json.loads(fixed_policy(claude, REGISTRY).text)["permissions"]["deny"]), per_instance)
        gemini = REGISTRY.harnesses["gemini-cli"]
        self.assertEqual(tomllib.loads(fixed_policy(gemini, REGISTRY).text)["rule"],
                         [dict(record) for p in always_on for record in gemini.render_policy(p.name, p.rules).rules])


class TestAlwaysOnMeansDeny(unittest.TestCase):
    """The scan rule that makes "only denies reach the fixed tier" a
    guarantee rather than a habit."""

    def scan(self, info, rules):
        with tempfile.TemporaryDirectory() as tmp:
            tag = Path(tmp) / "policy" / "x"
            tag.mkdir(parents=True)
            (tag / "tag.info").write_text(info)
            (tag / "tag.rules").write_text(rules)
            return Policy.scan(Path(tmp))

    def test_an_always_on_allow_is_refused_saying_why(self):
        with self.assertRaisesRegex(TagError, r'always_on = true needs stance = "deny".*outrank every per-instance deny'):
            self.scan('full_description = "x"\nstance = "allow"\nalways_on = true\n', '[allow]\ntools = ["web"]\n')

    def test_an_always_on_demand_is_refused(self):
        with self.assertRaisesRegex(TagError, "could not differ per cluster member"):
            self.scan('full_description = "x"\nstance = "demand"\nalways_on = true\n', '[demand]\nmode = "plan"\n')

    def test_an_always_on_deny_scans(self):
        (policy,) = self.scan('full_description = "x"\nstance = "deny"\nalways_on = true\n', '[deny]\nshell = ["sudo"]\n')
        self.assertTrue(policy.always_on)


class TestEveryBuildableHarnessBakesIt(unittest.TestCase):
    """The tree's own harnesses: the mapping, the build args and the
    Dockerfile must all say the same thing, or the tier silently vanishes."""

    def test_every_harness_the_launcher_can_run_has_a_policy_mapping(self):
        for key in ADAPTERS:
            with self.subTest(harness=key):
                self.assertIsNotNone(REGISTRY.harnesses[key].policy_mapping)

    def test_every_buildable_harness_names_a_fixed_path_and_forwards_both_args(self):
        self.assertTrue(dockerfile_harnesses())
        for key in dockerfile_harnesses():
            with self.subTest(harness=key):
                harness = REGISTRY.harnesses[key]
                self.assertIsNotNone(harness.policy_mapping.fixed_path)
                self.assertLessEqual(set(FIXED_ARGS), set(harness.docker.build_arg_forward))

    def test_the_block_is_the_same_in_every_harness_dockerfile(self):
        blocks = {key: fixed_block(key) for key in dockerfile_harnesses()}
        self.assertEqual(len(set(blocks.values())), 1, "the fixed-tier block has drifted between Dockerfiles")

    def test_the_block_comes_after_the_cli_install(self):
        # So a changed policy rebuilds this step alone, never the CLI.
        for key in dockerfile_harnesses():
            with self.subTest(harness=key):
                text = (HARNESS_DIR / key / "Dockerfile").read_text()
                self.assertLess(text.index('RUN : "${SOFTWARE_STACK_REFRESH}" \\\n && '), text.index(fixed_block(key)))

    def test_geminis_flag_follows_its_fixed_tier(self):
        # The system dir [fixed] fills makes Gemini ignore --admin-policy, so
        # the per-instance file goes in by --policy (the parser refuses the
        # other pairing; this pins the tree's choice).
        gemini = REGISTRY.harnesses["gemini-cli"]
        self.assertIsNotNone(gemini.policy_mapping.fixed_path)
        self.assertEqual(gemini.policy_args("/c")[0], "--policy")


class TestTheBlockRunsAsTheBuildRunsIt(unittest.TestCase):
    """The Dockerfile block's own shell, run by /bin/sh as `docker build`
    runs it, against a scratch path instead of /etc."""

    def run_block(self, path: str | None, content: str | None, count: int | None, *, owner: str = "0:755"):
        command = fixed_run("claude-code").replace('"0:755"', f'"{owner}"')
        env = {**os.environ, "FIXED_POLICY_PATH": path or "",
               "FIXED_POLICY_B64": base64.b64encode(content.encode()).decode() if content is not None else "",
               "FIXED_POLICY_COUNT": "" if count is None else str(count)}
        return subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.me = f"{os.getuid()}:755"

    def test_a_missing_arg_fails_the_build_instead_of_baking_nothing(self):
        # No defaults: an unstaged arg must not quietly become an empty bake —
        # least of all the count, where 0 is also a real value.
        path = f"{self.tmp.name}/etc/x/a.json"
        for args in ((None, "{}", 0), (path, None, 0), (path, "{}", None)):
            with self.subTest(args=args):
                done = self.run_block(*args, owner=self.me)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("no always-on policy was passed", done.stderr)

    def test_the_rendered_file_lands_decoded_in_a_new_755_dir(self):
        for harness in ("claude-code", "gemini-cli"):
            with self.subTest(harness=harness):
                fixed = fixed_policy(REGISTRY.harnesses[harness], REGISTRY)
                path = f"{self.tmp.name}/{harness}{fixed.path}"
                done = self.run_block(path, fixed.text, fixed.rules, owner=self.me)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertEqual(Path(path).read_text(), fixed.text)
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o644)

    def test_a_file_without_the_rendered_rules_fails_the_build(self):
        # "Non-empty and parses" would pass every one of these: the check is
        # on the guarantee — as many rules as the launcher rendered.
        comments_only = "# Baked into the image by the launcher\n# (nothing interpolated)\n"
        for name, content, count in (("a.toml", comments_only, 1), ("b.json", '{"permissions": {"deny": []}}', 2),
                                     ("c.json", "{}", 2), ("d.toml", "[[rule]]\ntoolName = \"x\"\n", 2)):
            with self.subTest(file=name):
                done = self.run_block(f"{self.tmp.name}/etc/x/{name}", content, count, owner=self.me)
                self.assertNotEqual(done.returncode, 0)
                self.assertIn("the launcher rendered", done.stderr)

    def test_a_tree_with_no_always_on_policy_still_bakes_a_valid_empty_file(self):
        # The designed empty state: count 0, and the file still EXISTS — on
        # Gemini CLI its name alone keeps a workspace's admin paths out.
        for name, content in (("a.toml", "# Baked into the image by the launcher\n"), ("b.json", "{}\n")):
            with self.subTest(file=name):
                self.assertEqual(self.run_block(f"{self.tmp.name}/etc/x/{name}", content, 0, owner=self.me).returncode, 0)

    def test_a_file_that_does_not_parse_fails_the_build(self):
        for name, content in (("a.json", "{not json"), ("a.toml", "[[rule]\n"), ("b.json", "")):
            with self.subTest(file=name):
                self.assertNotEqual(self.run_block(f"{self.tmp.name}/etc/x/{name}", content, 0, owner=self.me).returncode, 0)

    @unittest.skipIf(os.getuid() == 0, "run as root, a scratch dir IS root's")
    def test_a_dir_root_does_not_own_fails_the_build(self):
        # The condition Gemini CLI would otherwise skip the whole dir for.
        self.assertNotEqual(self.run_block(f"{self.tmp.name}/etc/x/a.json", "{}", 0).returncode, 0)


class TestTheMappingsStateTheirTier(unittest.TestCase):
    """Every shipped policy.mapping carries a [fixed] table (the parser
    refuses one without; this reads the files as data)."""

    def test_each_shipped_mapping_has_a_fixed_table(self):
        for mapping in sorted(HARNESS_DIR.glob("*/policy.mapping")):
            with self.subTest(harness=mapping.parent.name):
                self.assertIn("fixed", read_toml(mapping))


if __name__ == "__main__":
    unittest.main()
