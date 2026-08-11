"""Group identity and durable state for `{cowork}` collaborations.

Two files, deliberately split by who may read them:

  `<manager>/<group-key>/session.json`   one per group, inside the MANAGER's own
      mounted dir — so the manager can read its own group's membership, task, and
      round budget without the hub copying anything. Its *presence* is also what
      marks a directory as a group, which is why there is no separate registry:
      `discover_sessions` scans for it.

  `group_hosting/hub.state.json`         the hub's own bookkeeping, at the root
      and therefore outside every participant's mount. Holds only what agents
      must not see or tamper with: each participant's transcript high-water mark
      and its outstanding send.

Group membership deliberately lives in `session.json` alone and is NOT mirrored
into hub state: a restarted hub rediscovers groups by scanning, so duplicating
them would create two sources of truth for one fact.

Writes go through `file_access.write_text`, which is already atomic (same-dir
temp file, then `os.replace`) and creates parents — so a crash mid-write can
never leave a participant reading a truncated `session.json` through its
bind-mount.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from ..file_access import is_dir, is_file, iter_subdirs, read_text, write_text
from ..paths import (
    INBOX_SEPARATOR, cowork_group_path, group_hosting_dir, group_key,
    group_session_path, hub_state_path,
)

HUB_STATE_SCHEMA = 1     # bumped only on a breaking change to the on-disk shape


def _separator_free(label: str, kind: str) -> str:
    """`label`, or a ValueError if it carries a character some composed name
    needs to be able to split on.

    Two composed names lean on this. An inbox dir is named `<group>@<sender>`
    and sits as a SIBLING of the group dirs in the same tree, so the two must
    never be able to share a name. And the prompt marker is
    `[cowork task <manager>::<project>]` (mailbox.TAG_SEPARATOR), which
    attribution splits on its first `::` to rebuild the group key. Both are
    structural guarantees, not conventions — and they hold only as long as
    nothing that composes a name smuggles a separator in. The session suffix a
    user types is free text (`menu_picker.prompt_session` does not restrict
    characters) and a project label is written by an agent, so both are checked
    here, at the point a name enters durable state.

    Raises rather than sanitising: silently rewriting an id would leave the group
    keyed under a name its participants do not answer to."""
    if INBOX_SEPARATOR in label:
        raise ValueError(f"a cowork {kind} may not contain "
                         f"{INBOX_SEPARATOR!r} (it separates a group from a "
                         f"sender in inbox dir names): {label!r}")
    if ":" in label:
        raise ValueError(f"a cowork {kind} may not contain ':' (the prompt "
                         f"marker joins manager and project with '::'): "
                         f"{label!r}")
    return label


class GroupStatus(Enum):
    """A group's lifecycle. `CLOSED` groups stay on disk — their conversation is
    the record of what happened — so discovery returns them and callers filter."""
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True)
class Session:
    """One group's canonical state, as stored in its manager's `session.json`.

    Frozen because every mutation is a rewrite of the file anyway; use
    `dataclasses.replace` (see `with_coworker` / `with_round_used` / `closed`)
    so a caller cannot accidentally mutate a copy and forget to save it.

    `coworkers` excludes the manager: the manager is a participant, but it is
    identified by `manager` and the two roles are never interchangeable.
    """
    manager: str
    project: str
    task: str
    coworkers: tuple[str, ...] = ()
    round_budget: int = 6
    rounds_used: int = 0
    status: GroupStatus = GroupStatus.ACTIVE
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def key(self) -> str:
        """The group key — the one string naming this group in EVERY
        participant's tree, so a coworker in several groups keeps them apart."""
        return group_key(self.manager, self.project)

    @property
    def participants(self) -> tuple[str, ...]:
        """Manager first, then coworkers — everyone the hub may route to."""
        return (self.manager, *self.coworkers)

    @property
    def rounds_left(self) -> int:
        """Forwards still permitted before the hub stops relaying. Never
        negative, so callers can treat 0 as "stop" without clamping."""
        return max(0, self.round_budget - self.rounds_used)

    def with_coworker(self, coworker: str) -> Session:
        """This session plus `coworker`; unchanged if already a member, so a
        repeated `recruit` is idempotent rather than duplicating an entry.

        A manager recruiting ITSELF is the same no-op. Note this rejects only
        self — another `{manager}`-tagged instance is a perfectly good coworker,
        since `{manager}` nests inside `{cowork}`, and a guard that rejected
        managers outright would forbid the most capable coworkers there are."""
        if coworker in self.coworkers or coworker == self.manager:
            return self
        return replace(self, coworkers=(*self.coworkers, _separator_free(coworker, "instance id")))

    def without_coworker(self, coworker: str) -> Session:
        """This session minus `coworker`; unchanged if it was not a member —
        idempotent like `with_coworker`, so a repeated `release` is safe.
        The released peer's group dir and inboxes stay on disk: membership is
        routing state, and the work already exchanged is not the hub's to
        destroy."""
        if coworker not in self.coworkers:
            return self
        return replace(self, coworkers=tuple(c for c in self.coworkers
                                             if c != coworker))

    def with_round_used(self) -> Session:
        return replace(self, rounds_used=self.rounds_used + 1)

    def closed(self) -> Session:
        return replace(self, status=GroupStatus.CLOSED)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "manager": self.manager,
            "project": self.project,
            "task": self.task,
            "coworkers": list(self.coworkers),
            "round_budget": self.round_budget,
            "rounds_used": self.rounds_used,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Session | None:
        """Build from parsed JSON, or None if the required fields are missing or
        the wrong shape. Returning None rather than raising keeps one corrupt
        `session.json` from breaking discovery for every other group."""
        manager, project = payload.get("manager"), payload.get("project")
        if not isinstance(manager, str) or not isinstance(project, str):
            return None
        try:
            status = GroupStatus(payload.get("status", GroupStatus.ACTIVE.value))
        except ValueError:
            return None
        coworkers = payload.get("coworkers", [])
        if not isinstance(coworkers, list) or not all(isinstance(c, str) for c in coworkers):
            return None
        return cls(
            manager=manager,
            project=project,
            task=str(payload.get("task", "")),
            coworkers=tuple(coworkers),
            round_budget=int(payload.get("round_budget", 6)),
            rounds_used=int(payload.get("rounds_used", 0)),
            status=status,
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )


