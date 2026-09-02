"""The `cluster-chat` command members call — the argparse front end over
queue.py and gates.py. `post` speaks the member vocabulary (free / nop /
stance / hold; gate replies route through the gate machinery for caps and
the completion ping); `open` / `close` / `check-gate` are the gate verbs —
check-gate doubling as what the detached timers re-run.

Identity comes from `$CLUSTER_MEMBER` (every member carries it — the
`{clstr}` addendum's first bullet), never from a flag: a member cannot
misattribute a post by typo. `$CLUSTER_CHAT_READONLY` (any non-empty value)
is the queue-side mute for listen-only members — the guardrail the messaging
feature's `deny` cannot provide.

Exit codes: 0 ok; 2 for anything refused (protocol violation, mute, missing
identity) — printed as one plain line, because the caller is an agent whose
context the message lands in.

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package. The in-container shim runs `python3 -m cluster_work_protocol.cli`
with /opt on PYTHONPATH.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .config import CONFIG_IN_CONTAINER, load_config
from .gates import (
    MEMBERS_DIR_IN_CONTAINER, check_gate, close_gate, mentions, open_gate,
    owed_gates, post_reply, roster,
)
from .queue import PROTOCOL_DIR_IN_CONTAINER, Queue
from .schema import (
    KIND_FREE, KIND_HOLD, KIND_NOP, KIND_STANCE, REPLY_KINDS, Message,
    ProtocolError,
)
from .wake import ping_members

POST_KINDS = (KIND_FREE, KIND_NOP, KIND_STANCE, KIND_HOLD)
MEMBER_ENV = "CLUSTER_MEMBER"
READONLY_ENV = "CLUSTER_CHAT_READONLY"


def _render(message: Message) -> str:
    """One human line per message — the FILE stays the JSON record; this is
    what lands in an agent's context, so it is compact and self-labelling."""
    stance = f"({message.stance})" if message.stance is not None else ""
    gate = f" [gate {message.iteration}]" if message.iteration else ""
    body = f": {message.body}" if message.body else ""
    return f"#{message.seq} {message.ts} {message.member} {message.kind}{stance}{gate}{body}"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cluster-chat",
        description="The cluster work-protocol's queue — ordered, broadcast, "
                    "and the team's own journal.")
    parser.add_argument("--root", type=Path, default=PROTOCOL_DIR_IN_CONTAINER,
                        help="protocol dir (default: the in-container one)")
    parser.add_argument("--config", type=Path, default=CONFIG_IN_CONTAINER,
                        help="protocol config (default: the mounted one)")
    parser.add_argument("--members-dir", type=Path,
                        default=MEMBERS_DIR_IN_CONTAINER,
                        help="the roster dir (default: the in-container one)")
    verbs = parser.add_subparsers(dest="verb", required=True)

    post = verbs.add_parser(
        "post", help="append one message as $CLUSTER_MEMBER")
    post.add_argument("kind", choices=POST_KINDS)
    post.add_argument("body", nargs="?", default="",
                      help="reasons / riders / the remark (a nop takes none)")
    post.add_argument("--gate", help="gate id — required for nop/stance/hold")
    post.add_argument("--stance", type=int,
                      help="0-10, stance posts only (see `cluster-chat scale`)")

    read = verbs.add_parser("read", help="catch up on the queue")
    group = read.add_mutually_exclusive_group()
    group.add_argument("--since", type=int, metavar="SEQ",
                       help="everything after SEQ (cursor untouched)")
    group.add_argument("--all", action="store_true",
                       help="the whole journal (cursor untouched)")
    group.add_argument("--peek", action="store_true",
                       help="what's new to you, WITHOUT advancing your cursor")
    # default (no flag): what's new to you, advancing your cursor.

    opener = verbs.add_parser(
        "open", help="open a gate: everyone must reply once")
    opener.add_argument("gate_id", help="unique id (letters/digits/-/_)")
    opener.add_argument("question", help="what the team is assessing")

    closer = verbs.add_parser(
        "close", help="close YOUR gate with its resolution (the stop rule)")
    closer.add_argument("gate_id")
    closer.add_argument("resolution")

    checker = verbs.add_parser(
        "check-gate", help="nudge/timeout pass — what the timers re-run")
    checker.add_argument("gate_id")

    verbs.add_parser("scale", help="print the 0-10 stance meanings")
    verbs.add_parser(
        "brief", help="{cc}'s prompt-time briefing: unread queue lines, gates "
                      "you owe a reply to, and the standing gate rule")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.verb == "post":
            return _post(args)
        if args.verb == "read":
            return _read(args)
        if args.verb == "open":
            return _open(args)
        if args.verb == "close":
            return _close(args)
        if args.verb == "check-gate":
            return _check(args)
        if args.verb == "brief":
            return _brief(args)
        return _scale(args)
    except ProtocolError as error:
        print(f"cluster-chat: {error}")
        return 2


def _member(args: argparse.Namespace) -> str:
    member = os.environ.get(MEMBER_ENV, "")
    if not member:
        raise ProtocolError(
            f"${MEMBER_ENV} is not set — cluster-chat only works inside a "
            f"cluster member's own session")
    return member


def _refuse_if_muted() -> None:
    if os.environ.get(READONLY_ENV):
        raise ProtocolError(
            f"this member is listen-only (${READONLY_ENV} is set) — reading "
            f"is fine, posting is not")


