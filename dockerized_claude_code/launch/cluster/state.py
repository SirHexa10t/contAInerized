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

from ..file_access import is_dir, is_file, iter_subdirs, read_text, write_text
from ..paths import (
    cluster_path, cluster_state_path, cluster_worktree_path, clusters_dir,
)
from ..tags.store import build_entry, entry_to_build
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
        """Member ids in definition order — which is tmux window order."""
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


# ============================================================
# Serialization
# ============================================================

def dumps(cluster: Cluster) -> str:
    """`cluster` as TOML: header, the cluster-level keys, then one key-sorted
    table per member.

    Tables are sorted so the file has a canonical form (a re-save with no change
    produces no diff), which means they cannot also carry launch order — hence
    the explicit `order` list. Window order is meaningful, so it is stored rather
    than inferred from whatever the tables happen to sort to."""
    lines = [_FILE_HEADER,
             f"project = {_toml_str(str(cluster.project))}"]
    if cluster.template is not None:
        lines.append(f"template = {_toml_str(cluster.template)}")
    lines.append(f"order = [{', '.join(_toml_str(i) for i in cluster.ids)}]")
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
    """Parse one cluster's TOML. `order` restores definition order, which the
    sorted tables cannot carry — window order is meaningful (a template author
    putting the lead first should get the lead first), so it is stored
    explicitly rather than inferred. An id in `order` that has no table, or a
    table missing from `order`, is a corrupt file and says so."""
    data = tomllib.loads(text)
    project = data.get("project")
    if not isinstance(project, str) or not project:
        raise ClusterError(f"cluster {session!r}: 'project' is missing")
    template = data.get("template") if isinstance(data.get("template"), str) else None

    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    raw_order = data.get("order")
    order = ([i for i in raw_order if isinstance(i, str)]
             if isinstance(raw_order, list) else sorted(tables))
    if set(order) != set(tables):
        raise ClusterError(
            f"cluster {session!r}: 'order' {sorted(order)} does not match the "
            f"member tables {sorted(tables)}")

    members = []
    for identifier in order:
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
