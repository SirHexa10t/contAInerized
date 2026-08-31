"""`cluster.toml` — one cluster's durable state, and how clusters are found.

    # clusters/devteam-poc/cluster.toml
    project  = "/home/u/proj"
    template = "devteam"

    [researcher__primary]
    engine      = "researcher"
    professions = ["code"]
    specialties = ["muxer", "cluster"]
    policies    = ["all-actions"]

The per-member tables are **exactly the shape `instances.toml` uses** — the same
four axis keys, round-tripped through the same `store.entry_to_build` /
`store.build_entry` helpers — because a member resolves to an `Instance` by the
ordinary path and a cluster must not introduce a second tag pipeline. What differs
is only the keying: an instance is keyed by `<agent>__<session>`, a member by its
id *within* its cluster, so the agent and role are read back out of the table name
(`member.split_member_id`) instead of being stored twice.

`project` is cluster-level rather than per-member: every member works the same
project. Each member's own *checkout* of it is derived from the session and
member id (`paths.cluster_worktree_path`), so it is a computed path and never
stored — which also means the checkout MECHANISM (local clone, chosen; worktrees,
documented in `alternative_parallel_setup_w_worktrees.md`) can change without
touching this file or any saved cluster.

Discovery is a scan for dirs containing `cluster.toml` — the same choice as
`{cowork}`'s scan for `session.json`, for the same reason: the directory is
already the authoritative record, so a separate registry could only disagree
with it.

**Serialization note.** This writes its own ~15-line TOML emitter rather than
calling `tags.store.dumps`, which is fixed to that file's header and its
`workspace`/`engine` pair. The *schema* logic is genuinely shared (the two
`store` helpers above); only the formatting is restated. If a third
axis-store appears, extract the emitter then — two similar shapes stay two.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..tags import Registry

from ..file_access import (
    force_remove, is_dir, is_file, iter_subdirs, read_text, write_text,
)
from ..paths import (
    cluster_path, cluster_state_path, cluster_worktree_path,
    cluster_worktrees_dir, clusters_dir,
)
from ..tags.lego import AgentBuild
from ..tags.store import build_entry, entry_to_build
from ..utils import shell_returncode
from . import worktree
from .member import ClusterError, Member, split_member_id, valid_label

_FILE_HEADER = (
    "# Cluster state — one table per member, keyed by <agent>__<role>.\n"
    "# Launcher-owned: rewritten whenever the cluster is created or modified.\n"
    "# Member tables carry the same four tag axes as instances.toml; the agent\n"
    "# and role are read back out of the table name, not stored twice.\n"
)
_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_AXES = ("professions", "specialties", "policies")


@dataclass(frozen=True)
class Cluster:
    """One cluster: where it works, what it was built from, and who is in it.

    Frozen for the same reason as `Member` — every mutation is a file rewrite, so
    `with_member` / `without_member` return new objects a caller must save."""
    session: str
    project: Path
    members: tuple[Member, ...]
    template: str | None = None

    def __post_init__(self) -> None:
        valid_label(self.session, "session name")
        if not self.members:
            raise ClusterError(
                f"cluster {self.session!r} has no members — a cluster with "
                f"nobody in it cannot be launched")
        seen: set[str] = set()
        for member in self.members:
            if member.id in seen:
                raise ClusterError(
                    f"cluster {self.session!r} has two members named "
                    f"{member.id!r} — roles must be unique within a cluster")
            seen.add(member.id)

    @property
    def ids(self) -> tuple[str, ...]:
        """Member ids in STORAGE order (id-sorted after a round trip). The
        order anything user-facing uses — windows, picker rows, previews — is
        `picker_order`, which needs the registry and so cannot live on the
        frozen record."""
        return tuple(m.id for m in self.members)

    def member(self, identifier: str) -> Member | None:
        """The member with this id, or None. Used by the picker to edit one."""
        return next((m for m in self.members if m.id == identifier), None)

    def worktree(self, identifier: str) -> Path:
        """Where this member's own checkout of the project lives (host side)."""
        return cluster_worktree_path(self.session, identifier)

    def with_member(self, member: Member) -> Cluster:
        """This cluster plus `member` — raising on a duplicate id, since silently
        ignoring one would leave the caller thinking it had added a member."""
        if self.member(member.id) is not None:
            raise ClusterError(
                f"{member.id!r} is already in cluster {self.session!r}")
        return replace(self, members=(*self.members, member))

    def without_member(self, identifier: str) -> Cluster:
        """This cluster minus one member (no-op if absent — removal is
        idempotent, unlike addition, because the end state is unambiguous)."""
        return replace(self, members=tuple(m for m in self.members
                                           if m.id != identifier))

    def with_build(self, identifier: str, build: AgentBuild) -> Cluster:
        """This cluster with one member's tags replaced — the picker's F2 edit.

        Order untouched (it is window order; re-tagging must not reshuffle),
        unknown ids raised on (the edit came from a row that names a member —
        missing means the file changed underneath, worth a loud stop), and the
        FORCED specialties re-applied: the tag form lets a user untick
        anything, but an edit is a second place a member's build enters the
        file, so it gets the same guarantee `from_template` gives the first —
        no path produces a member unaware it is one."""
        member = self.member(identifier)
        if member is None:
            raise ClusterError(
                f"cluster {self.session!r} has no member {identifier!r}")
        updated = with_forced_tags(replace(member, build=build))
        return replace(self, members=tuple(
            updated if m.id == identifier else m for m in self.members))


