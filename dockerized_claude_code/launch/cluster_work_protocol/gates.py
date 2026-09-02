"""Gates — the iteration machinery: open, reply accounting, nudge/timeout,
close. All STATE is derived from the journal (a gate is open iff its `open`
message has no `close`); nothing here keeps files of its own, so a crashed
fork or a re-run verb can never disagree with the record.

The liturgy this implements (plans/cluster_plan.md, iteration accounting +
PROPOSAL v2 + the wake decision):

- `open` appends the gate, pings every REQUIRED member (the roster minus the
  opener — the opener acts on the result, it doesn't owe itself a reply),
  and plants two detached one-shot timers that re-run `cluster-chat
  check-gate <id>` after `nudge_after_seconds` / `close_after_seconds`;
- a REPLY (nop / stance / hold) is guarded — gate open, reply cap, loop cap —
  and the reply that COMPLETES the gate pings the opener, detected inside
  the append's own lock so exactly one replier sees it;
- `check-gate` is one idempotent verb for both timers: before the close
  deadline it re-pings only the stragglers; after it, it records a TIMEOUT
  row per straggler (a nop is an assessment, a timeout is an absence) and
  pings the opener. It never authors the close: the RESOLUTION needs the
  opener's judgment, so closing stays the opener's act;
- `close` appends the resolution line (the protocol's stop rule) and only
  the opener may do it.

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import ProtocolConfig
from .queue import Queue
from .schema import (
    KIND_CLOSE, KIND_OPEN, KIND_TIMEOUT, REPLY_KINDS, Message, ProtocolError,
)
from .wake import ping_members

# Where the launcher materializes one config dir per member — the roster IS
# this directory listing (launching.prepare creates it; a drift-pin test ties
# the two spellings together).
MEMBERS_DIR_IN_CONTAINER = Path("/cluster/members")
_GATE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_MENTION_RE = re.compile(r"@([\w-]+)")


def roster(members_dir: Path) -> tuple[str, ...]:
    """Every member id, from the members dir. Loud when empty/absent — a
    gate with no roster can never complete, which would read as a hang."""
    if not members_dir.is_dir():
        raise ProtocolError(
            f"no members dir at {members_dir} — gates only work inside a "
            f"cluster container (the launcher creates it per member)")
    members = tuple(sorted(p.name for p in members_dir.iterdir() if p.is_dir()))
    if not members:
        raise ProtocolError(f"the members dir at {members_dir} is empty")
    return members


@dataclass(frozen=True)
class Gate:
    """One gate's state, derived from the journal. `required` excludes the
    opener; `replies` holds each required member's FIRST reply; `timeouts`
    are the members already recorded absent."""
    iteration: str
    opener: str
    body: str
    opened_ts: str
    required: tuple[str, ...]
    replies: dict[str, Message]
    timeouts: frozenset[str]
    closed: bool

    @property
    def stragglers(self) -> tuple[str, ...]:
        return tuple(member for member in self.required
                     if member not in self.replies
                     and member not in self.timeouts)

    @property
    def complete(self) -> bool:
        """Every required member accounted for — by reply or recorded
        timeout."""
        return not self.stragglers


def load_gate(journal: list[Message], iteration: str,
              members: tuple[str, ...]) -> Gate | None:
    """The gate as the journal tells it, or None when never opened."""
    opened = next((m for m in journal
                   if m.kind == KIND_OPEN and m.iteration == iteration), None)
    if opened is None:
        return None
    required = tuple(m for m in members if m != opened.member)
    replies: dict[str, Message] = {}
    timeouts: set[str] = set()
    closed = False
    for message in journal:
        if message.iteration != iteration:
            continue
        if message.kind in REPLY_KINDS and message.member in required:
            replies.setdefault(message.member, message)
        elif message.kind == KIND_TIMEOUT:
            timeouts.add(message.member)
        elif message.kind == KIND_CLOSE:
            closed = True
    return Gate(iteration=iteration, opener=opened.member, body=opened.body,
                opened_ts=opened.ts, required=required, replies=replies,
                timeouts=frozenset(timeouts), closed=closed)


def _check_gate_command(iteration: str, root: Path, config_path: Path,
                        members_dir: Path) -> str:
    """The exact re-invocation the timer forks run. EVERY path the opener
    used rides along — the fork inherits env, not argv, and a default-path
    fork against a custom-rooted gate dies silently (caught by the first
    host-side smoke: no --members-dir, no roster, no timeout rows)."""
    return shlex.join(["cluster-chat", "--root", str(root),
                       "--config", str(config_path),
                       "--members-dir", str(members_dir),
                       "check-gate", iteration])


def _spawn_timer(delay_seconds: int, command: str) -> None:
    """One detached fire-and-forget timer: sleeps, re-runs check-gate, dies.
    A detached session so it outlives the opener's CLI call; output dropped —
    check-gate's effects are queue rows and pings, not prose for a shell
    nobody is watching."""
    subprocess.Popen(["sh", "-c", f"sleep {delay_seconds}; exec {command}"],
                     start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def mentions(body: str, members: tuple[str, ...], author: str) -> tuple[str, ...]:
    """`@member` names in a body that name real, other members — the directed
    ping escape hatch any post may use."""
    named = set(_MENTION_RE.findall(body))
    return tuple(member for member in members
                 if member in named and member != author)


def open_iterations(journal: list[Message]) -> tuple[str, ...]:
    """Every gate id that was opened and not closed, in open order."""
    closed = {m.iteration for m in journal if m.kind == KIND_CLOSE}
    return tuple(m.iteration for m in journal
                 if m.kind == KIND_OPEN and m.iteration not in closed
                 and m.iteration is not None)


def owed_gates(journal: list[Message], members: tuple[str, ...],
               member: str) -> tuple[Gate, ...]:
    """The open gates `member` still owes a reply to — what `{cc}`'s
    prompt-time brief nags about. Derived every call (never cached): the
    nag must stop the moment the reply lands, and repeat until then."""
    out = []
    for iteration in open_iterations(journal):
        gate = load_gate(journal, iteration, members)
        if (gate is not None and member in gate.required
                and member not in gate.replies
                and member not in gate.timeouts):
            out.append(gate)
    return tuple(out)


def open_gate(queue: Queue, config: ProtocolConfig, *, iteration: str,
              body: str, opener: str, members_dir: Path,
              config_path: Path, timers: bool = True) -> list[str]:
    """Open a gate: append, ping every required member, plant the timers.
    Returns report lines for the opener's console (ping warnings included)."""
    if not _GATE_ID_RE.match(iteration):
        raise ProtocolError(
            f"gate id {iteration!r} — letters, digits, '-' and '_' only "
            f"(it rides shell commands and @mentions)")
    members = roster(members_dir)

    def guard(journal: list[Message]) -> None:
        if any(m.kind == KIND_OPEN and m.iteration == iteration
               for m in journal):
            raise ProtocolError(
                f"gate {iteration!r} already exists — gate ids are unique "
                f"per journal; pick a fresh one")

    queue.append_with(opener, KIND_OPEN, body, iteration=iteration,
                      guard=guard)
    required = tuple(m for m in members if m != opener)
    report = ping_members(
        required,
        f"gate {iteration} is open — {body} | catch up: `cluster-chat read "
        f"--new`, then reply EXACTLY once: `cluster-chat post nop --gate "
        f"{iteration}` (the fold) or `post stance <0-10> \"<reasons>\" "
        f"--gate {iteration}` or `post hold \"<why>\" --gate {iteration}`")
    if timers:
        command = _check_gate_command(iteration, queue.root, config_path,
                                      members_dir)
        _spawn_timer(config.nudge_after_seconds, command)
        _spawn_timer(config.close_after_seconds, command)
    report.append(f"gate {iteration} open — {len(required)} replies expected; "
                  f"nudge at {config.nudge_after_seconds}s, timeout at "
                  f"{config.close_after_seconds}s")
    return report


