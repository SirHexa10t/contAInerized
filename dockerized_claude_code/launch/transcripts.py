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

from .ai.adapter import Adapter
from .file_access import (
    history_files, read_text, subagent_transcript_files, transcript_files,
    transcript_layouts,
)
from .transcript_format import TranscriptHit, matched_turn, turn_text

__all__ = [
    "TranscriptHit", "continuable_jsonl_bytes", "find_turns",
    "has_continuable_jsonl", "last_answer_in_state", "last_history_mtime",
    "last_prompt_in_state", "unreadable_layout",
]

# The one line shape the launcher's parser reads (launch/transcript_format.py).
# An adapter whose `transcript_format` differs has its files FOUND — so a
# resume can still be decided — but never PARSED as if they were this shape.
READABLE_FORMAT = "claude-code"


def has_continuable_jsonl(state_dir: Path, adapter: Adapter) -> bool:
    """True iff `state_dir` holds at least one non-empty session transcript
    in `adapter`'s layout — something its CLI's resume can load.

    The adapter is REQUIRED, unlike every reader below: whether to resume is
    a question about the harness the instance runs NOW, and an instance
    switched from Claude Code to Gemini CLI still has Claude transcripts in
    its dir that Gemini cannot continue. Instance.has_continuable_history is
    the thin wrapper that knows which adapter."""
    return any(path.stat().st_size > 0 for path in transcript_files(state_dir, adapter))


def continuable_jsonl_bytes(state_dir: Path, adapter: Adapter) -> int:
    """Size in bytes of the transcript `adapter`'s resume would load — the
    most recently modified non-empty one — or 0 when the dir holds none. The
    size matters because Claude Code's `--continue` has been observed
    silently starting a FRESH conversation over a ~92 MB transcript
    (ISSUES.md, 2026-08-29): compute_resume_flag warns past a threshold
    instead of letting a launch discover it. Same adapter rule as above."""
    stats = [path.stat() for path in transcript_files(state_dir, adapter)]
    live = [stat for stat in stats if stat.st_size > 0]
    return max(live, key=lambda stat: stat.st_mtime).st_size if live else 0


def last_history_mtime(state_dir: Path) -> float | None:
    """The newest input-log mtime under `state_dir`, whichever CLI wrote it,
    or None when there is none yet — the "last used" signal. Any layout,
    because it is a display question: an instance used under either harness
    was used."""
    stamps = [path.stat().st_mtime for adapter in transcript_layouts(state_dir)
              for path in history_files(state_dir, adapter)]
    return max(stamps, default=None)


def unreadable_layout(state_dir: Path) -> str | None:
    """The CLI name of a layout under `state_dir` whose transcripts the
    launcher finds but cannot read yet (Gemini CLI today), else None. What a
    reader prints instead of an empty result, so "not read" never looks like
    "never said anything" (gate step4-start)."""
    return next((adapter.name for adapter in transcript_layouts(state_dir)
                 if adapter.transcript_format != READABLE_FORMAT), None)


def last_prompt_in_state(state_dir: Path) -> tuple[str, float] | None:
    """The most recent human prompt in a state dir's conversation transcripts,
    paired with its time as epoch seconds — or None if the dir holds no prompt
    yet, or none the launcher can read (`unreadable_layout` says which). A
    transcript "user" turn counts as a human prompt only when its
    `message.content` is text — a plain string or `text` blocks — never a
    `tool_result` echo (those are also typed "user" but aren't something the
    person asked). Malformed / schema-drifted lines are skipped, so a truncated
    or format-changed transcript degrades to "no prompt" rather than crashing
    the caller (e.g. quickie's `--history` listing)."""
    return _last_text_turn(state_dir, "user")


def last_answer_in_state(state_dir: Path) -> tuple[str, float] | None:
    """The most recent assistant answer in a state dir's transcripts, paired
    with its time as epoch seconds — or None if there's no answer yet. Same
    source and same graceful-degrade rules as last_prompt_in_state; the
    assistant's `text` blocks are the answer (redacted `thinking` and
    `tool_use` blocks carry no readable text, so `content_text` skips them).
    Powers quickie's `q --answer <id>`."""
    return _last_text_turn(state_dir, "assistant")


def find_turns(state_dir: Path, term: str) -> list[TranscriptHit]:
    """Every spoken turn in `state_dir`'s readable conversations containing
    `term`, case-insensitively, oldest first — the session transcripts AND
    the sub-agent transcripts beneath them. A layout the launcher cannot
    read yet contributes nothing; `unreadable_layout` is how a caller says so.

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
    for path in _readable_files(state_dir, subagents=True):
        for line in read_text(path).splitlines():
            if needle not in line.lower():
                continue                      # cheap reject before the parse
            hit = matched_turn(line, needle, path)
            if hit is not None:
                hits.append(hit)
    return sorted(hits, key=lambda hit: hit.when)


def _readable_files(state_dir: Path, *, subagents: bool) -> list[Path]:
    """The transcripts under `state_dir` in every layout the parser reads —
    with the sub-agents' when asked (a search wants them; "the last thing
    said here" does not)."""
    out: list[Path] = []
    for adapter in transcript_layouts(state_dir):
        if adapter.transcript_format != READABLE_FORMAT:
            continue
        out += transcript_files(state_dir, adapter)
        if subagents:
            out += subagent_transcript_files(state_dir, adapter)
    return out


def _last_text_turn(state_dir: Path, event_type: str) -> tuple[str, float] | None:
    """(text, epoch-seconds) of the latest transcript turn of `event_type`
    ("user" | "assistant") that carries readable text, scanning every session
    transcript the parser can read (sub-agents excluded)."""
    latest: tuple[str, float] | None = None
    for path in _readable_files(state_dir, subagents=False):
        for line in read_text(path).splitlines():
            found = turn_text(line, event_type)
            if found is not None and (latest is None or found[1] > latest[1]):
                latest = found
    return latest


