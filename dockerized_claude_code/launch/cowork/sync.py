"""The file plane for `{cowork}`: moving work between participants' trees.

Messages travel as prompts (see `mailbox`); files travel as copies. One rule
covers every participant, whatever its role:

    `<group>/` is yours to write. `<group>@<someone>/` is what someone sent
    you — an inbox, written only by the hub.

So both directions are the same operation, and `_deliver` is the only one there
is: copy the sender's working copy into the recipient's inbox-from-sender.
`hand_over` and `submit` just name the two directions.

Nothing the hub copies ever lands on a dir its owner writes, which is what makes
the handover non-destructive in both directions: a coworker's unsubmitted edit
survives a fresh hand-over, and a manager's canonical copy survives a submission.
The recipient decides what to merge, by diffing its inbox against its own copy —
see `review_command`, and note a plain `diff -r` is the wrong command.

The cost of that safety is staleness rather than loss: a recipient that ignores
an inbox works on old material. That is recoverable and visible, but only partly
detectable — `Delivery.changed` announces what moved when it moves, which is the
signal to lean on, and `not_taken_up` catches the unambiguous residue afterwards.

Inbox retention is the recipient's call: the hub neither versions nor deletes an
inbox, because different tasks want different handling. (A stale inbox is
indistinguishable from a fresh one on the next round, which is why both addendums
tell an agent to clear one once merged.)

Not covered, by design: two participants editing one file at the same time. That
is for the manager to sequence, not for a copy rule to arbitrate.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from ..file_access import copy_file, files_differ, is_file, iter_tree_files
from ..paths import (
    COWORK_IN_CONTAINER, cowork_group_path, cowork_inbox_path,
    group_conversation_path, group_hosting_dir, group_session_path,
)
from .group import Session
from .mailbox import MESSAGES_SUBDIR

# Entries at the ROOT of a group dir that belong to the hub rather than to the
# participant: never sent, never received. Derived from the builders that create
# them rather than re-spelled here, so renaming one of those files cannot quietly
# start copying it between trees.
# Root-level only, on purpose — a participant's own `messages/` subdirectory
# nested inside its work is work product, and dropping it would destroy output.
HUB_OWNED = frozenset({
    group_session_path(Path()).name,        # session.json — the group's canonical state
    group_conversation_path(Path()).name,   # conversation.md — the log, manager-side
    MESSAGES_SUBDIR,                        # staged message bodies the hub wrote
})


@dataclass(frozen=True)
class Delivery:
    """One transfer of files from one participant to another.

    Carries the two paths a review needs — `inbox`, where the files landed, and
    `working`, the recipient's own copy to compare against — so `review_command`
    is a pure function of a Delivery and needs no session threaded back in.

    `changed` is the subset that differs from the recipient's working copy, and it
    is the field worth quoting in a notification: naming twenty files when one
    moved buries the news. It reads meaningfully in both directions — downstream
    it is what the coworker altered, upstream it is what the manager altered since
    the coworker last looked. An EMPTY `changed` alongside a non-empty `files` is
    itself worth reporting: the sender's turn produced no file work at all, which
    is a likely-failed handover rather than a quiet success."""
    working: Path
    inbox: Path
    files: tuple[Path, ...]
    changed: tuple[Path, ...]


def work_files(group_dir: Path) -> tuple[Path, ...]:
    """Every participant-owned file in `group_dir`, as paths relative to it.

    Work product is the default and hub bookkeeping is the exception, so this
    subtracts HUB_OWNED rather than matching known-good names — a participant
    creating a file type nobody anticipated still gets its work moved."""
    return tuple(f for f in iter_tree_files(group_dir) if f.parts[0] not in HUB_OWNED)


def hand_over(session: Session, coworker: str) -> Delivery:
    """Send the manager's working copy to `coworker`'s inbox.

    Deliberately NOT written into the coworker's own `<group>/` dir: that would
    overwrite whatever it has in progress, and an edit it has not submitted yet
    would be gone with no way to notice. Landing in an inbox instead costs the
    coworker one merge step and buys it a diff of what actually moved upstream.

    The whole group dir is the material — a manager scopes a handover by what it
    puts in there, not by the hub filtering on its behalf."""
    return _deliver(session, sender=session.manager, recipient=coworker)


def submit(session: Session, coworker: str) -> Delivery:
    """Send `coworker`'s working copy to the manager's inbox for it.

    Two coworkers land in two separate inboxes rather than clobbering each other,
    and the canonical copy only ever changes when the manager changes it — the
    merge decision stays with a reasoning agent rather than a copy rule."""
    return _deliver(session, sender=coworker, recipient=session.manager)


def not_taken_up(session: Session, *, recipient: str, sender: str) -> tuple[Path, ...]:
    """Files in `recipient`'s inbox-from-`sender` that its working copy does not
    have AT ALL — material it was sent and never picked up.

    Deliberately absence, not difference. The moment a recipient takes a file up
    and works on it, its copy differs from the inbox by definition — so a
    difference check fires on every healthy round and cannot be told apart from
    "ignored the update". Separating those two needs a merge base, which is
    version control, and is well outside what a copy plane should grow into.

    Absence is the part that IS unambiguous: nobody has taken up a file they do
    not have. That catches the case worth catching — material sent and never
    looked at — without crying wolf on work in progress.

    Note the prevention signal is the stronger one: `Delivery.changed` says
    exactly what moved at the moment it moved, in both directions. Prefer telling
    a recipient that on delivery over auditing it afterwards.

    Both names are explicit for the same reason `_deliver`'s are: a manager holds
    one inbox PER coworker, so there is no single "the manager's inbox" to infer,
    and inferring the wrong one would answer a question nobody asked."""
    inbox = cowork_inbox_path(recipient, session.key, sender)
    working = cowork_group_path(recipient, session.key)
    return tuple(f for f in work_files(inbox) if not is_file(working / f))


def review_command(delivery: Delivery) -> str:
    """The exact `diff` invocation the recipient should run to review `delivery`.

    Lives here rather than in whatever composes the notification because the
    exclusions have to match HUB_OWNED exactly, and this module owns that set. A
    plain `diff -r` is NOT good enough: a working copy legitimately holds
    `session.json`, `conversation.md` and `messages/`, none of which are ever
    sent, so every round would report phantom "Only in" entries and bury the one
    real change among them.

    Emits CONTAINER paths, not the host paths a Delivery carries: the recipient is
    an agent inside a container, where the host paths this module copies between
    do not exist at all."""
    excluded = " ".join(f"-x {name}" for name in sorted(HUB_OWNED))
    working = shlex.quote(str(_in_container(delivery.working)))
    inbox = shlex.quote(str(_in_container(delivery.inbox)))
    return f"diff -r {excluded} {working} {inbox}"


def _in_container(path: Path) -> Path:
    """Where the owning participant sees one of its own group-hosting dirs.

    Every participant has `group_hosting/<its-id>/` bind-mounted at
    COWORK_IN_CONTAINER, and both a working copy and an inbox are DIRECT children
    of that dir — so swapping the host prefix for the mount point is exact rather
    than a guess, and needs only the final path component."""
    return COWORK_IN_CONTAINER / path.name


def _deliver(session: Session, *, sender: str, recipient: str) -> Delivery:
    """Copy `sender`'s working copy into `recipient`'s inbox-from-sender.

    The one transfer primitive: `hand_over` and `submit` differ only in which way
    round they pass these two names, which is the whole point of giving every
    participant the same two directory shapes.

    Keyword-only because `(session, coworker, manager)` and
    `(session, manager, coworker)` are both plausible-looking calls that would
    silently move files the wrong way.

    A full snapshot rather than only the changed files, deliberately: it lets one
    recursive diff show real differences and nothing else, which is the entire
    point of the review model.

    Both ends must be MEMBERS of the group. Raising here rather than reporting is
    the right shape for this layer: `relay.membership_problem` is what a caller
    consults to give a person a readable refusal, so reaching this guard means a
    caller skipped that and is about to copy one participant's files into an
    uninvolved instance's mounted dir, where it could read them."""
    for party in (sender, recipient):
        if party not in session.participants:
            raise ValueError(f"'{party}' is not in group '{session.key}', so no "
                             f"files may be copied to or from it")
    source = cowork_group_path(sender, session.key)
    working = cowork_group_path(recipient, session.key)
    inbox = cowork_inbox_path(recipient, session.key, sender)
    files = _copy_tree(source, inbox)
    changed = tuple(f for f in files if files_differ(source / f, working / f))
    return Delivery(working=working, inbox=inbox, files=files, changed=changed)


