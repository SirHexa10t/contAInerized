"""Who a manager could recruit: discovery of cowork-capable instances.

Answers one question — "who can I work with?" — and answers it from three
independent signals, none of which this module gathers itself:

  * **capability** — does the instance carry `{cowork}`? Without it there is no
    Stop hook and no `/cowork` mount, so the hub could wake it but never hear
    back. Read off the resolved identity (`Instance.is_cowork`).
  * **liveness** — is its container up? Injection types into a live TTY, so a
    stopped instance cannot be reached at all. One `docker ps`, via
    `docker_config`, which owns every docker call.
  * **commitments** — which active groups is it already in? A peer juggling
    three groups is a poor fourth choice, and that is the manager's call to make
    with the facts in front of it.

**Only self is excluded, never other managers.** `{manager}` nests inside
`{cowork}`, so a manager-tagged peer is cowork-capable by inheritance and is
among the most capable coworkers available — filtering by role would rule out the
best candidates. The asker passes its own id and gets a list with itself removed;
that is the layer where self-recruitment is prevented, which is why
`Session.with_coworker` only has to refuse to record it.

`needs_relaunch` exists because "nobody is available" is a confusing answer when
the real situation is "three peers are running, none of them tagged yet". A
`{cowork}` instance is tagged at LAUNCH — the tag arrives via `settings.json` —
so the fix is always a relaunch, and naming the instances makes that actionable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..agents_crud import instance_from_store, list_all_instances
from ..docker_config import docker_running_instances_subprocess
from ..tags import Registry
from .group import GroupStatus, discover_sessions


@dataclass(frozen=True)
class Candidate:
    """One instance a manager could recruit, with everything needed to choose
    between several without asking a follow-up question.

    `workspace` earns its place: two sessions of the same agent are told apart by
    what they are working on, not by their ids alone. `groups` is the
    commitment signal — see the module docstring."""
    instance: str
    running: bool
    workspace: str
    tags: tuple[str, ...]
    groups: tuple[str, ...]

    @property
    def committed(self) -> bool:
        """True when this peer is already in at least one active group."""
        return bool(self.groups)


@dataclass(frozen=True)
class Roster:
    """The answer to one `roster` request.

    `liveness_known` is False when `docker ps` could not be consulted at all. It
    is reported rather than folded into `running`, because "everyone is offline"
    and "we could not tell" lead a manager to opposite conclusions, and silently
    presenting the second as the first is how a hub earns distrust."""
    candidates: tuple[Candidate, ...]
    needs_relaunch: tuple[str, ...]
    liveness_known: bool


def survey(asker: str | None, registry: Registry) -> Roster:
    """Every cowork-capable instance except `asker`, plus who would qualify after
    a relaunch.

    `asker` is explicit — and may be None for a human at the CLI, who is not in
    the list to begin with — so that no caller can forget whose roster this is
    and hand a manager itself as a candidate.

    Groups are discovered ONCE and indexed, rather than asked per candidate:
    `group.sessions_for` rescans the whole tree on every call, which would make
    this O(instances x groups) for no benefit."""
    running = docker_running_instances_subprocess()
    live = running or frozenset()
    commitments = _active_groups_by_participant()

    candidates: list[Candidate] = []
    needs_relaunch: list[str] = []
    for instance_id in list_all_instances():
        if instance_id == asker:
            continue
        instance = instance_from_store(instance_id, registry)
        if instance is None:
            continue          # orphaned state dir: its agent's .md is gone, so it is not recruitable at all
        if not instance.is_cowork:
            if instance_id in live:
                needs_relaunch.append(instance_id)
            continue
        candidates.append(Candidate(
            instance=instance_id,
            running=instance_id in live,
            workspace=instance.workspace or "",
            tags=tuple(t.name for t in (*instance.professions, *instance.specialties,
                                        *instance.policies)),
            groups=commitments.get(instance_id, ()),
        ))
    return Roster(candidates=tuple(sorted(candidates, key=_recruitability)),
                  needs_relaunch=tuple(sorted(needs_relaunch)),
                  liveness_known=running is not None)


def reachable(roster: Roster) -> tuple[Candidate, ...]:
    """The candidates the hub could actually wake right now.

    Kept separate from `survey` rather than filtered inside it: whether a manager
    should see stopped peers at all is a presentation decision, and the two
    answers differ — "recruit this one now" versus "this one exists, start it
    first". A caller picks; the discovery does not decide for it."""
    return tuple(c for c in roster.candidates if c.running)


def describe(roster: Roster) -> str:
    """The roster as text to hand an agent.

    Written for a reader that must choose, so it leads with what distinguishes
    the candidates and states the awkward cases outright — an empty list, or a
    liveness probe that failed — rather than letting a manager infer them from
    silence."""
    lines: list[str] = []
    if not roster.liveness_known:
        lines.append("! Could not reach docker, so nothing below is confirmed "
                     "running. Treat the list as 'known', not 'reachable'.")
    if roster.candidates:
        lines.append(f"{len(roster.candidates)} cowork-capable peer(s):")
        lines.extend(f"  {_describe_candidate(c)}" for c in roster.candidates)
    else:
        lines.append("No cowork-capable peers are known.")
    if roster.needs_relaunch:
        lines.append("")
        lines.append(f"{len(roster.needs_relaunch)} running instance(s) are NOT "
                     f"cowork-capable and would need relaunching with the tag "
                     f"before they can take part:")
        lines.extend(f"  {instance}" for instance in roster.needs_relaunch)
    return "\n".join(lines)


def _describe_candidate(candidate: Candidate) -> str:
    """One roster line: id, whether it can be woken, what it is, what it is on."""
    parts = [candidate.instance,
             "running" if candidate.running else "STOPPED (cannot be woken)"]
    if candidate.tags:
        parts.append(", ".join(candidate.tags))
    if candidate.workspace:
        parts.append(f"in {candidate.workspace}")
    if candidate.groups:
        parts.append(f"already in {len(candidate.groups)} group(s): "
                     f"{', '.join(candidate.groups)}")
    return " | ".join(parts)


def _active_groups_by_participant() -> dict[str, tuple[str, ...]]:
    """Active group keys per participating instance, from one tree scan."""
    index: dict[str, list[str]] = {}
    for session in discover_sessions():
        if session.status is not GroupStatus.ACTIVE:
            continue
        for participant in session.participants:
            index.setdefault(participant, []).append(session.key)
    return {instance: tuple(keys) for instance, keys in index.items()}


def _recruitability(candidate: Candidate) -> tuple[object, ...]:
    """Sort key: wakeable first, then uncommitted, then by name.

    The order is the recommendation. An agent reading an injected list acts on
    what it sees first, so the best candidate belongs at the top rather than
    whoever happens to sort alphabetically."""
    return (not candidate.running, len(candidate.groups), candidate.instance)
