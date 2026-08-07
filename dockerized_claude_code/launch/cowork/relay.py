"""The hub loop for `{cowork}`: deliver, drain, attribute, route, log.

This module is orchestration only. Every mechanism it uses belongs to a peer:
`mailbox` stages messages and attributes replies, `sync` moves files, `journal`
records, `group` holds durable state, and `docker_config` owns the one docker
touchpoint (injection). What lives here is the *policy* — who gets told what, in
what order, and when to stop.

**The hub advances through transcripts, not through the outbox.** A capture is a
doorbell plus a pointer: "a turn finished, here is where to look". Its position in
the outbox carries no meaning, because a human-typed turn drops a capture the hub
never asked for and would shift everything after it. Attribution is an exact join
on `prompt_id`, so every capture resolves independently — see `mailbox.attribute`.

**A manager's reply is never auto-forwarded.** The hub injects notifications INTO
a manager, so forwarding its replies onward would close a loop: notify → reply →
notify. A manager's attributed turn is logged to the group's conversation and
stops there; sending work onward is an explicit act, driven by the human or by a
control request, never inferred from the fact that it said something.

**Rounds are counted on hub-to-coworker sends only.** That is the runaway guard: a
notification back to the manager is bookkeeping, not a round, so a chatty exchange
cannot be inflated by traffic the manager did not ask for.

**The hub only carries traffic between members of a group.** Both ends of every
send are checked against `Session.participants`, here rather than at a caller,
because the CLI is not the only caller a hub will have and a guard a caller can
skip is not a guard. Without it a typo'd recipient wakes an uninvolved instance,
stages a message into a tree that has no business holding one, records the traffic
as group history, and burns a round — all reported as success.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from ..docker_config import docker_attach_inject
from . import journal, mailbox, sync
from .group import (
    GroupStatus, HubState, ParticipantState, Session, discover_sessions,
    load_hub_state, save_session, update_participant,
)

POLL_INTERVAL = 2.0          # seconds between passes; a turn takes far longer than this
UNDELIVERED_AFTER = 900.0    # seconds before an unanswered send is called out (15 min)


class EventKind(Enum):
    """What one pass concluded about one input — a Stop-hook capture, or a
    control request (`cowork.control` reports through the same vocabulary so the
    operator reads one stream). Every input produces exactly one event, so a
    pass's events are a complete account of what it saw."""
    REPLY = "reply"                  # attributed and routed
    LOGGED = "logged"                # attributed to a manager's own turn — logged, not routed
    UNSOLICITED = "unsolicited"      # untagged prompt: a human typed it, so it is not ours
    UNKNOWN_GROUP = "unknown-group"  # tagged for a group that is not active on disk
    DUPLICATE = "duplicate"          # already handled before a crash lost the deletion
    CONTROL = "control"              # a manager's control request — honoured, or answered with an error
    DENIED = "denied"                # a control request from a non-manager — ignored, per the design


@dataclass(frozen=True)
class Event:
    """One capture's outcome, for the operator view and for tests. `group` is ""
    when the capture could not be attributed — that is the whole point of the
    UNSOLICITED kind, so it cannot carry one."""
    kind: EventKind
    instance: str
    group: str
    detail: str


@dataclass(frozen=True)
class Sent:
    """The result of one delivery attempt.

    Carries `session` because a send can change it — a round consumed, or the group
    closed on exhausting the budget — and the caller needs the version that was
    persisted rather than the one it passed in. `reason` is "" exactly when
    `delivered`, so a caller can report the failure without re-deriving it."""
    delivered: bool
    session: Session
    reason: str