def _copy_tree(source: Path, destination: Path) -> tuple[Path, ...]:
    """Copy every work file from one group dir into another; return what was
    copied, relative.

    The single choke point for both confinement checks — every transfer in this
    module goes through here, so guarding both ends in one place is what makes
    the invariant hold rather than several call sites remembering to.

    `overwrite_if_changed` skips files whose bytes already match, so a round that
    moved one file out of twenty leaves the other nineteen's mtimes alone — a
    recipient sorting an inbox by mtime sees the work, not the transfer. A missing
    `source` copies nothing rather than raising: a participant with no group dir
    yet has simply sent nothing."""
    source, destination = _confined(source), _confined(destination)
    files = work_files(source)
    for relative in files:
        copy_file(source / relative, destination / relative, overwrite_if_changed=True)
    return files


def _confined(path: Path) -> Path:
    """`path`, confirmed to sit inside the group-hosting tree.

    Every path this module touches is composed from an instance id, and ids reach
    the hub from a manager's own request — one holding `..` or a path separator
    would otherwise let a transfer write anywhere the launcher can write, or pull
    a file from outside the tree into a participant's dir. Checked on both ends
    for that second reason: confining only the destination still leaves an
    exfiltration path.

    Raises rather than skipping quietly, because a path outside the tree means the
    caller's inputs are wrong, and copying nothing would present that as a
    successful round."""
    if group_hosting_dir().resolve() not in path.resolve().parents:
        raise ValueError("refusing to touch a path outside the group-hosting "
                         f"tree: {path}")
    return path
