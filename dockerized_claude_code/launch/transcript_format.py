"""One transcript LINE, parsed — the only part of the session-record format
that also has to run INSIDE a container.

Everything here is pure: a JSON line in, what was said out, no disk and no
launcher imports. That is the whole reason the module exists. `transcripts.py`
owns the format for the host (which files to walk, which turn wins, what a
state dir can say about itself) and imports this; the container mounts THIS
FILE alone, at `_transcript_format.py` beside the other in-container helpers,
so `alt+f`'s popup answers with the same rules the host's `--find` does.

The alternative was a second, stdlib-only parse living in `settings/` next to
`_dump_last_msg.py`, pinned to this one by a test. Rejected at gate
history-find (strict-reviewer's call): a pin test can only fail AFTER the two
have already disagreed, and the shapes that would actually drift are the
quiet ones — content as blocks versus a plain string, a tool-result echo, a
missing field. One implementation cannot drift, so there is nothing to pin.
The constraint that buys it: STDLIB ONLY, and no `from .` imports, ever. The
file is imported as a top-level module in the container, where no `launch`
package exists.

The format is not ours and is not versioned (Claude Code's own docs call it
internal and say it changes between releases), so every parse here returns
None rather than raising. A schema change costs a hit, never a traceback.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

# Turn types that carry something a participant actually said. Everything else
# a transcript holds — `attachment`, `system`, `mode`, `last-prompt`,
# `ai-title`, `file-history-snapshot`, `cost-state`, … — is bookkeeping, and
# is roughly half of every file (measured: 333 spoken turns in 716 lines).
SPOKEN_TYPES = ("user", "assistant")


@dataclass(frozen=True)
class TranscriptHit:
    """One turn that contains a searched term.

    `where` is the offset of the match inside `text`, so a caller can quote
    around it without searching the string again — and without this module
    deciding how wide a quote should be, which is the reader's taste rather
    than the format's.

    `sidechain` is a LABEL, not a filter. Everywhere else a sidechain turn is
    discarded, because "what did the person last ask" must never answer with
    a sub-agent's prompt to itself. A search is the one question where those
    turns count: they are conversation that happened, and they live in their
    own files (verified 2026-09-19 on this project's corpus — 435 of 435
    sub-agent lines carry the flag, 0 of 12971 parent lines do, so reading
    both double-counts nothing). The field is THREE-valued at the source:
    true, false, and absent from 28% of lines — absent means "not a
    sidechain", which is what `.get()` yields.
    """
    speaker: str        # one of SPOKEN_TYPES
    text: str           # the whole turn, as said
    when: float         # epoch seconds
    where: int          # offset of the match within `text`
    source: Path        # the transcript the turn was read from
    sidechain: bool     # a sub-agent's turn rather than the session's own


def content_text(content: object) -> str:
    """The readable text of a message's `content`: the string itself, or the
    joined `text` blocks of a block list; "" for anything else.

    "" is the answer for a `tool_result`-only list (typed as a user turn, but
    nobody asked it), for an assistant turn that only thought or called a
    tool (`thinking` blocks are redacted and carry no text), and for a shape
    this reader has never seen. All three mean the same thing to every
    caller: nothing was said here."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    return ""


def turn_text(line: str, event_type: str) -> tuple[str, float] | None:
    """(what was said, epoch seconds) for a transcript line of `event_type`
    that carries readable text, else None — other turn types, tool-result
    echoes, sidechains and unparseable lines all return None.

    Sidechains are excluded HERE and kept by `matched_turn` below, and the
    difference is the question being asked: this one backs "what was last
    said in this conversation", where a sub-agent's aside is not an answer."""
    event = _parsed(line)
    if event is None or event.get("type") != event_type or event.get("isSidechain"):
        return None
    text = content_text(event.get("message", {}).get("content"))
    when = _timestamp(event)
    return (text, when) if text and when is not None else None


def matched_turn(line: str, needle: str, source: Path) -> TranscriptHit | None:
    """`line` as a hit when someone SAID `needle` in it, else None. `needle`
    must already be lowercased — the caller has it that way once, rather than
    this doing it per line.

    The term has to land in what was said. A tool call carrying it, a tool
    result echoing a file that contains it, and a `last-prompt` bookkeeping
    line holding a copy of the prompt all miss — which is the entire reason
    this parses instead of grepping: on this project's corpus one term
    matched 26 raw lines and 3 actual turns."""
    event = _parsed(line)
    if event is None or event.get("type") not in SPOKEN_TYPES:
        return None
    text = content_text(event.get("message", {}).get("content"))
    where = text.lower().find(needle)
    when = _timestamp(event)
    if where < 0 or when is None:
        return None
    return TranscriptHit(speaker=str(event["type"]), text=text, when=when,
                         where=where, source=source,
                         sidechain=bool(event.get("isSidechain")))


def _parsed(line: str) -> dict[str, Any] | None:
    """`line` as a JSON object, or None for junk.

    `Any` rather than `object` for the values: this is somebody else's
    schema, every reader here already guards each field it touches, and
    `object` would only move those guards into casts.

    Junk is a truncated write, a partial line read while the harness is
    still appending, or anything that is not an object at all."""
    try:
        event = json.loads(line)
    except (ValueError, TypeError):
        return None
    return event if isinstance(event, dict) else None


def _timestamp(event: dict[str, Any]) -> float | None:
    """The line's time as epoch seconds, or None when it has none this reader
    understands. Bookkeeping lines mostly carry no timestamp at all, so this
    doubles as a second gate on "is this a real turn"."""
    stamp = event.get("timestamp")
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
