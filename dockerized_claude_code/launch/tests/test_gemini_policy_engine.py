"""The launcher's rendered policy rules, run through Gemini CLI's OWN policy
engine — the one part of the Gemini half no other test can stand in for,
since a re-implementation of its matcher would only assert our beliefs
about it (plans/ISSUES.md, testing technique).

Every file here is written by the launcher's real code — `install_settings`
for an instance's rules file, `fixed_policy` for the image's fixed tier —
and loaded at the tier the CLI gives it at launch: the fixed file from its
system policies dir (ADMIN, 5), the instance's file by `--policy` (USER, 4),
the CLI's shipped defaults beneath (1). The engine itself is driven by
`probes/gemini_policy_engine.mjs`, a thin node driver; WHICH rules load and
WHAT each call must be decided live in this file.

It needs the CLI's bundle, named by LAUNCHER_TEST_GEMINI_BUNDLE — the [self]
image and CI set it, at the version pinned in `probes/gemini-cli.version`
(the one policy.mapping was verified against). By hand:
    npm install --prefix <dir> @google/gemini-cli@$(cat launch/tests/probes/gemini-cli.version)
    LAUNCHER_TEST_GEMINI_BUNDLE=<dir>/node_modules/@google/gemini-cli/bundle bash check.sh
With the variable SET, a missing bundle, a missing node or a version other
than the pin FAILS: the toolchain was promised, and a check that did not run
is not a check that passed (check.sh). Unset, the test skips — and check.sh
says so, loudly."""

import dataclasses
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from launch import paths
from launch.agents_crud import fixed_policy, install_settings
from launch.ai import GEMINI_CLI
from launch.tests.fixtures import REGISTRY, make_inst

BUNDLE_ENV = "LAUNCHER_TEST_GEMINI_BUNDLE"
PROBES = Path(__file__).parent / "probes"
PINNED_VERSION = (PROBES / "gemini-cli.version").read_text().strip()
ADMIN_TIER, USER_TIER = 5, 4          # Gemini CLI's ADMIN_POLICY_TIER / USER_POLICY_TIER (v0.61.0, core policy/config.ts)


def shell(command: str) -> tuple[str, dict]:
    return "run_shell_command", {"command": command}


