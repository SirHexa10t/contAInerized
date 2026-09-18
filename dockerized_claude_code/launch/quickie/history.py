"""`q --history` — list past question threads (oldest last-question first) — and
`q --answer <id>` — print a thread's saved answer.

A thread is a session dir under ~/.ai-agents/quickie/ (bar the shared
`communal/` workspace); its last question / last answer come from the
conversation transcript via file_access.last_prompt_in_state /
last_answer_in_state — the same records `--resume` continues, so a thread
appears here iff it's resumable. Threads with no question yet are skipped. The
pure helpers (one_line, format_time) are split out so they're unit-testable
without touching the filesystem."""

import sys
from datetime import datetime

from rich.console import Console
from rich.text import Text

from ..file_access import iter_subdirs
from ..transcripts import last_answer_in_state, last_prompt_in_state
from .render import print_markdown
from ..paths import quickie_communal_workspace, quickie_dir, quickie_state_dir_path

PROMPT_MAX = 180   # question chars shown before the listing cuts it with an ellipsis


def one_line(prompt: str) -> str:
    """A prompt squeezed to a single line and capped at PROMPT_MAX chars; a
    trailing '…' marks a question that was cut."""
    flat = " ".join(prompt.split())
    return flat if len(flat) <= PROMPT_MAX else flat[:PROMPT_MAX] + "…"


def format_time(when: float) -> str:
    """An epoch-seconds timestamp as a local `YYYY-MM-DD HH:MM` string."""
    return datetime.fromtimestamp(when).strftime("%Y-%m-%d %H:%M")


def collect_history() -> list[tuple[float, str, str]]:
    """(when, thread-id, last-question) for every thread that has a question,
    ascending by the last question's date. The communal workspace dir is not a
    thread."""
    communal_name = quickie_communal_workspace().name
    dated: list[tuple[float, str, str]] = []
    for session in iter_subdirs(quickie_dir()):
        if session.name == communal_name:
            continue
        found = last_prompt_in_state(session)
        if found is not None:
            prompt, when = found
            dated.append((when, session.name, prompt))
    dated.sort()   # by date, then id, then prompt — deterministic on same-timestamp ties
    return dated


def print_history() -> None:
    """Print the listing as `<grey timestamp>  <id> - "<question>"` lines, or a
    friendly note when no thread has a question yet. rich drops the grey when
    output isn't a TTY (e.g. piped)."""
    threads = collect_history()
    if not threads:
        print("  No past questions yet.")
        return
    console = Console()
    for when, thread_id, prompt in threads:
        # Built with Text (not markup) so a '[' in the question can't be parsed as a style tag.
        line = Text("  ")
        line.append(format_time(when), style="bright_black")   # grey
        line.append(f'  {thread_id} - "{one_line(prompt)}"')
        console.print(line)


def latest_thread() -> str | None:
    """The id of the most recently answered-or-asked thread, or None when
    there are none — `collect_history` is ascending, so it is the last."""
    history = collect_history()
    return history[-1][1] if history else None


def print_answer(session_id: str | None = None) -> None:
    """Print the saved answer for a past thread (`q --answer <id>`), or exit with
    a friendly note if the thread or its answer isn't found. Without an id the
    LATEST thread is meant: the answer to the question just asked is the one
    a reader wants nine times in ten, and typing a gibberish id to get it was
    the friction (operator, 2026-09-18). Rendered as markdown on a terminal
    and emitted as its source anywhere else — the same rule the live answer
    follows, so a reprint looks like the answer did."""
    if not session_id:
        session_id = latest_thread()
        if session_id is None:
            sys.exit('No quickie threads yet.  Ask one:  q "your question here"')
    state = quickie_state_dir_path(session_id)
    if not state.is_dir():
        sys.exit(f"No quickie thread '{session_id}'.  Run  q --history  to list them.")
    found = last_answer_in_state(state)
    if found is None:
        sys.exit(f"No saved answer for thread '{session_id}' yet.")
    print_markdown(found[0])