def send(session: Session, *, sender: str, recipient: str, body: str,
         journal_as: str | None = None) -> Sent:
    """Deliver `body` to `recipient` and wake it. Consumes a round when the
    recipient is a coworker.

    Order matters and is not arbitrary: stage the file, inject, and only then
    record the send as outstanding. An injection that never landed must look
    undelivered to the next pass so it can be retried — recording first would
    leave the hub waiting forever for a reply to a prompt nobody received.

    `journal_as` records something shorter than `body` in the conversation. It
    exists for bodies DERIVED from an entry already logged: a notification quotes
    the reply logged immediately above it, and writing both puts the same text in
    the log twice — which a human re-reads and a relaunched participant pays for
    in context, since `journal.read_journal` is how it recovers the thread."""
    problem = membership_problem(session, sender=sender, recipient=recipient)
    if problem is not None:
        return Sent(False, session, problem)
    if session.status is not GroupStatus.ACTIVE:
        return Sent(False, session, f"group '{session.key}' is closed")

    is_round = recipient != session.manager
    if is_round and session.rounds_left == 0:
        closed = save_session(session.closed())
        journal.append(closed, journal.Direction.NOTE, recipient,
                       f"round budget of {closed.round_budget} exhausted — group closed "
                       f"without delivering this message")
        return Sent(False, closed, f"round budget of {closed.round_budget} exhausted")

    staged = mailbox.stage_message(recipient, session.key, sender, body,
                                   seq=mailbox.next_seq(recipient, session.key))
    prompt = mailbox.pointer_prompt(session.key, sender, staged)
    if not docker_attach_inject(recipient, prompt):
        journal.append(session, journal.Direction.NOTE, recipient,
                       f"message staged at {staged} but injection failed — "
                       f"the recipient has not been told")
        return Sent(False, session, f"could not inject into '{recipient}'")

    delivered = save_session(session.with_round_used()) if is_round else session
    journal.append(delivered, journal.Direction.TO, recipient, journal_as or body)
    _mark_outstanding(recipient, delivered.key)
    return Sent(True, delivered, "")


def membership_problem(session: Session, *, sender: str,
                       recipient: str) -> str | None:
    """Why this pair cannot exchange a message in this group, or None if they can.

    Separate from `send` because a caller sometimes has to know BEFORE it acts:
    the CLI moves files on `--with-files` before the message goes, and copying a
    manager's working copy into an uninvolved instance's `/cowork` mount — where
    that instance can read it — is not undone by refusing the message afterwards.

    Names every outsider at once, so a caller fixing a two-ended mistake does not
    need two attempts to find out."""
    outsiders = [p for p in (sender, recipient) if p not in session.participants]
    if not outsiders:
        return None
    return (f"{' and '.join(repr(o) for o in outsiders)} not in "
            f"'{session.key}' — recruit first")


def poll_once() -> tuple[Event, ...]:
    """One pass: read every participant's outbox, resolve each capture, act.

    Sessions are re-discovered every pass rather than cached, so a group created
    or closed while the hub runs is picked up without a restart — discovery is a
    directory scan, which is cheap next to a poll interval."""
    active = {s.key: s for s in discover_sessions() if s.status is GroupStatus.ACTIVE}
    events: list[Event] = []
    for instance in mailbox.instances_with_outbox():
        for capture in mailbox.read_captures(instance):
            events.append(_handle(capture, active))
            mailbox.consume(capture)
    return tuple(events)


def serve(interval: float = POLL_INTERVAL, *, report: bool = True,
          passes: int | None = None,
          also_poll: Callable[[], tuple[Event, ...]] | None = None) -> None:
    """Poll until interrupted. `passes` bounds the loop for a caller that wants a
    finite run (tests, a one-shot drain); None means forever.

    `also_poll` is a second per-pass event source — the control channel. A
    callback rather than an import, because the dependency runs the other way:
    `cowork.control` calls `send` here, so relay importing it back would be a
    cycle. The caller that knows about both (the CLI) wires them together.

    Deliberately thin — every decision lives in `poll_once` and the callback, so
    the loop itself has nothing to get wrong."""
    remaining = passes
    while remaining is None or remaining > 0:
        events = poll_once() + (also_poll() if also_poll else ())
        for event in events:
            if report:
                print(f"  [{event.kind.value}] {event.instance}"
                      f"{' ' + event.group if event.group else ''}: {event.detail}")
        if remaining is not None:
            remaining -= 1
            if remaining == 0:
                return
        time.sleep(interval)


def overdue_sends(state: HubState | None = None, *, now: float | None = None,
                  ) -> tuple[tuple[str, str, float], ...]:
    """`(instance, group, waited_seconds)` for each send still unanswered past
    UNDELIVERED_AFTER — unpack as `for instance, group, waited in ...`.

    An injection is confirmed by the reply that follows it, never by the attach
    returning — the prompt could have landed in a session that then hung. So a send
    with no reply after long enough is the only signal that something is wrong, and
    reporting it is all the hub should do: retrying blind risks asking twice, which
    is worse than one stalled ask a human can see."""
    hub = load_hub_state() if state is None else state
    moment = time.time() if now is None else now
    return tuple(
        (instance, pending.outstanding_send, moment - pending.sent_at)
        for instance, pending in sorted(hub.participants.items())
        if pending.outstanding_send and pending.sent_at is not None
        and moment - pending.sent_at > UNDELIVERED_AFTER
    )