# ============================================================
# Serialization
# ============================================================

def dumps(cluster: Cluster) -> str:
    """`cluster` as TOML: header, the cluster-level keys, then one key-sorted
    table per member.

    Tables are sorted so the file has a canonical form (a re-save with no
    change produces no diff) — and that IS the whole ordering story: no
    `order` field. Window/display order is DERIVED at use time from the same
    logic that sorts the picker's agent rows (`picker_order`), a decision made
    to keep one ordering everywhere instead of an authored sequence the user
    would have to manage (an earlier format stored `order`; `loads` ignores it
    in old files)."""
    lines = [_FILE_HEADER,
             f"project = {_toml_str(str(cluster.project))}"]
    if cluster.template is not None:
        lines.append(f"template = {_toml_str(cluster.template)}")
    blocks = ["\n".join(lines) + "\n"]
    for member in sorted(cluster.members, key=lambda m: m.id):
        entry = build_entry(member.build, workspace=None)
        table = [f"[{_toml_key(member.id)}]"]
        if entry.get("engine") is not None:
            table.append(f"engine = {_toml_str(entry['engine'])}")
        for axis in _AXES:
            values = ", ".join(_toml_str(v) for v in entry.get(axis, []))
            table.append(f"{axis} = [{values}]")
        blocks.append("\n".join(table) + "\n")
    return "\n".join(blocks)


def loads(session: str, text: str) -> Cluster:
    """Parse one cluster's TOML. Members come back id-sorted — the canonical
    STORAGE order; the order anything displays or launches in is derived from
    the registry at use time (`picker_order`). A legacy file's `order` key is
    ignored rather than validated: the field carried authored window order,
    a concept this format dropped."""
    data = tomllib.loads(text)
    project = data.get("project")
    if not isinstance(project, str) or not project:
        raise ClusterError(f"cluster {session!r}: 'project' is missing")
    template = data.get("template") if isinstance(data.get("template"), str) else None

    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    members = []
    for identifier in sorted(tables):
        agent, role = split_member_id(identifier)
        members.append(Member(agent=agent, role=role,
                              build=entry_to_build(tables[identifier])))
    return Cluster(session=session, project=Path(project),
                   members=tuple(members), template=template)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.match(key) else json.dumps(key)


def _toml_str(value: str) -> str:
    # JSON string escaping is a subset of TOML basic-string escaping, so
    # json.dumps emits a valid TOML string (same trick as tags/store.py).
    return json.dumps(value)


# ============================================================
# Disk
# ============================================================

def save(cluster: Cluster) -> Cluster:
    """Persist and return it, so callers can chain (`cluster = save(...)`) and
    never hold a stale object — the shape `cowork.group.save_session` uses."""
    write_text(cluster_state_path(cluster.session), dumps(cluster))
    return cluster


def load(session: str) -> Cluster | None:
    """One cluster by session name, or None when there is no such cluster.

    None rather than raising: "no cluster called that" is an answer a CLI prints,
    not a fault. A cluster whose file exists but is corrupt DOES raise — that is
    a fault, and silently treating it as absent would invite overwriting it."""
    path = cluster_state_path(session)
    if not is_file(path):
        return None
    return loads(session, read_text(path))


def discover() -> list[Cluster]:
    """Every cluster on disk, session-sorted. Unreadable ones are skipped rather
    than fatal, so one corrupt cluster cannot hide the healthy ones from a
    listing (audit is where corruption gets reported)."""
    if not is_dir(clusters_dir()):
        return []
    found = []
    for directory in iter_subdirs(clusters_dir()):
        if not is_file(cluster_state_path(directory.name)):
            continue
        try:
            cluster = load(directory.name)
        except (ClusterError, tomllib.TOMLDecodeError, OSError):
            continue
        if cluster is not None:
            found.append(cluster)
    return sorted(found, key=lambda c: c.session)


def exists(session: str) -> bool:
    """Whether a cluster of this name is already on disk — the guard a `create`
    consults before writing, so it refuses rather than clobbering."""
    return is_file(cluster_state_path(session))