_COMPLETION_PING = ("gate {iteration}: all replies are in — `cluster-chat "
                    "read --new`, then close with the resolution: "
                    "`cluster-chat close {iteration} \"<resolution>\"`")


def post_reply(queue: Queue, config: ProtocolConfig, *, member: str,
               kind: str, body: str, iteration: str,
               stance: int | None, members_dir: Path,
               ) -> tuple[Message, list[str]]:
    """A member's gate reply, fully guarded, with the completion ping when
    this reply is the one that finishes the round."""
    members = roster(members_dir)

    def guard(journal: list[Message]) -> None:
        gate = load_gate(journal, iteration, members)
        if gate is None:
            raise ProtocolError(f"no gate {iteration!r} — open it first, or "
                                f"check the id (`cluster-chat read --all`)")
        if gate.closed:
            raise ProtocolError(f"gate {iteration!r} is closed — its "
                                f"resolution is on the queue")
        thread = [m for m in journal if m.iteration == iteration]
        if len(thread) >= config.loop_cap:
            raise ProtocolError(
                f"gate {iteration!r} hit the loop cap ({config.loop_cap} "
                f"messages) — the protocol degrades to silence, not a spend "
                f"loop; a human should look at this thread")
        replies = [m for m in thread
                   if m.kind in REPLY_KINDS and m.member == member]
        if len(replies) >= config.reply_cap:
            raise ProtocolError(
                f"{member} already replied on gate {iteration!r} "
                f"(cap {config.reply_cap}) — follow-ups belong to whoever an "
                f"@mention names")

    def check(journal: list[Message]) -> Gate | None:
        return load_gate(journal, iteration, members)

    message, gate = queue.append_with(member, kind, body,
                                      iteration=iteration, stance=stance,
                                      guard=guard, check=check)
    report: list[str] = []
    if gate is not None and gate.complete and not gate.closed:
        report += ping_members(
            (gate.opener,), _COMPLETION_PING.format(iteration=iteration))
        report.append(f"yours was the last reply — {gate.opener} was pinged "
                      f"to close gate {iteration}")
    return message, report


