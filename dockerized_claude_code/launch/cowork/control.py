"""The agent-facing control channel: how a `{manager}` drives the hub.

A manager writes a request FILE into its own `/cowork/control/` — the one
writable surface a contained agent has — and the hub answers by writing a reply
under `control/replies/` and injecting a one-line pointer at it. File-based like
everything else here: durable across hub restarts, inspectable by a human, and
needing no protocol beyond "first line is the command, the rest is the body".

**The gate is the requester's tags, checked hub-side, and that is the only place
it can be.** `control/` necessarily sits inside `/cowork`, so ANY `{cowork}`
instance can write a request file there — directory permissions cannot tell a
manager's request from a coworker's. So every request is resolved to its
instance's tag set (`Instance.is_manager`) before anything is honoured; a
non-manager's request is parked and reported, never answered, never acted on.
Coworkers participate by replying to their turns, not by commanding the hub.

**Group-scoped verbs additionally require OWNERSHIP.** A manager-tagged peer
recruited into someone else's group is still just a coworker there: `send`,
`release` and `done` act only on groups the requester hosts. Without that, two
managers in one group could each spend the other's round budget.

Replies are injected UNTAGGED on purpose: a `[cowork task <manager>::<project>]`
prefix marks traffic whose answer should be routed, and the manager's
acknowledgement of a roster is nobody's business — untagged, it drains as
unsolicited and stops.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..agents_crud import instance_from_store
from ..docker_config import docker_attach_inject
from ..file_access import (
    ensure_dir, is_dir, is_file_recent, iter_files, iter_subdirs, move_path,
    read_text, remove_path, write_text,
)
from ..paths import COWORK_IN_CONTAINER, cowork_dir_path, group_hosting_dir
from ..tags import Registry
from ..tags.identity import Instance
from . import journal, relay, roster, sync
from .group import GroupStatus, Session, create_session, hosted_by, save_session
from .relay import Event, EventKind

CONTROL_SUBDIR = "control"        # requests, directly inside a participant's own dir
REPLIES_SUBDIR = "replies"        # hub-written answers — a subdir so the request scan never re-reads them
REJECTED_SUBDIR = "rejected"      # denied or unreadable requests — kept, not deleted
SETTLE_SECONDS = 2.0              # a just-modified file may still be mid-write; give it a pass
FILES_FLAG = "+files"             # `send` suffix: hand the working copy over with the message


def poll_control(registry: Registry,
                 lookup: Callable[[str], Instance | None] | None = None,
                 ) -> tuple[Event, ...]:
    """One pass over every participant's `control/`: gate, dispatch, reply.

    `lookup` resolves an instance id to its tag-bearing identity; the default is
    the store (`agents_crud.instance_from_store`), injectable so tests can gate
    without building real store entries. `registry` is taken from the caller
    rather than scanned here because the hub polls every couple of seconds and
    the tag tree does not change mid-run."""
    resolve = lookup if lookup is not None else (
        lambda instance: instance_from_store(instance, registry))
    events: list[Event] = []
    for asker in _instances_with_control():
        control = cowork_dir_path(asker) / CONTROL_SUBDIR
        for request in iter_files(control):
            if is_file_recent(request, SETTLE_SECONDS):
                continue          # possibly still being written — next pass gets it
            events.append(_handle(asker, request, registry, resolve))
    return tuple(events)


def _handle(asker: str, request: Path, registry: Registry,
            lookup: Callable[[str], Instance | None]) -> Event:
    """Resolve one request file to one event, consuming the file."""
    text = read_text(request).strip()
    identity = lookup(asker)
    if identity is None or not identity.is_manager:
        _reject(asker, request)
        return Event(EventKind.DENIED, asker, "",
                     "control request from a non-manager — ignored")
    if not text:
        _reject(asker, request)
        return Event(EventKind.CONTROL, asker, "", "empty request file — rejected")

    command, _, body = text.partition("\n")
    verb, *args = command.split()
    handler = _VERBS.get(verb)
    detail = (handler(asker, args, body.strip(), registry) if handler
              else _refuse(asker, verb, f"unknown verb '{verb}' — expected one "
                                        f"of {', '.join(sorted(_VERBS))}"))
    remove_path(request)
    return Event(EventKind.CONTROL, asker, "", detail)


# ============================================================
# Verbs — each returns the one-line detail for the operator's event stream
# ============================================================

def _roster(asker: str, args: list[str], body: str, registry: Registry) -> str:
    """`roster` — who could be recruited, with the asker excluded."""
    _answer(asker, "roster", roster.describe(roster.survey(asker, registry)))
    return "answered a roster request"


def _recruit(asker: str, args: list[str], body: str, registry: Registry) -> str:
    """`recruit <project> <peer>...` — create or extend a group the asker hosts.
    The body is the task statement, recorded on creation."""
    if not args:
        return _refuse(asker, "recruit",
                       "usage: recruit <project> <peer-id> ... (body = the task)")
    project, peers = args[0], args[1:]
    try:
        session = create_session(asker, project, body)
        for peer in peers:
            session = session.with_coworker(peer)
        session = save_session(session)
    except ValueError as error:          # separator guard: '@' in a name
        return _refuse(asker, "recruit", str(error))
    members = "\n".join(f"  - {c}" for c in session.coworkers) or "  (none yet)"
    _answer(asker, "recruit",
            f"Group `{session.key}` — {session.rounds_left} of "
            f"{session.round_budget} round(s) left.\n\nCoworkers:\n{members}\n\n"
            f"Your working copy is {COWORK_IN_CONTAINER / session.key}/ — put the "
            f"material there, then `send {session.key} <peer-id> +files`.")
    return f"recruited {len(peers) or 'no'} peer(s) into '{session.key}'"


def _send(asker: str, args: list[str], body: str, registry: Registry) -> str:
    """`send <group> <peer> [+files]` — deliver the body, optionally handing the
    working copy over first (files land before the message that cites them)."""
    with_files = FILES_FLAG in args
    args = [a for a in args if a != FILES_FLAG]
    if len(args) != 2:
        return _refuse(asker, "send",
                       f"usage: send <group-key> <peer-id> [{FILES_FLAG}] "
                       f"(body = the message)")
    key, peer = args
    session = _hosted(asker, key)
    if session is None:
        return _refuse(asker, "send", _not_yours(asker, key))
    if not body:
        return _refuse(asker, "send", "the message body is empty — write it on "
                                      "the lines after the command")
    problem = relay.membership_problem(session, sender=asker, recipient=peer)
    if problem is not None:
        return _refuse(asker, "send", problem)
    if with_files:
        sync.hand_over(session, peer)
    outcome = relay.send(session, sender=asker, recipient=peer, body=body)
    if not outcome.delivered:
        return _refuse(asker, "send", outcome.reason)
    _answer(asker, "send",
            f"Delivered to {peer}"
            f"{' with your working copy' if with_files else ''} — "
            f"{outcome.session.rounds_left} of {outcome.session.round_budget} "
            f"round(s) left. The reply will arrive as a prompt.")
    return f"sent to {peer} in '{key}'"


def _release(asker: str, args: list[str], body: str, registry: Registry) -> str:
    """`release <group> <peer>` — drop a peer; its dirs stay on disk."""
    if len(args) != 2:
        return _refuse(asker, "release", "usage: release <group-key> <peer-id>")
    key, peer = args
    session = _hosted(asker, key)
    if session is None:
        return _refuse(asker, "release", _not_yours(asker, key))
    if peer not in session.coworkers:
        return _refuse(asker, "release", f"'{peer}' is not in '{key}'")
    save_session(session.without_coworker(peer))
    journal.append(session, journal.Direction.NOTE, peer, "released from the group")
    _answer(asker, "release", f"Released {peer} from `{key}`. Its inbox and "
                              f"working copy are kept on disk.")
    return f"released {peer} from '{key}'"


def _done(asker: str, args: list[str], body: str, registry: Registry) -> str:
    """`done <group>` — close a group the asker hosts."""
    if len(args) != 1:
        return _refuse(asker, "done", "usage: done <group-key>")
    session = _hosted(asker, args[0])
    if session is None:
        return _refuse(asker, "done", _not_yours(asker, args[0]))
    closed = save_session(session.closed())
    journal.append(closed, journal.Direction.NOTE, asker,
                   f"group closed by its manager after {closed.rounds_used} round(s)")
    _answer(asker, "done", f"`{closed.key}` is closed. Its files and "
                           f"conversation.md are kept.")
    return f"closed '{closed.key}'"


_VERBS = {"roster": _roster, "recruit": _recruit, "send": _send,
          "release": _release, "done": _done}


# ============================================================
# Plumbing
# ============================================================

def _hosted(asker: str, key: str) -> Session | None:
    """The ACTIVE group `key` among those the asker hosts — the ownership gate
    for every group-scoped verb (see the module docstring for why membership
    alone is not enough)."""
    return next((s for s in hosted_by(asker)
                 if s.key == key and s.status is GroupStatus.ACTIVE), None)


def _not_yours(asker: str, key: str) -> str:
    """One wording for every ownership refusal, listing what WOULD work."""
    yours = ", ".join(s.key for s in hosted_by(asker)) or "(none)"
    return f"no active group '{key}' hosted by you. Yours: {yours}"


def _refuse(asker: str, verb: str, reason: str) -> str:
    """Answer a manager's malformed or unactionable request with the reason.

    Managers get told; non-managers do not (their requests are parked unread —
    the plan's rule, and answering would invite probing). The reply is the same
    pointer mechanism as success, so the manager's next read explains itself."""
    _answer(asker, verb, f"Refused: {reason}")
    return f"refused '{verb}': {reason}"


def _answer(asker: str, verb: str, text: str) -> None:
    """Write the reply file and inject the pointer at it.

    Numbered from what is on disk (like mailbox.next_seq) so no answer ever
    overwrites an unread one. Injection failure is tolerable here: the file is
    durable, and the manager's addendum names the replies dir, so a wedged
    injection costs discovery time rather than the answer."""
    replies = cowork_dir_path(asker) / CONTROL_SUBDIR / REPLIES_SUBDIR
    sequence = sum(1 for _ in iter_files(replies, suffix=".md")) + 1
    name = f"{sequence:03d}-{verb}.md"
    write_text(replies / name, text.rstrip() + "\n")
    docker_attach_inject(
        asker, f"The hub answered your '{verb}' request — read "
               f"{COWORK_IN_CONTAINER / CONTROL_SUBDIR / REPLIES_SUBDIR / name} "
               f"before deciding your next step.")


def _reject(asker: str, request: Path) -> None:
    """Park a request that will not be honoured where it cannot be re-read but
    stays inspectable — same default as mailbox's capture rejection."""
    destination = (cowork_dir_path(asker) / CONTROL_SUBDIR / REJECTED_SUBDIR
                   / request.name)
    ensure_dir(destination.parent)
    move_path(request, destination)


def _instances_with_control() -> tuple[str, ...]:
    """Every instance with a control dir on disk, sorted — mirrors
    mailbox.instances_with_outbox, and for the same reason: requests must be
    consumed (or parked) even when their group has closed, or they accumulate."""
    return tuple(sorted(d.name for d in iter_subdirs(group_hosting_dir())
                        if is_dir(d / CONTROL_SUBDIR)))
