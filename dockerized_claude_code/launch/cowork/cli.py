"""`cowork`'s command line — an argparse front end so `cowork -h` prints THIS
tool's help rather than claude's.

Six subcommands, each the only entry point to one operation:

    roster    who could be recruited
    recruit   create or extend a group
    send      deliver a message (and optionally the files) to a participant
    status    what the hub and every group are doing
    serve     run the hub loop
    close     end a group

The operator is a human here, so nothing is tag-gated. That gating belongs to the
agent-facing control channel — a `{manager}`'s own `roster` / `recruit` / `done`
requests must be honoured only from a manager, whereas a person at a terminal is
the one deciding in the first place.

Every subcommand prints a human-readable result and returns an exit code; none of
them raise on ordinary refusals (an unknown group, a closed group, a stopped
recipient), because those are answers, not faults.
"""

from __future__ import annotations

import argparse

from ..paths import AGENTS_DIR, cowork_inbox_path
from ..tags import scan_all
from . import control, lifecycle, relay, roster, sync
from .group import (
    GroupStatus, Session, create_session, discover_sessions, save_session,
)

EXIT_OK = 0
EXIT_REFUSED = 1        # the command was understood and declined — not a crash


def build_parser() -> argparse.ArgumentParser:
    """The whole CLI surface. Subparsers rather than flags because the verbs take
    genuinely different arguments, and `cowork send -h` should describe sending."""
    parser = argparse.ArgumentParser(
        prog="cowork", description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    listing = subs.add_parser("roster", help="list instances that could be recruited")
    listing.add_argument("--as", dest="asker", metavar="INSTANCE",
                         help="the asking manager, excluded from its own roster")
    listing.add_argument("--reachable", action="store_true",
                         help="only peers the hub could wake right now")

    hire = subs.add_parser("recruit", help="create or extend a group")
    hire.add_argument("manager", help="the instance hosting the group")
    hire.add_argument("project", help="space-free label; with the manager it names the group")
    hire.add_argument("coworker", nargs="*", help="instances to add (repeatable, idempotent)")
    hire.add_argument("--task", default="", help="what the group is for")
    hire.add_argument("--budget", type=int, default=6, metavar="N",
                      help="hub-to-coworker sends allowed before the group closes (default 6)")

    post = subs.add_parser("send", help="deliver a message to a participant")
    post.add_argument("group", help="group key, as shown by `status`")
    post.add_argument("recipient", help="the instance to deliver to and wake")
    post.add_argument("message", nargs="+", help="the message body (quote it)")
    post.add_argument("--from", dest="sender", metavar="INSTANCE",
                      help="who it is from (default: the group's manager)")
    post.add_argument("--with-files", action="store_true",
                      help="also copy the sender's working copy into the recipient's inbox")

    report = subs.add_parser("status", help="what the hub and every group are doing")
    report.add_argument("group", nargs="?", help="limit to one group")

    loop = subs.add_parser("serve", help="run the hub loop")
    loop.add_argument("--interval", type=float, default=relay.POLL_INTERVAL,
                      metavar="SECONDS", help="seconds between passes")
    loop.add_argument("--once", action="store_true",
                      help="make a single pass and exit (drains a backlog)")

    ending = subs.add_parser("close", help="end a group")
    ending.add_argument("group", help="group key, as shown by `status`")
    return parser


def main(argv: list[str]) -> int:
    """Parse argv and dispatch. Returns an exit code rather than calling sys.exit,
    so the entry script owns the process and tests can call this directly."""
    args = build_parser().parse_args(argv)
    handlers = {"roster": _roster, "recruit": _recruit, "send": _send,
                "status": _status, "serve": _serve, "close": _close}
    return handlers[args.command](args)


def _roster(args: argparse.Namespace) -> int:
    """Print who could be recruited."""
    survey = roster.survey(args.asker, scan_all(AGENTS_DIR))
    if args.reachable:
        survey = roster.Roster(candidates=roster.reachable(survey),
                               needs_relaunch=survey.needs_relaunch,
                               liveness_known=survey.liveness_known)
    print(roster.describe(survey))
    return EXIT_OK


def _recruit(args: argparse.Namespace) -> int:
    """Create the group if new, then add each coworker.

    Idempotent throughout: `create_session` returns an existing group untouched
    rather than resetting its round count, and `with_coworker` ignores a repeat. So
    re-running a recruit to add one more peer is safe, which is the common case."""
    session = create_session(args.manager, args.project, args.task,
                             round_budget=args.budget)
    for coworker in args.coworker:
        session = session.with_coworker(coworker)
    session = save_session(session)
    print(f"  Group '{session.key}' — {len(session.coworkers)} coworker(s), "
          f"{session.rounds_left} of {session.round_budget} round(s) left")
    for coworker in session.coworkers:
        print(f"    {coworker}")
    if not session.coworkers:
        print("    (none yet — pass coworker ids to add them)")
    return EXIT_OK


def _send(args: argparse.Namespace) -> int:
    """Deliver a message, optionally pushing files with it.

    Files first when asked, so the recipient's inbox is already in place when the
    message that refers to it arrives."""
    session = _resolve(args.group)
    if session is None:
        return EXIT_REFUSED
    sender = args.sender or session.manager
    # Checked BEFORE any file moves: `--with-files` copies the manager's working
    # copy into the recipient's readable mount, which refusing the message later
    # does not take back.
    problem = relay.membership_problem(session, sender=sender,
                                       recipient=args.recipient)
    if problem is not None:
        print(f"  Refusing: {problem}")
        return EXIT_REFUSED
    if args.with_files:
        if sender != session.manager:
            print(f"  Refusing: --with-files pushes the MANAGER's working copy, "
                  f"but --from is '{sender}'. Let the hub submit a coworker's files "
                  f"when its turn ends instead.")
            return EXIT_REFUSED
        delivery = sync.hand_over(session, args.recipient)
        print(f"  {len(delivery.files)} file(s) into {delivery.inbox.name}"
              f"{f', {len(delivery.changed)} changed' if delivery.changed else ''}")

    outcome = relay.send(session, sender=sender, recipient=args.recipient,
                         body=" ".join(args.message))
    if not outcome.delivered:
        print(f"  Not delivered: {outcome.reason}")
        return EXIT_REFUSED
    print(f"  Delivered to {args.recipient} — "
          f"{outcome.session.rounds_left} of {outcome.session.round_budget} round(s) left")
    return EXIT_OK


def _status(args: argparse.Namespace) -> int:
    """Report the hub, then each group. Prints the awkward states outright — no
    hub running, a group with no coworkers, material never taken up — since those
    are what a person runs `status` to find out."""
    holder = lifecycle.owner()
    print(f"  hub: running (pid {holder.pid})" if holder
          else f"  hub: not running — start it with `cowork serve` "
               f"(pidfile: {lifecycle.pid_file()})")

    for instance, group, waited in relay.overdue_sends():
        print(f"  ! {instance} has not answered {group} for "
              f"{int(waited // 60)} min — the injection may not have landed")

    sessions = [s for s in discover_sessions() if args.group in (None, s.key)]
    if not sessions:
        print(f"  no group '{args.group}'" if args.group else "  no groups yet")
        return EXIT_OK
    for session in sessions:
        _print_group(session)
    return EXIT_OK


def _serve(args: argparse.Namespace) -> int:
    """Run the hub loop under the singleton guard, polling both event sources:
    the outboxes (replies) and the control channel (managers' requests). The
    registry is scanned once — the control gate needs it on every pass, and the
    tag tree does not change mid-run.

    The guard is released in a `finally` so an interrupted hub does not leave a
    pidfile that blocks the next one — the stale-file path exists for crashes, not
    for ordinary Ctrl-C."""
    claimed = lifecycle.claim()
    if claimed is None:
        existing = lifecycle.owner()
        print(f"  A hub is already running (pid {existing.pid if existing else '?'}). "
              f"Two would each drain half the captures, so this one is exiting.")
        return EXIT_REFUSED
    registry = scan_all(AGENTS_DIR)
    print(f"  hub serving (pid {claimed.pid}); Ctrl-C to stop")
    try:
        relay.serve(interval=args.interval, passes=1 if args.once else None,
                    also_poll=lambda: control.poll_control(registry))
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        lifecycle.release(claimed)
    return EXIT_OK


def _close(args: argparse.Namespace) -> int:
    """End a group. Its directories and log stay on disk — the work and the
    discussion outlive the routing."""
    session = _resolve(args.group, require_active=False)
    if session is None:
        return EXIT_REFUSED
    if session.status is GroupStatus.CLOSED:
        print(f"  '{session.key}' was already closed")
        return EXIT_OK
    save_session(session.closed())
    print(f"  '{session.key}' closed after {session.rounds_used} round(s); "
          f"its files and conversation.md are kept")
    return EXIT_OK


def _resolve(key: str, *, require_active: bool = True) -> Session | None:
    """The session named by `key`, or None having explained why not.

    Searched by scanning rather than composed from the key, because a key does not
    say who hosts it — `<manager>-<project>` cannot be split back apart when either
    half may contain the separator."""
    for session in discover_sessions():
        if session.key != key:
            continue
        if require_active and session.status is not GroupStatus.ACTIVE:
            print(f"  '{key}' is closed — reopen it by recruiting again, or pick another")
            return None
        return session
    print(f"  No group '{key}'. `cowork status` lists them.")
    return None


def _print_group(session: Session) -> None:
    """One group's block: who, how many rounds are left, and what is waiting."""
    print(f"\n  {session.key}  [{session.status.value}]")
    print(f"    manager   {session.manager}")
    print(f"    task      {session.task or '(none recorded)'}")
    print(f"    rounds    {session.rounds_used} used, {session.rounds_left} left "
          f"of {session.round_budget}")
    if not session.coworkers:
        print("    coworkers (none — recruit some before sending)")
        return
    for coworker in session.coworkers:
        outstanding = sync.not_taken_up(session, recipient=coworker,
                                       sender=session.manager)
        submitted = sync.work_files(
            cowork_inbox_path(session.manager, session.key, coworker))
        notes = []
        if outstanding:
            notes.append(f"{len(outstanding)} file(s) sent but never picked up")
        if submitted:
            notes.append(f"{len(submitted)} file(s) waiting in your inbox")
        print(f"    coworker  {coworker}"
              f"{'  — ' + '; '.join(notes) if notes else ''}")
