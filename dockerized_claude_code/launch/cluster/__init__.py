"""`launch.cluster` — cohabiting agents: N members, one container, one project.

The second collaboration mode, beside `{cowork}`. Where cowork routes messages
between ISOLATED agents in separate containers through a host-side hub, a cluster
puts N members in ONE container so they share a filesystem — which is what makes
direct agent-to-agent messaging possible at all (Anthropic's cross-session
messaging needs the sessions to see the same registration files, and a container
barrier breaks that). Design record: `cluster_plan.md`.

**This package is PoC-0.** It models and assembles; it does not launch. There is
no image build and no `docker run` here — that is the integration step, and
`docker_config` already owns container assembly, so a cluster variant belongs
there rather than reimplemented in this package. What exists today:

    member       what a member is; id composition; name validation
    legoset      `.legoset` parsing — a cluster template's default membership
    state        `cluster.toml` — durable member set + per-member tags, discovery
    worktree     writer safety: one git worktree per member (argv assembly + probes)
    tmux         the multiplexer: N members → one window (pure argv assembly)
    launch_plan  the join: a Cluster + env → worktrees, panes, mounts
    cli          the PoC command line

Dependency direction, mirroring `launch.cowork`: a LEAF CONSUMER of the core.
This package imports `paths`, `file_access`, `utils`, and `tags`; nothing in
`launch/` imports it back. It owns no docker calls.
"""

from ..tags.ui_profile import muxer_backend
from .legoset import ClusterTemplate, load_legoset
from .member import ClusterError, Member, member_id, split_member_id
from .state import Cluster


def backend() -> str:
    """The multiplexer backend for this launch — SOLO launches and cluster
    launches alike, which is why the switch lives here on the package rather
    than in either integrator. The choice is the OPERATOR'S PREFERENCE,
    persisted in `~/.claude-agents/ui_profile.toml` (`herdr_instead_of_tmux`,
    herdr by default; edited from the picker's "(Edit Preferences)" form or
    by hand) and read fresh each launch. It superseded a `MUXER_BACKEND` env
    var, retired 2026-08-30 because files-and-flags is this launcher's house
    pattern — nothing else steers behavior through the environment. The
    strict/lenient read semantics live with the profile:
    `tags/ui_profile.muxer_backend`."""
    return muxer_backend()


__all__ = ["Cluster", "ClusterError", "ClusterTemplate", "Member", "backend",
           "load_legoset", "member_id", "split_member_id"]