def check_gate(queue: Queue, config: ProtocolConfig, *, iteration: str,
               members_dir: Path) -> list[str]:
    """The timers' verb, idempotent by construction: closed → nothing;
    complete-but-open → (re-)ping the opener (doubles as the retry when the
    completion ping was lost); before the deadline → nudge stragglers; after
    it → record TIMEOUT rows and ping the opener. Which branch runs is
    derived from the journal + clock, so nudge- and close-forks share it."""
    members = roster(members_dir)
    gate = load_gate(queue.read_all(), iteration, members)
    if gate is None:
        raise ProtocolError(f"no gate {iteration!r} to check")
    if gate.closed:
        return []
    if gate.complete:
        report = ping_members((gate.opener,),
                              _COMPLETION_PING.format(iteration=iteration))
        return report + [f"gate {iteration} is complete — opener pinged"]
    deadline = (datetime.fromisoformat(gate.opened_ts)
                + timedelta(seconds=config.close_after_seconds))
    if datetime.now(timezone.utc) < deadline:
        report = ping_members(
            gate.stragglers,
            f"reminder — gate {iteration} still needs your ONE reply: "
            f"`cluster-chat read --new`, then post nop/stance/hold --gate "
            f"{iteration}")
        return report + [f"nudged {len(gate.stragglers)} straggler(s) on "
                         f"gate {iteration}"]
    for straggler in gate.stragglers:
        # A timeout row per absent member — the closer authors it, `member`
        # names the straggler (schema's documented shape). Guarded so a
        # re-run fork can never double-record.
        def guard(journal: list[Message], who: str = straggler) -> None:
            already = any(m.kind == KIND_TIMEOUT and m.iteration == iteration
                          and m.member == who for m in journal)
            if already:
                raise ProtocolError("already recorded")
        try:
            queue.append_with(straggler, KIND_TIMEOUT,
                              "no reply before close_after — recorded absent "
                              "(an absence, not a nop)",
                              iteration=iteration, guard=guard)
        except ProtocolError:
            pass
    report = ping_members(
        (gate.opener,),
        f"gate {iteration} timed out waiting on "
        f"{', '.join(gate.stragglers)} — timeouts are recorded; read and "
        f"close when ready: `cluster-chat close {iteration} "
        f"\"<resolution>\"`")
    return report + [f"gate {iteration}: recorded timeout(s) for "
                     f"{', '.join(gate.stragglers)}; opener pinged"]


def close_gate(queue: Queue, *, member: str, iteration: str,
               resolution: str, members_dir: Path) -> Message:
    """The stop rule: the opener — only the opener — appends the resolution
    line, and the gate refuses everything thereafter."""
    members = roster(members_dir)

    def guard(journal: list[Message]) -> None:
        gate = load_gate(journal, iteration, members)
        if gate is None:
            raise ProtocolError(f"no gate {iteration!r} to close")
        if gate.closed:
            raise ProtocolError(f"gate {iteration!r} is already closed")
        if member != gate.opener:
            raise ProtocolError(
                f"only the opener ({gate.opener}) closes gate "
                f"{iteration!r} — the resolution is their judgment call")

    message, _ = queue.append_with(member, KIND_CLOSE, resolution,
                                   iteration=iteration, guard=guard)
    return message
