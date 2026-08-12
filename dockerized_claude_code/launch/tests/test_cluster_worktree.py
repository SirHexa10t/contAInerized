"""Tests for launch.cluster.worktree — the writer-safety model.

Unlike the tmux layer, this one CAN be verified end to end: git is a hard
dependency of the launcher's own workflow, so the live tests below build a real
repository in a tmpdir and create real worktrees. That matters because worktrees
are the mechanism standing between N cohabiting members and silently clobbered
work — the argv being well-formed is not the same as git accepting it.

The argv-assembly tests come first (no repo needed), the live ones after.
"""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from launch.cluster import worktree
from launch.cluster.member import ClusterError
from launch.utils import shell_capture


class TestBranchNaming(unittest.TestCase):
    def test_branches_are_namespaced_under_cluster(self):
        # `git branch --list 'cluster/*'` must show every branch the launcher
        # created and nothing else — that is what makes cleanup reviewable.
        self.assertEqual(worktree.branch_name("poc", "researcher__primary"),
                         "cluster/poc/researcher__primary")

    def test_illegal_names_cannot_reach_git(self):
        for session, member in (("a/b", "m"), ("s", "a b"), ("s", "a:b")):
            with self.subTest(session=session, member=member):
                with self.assertRaises(ClusterError):
                    worktree.branch_name(session, member)


class TestPlanAndArgv(unittest.TestCase):
    def setUp(self):
        self.root = Path("/state/clusters/poc/worktrees")
        self.trees = worktree.plan("poc", ("alpha", "beta"), self.root)

    def test_one_worktree_per_member_under_the_root(self):
        self.assertEqual([t.path for t in self.trees],
                         [self.root / "alpha", self.root / "beta"])

    def test_each_gets_its_own_branch(self):
        self.assertEqual([t.branch for t in self.trees],
                         ["cluster/poc/alpha", "cluster/poc/beta"])

    def test_add_argv_names_its_own_repo(self):
        # `-C project` rather than a cwd change, so a dry-run line can be pasted
        # into a shell unchanged.
        argv = worktree.add_argv(Path("/proj"), self.trees[0])
        self.assertEqual(argv, ("git", "-C", "/proj", "worktree", "add",
                                "-b", "cluster/poc/alpha",
                                str(self.root / "alpha")))

    def test_remove_argv_forces(self):
        # A member will normally have left uncommitted edits; refusing on dirt
        # would strand the directory.
        self.assertIn("--force", worktree.remove_argv(Path("/proj"), self.trees[0]))

    def test_add_commands_skips_existing_worktrees(self):
        # Re-running create must be safe; `git worktree add` onto an existing
        # path errors rather than no-ops.
        commands = worktree.add_commands(
            Path("/proj"), self.trees, existing=frozenset({self.root / "alpha"}))
        self.assertEqual(len(commands), 1)
        self.assertIn(str(self.root / "beta"), commands[0])

    def test_add_commands_with_nothing_existing_covers_everyone(self):
        self.assertEqual(len(worktree.add_commands(Path("/proj"), self.trees)), 2)


