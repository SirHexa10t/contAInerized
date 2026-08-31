"""`.legoset` — a cluster template (TOML syntax).

`agents/<name>.legoset` names a default membership: which agents, how many of
each, and each one's default role. It is to a cluster what `.lego` is to an
instance — a *starting point* the creation form opens on, never a lock. The user
adds, drops, renames, and re-tags members afterwards.

    # agents/devteam.legoset
    members = [
      { agent = "project-starter" },
      { agent = "refactorer" },
      { agent = "researcher", role = "primary" },
      { agent = "researcher", role = "adversarial" },
      { agent = "bug-investigator" },
    ]

A bare string is sugar for the one-key table, so a template with no interesting
roles stays a one-liner:

    members = ["project-starter", "refactorer"]

Kept separate from the tag tree and from `.lego`, exactly as `lego.py` is: a
`.legoset` names AGENTS, it is not one, and it says nothing about tags — each
member inherits its own agent's `.lego`. Reference validity (do these agents
exist?) is checked against the registry by `validate`, not here, mirroring how
`lego.load_lego` leaves validation to `Registry.validate_build`.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..tags.base import read_toml
from ..tags.lego import AgentBuild, load_lego
from .member import ClusterError, Member, member_id

LEGOSET_SUFFIX = ".legoset"
LEGO_SUFFIX = ".lego"      # the per-agent build file each member inherits
MEMBER_KEYS = ("agent", "role")


@dataclass(frozen=True)
class ClusterTemplate:
    """A parsed `.legoset`: its name (the file stem), its default members, and
    an optional one-line description (what the picker's template row shows
    after the name — the same slot an agent row fills from its `.md`'s first
    line; empty means the row falls back to enumerating the members).

    Members keep the file's PARSE order for determinism (the form's prefill
    lists them as authored), but the order carries no launch meaning: window
    and display order are DERIVED everywhere by picker-sort
    (`state.picker_order`) — decided so ordering is one less thing to author."""
    name: str
    members: tuple[Member, ...]
    description: str = ""

    @property
    def agents(self) -> frozenset[str]:
        """Every agent named, deduplicated — what a registry check consults."""
        return frozenset(m.agent for m in self.members)


def load_legoset(path: Path) -> ClusterTemplate:
    """Parse a `.legoset`. Unlike `load_lego`, a MISSING file is an error rather
    than an empty default: an instance with no `.lego` is a legitimate
    all-defaults build, whereas a cluster with no members is nothing at all.

    Type-checks every entry and rejects duplicate ids, so a template that would
    have produced two members writing the same directory fails here — at parse
    time, naming the file — rather than at launch."""
    if not path.is_file():
        raise ClusterError(f"no cluster template at {path}")
    data = read_toml(path)

    description = data.get("description", "")
    if not isinstance(description, str):
        raise ClusterError(f"{path}: 'description' must be a string")

    raw = data.get("members")
    if not isinstance(raw, list) or not raw:
        raise ClusterError(f"{path}: 'members' must be a non-empty list")

    members: list[Member] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(raw):
        member = _member_from(entry, path, index)
        if member.id in seen:
            raise ClusterError(
                f"{path}: two members would both be named {member.id!r} "
                f"(entries {seen[member.id]} and {index}) — give at least one of "
                f"them a distinct 'role'")
        seen[member.id] = index
        members.append(member)
    return ClusterTemplate(name=path.stem, members=tuple(members),
                           description=description.strip())


def _member_from(entry: Any, path: Path, index: int) -> Member:
    """One `members` entry as a `Member`, in either accepted form."""
    if isinstance(entry, str):
        return Member.of(entry)
    if not isinstance(entry, dict):
        raise ClusterError(
            f"{path}: members[{index}] must be a string or a table with 'agent' "
            f"(and optionally 'role'), got {type(entry).__name__}")
    unknown = set(entry) - set(MEMBER_KEYS)
    if unknown:
        raise ClusterError(
            f"{path}: members[{index}] has unknown key(s) "
            f"{', '.join(sorted(unknown))} — only {', '.join(MEMBER_KEYS)} are "
            f"recognised (tags come from the agent's own .lego)")
    agent = entry.get("agent")
    if not isinstance(agent, str) or not agent:
        raise ClusterError(f"{path}: members[{index}] needs a non-empty string 'agent'")
    role = entry.get("role")
    if role is not None and (not isinstance(role, str) or not role):
        raise ClusterError(f"{path}: members[{index}]'s 'role' must be a non-empty string")
    return Member.of(agent, role)


def validate(template: ClusterTemplate, known_agents: frozenset[str]) -> None:
    """Raise unless every agent the template names exists.

    Takes the agent NAME SET rather than a Registry so the check is trivially
    testable and so this module keeps its one dependency direction (it already
    imports the tag package for TOML reading, not for lookups). The caller
    supplies `agents_crud.agent_md_index`'s keys, or the registry's."""
    missing = sorted(template.agents - known_agents)
    if missing:
        raise ClusterError(
            f"cluster template {template.name!r} names unknown agent(s): "
            f"{', '.join(missing)}")


def instantiate(template: ClusterTemplate, agents_dir: Path) -> tuple[Member, ...]:
    """The template's members, each carrying its own agent's `.lego` defaults.

    Parsing a `.legoset` deliberately yields members with EMPTY builds — the file
    names agents, not tags. This is the step that gives each member the starting
    point its agent declares (engine, professions, specialties, policies), which
    is what makes a `refactorer` member arrive with `[code]` and `<-gpush>` rather
    than as a bare base image.

    Separate from `load_legoset` because it touches disk per agent: parsing stays
    pure and testable, and the lookup happens once, here, at instantiation."""
    return tuple(
        Member(agent=member.agent, role=member.role,
               build=load_lego(agents_dir / f"{member.agent}{LEGO_SUFFIX}"))
        for member in template.members)


def auto_roles(picks: Sequence[tuple[str, str | None]]) -> list[tuple[str, str]]:
    """Final `(agent, role)` pairs for an ordered pick list, disambiguating
    duplicates without asking the user anything.

    The creation form deliberately has NO per-member editing (decided: role
    fields mid-flow are noise during setup; fine detail is edited later, from
    the picker). So when picking twice adds a second `researcher`, the roles
    that keep member ids unique must come from somewhere — here:

    - a role carried in (a template's `primary`) is kept verbatim;
    - an agent picked ONCE with no role stays bare (`researcher`, the collapsed
      id — the common case reads clean);
    - unroled entries of a MULTIPLY-picked agent are numbered in pick order
      (`golem` twice → `golem__1`, `golem__2`) — BOTH numbered, because a bare
      `golem` beside a `golem__2` would read as the senior of the two, which a
      duplicate is not;
    - numbering skips any value an explicit role already claims, so a template
      role that happens to be `"1"` cannot collide.

    Pure and order-preserving (order is tmux window order), so the form can
    call it live to PREVIEW the ids each pick will create."""
    totals = Counter(agent for agent, _ in picks)
    claimed: dict[str, set[str]] = defaultdict(set)
    for agent, role in picks:
        if role is not None:
            claimed[agent].add(role)
    next_number: dict[str, int] = defaultdict(int)
    out: list[tuple[str, str]] = []
    for agent, role in picks:
        if role is None:
            if totals[agent] == 1:
                role = agent                      # collapses to the bare id
            else:
                number = next_number[agent] + 1
                while str(number) in claimed[agent]:
                    number += 1
                next_number[agent] = number
                role = str(number)
                claimed[agent].add(role)
        out.append((agent, role))
    return out


def assemble(picks: Sequence[tuple[str, str | None]],
             agents_dir: Path) -> tuple[Member, ...]:
    """Ordered form picks → members carrying their agents' `.lego` defaults —
    the form-side sibling of `instantiate`, for a membership the user grew by
    hand rather than a template's verbatim list. Roles come from `auto_roles`;
    each agent's `.lego` is read once however many members it yields."""
    builds: dict[str, AgentBuild] = {}
    members: list[Member] = []
    for agent, role in auto_roles(picks):
        if agent not in builds:
            builds[agent] = load_lego(agents_dir / f"{agent}{LEGO_SUFFIX}")
        members.append(Member(agent=agent, role=role, build=builds[agent]))
    return tuple(members)


def reassemble(existing: tuple[Member, ...],
               picks: Sequence[tuple[str, str | None]],
               agents_dir: Path) -> tuple[Member, ...]:
    """Form picks applied to an EXISTING membership — the edit-flow sibling of
    `assemble`. The distinction it exists for: a surviving member keeps its
    CURRENT build (per-member tag edits made via F2 must not be wiped back to
    the agent's `.lego` defaults by an unrelated rename), while a newly picked
    member starts from its `.lego` exactly as creation would. A pick whose
    derived id matches nobody is new; an existing member no pick derives is
    dropped."""
    current = {member.id: member for member in existing}
    builds: dict[str, AgentBuild] = {}
    members: list[Member] = []
    for agent, role in auto_roles(picks):
        identifier = member_id(agent, role)
        if identifier in current:
            members.append(current[identifier])
            continue
        if agent not in builds:
            builds[agent] = load_lego(agents_dir / f"{agent}{LEGO_SUFFIX}")
        members.append(Member(agent=agent, role=role, build=builds[agent]))
    return tuple(members)


def discover_templates(agents_dir: Path) -> dict[str, Path]:
    """Every `.legoset` in the agents dir, by name — the pick list a cluster
    form opens with. A scan rather than a registry, for the same reason cowork
    discovers groups by scanning: the directory IS the record."""
    return {path.stem: path for path in sorted(agents_dir.glob(f"*{LEGOSET_SUFFIX}"))}
