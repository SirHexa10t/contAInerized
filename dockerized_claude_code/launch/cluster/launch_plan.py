"""Turning a `Cluster` into the things that actually start it.

This is the only module that knows how the pieces relate: a member's tags resolve
to an `Instance` (the ordinary pipeline), that instance's engine conf becomes its
window's env, its worktree becomes its window's cwd, and the whole set becomes a
tmux session. Nothing here executes anything — a `LaunchPlan` is a value the CLI
prints, writes as a script, or (after integration) runs.

**Two workspace models, and PoC-0 uses the simple one.**

*Shared (the default).* Every member sees the one project at
`WORKSPACE_IN_CONTAINER`, exactly as a solo instance does. Simple, faithful to
the solo mental model — and UNSAFE for concurrent file work, since the launcher's
edits are read-then-write with no locking. PoC-0 accepts that deliberately: it
proves cohabitation and switching, not parallel editing.

*Personal (opt-in, interim).* Each member gets its own git worktree; the
worktrees dir mounts whole at `WORKSPACES_IN_CONTAINER` and each member's **cwd**
is its own subdir — so the per-member PATH differs while the mount point does
not, which is the asymmetry cohabitation forces (one container cannot mount two
trees at one point).

The eventual model is undecided and is NOT worktrees — see `cluster_plan.md`'s
"Personal workspaces" decision: per-member CLONES with the project as the shared
upstream `origin`, whose placement (and the clone-vs-worktree mechanism itself)
still needs settling. `--worktrees` exists so isolation is available meanwhile
without pre-empting that call.

**Why the image is a union.** One container means one image, so its layers are
the union of every member's build steps. Two members can differ in anything that
lives in a session's own env or settings (model, effort, policies) and in nothing
that is container-level (firewall, capabilities) — that constraint is recorded in
`cluster_plan.md` and enforced here only insofar as `image_tags` collects the
union rather than pretending per-member images exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..paths import (
    CLUSTER_IN_CONTAINER, WORKSPACE_IN_CONTAINER, WORKSPACES_IN_CONTAINER,
)
from .member import Member
from .state import Cluster
from .tmux import Pane
from .worktree import Worktree, plan as plan_worktrees

# What each member runs. A tuple so a caller can extend it (extra claude args)
# without this module deciding the whole command line.
DEFAULT_MEMBER_COMMAND = ("claude",)


@dataclass(frozen=True)
class MemberPlan:
    """One member, fully resolved into what starts it."""
    member: Member
    worktree: Worktree
    env: dict[str, str]
    command: tuple[str, ...]
    personal_workspace: bool = False

    @property
    def container_cwd(self) -> Path:
        """Where this member's shell starts INSIDE the container.

        The shared project when workspaces are shared; its own subdir of the
        worktrees mount when they are personal (see the module docstring)."""
        if not self.personal_workspace:
            return WORKSPACE_IN_CONTAINER
        return WORKSPACES_IN_CONTAINER / self.member.id

    def pane(self) -> Pane:
        """This member as a tmux window."""
        return Pane(name=self.member.id, command=self.command,
                    cwd=self.container_cwd, env=self.env)


@dataclass(frozen=True)
class LaunchPlan:
    """Everything needed to start one cluster, in the order it happens.

    Holds no live handles and runs nothing, so it can be printed for review,
    diffed between runs, or asserted on in a test — the same reason
    `docker_config`'s dry-run projects rather than simulates."""
    cluster: Cluster
    members: tuple[MemberPlan, ...]

    @property
    def session(self) -> str:
        return self.cluster.session

    def panes(self) -> tuple[Pane, ...]:
        """The windows, in member (template) order."""
        return tuple(m.pane() for m in self.members)

    def worktrees(self) -> tuple[Worktree, ...]:
        return tuple(m.worktree for m in self.members)

    @property
    def container_banner(self) -> Path:
        """The banner file as the CONTAINER sees it.

        tmux runs inside the container, so a status line pointing at the host
        path would `cat` nothing and the banner would silently render empty —
        the same host-path-in-a-container-command bug `{cowork}`'s review
        command once shipped. Derived from the host filename rather than a second
        literal, so the two cannot drift: the cluster dir mounts whole at
        CLUSTER_IN_CONTAINER, and the banner is a direct child of it."""
        from ..paths import cluster_banner_path
        return CLUSTER_IN_CONTAINER / cluster_banner_path(self.session).name

    @property
    def container_shell_cwd(self) -> Path:
        """Where `{muxer}`'s free terminal opens.

        The project root, not a member's checkout: the operator's shell is for
        the cluster as a whole (inspect, run a CLI, tail a log), and starting it
        inside one member's tree would imply an ownership it does not have."""
        return (WORKSPACES_IN_CONTAINER if self.personal_workspaces
                else WORKSPACE_IN_CONTAINER)

    @property
    def personal_workspaces(self) -> bool:
        """Whether members have their own checkouts. Read off the members rather
        than stored twice, so the two can never disagree."""
        return any(m.personal_workspace for m in self.members)

    def mounts(self) -> dict[Path, str]:
        """Host path → container target for the cluster's own mounts.

        The shared cluster dir is always there (the banner today, the
        message-queue later). The workspace mount is one of two shapes: the
        project itself at `/workspace` (shared), or the worktrees dir at
        `/workspaces` whose subdirs are the members' cwds (personal).

        Everything else a member needs — its state dir, caches, creds — is
        per-member and comes from the ordinary instance mount set at integration
        time."""
        from ..paths import cluster_path, cluster_worktrees_dir
        workspace = (
            {cluster_worktrees_dir(self.session): str(WORKSPACES_IN_CONTAINER)}
            if self.personal_workspaces
            else {self.cluster.project: str(WORKSPACE_IN_CONTAINER)})
        return {**workspace, cluster_path(self.session): str(CLUSTER_IN_CONTAINER)}