class LiveGitRepo(unittest.TestCase):
    """A real repository, for the tests that need git to actually agree."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.project = self.tmp / "project"
        self.project.mkdir()
        self.worktrees = self.tmp / "worktrees"
        self._git("init", "-q", ".")
        self._git("config", "user.email", "t@e.st")
        self._git("config", "user.name", "Test")

    def _git(self, *args: str) -> None:
        result = shell_capture("git", "-C", str(self.project), *args)
        self.assertEqual(result.returncode, 0, result.stderr)

    def commit_something(self) -> None:
        (self.project / "app.py").write_text("print('hi')\n")
        self._git("add", "-A")
        self._git("commit", "-qm", "init")


class TestChecks(LiveGitRepo):
    def test_a_committed_repo_is_accepted(self):
        self.commit_something()
        self.assertIsNone(worktree.check(self.project))

    def test_a_non_repo_is_refused_with_a_remedy(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        refusal = worktree.check(plain)
        self.assertIsNotNone(refusal)
        self.assertIn("git init", refusal.remedy)

    def test_an_empty_repo_is_refused_separately(self):
        # A distinct failure from "not a repo", with its own fix. NOT because git
        # errors: measured, `worktree add -b` on an empty repo succeeds by
        # inferring --orphan, which would hand every member an EMPTY checkout of
        # a project that has files. Refusing is the useful answer.
        refusal = worktree.check(self.project)
        self.assertIsNotNone(refusal)
        self.assertIn("commit", refusal.remedy)

    def test_has_commits_is_the_probe_not_symbolic_ref(self):
        # The bug this pins: `symbolic-ref` prints "master" on an unborn HEAD and
        # exits 0, so it cannot detect an empty repo. `rev-parse --verify` can.
        self.assertFalse(worktree.has_commits(self.project))
        self.commit_something()
        self.assertTrue(worktree.has_commits(self.project))

    def test_a_missing_directory_is_refused(self):
        self.assertIsNotNone(worktree.check(self.tmp / "nope"))

    def test_a_refusal_reads_as_one_sentence(self):
        self.assertIn("—", str(worktree.check(self.tmp / "nope")))

    def test_is_git_repo_sees_through_a_subdirectory(self):
        # A user may legitimately point at a subdir of their project.
        self.commit_something()
        (self.project / "sub").mkdir()
        self.assertTrue(worktree.is_git_repo(self.project / "sub"))

    def test_head_branch_after_a_commit(self):
        self.commit_something()
        self.assertIsNotNone(worktree.head_branch(self.project))


class TestLiveWorktrees(LiveGitRepo):
    """The mechanism itself: does git build the isolated checkouts we claim?"""

    def setUp(self):
        super().setUp()
        self.commit_something()
        self.trees = worktree.plan("poc", ("alpha", "beta"), self.worktrees)
        for tree in self.trees:
            result = shell_capture(*worktree.add_argv(self.project, tree))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_each_member_has_its_own_checkout_of_the_project(self):
        for tree in self.trees:
            with self.subTest(member=tree.member):
                self.assertTrue((tree.path / "app.py").is_file())

    def test_git_reports_every_worktree(self):
        known = worktree.existing_worktrees(self.project)
        for tree in self.trees:
            self.assertIn(tree.path.resolve(), {p.resolve() for p in known})

    def test_each_is_on_its_own_branch(self):
        for tree in self.trees:
            with self.subTest(member=tree.member):
                self.assertEqual(worktree.head_branch(tree.path), tree.branch)

    def test_an_edit_in_one_member_does_not_touch_another(self):
        # The whole point: this is what stops two members losing each other's
        # work, and it is a property of git rather than of our code — so it is
        # worth proving rather than assuming.
        alpha, beta = self.trees
        (alpha.path / "app.py").write_text("alpha's version\n")
        self.assertEqual((beta.path / "app.py").read_text(), "print('hi')\n")
        self.assertEqual((self.project / "app.py").read_text(), "print('hi')\n")

    def test_add_commands_is_idempotent_against_a_live_repo(self):
        # Re-running create after a partial failure must not re-add.
        commands = worktree.add_commands(
            self.project, self.trees, worktree.existing_worktrees(self.project))
        self.assertEqual(commands, ())

    def test_removal_drops_the_checkout_but_keeps_the_branch(self):
        # Committed work stays reachable after a destroy — the branch is the
        # record, the directory is not.
        alpha = self.trees[0]
        shell_capture(*worktree.remove_argv(self.project, alpha))
        shell_capture(*worktree.prune_argv(self.project))
        self.assertFalse(alpha.path.exists())
        branches = shell_capture("git", "-C", str(self.project), "branch", "--list",
                                 alpha.branch)
        self.assertIn(alpha.branch, branches.stdout)

    def test_a_dirty_member_can_still_be_removed(self):
        beta = self.trees[1]
        (beta.path / "app.py").write_text("uncommitted mess\n")
        result = shell_capture(*worktree.remove_argv(self.project, beta))
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
