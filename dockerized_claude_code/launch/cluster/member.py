"""What one cluster member IS, and what makes a name legal.

A member is *an instance in all but placement*: the same agent `.md` persona, the
same `.lego` defaults, the same four tag axes. What a cluster adds is the **role**
— a short label that distinguishes two members built from the same agent, so a
`devteam` can hold a primary and an adversarial researcher without either
shadowing the other.

**Member ids.** `<agent>__<role>`, collapsing to plain `<agent>` when the role is
just the agent's own name. So a five-member devteam reads:

    project-starter   refactorer   researcher__primary
    researcher__adversarial   bug-investigator

rather than the uniform-but-noisy `project-starter__project-starter`. `__` is
already this project's identity separator (`<agent>__<session>` for instances),
so the shape is familiar and `split_member_id` inverts it exactly.

**Why names are validated here, at the point one is composed.** A member id is
not just a directory name — it is also a **tmux window name**, and tmux addresses
windows with `session:window.pane`. A role containing `:` or `.` would make
`-t cluster:my.role` ambiguous, and tmux would act on the wrong target rather
than fail. Raising when the name is composed is how that never reaches a command
line. (Same argument as `cowork.group._separator_free`, different characters.)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..tags.lego import AgentBuild

MEMBER_SEPARATOR = "__"     # between agent and role in a member id

# Rejected in any agent name, role, or cluster session name:
#   ':' '.'   tmux target syntax is `session:window.pane` — either would make an
#             addressed window ambiguous rather than invalid
#   '/'       every one of these becomes a path component
#   '@'       reserved by {cowork} for inbox dirs; keep the two features' name
#             spaces mutually legible if a tree ever holds both
#   space/tab a tmux window name and a shell word both end at whitespace
_FORBIDDEN = (":", ".", "/", "@", " ", "\t", "\n")


class ClusterError(Exception):
    """A cluster definition that cannot be honoured — a bad name, an unknown
    agent, a duplicate role. Raised at parse/compose time, never at launch."""


def valid_label(label: str, kind: str) -> str:
    """`label` unchanged, or a ClusterError naming what is wrong with it.

    Raises rather than sanitising, for the reason `{cowork}` learned: silently
    rewriting an id leaves the thing keyed under a name its own participants do
    not answer to."""
    if not label:
        raise ClusterError(f"a cluster {kind} may not be empty")
    for char in _FORBIDDEN:
        if char in label:
            shown = repr(char) if char.strip() else "whitespace"
            raise ClusterError(
                f"a cluster {kind} may not contain {shown} — it becomes both a "
                f"path component and a tmux window name: {label!r}")
    return label


def valid_role(agent: str, role: str) -> str:
    """A role, additionally rejecting the separator itself.

    `__` inside a role would make `split_member_id` pick the wrong split point,
    so the id would no longer round-trip to the pair it was built from."""
    valid_label(role, "role")
    if MEMBER_SEPARATOR in role:
        raise ClusterError(
            f"a cluster role may not contain {MEMBER_SEPARATOR!r} (it separates "
            f"the agent from the role in a member id): {role!r}")
    valid_label(agent, "agent name")
    if MEMBER_SEPARATOR in agent:
        raise ClusterError(
            f"a cluster member's agent name may not contain "
            f"{MEMBER_SEPARATOR!r}: {agent!r}")
    return role


def member_id(agent: str, role: str) -> str:
    """The one string naming this member — in its state dir, its worktree, its
    tmux window, and (later) as its messaging peer name.

    Collapses to `<agent>` when the role adds nothing, so the common
    one-of-each cluster gets clean names."""
    valid_role(agent, role)
    return agent if role == agent else f"{agent}{MEMBER_SEPARATOR}{role}"


def split_member_id(identifier: str) -> tuple[str, str]:
    """`(agent, role)` from a member id — the exact inverse of `member_id`.

    Splits on the FIRST separator: an agent name cannot contain `__` (rejected
    above) while a role cannot either, so there is exactly one to find."""
    agent, separator, role = identifier.partition(MEMBER_SEPARATOR)
    return (agent, role) if separator else (agent, agent)


@dataclass(frozen=True)
class Member:
    """One member of a cluster: which agent, under which role, with which tags.

    `build` is the same `AgentBuild` a `.lego` parses to and a store entry
    round-trips through, so a member resolves to an `Instance` by the ordinary
    path — a cluster introduces no second tag pipeline.

    Frozen like `cowork.group.Session`, and for the same reason: every change is
    a rewrite of `cluster.toml` anyway, so `dataclasses.replace` keeps a caller
    from mutating a copy and forgetting to save it."""
    agent: str
    role: str
    build: AgentBuild = field(default_factory=AgentBuild)

    def __post_init__(self) -> None:
        valid_role(self.agent, self.role)

    @property
    def id(self) -> str:
        """This member's id — see `member_id`."""
        return member_id(self.agent, self.role)

    @classmethod
    def of(cls, agent: str, role: str | None = None,
           build: AgentBuild | None = None) -> Member:
        """A member with the role defaulted to the agent's own name.

        The default is what makes `role` optional in a `.legoset` while keeping
        every member's id unambiguous — see `member_id`."""
        return cls(agent=agent, role=role if role is not None else agent,
                   build=build if build is not None else AgentBuild())