def _mention_pings(args: argparse.Namespace, author: str, body: str) -> list[str]:
    """@mention pings for any successful post — best-effort: outside a real
    cluster (no members dir) there is nobody to ping, not an error."""
    if not body or "@" not in body:
        return []
    try:
        members = roster(args.members_dir)
    except ProtocolError:
        return []
    named = mentions(body, members, author)
    if not named:
        return []
    return ping_members(
        named, f"{author} mentioned you on the queue — cluster-chat read --new")


def _post(args: argparse.Namespace) -> int:
    _refuse_if_muted()
    if args.body and args.kind == KIND_NOP:
        raise ProtocolError(
            "a nop takes no body — it is the fold, and it carries nothing")
    member = _member(args)
    queue = Queue(args.root, load_config(args.config).lock_wait_seconds)
    report: list[str] = []
    if args.kind in REPLY_KINDS:
        if not args.gate:
            raise ProtocolError(
                f"a {args.kind} is a gate reply — say which gate: --gate <id>")
        message, report = post_reply(
            queue, load_config(args.config), member=member, kind=args.kind,
            body=args.body, iteration=args.gate, stance=args.stance,
            members_dir=args.members_dir)
    else:
        message = queue.append(member, args.kind, args.body,
                               iteration=args.gate, stance=args.stance)
    print(f"posted {_render(message)}")
    for line in report + _mention_pings(args, member, args.body):
        print(line)
    return 0


def _open(args: argparse.Namespace) -> int:
    _refuse_if_muted()
    config = load_config(args.config)
    queue = Queue(args.root, config.lock_wait_seconds)
    for line in open_gate(queue, config, iteration=args.gate_id,
                          body=args.question, opener=_member(args),
                          members_dir=args.members_dir,
                          config_path=args.config):
        print(line)
    return 0


def _close(args: argparse.Namespace) -> int:
    _refuse_if_muted()
    queue = Queue(args.root, load_config(args.config).lock_wait_seconds)
    message = close_gate(queue, member=_member(args), iteration=args.gate_id,
                         resolution=args.resolution,
                         members_dir=args.members_dir)
    print(f"closed {_render(message)}")
    return 0


def _check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    queue = Queue(args.root, config.lock_wait_seconds)
    for line in check_gate(queue, config, iteration=args.gate_id,
                           members_dir=args.members_dir):
        print(line)
    return 0


def _read(args: argparse.Namespace) -> int:
    # Reads take no lock and need no config — they must work even when the
    # config mount is broken, or debugging would need the thing being
    # debugged.
    queue = Queue(args.root, lock_wait_seconds=1)
    if args.since is not None:
        messages = queue.read_since(args.since)
    elif args.all:
        messages = queue.read_all()
    else:
        messages = queue.read_new(_member(args), advance=not args.peek)
    if not messages:
        print("(nothing new)" if not args.all else "(empty queue)")
        return 0
    for message in messages:
        print(_render(message))
    return 0


STANDING_RULE = (
    "Reminder: a consequential commitment — the plan you are about to "
    "execute, architecture, dependencies, schema/API, a substantial rewrite, "
    "a roadmap change — needs a gate FIRST: "
    "`cluster-chat open <short-id> \"<the decision>\"`.")


def _brief(args: argparse.Namespace) -> int:
    """`{cc}`'s prompt-time briefing, run by that tag's UserPromptSubmit
    hook — printed straight into the member's context at the moment a task
    arrives, which is the one moment static CLAUDE.md text demonstrably
    failed to reach (plans/ISSUES.md, 2026-09-02).

    Three things, cheapest first: unread queue lines (this is also a SECOND
    delivery path — a member sees gate traffic here even if its pane wake
    was lost), the gates it still owes a reply to (repeated every prompt
    until answered — the nag IS the enforcement), and the standing rule.

    ALWAYS exits 0, whatever breaks: a UserPromptSubmit hook that fails
    non-zero BLOCKS the prompt, and no briefing is worth costing the
    operator a turn. Outside a cluster (no queue, no roster) it prints
    nothing at all."""
    try:
        member = os.environ.get(MEMBER_ENV, "")
        if not member:
            return 0
        queue = Queue(args.root, lock_wait_seconds=1)
        if not queue.chat_path.exists():
            print(f"[cluster-chat] {STANDING_RULE}")
            return 0
        fresh = queue.read_new(member)
        if fresh:
            print(f"[cluster-chat] {len(fresh)} new queue message(s):")
            for message in fresh:
                print(f"  {_render(message)}")
        owed = owed_gates(queue.read_all(), roster(args.members_dir), member)
        for gate in owed:
            print(f"[cluster-chat] YOU OWE A REPLY on gate {gate.iteration} "
                  f"({gate.body!r}) — answer it before continuing: "
                  f"`cluster-chat post nop --gate {gate.iteration}` (the "
                  f"fold), or `post stance <0-10> \"<reasons>\" --gate "
                  f"{gate.iteration}`, or `post hold \"<why>\" --gate "
                  f"{gate.iteration}`.")
        if not owed:
            print(f"[cluster-chat] {STANDING_RULE}")
    except Exception:      # noqa: BLE001 — a hook must never block a prompt
        pass
    return 0


def _scale(args: argparse.Namespace) -> int:
    for value, meaning in sorted(load_config(args.config).scale.items()):
        print(f"{value:>2} — {meaning}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