def session_dir(session: Session) -> Path:
    """Where this group's canonical state lives: the group dir inside its
    MANAGER's mounted tree. A coworker's same-named dir is its working copy and
    deliberately holds no `session.json`."""
    return cowork_group_path(session.manager, session.key)


def save_session(session: Session, *, now: float | None = None) -> Session:
    """Persist `session`, stamping `updated_at` (and `created_at` on first
    write). Returns the stamped copy so the caller keeps what is on disk rather
    than the pre-write value."""
    stamp = time.time() if now is None else now
    stamped = replace(session, updated_at=stamp,
                      created_at=session.created_at or stamp)
    write_text(group_session_path(session_dir(stamped)), stamped.to_json())
    return stamped


def load_session(group_dir: Path) -> Session | None:
    """The Session recorded in `group_dir`, or None when there is no readable
    `session.json` there — which is also how "this directory is not a group"
    is expressed (a coworker's working copy, or a manager's inbox dir, which
    shares the group dir's name prefix)."""
    path = group_session_path(group_dir)
    if not is_file(path):
        return None      # read_text raises on a missing file; absence is the common case here
    text = read_text(path)
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return Session.from_payload(payload) if isinstance(payload, dict) else None


def create_session(manager: str, project: str, task: str,
                   round_budget: int = 6) -> Session:
    """Create and persist a new group hosted by `manager`.

    `project` is the manager's own space-free label, so the same participants can
    be convened again for a different task as a separate group. Recreating an
    existing key returns the stored session untouched rather than resetting its
    round count — re-issuing `recruit` should be safe.

    Both halves of the key are separator-checked before anything is written: the
    key names this group's directory in EVERY participant's tree, so a bad one
    would not be a local mistake."""
    _separator_free(manager, "instance id")
    _separator_free(project, "project label")
    existing = load_session(cowork_group_path(manager, group_key(manager, project)))
    if existing is not None:
        return existing
    return save_session(Session(manager=manager, project=project, task=task,
                                round_budget=round_budget))


def discover_sessions() -> list[Session]:
    """Every group on disk, found by scanning rather than from a registry.

    Walks `group_hosting/<instance>/<dir>/` and treats any dir holding a
    readable `session.json` as a group. Two things are checked rather than
    assumed, because both indicate state that has drifted:

      * the recorded `manager` must match the enclosing instance dir — a group
        found under someone else's tree is misplaced, not authoritative;
      * the dir name must equal the session's own key.

    Mismatches are skipped so one bad directory cannot poison discovery;
    `misfiled_sessions` below is their report, for the audit.

    Sorted by key for stable output.
    """
    found: list[Session] = []
    for instance_dir, candidate in _group_candidates():
        session = load_session(candidate)
        if session is None:
            continue
        if session.manager != instance_dir.name or candidate.name != session.key:
            continue
        found.append(session)
    return sorted(found, key=lambda s: s.key)


def misfiled_sessions() -> list[tuple[Path, str]]:
    """Every dir holding a `session.json` that `discover_sessions` SKIPS,
    each with why — the complement of discovery, fed to the audit.

    Sharing `_group_candidates` with discovery is the point: the two answers
    partition the same walk, so a dir can never fall between them. Three ways
    in: the file does not parse; it parses but records a different manager
    than the tree it sits in (someone's copy, or a hand-move); or the dir was
    renamed away from the session's own key. All three mean the hub will not
    route for this group until a human decides which side is right — which is
    exactly what makes them audit findings rather than log lines."""
    out: list[tuple[Path, str]] = []
    for instance_dir, candidate in _group_candidates():
        session = load_session(candidate)
        if session is None:
            out.append((candidate, "session.json is unreadable or malformed"))
        elif session.manager != instance_dir.name:
            out.append((candidate, f"records manager '{session.manager}' but sits "
                                   f"in '{instance_dir.name}'s tree"))
        elif candidate.name != session.key:
            out.append((candidate, f"dir name does not match its session key "
                                   f"'{session.key}' — was the dir renamed?"))
    return sorted(out)


