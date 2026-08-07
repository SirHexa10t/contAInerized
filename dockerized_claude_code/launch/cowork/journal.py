"""The per-group conversation log for `{cowork}`.

One append-only `conversation.md` per group, inside its manager's own group dir —
so the manager can read the discussion it hosts without the hub copying anything,
and a relaunched manager recovers the thread by reading it. Coworkers do not see
it (their mount is their own dir); the hub copies it to one on request.

`tail -f` is the intended live view, so the format optimises for reading raw
rather than rendered: a one-line header per entry, then the body indented. The
indent does real work beyond looks — it stops a body's own markdown (`#`
headings, `---` rules, code fences) from restructuring the document it is being
quoted into, which matters because bodies are agent prose the hub does not
control.

Only ATTRIBUTED traffic belongs here: an entry is always tied to one group. A
capture the hub could not attribute — a human-typed turn — has no group by
definition, so it is reported by the relay rather than filed against a
conversation it was not part of.
"""

from __future__ import annotations

import time
from enum import Enum
from pathlib import Path

from ..file_access import append_text, is_file, read_text
from ..paths import group_conversation_path
from .group import Session, session_dir


class Direction(Enum):
    """What an entry records, as a glyph that stays legible in a raw tail.

    Members expose `.glyph`, used as the entry marker.
    """
    TO = "→"        # hub delivered a message to this participant
    FROM = "←"      # this participant replied
    NOTE = "!!"     # hub-side event: undelivered, round cap reached, session closed

    @property
    def glyph(self) -> str:
        return self.value


def journal_path(session: Session) -> Path:
    """This group's `conversation.md`, in the manager's group dir."""
    return group_conversation_path(session_dir(session))


def format_entry(direction: Direction, participant: str, body: str,
                 *, now: float | None = None) -> str:
    """One rendered entry, including its trailing blank line.

    Split from `append` so the formatting is testable without touching disk, and
    so a caller that wants to show an entry without logging it can reuse it."""
    stamp = time.strftime("%H:%M:%S", time.localtime(time.time() if now is None else now))
    lines = body.strip().splitlines() or [""]
    indented = "\n".join(f"    {line}" if line.strip() else "" for line in lines)
    return f"[{stamp}] {direction.glyph} {participant}\n{indented}\n\n"


def append(session: Session, direction: Direction, participant: str, body: str,
           *, now: float | None = None) -> None:
    """Record one entry against this group's conversation.

    Append rather than rewrite: the log is the durable record of what happened,
    so a crash must never be able to truncate it, and a reader tailing it should
    only ever see whole entries."""
    append_text(journal_path(session), format_entry(direction, participant, body, now=now))


def read_journal(session: Session) -> str:
    """The whole conversation so far, or "" when nothing has been logged yet.

    Used to hand a relaunched participant its context back — the reason the log
    lives in the manager's mounted dir rather than somewhere hub-private."""
    path = journal_path(session)
    return read_text(path) if is_file(path) else ""
