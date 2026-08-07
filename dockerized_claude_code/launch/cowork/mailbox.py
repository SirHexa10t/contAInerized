"""The message plane for `{cowork}`: staging messages in, attributing replies out.

Two directions, and they are asymmetric.

**Outbound (hub → participant).** The hub writes the message body into the
recipient's group dir and injects a one-line pointer at it. Injection types
keystrokes, so an embedded newline would read as Enter and submit a fragment —
a pointer sidesteps that entirely and has no length limit.

**Inbound (participant → hub).** The `{cowork}` Stop hook drops a JSON capture
into `<instance>/outbox/` on every turn. A capture identifies its session but
NOT its group, and human-typed turns fire the hook exactly like hub-driven ones —
so attribution cannot come from the outbox's ordering (that would be FIFO, which
one typed turn breaks permanently). Instead each capture carries `prompt_id`,
which joins to the transcript's `promptId`, and the group is read out of the
paired prompt's own text. Verified against real transcripts: one `prompt_id`
resolves to exactly one prompt, with tool-result echoes excluded.

A prompt with no tag is a human turn: logged as unsolicited, never routed.

Note the capture's `transcript_path` is a CONTAINER path — the hook runs inside
the container, where the state dir is mounted at `~/.claude`. A host-side hub
must translate it, which `host_transcript_path` does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..file_access import (
    ensure_dir, is_dir, is_file, iter_files, iter_subdirs, move_path, read_text,
    remove_path, write_text,
)
from ..paths import (
    CLAUDE_CONFIG_IN_CONTAINER, COWORK_IN_CONTAINER, cowork_group_path,
    cowork_outbox_path, group_hosting_dir, instance_state_dir_path,
)

# The machine-readable marker the hub prepends to every injected prompt. Kept
# bracket-delimited and leading so it is trivially parseable, survives being
# typed as keystrokes, and stays legible to the human watching the session.
GROUP_TAG_RE = re.compile(r"^\s*\[cowork\s+([^\]]+)\]")
MESSAGES_SUBDIR = "messages"      # staged message bodies, inside the recipient's group dir
REJECTED_SUBDIR = "rejected"      # captures that would not parse — kept, not deleted


def tag_message(group: str, body: str) -> str:
    """`body` prefixed with the group marker, ready to inject or stage."""
    return f"[cowork {group}] {body}"


def group_from_prompt(text: str) -> str | None:
    """The group key a prompt was tagged with, or None for an untagged prompt —
    which is how a human-typed turn is recognised, since it cannot carry a tag
    the hub never wrote."""
    match = GROUP_TAG_RE.match(text)
    return match.group(1).strip() if match else None


# ============================================================
# Outbound: staging a message for a participant
# ============================================================

def container_group_path(group: str) -> Path:
    """Where a participant sees its own group dir. Every participant mounts its
    `group_hosting/<id>/` at COWORK_IN_CONTAINER, so the in-container path is the
    same for everyone regardless of whose dir it is."""
    return COWORK_IN_CONTAINER / group


def stage_message(recipient: str, group: str, sender: str, body: str,
                  *, seq: int) -> Path:
    """Write `body` into `recipient`'s group dir; return the CONTAINER path to
    quote in the injected pointer.

    Numbered per group so the recipient can read them in order and the hub never
    overwrites an unread message."""
    name = f"{seq:03d}-from-{sender}.md"
    write_text(cowork_group_path(recipient, group) / MESSAGES_SUBDIR / name,
               f"# from: {sender}\n# group: {group}\n\n{body.strip()}\n")
    return container_group_path(group) / MESSAGES_SUBDIR / name


def next_seq(recipient: str, group: str) -> int:
    """The number the next message staged for `recipient` in `group` should carry.

    Counted from what is on disk rather than tracked in hub state: the numbering
    exists so a recipient reads its messages in order and the hub never overwrites
    an unread one, and the directory is already the authoritative record of both.
    A counter in hub state could disagree with the files after a manual clear-out;
    this cannot.

    Lives here because this module owns MESSAGES_SUBDIR and the `NNN-from-<id>.md`
    name format — a caller deriving the number itself would have to know both."""
    staged = cowork_group_path(recipient, group) / MESSAGES_SUBDIR
    return sum(1 for _ in iter_files(staged, suffix=".md")) + 1


def pointer_prompt(group: str, sender: str, path: Path) -> str:
    """The one-line, tagged prompt that tells a recipient where to read.

    Single line by necessity: injection types the text, so a newline would
    submit early. The tag is what makes the eventual reply attributable."""
    return tag_message(group, f"A message from {sender} is at {path} — read that "
                              f"file and reply to it.")


# ============================================================
# Inbound: captures and attribution
# ============================================================

@dataclass(frozen=True)
class Capture:
    """One Stop-hook capture, as dropped into a participant's outbox."""
    instance: str
    source: Path
    prompt_id: str | None
    session_id: str | None
    transcript_path: str | None
    answer: str

    @classmethod
    def from_payload(cls, instance: str, source: Path,
                     payload: dict[str, Any]) -> Capture | None:
        """Build from parsed hook JSON, or None if there is no reply text worth
        routing (an empty turn has nothing to forward)."""
        answer = str(payload.get("last_assistant_message") or "").strip()
        if not answer:
            return None
        def _str(key: str) -> str | None:
            value = payload.get(key)
            return value if isinstance(value, str) and value else None
        return cls(instance=instance, source=source, prompt_id=_str("prompt_id"),
                   session_id=_str("session_id"),
                   transcript_path=_str("transcript_path"), answer=answer)


