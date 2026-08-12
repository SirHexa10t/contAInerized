"""Writer safety: one git worktree per member.

The hazard this exists for: N members share one project, the launcher's edits are
read-then-write with no locking, and two members editing one file silently lose
work. A worktree gives each member **its own checkout on its own branch** of the
same repository, so concurrent editing is exactly the thing git is built for and
integration is a merge rather than a race.

This is the *default* mechanism, not the only one — the design record
(`cluster_plan.md`) keeps lock-files as a last resort for genuine same-file work,
and `{ro}` for a member that should never write at all.

**Commands are ASSEMBLED here and RUN by the caller.** Every function returns an
argv tuple; nothing in this module shells out except the four narrow probes at the
bottom (`is_git_repo`, `has_commits`, `head_branch`, `existing_worktrees`), which
read state and change nothing. That split keeps the interesting logic — what to
run, in what order, and when to refuse — testable without a repository, while the
live probes stay small enough to verify against a real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..utils import shell_capture
from .member import valid_label

BRANCH_PREFIX = "cluster"          # branch names are <prefix>/<session>/<member-id>


@dataclass(frozen=True)
class Worktree:
    """One member's checkout: where it goes, and on which branch."""
    member: str
    path: Path
    branch: str


@dataclass(frozen=True)
class Refusal:
    """Why worktrees cannot be prepared for this project.

    A refusal rather than an exception because it is an ANSWER — "this project
    isn't a git repo" is something the CLI prints with the fix, not a fault. It
    carries the remedy so no caller has to invent the wording."""
    reason: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.reason} — {self.remedy}"


def branch_name(session: str, member: str) -> str:
    """The branch a member works on: `cluster/<session>/<member-id>`.

    Namespaced under `cluster/` so a `git branch --list 'cluster/*'` shows every
    branch the launcher ever created and nothing else — which is what makes
    cleanup reviewable. Both components are already validated (no `/`, no
    whitespace, no `:` — see member.valid_label), so the result cannot contain a
    path traversal or a git refname the tool would reject."""
    valid_label(session, "session name")
    valid_label(member, "member id")
    return f"{BRANCH_PREFIX}/{session}/{member}"


def plan(session: str, members: tuple[str, ...], worktrees_root: Path
         ) -> tuple[Worktree, ...]:
    """What each member's checkout should be. Pure — decides nothing about
    whether the repo can support it (see `check`), only what the layout is."""
    return tuple(Worktree(member=member, path=worktrees_root / member,
                          branch=branch_name(session, member))
                 for member in members)


def check(project: Path) -> Refusal | None:
    """None when `project` can host member worktrees, else why not.

    Two things must hold, and they fail for different reasons worth separating:
    the path must be a git repository (worktrees are a git feature), and it must
    have at least one COMMIT.

    The commit check is not about git erroring — measured on git 2.x, `worktree
    add -b` on an empty repo SUCCEEDS by inferring `--orphan`. It is about the
    result being useless: with nothing committed, every member's worktree is an
    empty directory, so the project the user pointed at would be invisible to all
    of them. Refusing beats handing five agents an empty checkout each."""
    if not project.is_dir():
        return Refusal(f"{project} is not a directory",
                       "point the cluster at the project you want worked on")
    if not is_git_repo(project):
        return Refusal(
            f"{project} is not a git repository, so per-member worktrees "
            f"cannot be created",
            "run `git init` there and make one commit, or choose a different "
            "writer-safety model (see cluster_plan.md)")
    if not has_commits(project):
        return Refusal(
            f"{project} has no commits yet, so every member's worktree would be "
            f"an empty directory (git infers --orphan rather than failing)",
            "make one commit in the project first, so there is something to "
            "check out")
    return None


def add_argv(project: Path, worktree: Worktree) -> tuple[str, ...]:
    """`git worktree add` for one member, creating its branch off the current
    HEAD.

    `-C project` rather than a cwd change: the launcher runs from wherever the
    user invoked it, and an argv that names its own repository is one a person
    can copy out of a dry-run and paste into a shell."""
    return ("git", "-C", str(project), "worktree", "add",
            "-b", worktree.branch, str(worktree.path))


def remove_argv(project: Path, worktree: Worktree) -> tuple[str, ...]:
    """`git worktree remove` for one member.

    `--force` because a member will normally have left uncommitted edits, and a
    teardown that refuses on dirt would strand the directory. The work is not
    lost silently: `destroy` is an explicit operation, and the member's BRANCH
    survives a worktree removal — committed work stays reachable."""
    return ("git", "-C", str(project), "worktree", "remove", "--force",
            str(worktree.path))


def prune_argv(project: Path) -> tuple[str, ...]:
    """`git worktree prune` — drops administrative entries whose directories are
    already gone, so a half-finished teardown does not leave git believing in
    worktrees that no longer exist."""
    return ("git", "-C", str(project), "worktree", "prune")


def add_commands(project: Path, worktrees: tuple[Worktree, ...],
                 existing: frozenset[Path] = frozenset()
                 ) -> tuple[tuple[str, ...], ...]:
    """The `git worktree add` sequence, skipping members that already have one.

    Idempotent by design: re-running a create must be safe (the same reason
    `cowork.group.create_session` returns an existing group untouched), and
    `git worktree add` onto an existing path fails rather than no-ops."""
    return tuple(add_argv(project, w) for w in worktrees
                 if w.path.resolve() not in {p.resolve() for p in existing})


# ============================================================
# Live probes — the only things here that touch a repository
# ============================================================

def is_git_repo(project: Path) -> bool:
    """Whether `project` is inside a git work tree.

    `--is-inside-work-tree` rather than testing for a `.git` directory: that
    answers correctly for a worktree (whose `.git` is a FILE) and for a
    subdirectory of a repo, both of which a user may legitimately point at."""
    result = shell_capture("git", "-C", str(project), "rev-parse",
                           "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


def has_commits(project: Path) -> bool:
    """Whether the repo has at least one commit.

    `rev-parse --verify HEAD` is the right probe and `symbolic-ref` is NOT: on a
    fresh `git init`, HEAD symbolically points at `refs/heads/master` before that
    ref exists, so symbolic-ref happily prints "master" and exits 0. Using it
    here would have let `check` pass an empty repository — caught by a test that
    built one."""
    return shell_capture("git", "-C", str(project), "rev-parse", "--verify",
                         "--quiet", "HEAD").returncode == 0


def head_branch(project: Path) -> str | None:
    """The branch name a checkout is on, or None when HEAD is detached.

    Answers "which branch is this worktree on" — used to confirm each member got
    its own. It does NOT prove any commit exists (see `has_commits` for why), so
    never reach for it as an emptiness check."""
    result = shell_capture("git", "-C", str(project), "symbolic-ref",
                           "--quiet", "--short", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def existing_worktrees(project: Path) -> frozenset[Path]:
    """Every worktree path git currently knows about for this repo.

    Parses `--porcelain` rather than the human listing: the porcelain format is a
    documented stable contract (`worktree <path>` lines), whereas the default
    output aligns columns and would break on a path containing spaces."""
    result = shell_capture("git", "-C", str(project), "worktree", "list",
                           "--porcelain")
    if result.returncode != 0:
        return frozenset()
    return frozenset(Path(line.removeprefix("worktree ").strip())
                     for line in result.stdout.splitlines()
                     if line.startswith("worktree "))
