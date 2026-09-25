"""Reading Claude Code's own session records — what a state dir can tell the
launcher about the conversation inside it.

Five questions, all answered from `<state_dir>/`:

  - is there anything `claude --continue` could load, and how big is it
    (`has_continuable_jsonl`, `continuable_jsonl_bytes`)
  - when was this instance last used (`last_history_mtime`)
  - what was said last, by whom (`last_prompt_in_state`, `last_answer_in_state`)
  - where was a term said, across everything this dir holds (`find_turns`)

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

Still the place to add "a new thing to read out of a session transcript"
(.claude_dev_guidelines). What lives one door down in `transcript_format` is
only the per-LINE parse, and only because the container needs the same rules
without the `launch` package: this module is which FILES to walk and which
turn wins, that one is what a single line says.
"""

from pathlib import Path

from .file_access import read_text
from .paths import (
    state_history_path, state_workspace_jsonls, state_workspace_subagent_jsonls,
)
from .transcript_format import TranscriptHit, matched_turn, turn_text

__all__ = [
    "TranscriptHit", "continuable_jsonl_bytes", "find_turns",
    "has_continuable_jsonl", "last_answer_in_state", "last_history_mtime",
    "last_prompt_in_state",
]

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
    carry no readable text, so `content_text` skips them). Powers quickie's
    `q --answer <id>`."""
    return _last_text_turn(state_dir, "assistant")


def find_turns(state_dir: Path, term: str) -> list[TranscriptHit]:
    """Every spoken turn in `state_dir`'s conversations containing `term`,
    case-insensitively, oldest first — the session transcripts AND the
    sub-agent transcripts beneath them.

    SPOKEN turns only: the same `type` + readable-text rules the rest of this
    module applies, so a tool call carrying the term, a tool result echoing a
    file that contains it, and the bookkeeping lines that make up about half
    of a transcript all miss. That is the point of parsing rather than
    grepping — on this project's own corpus one term matched 26 raw lines and
    3 actual turns. Each parse still degrades to "skip this line" rather than
    raising, so a truncated or schema-drifted transcript costs its own hits
    and nothing else."""
    needle = term.lower()
    hits: list[TranscriptHit] = []
    for jsonl in (*state_workspace_jsonls(state_dir),
                  *state_workspace_subagent_jsonls(state_dir)):
        for line in read_text(jsonl).splitlines():
            if needle not in line.lower():
                continue                      # cheap reject before the parse
            hit = matched_turn(line, needle, jsonl)
            if hit is not None:
                hits.append(hit)
    return sorted(hits, key=lambda hit: hit.when)


def _last_text_turn(state_dir: Path, event_type: str) -> tuple[str, float] | None:
    """(text, epoch-seconds) of the latest transcript turn of `event_type`
    ("user" | "assistant") that carries readable text, scanning every session
    JSONL under `projects/-workspace/`."""
    latest: tuple[str, float] | None = None
    for jsonl in state_workspace_jsonls(state_dir):
        for line in read_text(jsonl).splitlines():
            found = turn_text(line, event_type)
            if found is not None and (latest is None or found[1] > latest[1]):
                latest = found
    return latest