def host_transcript_path(instance: str, container_path: str) -> Path | None:
    """Translate a capture's in-container `transcript_path` to the host path.

    The hook runs inside the container, where the instance's state dir is mounted
    at `~/.claude` — so the recorded path is meaningless to a host-side hub until
    it is rebased onto `instances/<instance>/`. Returns None for a path that is
    not under the expected mount (a host-run agent, or a layout change), so the
    caller can fall back rather than read the wrong file."""
    prefix = str(CLAUDE_CONFIG_IN_CONTAINER)
    if not container_path.startswith(prefix):
        return None
    relative = container_path[len(prefix):].lstrip("/")
    return instance_state_dir_path(instance) / relative


def prompt_text(transcript: Path, prompt_id: str) -> str | None:
    """The user prompt that began the turn identified by `prompt_id`.

    Filters the transcript three ways, each for a reason seen in real data:
    `promptId` selects the turn; sidechain entries are subagent traffic, not this
    conversation; and user-role entries carrying a `tool_result` block are the
    harness echoing tool output back, not something anyone typed — a single turn
    routinely has ten of those.
    """
    if not is_file(transcript):
        return None
    for line in read_text(transcript).splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict) or entry.get("promptId") != prompt_id:
            continue
        if entry.get("isSidechain"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            continue
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue
        return " ".join(str(b.get("text", "")) for b in content
                        if isinstance(b, dict) and b.get("type") == "text")
    return None


def attribute(capture: Capture) -> str | None:
    """The group `capture` answers, or None when it cannot be attributed.

    None covers both "a human typed this" (untagged prompt) and "the pairing was
    unavailable" (no prompt_id, or an unreadable transcript). Both mean the same
    thing to a caller: log it, do not route it."""
    if capture.prompt_id is None or capture.transcript_path is None:
        return None
    transcript = host_transcript_path(capture.instance, capture.transcript_path)
    if transcript is None:
        return None
    text = prompt_text(transcript, capture.prompt_id)
    return group_from_prompt(text) if text else None


def read_captures(instance: str) -> list[Capture]:
    """Every capture waiting in `instance`'s outbox, oldest first (filenames lead
    with an epoch-nanosecond stamp, so name order is time order).

    Reads but does NOT delete: `consume` is a separate call so a caller that dies
    mid-handling leaves the file in place and retries on the next pass rather than
    losing the turn. Calling `consume` per capture once handled is what bounds the
    directory — one file per turn would otherwise accumulate forever.

    A capture that will not parse is the exception: it is MOVED to `rejected/`
    here, since there is nothing to hand back and re-reading it every pass would
    spin. Moved rather than deleted, because silently destroying state the hub
    failed to understand is the wrong default — it stays inspectable.
    """
    outbox = cowork_outbox_path(instance)
    if not is_dir(outbox):
        return []
    captures: list[Capture] = []
    for path in iter_files(outbox, suffix=".json"):
        try:
            payload = json.loads(read_text(path))
        except (json.JSONDecodeError, OSError):
            payload = None
        capture = (Capture.from_payload(instance, path, payload)
                   if isinstance(payload, dict) else None)
        if capture is None:
            _reject(outbox, path)
            continue
        captures.append(capture)
    return captures


def instances_with_outbox() -> tuple[str, ...]:
    """Every instance that has an outbox on disk, sorted, whether or not it is
    currently in a group.

    This — rather than group membership — is what a drain must iterate. The
    `{cowork}` Stop hook fires on EVERY turn for the whole life of a tagged
    instance, so an instance between groups (its normal state most of the time)
    still drops a capture per turn. Draining only active participants would leave
    those accumulating without bound, and would strand any capture that arrived
    just as its group closed. Unattributable captures are exactly what the
    caller's "unsolicited" path is for."""
    return tuple(sorted(d.name for d in iter_subdirs(group_hosting_dir())
                        if is_dir(cowork_outbox_path(d.name))))


def consume(capture: Capture) -> None:
    """Delete a capture's file once it has been fully handled. Separate from
    `read_captures` so a caller that crashes mid-handling leaves the file in
    place and retries, rather than losing the turn."""
    remove_path(capture.source)


def _reject(outbox: Path, path: Path) -> None:
    """Park an unparseable capture where it cannot be re-read."""
    destination = outbox / REJECTED_SUBDIR / path.name
    ensure_dir(destination.parent)
    move_path(path, destination)