def rename(cluster: Cluster, new_session: str) -> Cluster:
    """The cluster under a new name: its whole directory moves (member state
    dirs, banner, script, any worktrees ride along) and the state saves under
    the new session. Refuses a collision rather than merging two clusters.

    KNOWN LIMITATION, deliberate: worktree BRANCH names embed the old session
    (`cluster/<old>/*`) and are not rewritten — git branches are the user's
    history, and silently renaming them is how work gets lost. A worktree
    cluster that must be branch-consistent is destroy-and-recreate territory."""
    valid_label(new_session, "session name")
    if new_session == cluster.session:
        return save(cluster)
    if exists(new_session):
        raise ClusterError(
            f"a cluster named {new_session!r} already exists — renaming "
            f"{cluster.session!r} onto it would merge two clusters")
    from ..file_access import move_path
    move_path(cluster_path(cluster.session), cluster_path(new_session))
    return save(replace(cluster, session=new_session))


def picker_order(members: tuple[Member, ...],
                 registry: "Registry") -> tuple[Member, ...]:
    """Members in the picker's order — THE member ordering, everywhere.

    Same logic that sorts the agent Create rows (profession group, engine
    family, name — via `creatable_agents`), members of one agent then
    id-alphabetical beneath it. Decided over an authored `order` field: one
    derived ordering the user never has to manage, consistent with how the
    picker already arranges agents, at the accepted cost that a template's
    file order and the form's pick order carry no meaning.

    Every consumer of a member SEQUENCE goes through here — tmux window
    creation, the picker's member rows, previews, summaries — so windows,
    `^b 1..9` numbers, and row order can never disagree. An agent missing
    from the registry index sorts last rather than crashing: the row/preview
    renderers show such members as problems, and ordering is not the place to
    die. Lazy import: agents_crud is core-layer and imports no cluster code,
    but keeping state.py's import list honest about its own weight matters
    more than saving a line here."""
    from ..agents_crud import creatable_agents
    rank = {agent.name: index
            for index, agent in enumerate(creatable_agents(registry))}
    return tuple(sorted(members,
                        key=lambda m: (rank.get(m.agent, len(rank)), m.id)))


def destroy(cluster: Cluster) -> None:
    """Remove the cluster from disk — the inverse of `save`, at directory level.

    Worktrees first (checkouts only: `git worktree remove` drops the checkout,
    not the commits, so a member's branches stay reachable under
    `cluster/<session>/*`), then the state directory. The worktree teardown is
    GUARDED on any worktree actually existing: a shared-workspace cluster never
    made any, and its project may not even be a git repository — running git
    against it would fail noisily for nothing. Prints nothing; the CLI and the
    picker each narrate their own way. Shared by both so there is exactly one
    definition of what destroying a cluster means."""
    if any(cluster.worktree(identifier).is_dir() for identifier in cluster.ids):
        trees = worktree.plan(cluster.session, cluster.ids,
                              cluster_worktrees_dir(cluster.session))
        existing = {p.resolve() for p in worktree.existing_worktrees(cluster.project)}
        for tree in trees:
            if tree.path.resolve() in existing:
                shell_returncode(*worktree.remove_argv(cluster.project, tree))
        shell_returncode(*worktree.prune_argv(cluster.project))
    force_remove(cluster_path(cluster.session))


# Tags every member carries, whatever its agent's `.lego` says. `cluster` is what
# tells an agent it is one of several (without it a member would introduce itself
# wrongly and address nobody); `muxer` is its tree parent, added explicitly rather
# than left to the form's auto-tick because a cluster is created programmatically
# and must not depend on interactive cascade behaviour to be launchable.
FORCED_SPECIALTIES = ("muxer", "cluster")


def with_forced_tags(member: Member) -> Member:
    """`member` with the cluster specialties merged into its build.

    Order-preserving union rather than an append: an agent whose `.lego` already
    lists one of them must not end up carrying it twice (the store dedupes, but a
    duplicate would show twice in the picker before it ever reached disk)."""
    have = tuple(member.build.specialties)
    missing = tuple(name for name in FORCED_SPECIALTIES if name not in have)
    if not missing:
        return member
    return replace(
        member,
        build=replace(member.build, specialties=have + missing))


def from_template(session: str, project: Path, members: tuple[Member, ...],
                  *, template: str | None = None) -> Cluster:
    """Build a fresh cluster. Separate from `Cluster(...)` so the creation path
    has one named entry point, and so `paths.cluster_path` interest stays here
    rather than in every caller.

    Applies FORCED_SPECIALTIES here — at the ONE place a cluster comes into
    existence — so there is no path that produces a member unaware it is one."""
    valid_label(session, "session name")
    return Cluster(session=session, project=project,
                   members=tuple(with_forced_tags(m) for m in members),
                   template=template)


def cluster_dir(session: str) -> Path:
    """The cluster's own directory — re-exported so callers do not each import
    the path builder alongside this module."""
    return cluster_path(session)