def build(cluster: Cluster, *, env_for: dict[str, dict[str, str]] | None = None,
          command: tuple[str, ...] = DEFAULT_MEMBER_COMMAND,
          command_for: dict[str, tuple[str, ...]] | None = None,
          personal_workspaces: bool = False) -> LaunchPlan:
    """A `LaunchPlan` for `cluster`.

    `env_for` maps member id → that member's environment (in practice its
    resolved engine conf). Injected rather than resolved here so this module
    stays free of the tag/registry pipeline: PoC-0 passes a literal, and
    integration passes `Instance.conf` per member without this signature
    changing. A member absent from the map simply gets no extra env.

    `command_for` is the same shape for the member's ARGV — integration
    resolves per-member effort flags, `--continue`, and specialty claude_args
    (two members legitimately run different command lines). A member absent
    from the map runs `command` (the shared default), so PoC callers and tests
    keep passing one tuple.

    Every member's own id is exported as `CLUSTER_MEMBER` (and the cluster's as
    `CLUSTER_SESSION`) because a cohabiting agent otherwise has no way to know
    which member it is — its persona is shared with its siblings, and `hostname`
    is the container's, not its own."""
    env_for = env_for or {}
    command_for = command_for or {}
    worktrees = {w.member: w for w in plan_worktrees(
        cluster.session, cluster.ids, _worktrees_root(cluster.session))}
    members = tuple(
        MemberPlan(
            member=member,
            worktree=worktrees[member.id],
            env={**env_for.get(member.id, {}),
                 "CLUSTER_SESSION": cluster.session,
                 "CLUSTER_MEMBER": member.id,
                 "CLUSTER_ROLE": member.role},
            command=command_for.get(member.id, command),
            personal_workspace=personal_workspaces,
        )
        for member in cluster.members
    )
    return LaunchPlan(cluster=cluster, members=members)


def _worktrees_root(session: str) -> Path:
    """Imported lazily-ish through a helper so the path builder is read at call
    time (see paths.py's builder rationale — a module-level import of the value
    would bind it before a test could redirect AGENTS_STATE)."""
    from ..paths import cluster_worktrees_dir
    return cluster_worktrees_dir(session)