def _group_candidates() -> Iterator[tuple[Path, Path]]:
    """(instance_dir, candidate) for every dir CARRYING a session.json —
    the one walk both discovery and its audit-facing complement read, so their
    answers partition the tree instead of drifting apart. Dirs without the
    file (working copies, inboxes) are nobody's finding and are not yielded."""
    for instance_dir in iter_subdirs(group_hosting_dir()):
        for candidate in iter_subdirs(instance_dir):
            if is_file(group_session_path(candidate)):
                yield instance_dir, candidate


def sessions_for(instance: str) -> list[Session]:
    """Groups `instance` takes part in, as manager or coworker."""
    return [s for s in discover_sessions() if instance in s.participants]


def hosted_by(manager: str) -> list[Session]:
    """Groups `manager` hosts — what a manager lists to re-establish past work."""
    return [s for s in discover_sessions() if s.manager == manager]


@dataclass(frozen=True)
class ParticipantState:
    """The hub's private bookkeeping for one participant.

    `last_prompt_id` is the `prompt_id` of the newest turn already processed. It
    is an identity, not a count of capture files, so a crash between handling a
    capture and consuming it costs at most a skipped duplicate rather than a
    replayed forward. It remembers ONE turn, which is what that window can produce.

    `outstanding_send` names the group a delivery is awaiting a reply for. It is
    used for delivery checking (a send with no matching turn after a timeout
    means the injection did not land), not for attributing replies — attribution
    comes from pairing the reply with its prompt via the transcript.
    """
    last_prompt_id: str | None = None
    outstanding_send: str | None = None
    sent_at: float | None = None


@dataclass(frozen=True)
class HubState:
    """Everything the hub must remember across restarts, and nothing an agent
    should see. Group membership is absent on purpose — that is `session.json`'s
    job, and a restarted hub rediscovers it by scanning."""
    participants: dict[str, ParticipantState]
    schema: int = HUB_STATE_SCHEMA

    def for_participant(self, instance: str) -> ParticipantState:
        """This participant's state, defaulted for one not seen before."""
        return self.participants.get(instance, ParticipantState())

    def with_participant(self, instance: str, state: ParticipantState) -> HubState:
        return replace(self, participants={**self.participants, instance: state})

    def to_json(self) -> str:
        payload = {
            "schema": self.schema,
            "participants": {
                name: {"last_prompt_id": st.last_prompt_id,
                       "outstanding_send": st.outstanding_send,
                       "sent_at": st.sent_at}
                for name, st in sorted(self.participants.items())
            },
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def load_hub_state() -> HubState:
    """Hub state from disk, or an empty state when absent or unreadable.

    Degrading to empty rather than raising is deliberate: the worst case is that
    the hub reprocesses recent turns, whereas refusing to start would strand
    every group. A schema from the future is also treated as empty — better to
    re-derive than to misread a shape we do not know."""
    if not is_file(hub_state_path()):
        return HubState(participants={})     # first run: no state yet
    text = read_text(hub_state_path())
    if not text.strip():
        return HubState(participants={})
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return HubState(participants={})
    if not isinstance(payload, dict) or int(payload.get("schema", 0)) > HUB_STATE_SCHEMA:
        return HubState(participants={})
    raw = payload.get("participants", {})
    if not isinstance(raw, dict):
        return HubState(participants={})
    participants = {
        name: ParticipantState(
            last_prompt_id=entry.get("last_prompt_id"),
            outstanding_send=entry.get("outstanding_send"),
            sent_at=entry.get("sent_at"),
        )
        for name, entry in raw.items() if isinstance(entry, dict)
    }
    return HubState(participants=participants)


def save_hub_state(state: HubState) -> None:
    """Persist hub state. Called after every change, so a crash costs at most
    the turn in flight."""
    write_text(hub_state_path(), state.to_json())


def update_participant(instance: str, state: ParticipantState) -> HubState:
    """Persist one participant's bookkeeping, leaving every other untouched.

    The load-modify-save cycle lives here rather than at call sites: this module
    is the only writer of hub state, and a caller holding a copy across its own
    load and save could drop another participant's update made in between."""
    updated = load_hub_state().with_participant(instance, state)
    save_hub_state(updated)
    return updated


def orphan_group_dirs() -> list[Path]:
    """Group dirs whose enclosing instance no longer has a state dir — left
    behind when an instance is deleted while still a group member. Reported by
    the audit rather than cleaned up here, since the work inside may still be
    wanted."""
    from ..paths import instance_state_dir_path
    return [d for d in iter_subdirs(group_hosting_dir())
            if not is_dir(instance_state_dir_path(d.name))]