def _handle(capture: mailbox.Capture, active: dict[str, Session]) -> Event:
    """Resolve one capture and act on it. Returns without touching the capture
    file — `poll_once` consumes it, so a crash in here retries rather than
    silently dropping a turn."""
    state = load_hub_state()
    known = state.for_participant(capture.instance)
    if capture.prompt_id is not None and capture.prompt_id == known.last_prompt_id:
        return Event(EventKind.DUPLICATE, capture.instance, "",
                     "already handled on an earlier pass")

    group = mailbox.attribute(capture)
    if group is None:
        return Event(EventKind.UNSOLICITED, capture.instance, "",
                     "a turn the hub did not prompt — not routed")
    session = active.get(group)
    if session is None:
        return Event(EventKind.UNKNOWN_GROUP, capture.instance, group,
                     "tagged for a group that is not active — not routed")

    journal.append(session, journal.Direction.FROM, capture.instance, capture.answer)
    _clear_outstanding(capture.instance, capture.prompt_id)
    if capture.instance == session.manager:
        return Event(EventKind.LOGGED, capture.instance, group,
                     "the manager's own turn — logged, never forwarded")
    return _forward_to_manager(session, capture, group)


def _forward_to_manager(session: Session, capture: mailbox.Capture,
                        group: str) -> Event:
    """Take a coworker's files, then tell the manager both things at once.

    Files first: the notification quotes the inbox, so the inbox has to exist
    before the manager can be pointed at it."""
    submission = sync.submit(session, capture.instance)
    changed = len(submission.changed)
    outcome = send(session, sender=capture.instance, recipient=session.manager,
                   body=_notification(capture, submission),
                   journal_as=f"notified about {capture.instance}'s reply above "
                              f"({changed} file(s) differ from the working copy)")
    detail = f"{changed} file(s) changed"
    if not outcome.delivered:
        detail += f"; manager NOT notified ({outcome.reason})"
    return Event(EventKind.REPLY, capture.instance, group, detail)


def _notification(capture: mailbox.Capture, submission: sync.Delivery) -> str:
    """What the manager is told when a coworker replies: the reply itself, then
    the files, then the one command that shows what moved.

    The file section is omitted entirely when nothing was submitted, rather than
    saying "0 files" — a manager reading "no files" still has to work out whether
    that matters, while an absent section reads as a plain message."""
    parts = [capture.answer.strip()]
    if submission.files:
        changed = ("\n".join(f"  - {path}" for path in submission.changed)
                   if submission.changed else "  (none differ from your copy)")
        parts.append(f"## Files submitted by {capture.instance}\n\n"
                     f"{len(submission.files)} file(s) are in the inbox. "
                     f"Differing from your working copy:\n{changed}\n\n"
                     f"Review with:\n\n    {sync.review_command(submission)}\n\n"
                     f"Clear the inbox once you have merged what you want — a stale "
                     f"inbox is indistinguishable from a fresh one next round.")
    return "\n\n".join(parts)


def _mark_outstanding(recipient: str, group: str) -> None:
    """Record that `recipient` owes a reply for `group`.

    Carries `last_prompt_id` forward rather than resetting it: that field is the
    duplicate guard for REPLIES, and a fresh send says nothing about which turn
    was last processed."""
    prior = load_hub_state().for_participant(recipient).last_prompt_id
    update_participant(recipient, ParticipantState(
        last_prompt_id=prior, outstanding_send=group, sent_at=time.time()))


def _clear_outstanding(instance: str, prompt_id: str | None) -> None:
    """Note the reply arrived, and remember which turn it answered.

    `last_prompt_id` guards the one duplicate a crash can produce: a capture
    handled but not yet deleted is re-read on the next pass. It remembers a single
    turn, so two captures handled-and-not-deleted would still see the older one
    reprocessed — acceptable, because reprocessing costs a repeated journal entry
    and an idempotent re-copy, not lost work."""
    update_participant(instance, ParticipantState(
        last_prompt_id=prompt_id, outstanding_send=None, sent_at=None))
