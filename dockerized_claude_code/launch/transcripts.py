"""Reading Claude Code's own session records — what a state dir can tell the
launcher about the conversation inside it.

Four questions, all answered from `<state_dir>/`:

  - is there anything `claude --continue` could load, and how big is it
    (`has_continuable_jsonl`, `continuable_jsonl_bytes`)
  - when was this instance last used (`last_history_mtime`)
  - what was said last, by whom (`last_prompt_in_state`, `last_answer_in_state`)

Split out of `file_access` 2026-09-03. These are not file-access primitives:
they encode the SHAPE of a foreign format — which turn types count, that a
`tool_result` is typed "user" without being something a person asked, that
sidechains are somebody else's conversation, that `thinking` blocks carry no
readable text. Two of the functions here touch no disk at all. Keeping that
knowledge beside `write_text` and `iter_subdirs` meant the format's quirks
had no home of their own, and a reader looking for "how do we know what the
user last said" had to find it inside the disk layer.

The format is not ours and is not versioned: every parse degrades to None
rather than raising, so a truncated transcript or an upstream schema change
shows up as "no prompt" in a picker preview instead of a traceback in a
launch. Disk access still goes through `file_access` — this module reads
through it, exactly like every other consumer.
"""

import json
from datetime import datetime
from pathlib import Path

from .file_access import read_text
from .paths import state_history_path, state_workspace_jsonls

def has_continuable_jsonl(state_dir: Path) -> bool:
    """True iff `state_dir` has at least one non-empty session JSONL — i.e.,
    something `claude --continue` can load. `state_workspace_jsonls` points
    at `projects/-workspace/`, where only session-UUID JSONLs live —
    `history.jsonl` is a sibling of `projects/` at the state-dir root, so
    it never shows up in the iteration. Instance.has_continuable_history
    is a thin wrapper — pulling the disk-walk out of the dataclass keeps
    tags/identity.py a pure data layer."""
    return any(jsonl.stat().st_size > 0 for jsonl in state_workspace_jsonls(state_dir))


def continuable_jsonl_bytes(state_dir: Path) -> int:
    """Size in bytes of the transcript `claude --continue` would load — the
    most recently modified non-empty session JSONL — or 0 when the dir holds
    none. The size matters because `--continue` has been observed silently
    starting a FRESH conversation over a ~92 MB transcript (ISSUES.md,
    2026-08-29): compute_resume_flag warns past a threshold instead of letting
    a launch discover it. Instance.continuable_history_bytes is the thin
    wrapper, same split as `has_continuable_jsonl` above."""
    stats = [jsonl.stat() for jsonl in state_workspace_jsonls(state_dir)]
    live = [stat for stat in stats if stat.st_size > 0]
    return max(live, key=lambda stat: stat.st_mtime).st_size if live else 0


def last_history_mtime(state_dir: Path) -> float | None:
    """Mtime of `<state_dir>/history.jsonl` (the per-launch input log), or
    None if it doesn't exist yet. `state_history_path` encodes the layout
    fact that this file has a single deterministic location — no walk
    needed. Instance.last_used_mtime is a thin wrapper around this
    (same reason as above)."""
    history = state_history_path(state_dir)
    return history.stat().st_mtime if history.is_file() else None


def last_prompt_in_state(state_dir: Path) -> tuple[str, float] | None:
    """The most recent human prompt in a state dir's conversation transcript,
    paired with its time as epoch seconds — or None if the dir holds no prompt
    yet. Reads the session JSONLs under `projects/-workspace/` (the same
    records `claude --continue` loads, so a dir yields a prompt here iff it is
    resumable). A transcript "user" turn counts as a human prompt only when its
    `message.content` is text — a plain string or `text` blocks — never a
    `tool_result` echo (those are also typed "user" but aren't something the
    person asked). Malformed / schema-drifted lines are skipped, so a truncated
    or format-changed transcript degrades to "no prompt" rather than crashing
    the caller (e.g. quickie's `--history` listing)."""
    return _last_text_turn(state_dir, "user")


def last_answer_in_state(state_dir: Path) -> tuple[str, float] | None:
    """The most recent assistant answer in a state dir's transcript, paired with
    its time as epoch seconds — or None if there's no answer yet. Same source
    and same graceful-degrade rules as last_prompt_in_state; the assistant's
    `text` blocks are the answer (redacted `thinking` and `tool_use` blocks
    carry no readable text, so `_content_text` skips them). Powers quickie's
    `q --answer <id>`."""
    return _last_text_turn(state_dir, "assistant")


def _last_text_turn(state_dir: Path, event_type: str) -> tuple[str, float] | None:
    """(text, epoch-seconds) of the latest transcript turn of `event_type`
    ("user" | "assistant") that carries readable text, scanning every session
    JSONL under `projects/-workspace/`."""
    latest: tuple[str, float] | None = None
    for jsonl in state_workspace_jsonls(state_dir):
        for line in read_text(jsonl).splitlines():
            found = _transcript_turn(line, event_type)
            if found is not None and (latest is None or found[1] > latest[1]):
                latest = found
    return latest


def _transcript_turn(line: str, event_type: str) -> tuple[str, float] | None:
    """(text, epoch-seconds) for a transcript JSONL line of `event_type` that
    carries readable text, else None — other turn types, tool-result echoes,
    sidechains, and unparseable lines all return None."""
    try:
        event = json.loads(line)
        if event.get("type") != event_type or event.get("isSidechain"):
            return None
        text = _content_text(event.get("message", {}).get("content"))
        if not text:
            return None
        return text, datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError, KeyError, json.JSONDecodeError):
        return None


def _content_text(content: object) -> str:
    """The human-typed text of a user message's `content`: the string itself,
    or the joined `text` blocks of a block list; "" for a tool_result-only list
    (or any other shape), which marks 'not a human prompt'."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(
            block["text"] for block in content
            if isinstance(block, dict) and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        ).strip()
    return ""
