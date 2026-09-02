"""The message shape and reply vocabulary — the protocol's data layer.

One JSON object per chat.jsonl line. `seq` (append order, assigned by
queue.py under its lock) IS the total order — the serial chain the whole
design started from. Kinds:

    open      a gate begins (body: what is being assessed)     needs iteration
    nop       the FOLD — declines this hand, carries NOTHING   needs iteration
    stance    a 0-10 assessment (+ reasons/riders in body)     needs iteration
    hold      process verb: pause, diverges from core goals    needs iteration
    timeout   a straggler recorded absent at close — AUTHORED
              by the closer, `member` names the straggler      needs iteration
    close     the gate's resolution line (the stop rule)       needs iteration
    free      ordinary chatter between gates — no ack burden   iteration optional

The nop-carries-nothing rule is enforced HERE, not just documented: a nop is
the intent not to participate (poker's fold), so a stance value on any
non-`stance` kind is a validation error. `5` is an informed stance —
"torn by conflicting points" — never a spelling of ignorance; ignorance is
the nop (plans/cluster_plan.md, 2026-08-31, operator).

STANDALONE CONSTRAINT (see __init__): stdlib only, no imports beyond this
package.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

KIND_OPEN = "open"
KIND_NOP = "nop"
KIND_STANCE = "stance"
KIND_HOLD = "hold"
KIND_TIMEOUT = "timeout"
KIND_CLOSE = "close"
KIND_FREE = "free"
KINDS = (KIND_OPEN, KIND_NOP, KIND_STANCE, KIND_HOLD, KIND_TIMEOUT,
         KIND_CLOSE, KIND_FREE)
# What counts as a member's gate reply (the accounting set gates.py tallies).
REPLY_KINDS = (KIND_NOP, KIND_STANCE, KIND_HOLD)
STANCE_MIN, STANCE_MAX = 0, 10


class ProtocolError(Exception):
    """A message or configuration the protocol cannot honour — malformed
    shape, a stance where none belongs, a tunable missing from the config.
    Loud by design: trial-and-error needs typos to surface, not default."""


@dataclass(frozen=True)
class Message:
    """One chat.jsonl line. `seq` is queue-assigned (0 = not yet appended);
    `iteration` is the gate id for everything gate-scoped; `stance` exists
    exactly on `stance` messages."""
    seq: int
    ts: str
    member: str
    kind: str
    body: str = ""
    iteration: str | None = None
    stance: int | None = None

    def validate(self) -> None:
        """Raise ProtocolError naming exactly what is wrong, or return."""
        if self.kind not in KINDS:
            raise ProtocolError(
                f"unknown kind {self.kind!r} — one of {', '.join(KINDS)}")
        if self.kind != KIND_FREE and not self.iteration:
            raise ProtocolError(
                f"a {self.kind!r} message needs its gate's `iteration` id")
        if self.kind == KIND_STANCE:
            if self.stance is None:
                raise ProtocolError("a stance message needs `stance` "
                                    f"({STANCE_MIN}-{STANCE_MAX})")
            if not (STANCE_MIN <= self.stance <= STANCE_MAX):
                raise ProtocolError(
                    f"stance {self.stance} is outside "
                    f"{STANCE_MIN}-{STANCE_MAX}")
        elif self.stance is not None:
            # The fold rule, enforced: a nop (or any non-stance kind)
            # carries no assessment data.
            raise ProtocolError(
                f"a {self.kind!r} message may not carry a stance — "
                f"a nop is the fold and carries nothing")
        if not self.member:
            raise ProtocolError("a message needs its author in `member`")

    def to_line(self) -> str:
        """One compact JSON line, None fields omitted (the store-emitter
        convention: absent, not null), newline-terminated."""
        payload = {key: value for key, value in asdict(self).items()
                   if value is not None}
        return json.dumps(payload, ensure_ascii=False) + "\n"

    @classmethod
    def from_line(cls, line: str) -> "Message":
        """Parse one chat.jsonl line back into a validated Message."""
        try:
            data = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProtocolError(f"unparseable chat line: {line!r}") from error
        try:
            message = cls(**data)
        except TypeError as error:
            raise ProtocolError(f"chat line with unknown fields: {line!r}") from error
        message.validate()
        return message