class TestGeminiPolicyEngine(unittest.TestCase):
    """Shipped builds, each loaded the way the CLI loads them at launch."""

    bundle: Path
    root: Path
    tmp: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls):
        bundle = os.environ.get(BUNDLE_ENV)
        if not bundle:
            raise unittest.SkipTest(f"{BUNDLE_ENV} is unset: the policy rules were NOT run through Gemini CLI's "
                                    f"engine (the [self] image and CI set it)")
        cls.bundle = Path(bundle)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def install(self, name: str, *, policies=(), specialties=()) -> Path:
        """The instance's rules file, as a launch writes it: through
        install_settings, for Gemini CLI, into a scratch state dir."""
        inst = dataclasses.replace(make_inst("poet", name, policies=policies, specialties=specialties),
                                   harness=REGISTRY.harnesses["gemini-cli"])
        with patch.object(paths, "AGENTS_STATE", self.root / "state"):
            install_settings(inst, REGISTRY, GEMINI_CLI)
            return inst.state_dir / REGISTRY.harnesses["gemini-cli"].policy_file

    def fixed(self) -> Path:
        """The image's fixed tier, as its Dockerfile bakes it."""
        fixed = fixed_policy(REGISTRY.harnesses["gemini-cli"], REGISTRY)
        assert fixed is not None, "gemini-cli's policy.mapping names no fixed tier"
        path = self.root / "system-policies" / "launcher-always-on.toml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(fixed.text)
        return path

    def decide(self, scenarios: dict[str, tuple[list[tuple[Path, int]], list[tuple[str, tuple[str, dict]]]]]) -> dict:
        """Ask the engine; `scenarios` maps a name to (layers, checks), a
        check being (approval mode, (tool, args)). Fails, never skips, on
        anything wrong with the promised toolchain."""
        if shutil.which("node") is None:
            self.fail(f"{BUNDLE_ENV} is set but there is no `node` on PATH to drive the engine")
        if not (self.bundle / "policies").is_dir():
            self.fail(f"{BUNDLE_ENV}={self.bundle} is not a Gemini CLI bundle (no policies/ in it)")
        spec = {"scenarios": [{"name": name, "layers": [{"path": str(p), "tier": t} for p, t in layers],
                               "checks": [{"mode": mode, "tool": tool, "args": args} for mode, (tool, args) in checks]}
                              for name, (layers, checks) in scenarios.items()]}
        spec_file, result_file = self.root / "spec.json", self.root / "result.json"
        spec_file.write_text(json.dumps(spec))
        done = subprocess.run(["node", str(PROBES / "gemini_policy_engine.mjs"), str(self.bundle),
                               str(spec_file), str(result_file)], capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr[-2000:])
        result = json.loads(result_file.read_text())
        self.assertEqual(result["version"], PINNED_VERSION,
                         f"the bundle is Gemini CLI {result['version']}, the pin is {PINNED_VERSION} — "
                         f"re-verify policy.mapping against it, then move probes/gemini-cli.version")
        for name, errors in result["load_errors"].items():
            self.assertEqual(errors, [], f"{name}: the CLI could not load a rules file the launcher wrote")
        return result["decisions"]

    def assert_decisions(self, scenarios, expected: dict[str, list[str]]) -> None:
        """`expected` per check: allow / deny / ask_user, `not-allow`,
        `not-deny`, or `gap` — a spelling the rules are KNOWN not to catch
        (plans/ISSUES.md, "Shell-prefix rules are porous"), pinned so the day
        the engine starts catching it this test says so."""
        decisions = self.decide(scenarios)
        for name, (_, checks) in scenarios.items():
            for (mode, (tool, args)), want, got in zip(checks, expected[name], decisions[name]):
                with self.subTest(scenario=name, mode=mode, call=args.get("command", tool)):
                    if want == "not-allow":
                        self.assertNotEqual(got, "allow", "must not be allowed here")
                    elif want == "not-deny":
                        self.assertNotEqual(got, "deny", "must not be denied here")
                    elif want == "gap":
                        self.assertNotEqual(got, "deny", "a known gap closed: update plans/ISSUES.md and this test")
                    else:
                        self.assertEqual(got, want)

    def test_shipped_builds_are_decided_as_their_policies_say(self):
        fixed = (self.fixed(), ADMIN_TIER)
        write = ("write_file", {"file_path": "/workspace/x", "content": "y"})
        scenarios = {
            # project-starter: <!plan> + <+all>, and the always-on <-su>.
            "project-starter": ([fixed, (self.install("ps", policies=("plan-first", "all-actions")), USER_TIER)], [
                ("default", shell("ls -la")), ("default", shell("sudo ls")), ("default", shell("cd /tmp && sudo ls")),
                ("default", shell("bash -c 'sudo ls'")), ("default", shell("echo $(sudo id)")),
                ("default", write),
                ("plan", write), ("plan", shell("ls -la")), ("plan", ("invoke_agent", {"agent_name": "x"})),
                ("yolo", shell("sudo ls")), ("yolo", shell("ls -la")),
            ]),
            # refactorer: <-gpush>'s stems.
            "refactorer": ([fixed, (self.install("rf", policies=("vcs-safe",)), USER_TIER)], [
                ("default", shell("git push origin main")), ("default", shell("git pushall")),
                ("default", shell("echo hi && git push")), ("default", shell("git reset --hard HEAD~1")),
                ("default", shell("git reset --soft HEAD")), ("default", shell("git status")),
                ("yolo", shell("git push")), ("yolo", shell("git status")),
            ]),
            # <-git>, <-net> and {ro}'s write deny.
            "locked": ([fixed, (self.install("lk", policies=("no-git", "no-net"), specialties=("read-only",)), USER_TIER)], [
                ("default", shell("git status")), ("default", shell("gitk")), ("default", shell("curl https://x")),
                ("default", shell("wget https://x")), ("default", ("web_fetch", {"prompt": "x"})),
                ("default", ("google_web_search", {"query": "x"})), ("default", write),
                ("default", ("replace", {"file_path": "/workspace/x"})),
                ("default", ("read_file", {"file_path": "/workspace/x"})), ("yolo", write),
            ]),
            # <-gw>: git stays read-only — every write denied, however it is
            # prefixed by a global option or chained, and the reads still run.
            # Built as the form builds it: <-gw> brings <-gpush>, which it nests under.
            "read-only git": ([fixed, (self.install("gw", policies=("vcs-safe", "no-git-write", "all-actions")), USER_TIER)], [
                ("default", shell("git add .")), ("default", shell("git stage x")),
                ("default", shell("git commit -m x")), ("default", shell("git commit-tree HEAD^{tree}")),
                ("default", shell("git push origin main")), ("default", shell("git pushall")),
                ("default", shell("git -C /x commit -m x")), ("default", shell("git -c user.name=x commit")),
                ("default", shell("cd /x && git commit -m x")), ("default", shell("git remote add up https://x")),
                ("default", shell("git lfs push origin")), ("default", shell("git merge feature")),
                ("default", shell("git checkout main")), ("default", shell("git switch -c x")),
                ("default", shell("git restore f")), ("default", shell("git reset --soft HEAD~1")),
                ("default", shell("git branch -D x")), ("default", shell("git tag v1")),
                ("default", shell("git stash")), ("default", shell("git config user.name x")),
                ("default", shell("git update-ref refs/heads/x HEAD")), ("yolo", shell("git commit -m x")),
                ("default", shell("git status")), ("default", shell("git log --oneline")),
                ("default", shell("git diff HEAD")), ("default", shell("git show HEAD")),
                ("default", shell("git blame f")), ("default", shell("git grep x")),
                ("default", shell("git ls-files")), ("default", shell("git rev-parse HEAD")),
                ("default", shell("git merge-base HEAD main")), ("default", shell("git cherry main")),
                ("default", shell("git for-each-ref refs/heads")), ("default", shell("git log -g")),
                ("default", shell("git fetch origin")), ("default", shell("git pull --ff-only")),
                ("default", shell("git remote -v")), ("default", shell("git remote update")),
                ("default", shell("git lfs pull")),
                ("default", shell("GIT_DIR=x git commit -m x")), ("default", shell("/usr/bin/git commit -m x")),
                ("default", shell("git ci -m x")),
            ]),
            # A nested CLI that never loaded the instance's file: the fixed
            # tier alone still refuses sudo — the reason it exists.
            "fixed tier alone": ([fixed], [
                ("default", shell("sudo ls")), ("yolo", shell("sudo -n true")), ("yolo", shell("ls")),
            ]),
            # The known gaps, measured: spellings a prefix rule does not see.
            "known gaps": ([fixed, (self.install("gp", policies=("vcs-safe", "all-actions")), USER_TIER)], [
                ("default", shell("/usr/bin/sudo ls")), ("default", shell("env sudo ls")),
                ("default", shell("x=1 sudo ls")), ("default", shell("eval 'sudo ls'")),
                ("default", shell("git -C /x push")), ("default", shell("git  push")),
            ]),
        }
        self.assert_decisions(scenarios, {
            "project-starter": ["allow", "deny", "deny", "deny", "deny", "allow",
                                "not-allow", "not-allow", "not-allow", "deny", "allow"],
            "refactorer": ["deny", "deny", "deny", "deny", "not-deny", "not-deny", "deny", "allow"],
            "locked": ["deny", "not-deny", "deny", "deny", "deny", "deny", "deny", "deny", "not-deny", "deny"],
            "fixed tier alone": ["deny", "deny", "allow"],
            "read-only git": ["deny"] * 22 + ["allow"] * 17 + ["gap"] * 3,
            "known gaps": ["gap"] * 6,
        })

    def test_if_the_flag_is_dropped_the_deny_still_outranks_a_saved_always_allow(self):
        # The fallback: were --policy ever lost (a launch assembly that forgot
        # it), or passed as --admin-policy (which the populated system dir
        # makes Gemini ignore — the mismatch the mapping's parser refuses, and
        # the one that leans on this margin), Gemini reads its default user
        # dir — where
        # our launcher.toml sits BESIDE auto-saved.toml, a .toml in the same
        # directory, so both load at the user tier. Gemini saves an "always
        # allow" at priority 950; our denies are 999. That 49-thousandth
        # margin is the whole defence in that state (researcher__primary,
        # gate fixed-tier), so it is pinned here, with the fixed tier absent
        # too. An always-allow EDITED to 999 would tie — ties go by read
        # order — which is why the launcher passes --policy at all.
        rules_file = self.install("fb")
        (rules_file.parent / "auto-saved.toml").write_text(
            '[[rule]]\ntoolName = "run_shell_command"\ncommandPrefix = "sudo"\ndecision = "allow"\npriority = 950\n')
        self.assert_decisions({"user dir read whole": ([(rules_file.parent, USER_TIER)], [
            ("default", shell("sudo ls")), ("yolo", shell("sudo ls")),
        ])}, {"user dir read whole": ["deny", "deny"]})


if __name__ == "__main__":
    unittest.main()
