"""`--find` — where a term was said, across every conversation on this host.

The question this answers is "did we discuss X, and in WHICH session", which
nothing else could: a terminal's find and both multiplexers' copy-mode
searches read a RAM ring buffer of rendered output (herdr keeps 10 MB per
pane, tmux 2000 lines, neither survives a restart, and rows drawn on the
alternate screen never enter it at all), so they cannot see another session,
yesterday, or often even the current session. The transcripts on disk are the
only durable record, and `transcripts.find_turns` reads them.

Three layers, deliberately apart:
  file_access.iter_conversation_dirs  which dirs hold a conversation (disk)
  transcripts.find_turns              which turns in one dir said it (format)
  here                                labelling, grouping, ranking, printing

NO INDEX, measured rather than assumed: a parsed scan of this project's own
36 MB corpus takes 0.22 s, about 5 ms per MB, and the largest single
transcript the project has seen (92 MB) would cost half a second. Revisit if
a scan ever passes REVISIT_SECONDS — an index would then have to be built,
invalidated and kept honest, which is a lot of machinery to buy back a
second.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console                                           # dep — declared in pyproject.toml [project]
from rich.text import Text

from .file_access import iter_conversation_dirs
from .paths import clusters_dir, instances_dir, quickie_dir
from .transcripts import TranscriptHit, find_turns
from .utils import one_line, plural, stamp_time

# How much of a matched turn a result line shows. Narrower than the picker
# preview's 250 (`LAST_PROMPT_PREVIEW_CHARS`) because that pane shows ONE
# prompt while this prints many rows — and a transcript turn can be as long
# as whatever was ever pasted into it.
HIT_PREVIEW_CHARS = 150
HIT_PREVIEW_LEAD = 50       # characters of run-up before the match, so it reads in context

# Hits shown per conversation before the rest are counted rather than
# printed. A term someone worked on all day appears dozens of times in one
# session; the question is WHICH session, and three lines answer it.
HITS_SHOWN = 3

# Past this, the no-index decision above deserves re-measuring.
REVISIT_SECONDS = 5.0


@dataclass(frozen=True)
class ConversationFinds:
    """One conversation's hits, with the name the operator knows it by."""
    label: str
    state_dir: Path
    hits: list[TranscriptHit]

    @property
    def when(self) -> float:
        """The newest hit's time — how this conversation is ranked. Recency
        of the MATCH, not of the conversation: a term last said months ago in
        a session still running belongs below one said this morning."""
        return max(hit.when for hit in self.hits)


def conversation_label(state_dir: Path) -> str:
    """The name to print for a conversation dir, derived from which root it
    sits under rather than from any file inside it: an instance by its id, a
    cluster member as `<cluster> / <member>`, a quickie thread as `q /
    <id>`. A dir under none of the three (a layout that changed under us)
    prints its path, which is still something to go and look at."""
    for root, kind in ((instances_dir(), "instance"),
                       (clusters_dir(), "cluster"),
                       (quickie_dir(), "quickie")):
        try:
            parts = state_dir.relative_to(root).parts
        except ValueError:
            continue
        if kind == "cluster" and len(parts) >= 3:      # <session>/members/<member-id>
            return f"{parts[0]} / {parts[2]}"
        if kind == "quickie":
            return f"q / {parts[0]}"
        return parts[0]
    return str(state_dir)


def find_in_history(term: str) -> list[ConversationFinds]:
    """Every conversation that said `term`, most recent match first.

    Conversations with no hit are dropped rather than listed empty: the
    answer to "which session was it" is a short list, and a host with fifty
    instances would bury it."""
    found = [ConversationFinds(label=conversation_label(state_dir),
                               state_dir=state_dir, hits=hits)
             for state_dir in iter_conversation_dirs()
             if (hits := find_turns(state_dir, term))]
    return sorted(found, key=lambda conversation: conversation.when, reverse=True)


def quote(hit: TranscriptHit) -> str:
    """The matched turn as one line, with enough run-up that the match reads
    in context — and an ellipsis on each side, because it is a fragment."""
    start = max(0, hit.where - HIT_PREVIEW_LEAD)
    body = one_line(hit.text[start:], HIT_PREVIEW_CHARS)
    return f"…{body}" if start else body


def print_findings(term: str, found: list[ConversationFinds]) -> None:
    """Print the result: a conversation per block, its newest match first,
    each hit as `<time>  <speaker>  <quote>`. Grey for the times and the
    counts, so the conversation names and what was said carry the eye. rich
    drops the colour when output is piped."""
    console = Console()
    if not found:
        print(f'  No conversation mentions "{term}".')
        return
    turns = sum(len(conversation.hits) for conversation in found)
    print(f'  "{term}" — {turns} turn{plural(turns)} '
          f'in {len(found)} conversation{plural(len(found))}:')
    for conversation in found:
        console.print(_heading(conversation))
        for hit in conversation.hits[:HITS_SHOWN]:
            # One row per hit, whatever the terminal width: a wrapped quote
            # reads as two hits and breaks the column the times line up in.
            console.print(_hit_line(hit), no_wrap=True, overflow="ellipsis", crop=True)
        if (rest := len(conversation.hits) - HITS_SHOWN) > 0:
            console.print(Text(f"        + {rest} more", style="bright_black"))


def _heading(conversation: ConversationFinds) -> Text:
    """`<label>   <newest match time>   <n turns>`."""
    line = Text("\n  ")
    line.append(conversation.label, style="bold")
    line.append(f"   {stamp_time(conversation.when)}", style="bright_black")
    line.append(f"   {len(conversation.hits)} turn"
                f"{plural(len(conversation.hits))}", style="bright_black")
    return line


def _hit_line(hit: TranscriptHit) -> Text:
    """One matched turn. A sub-agent's turn says so: it happened under this
    conversation but nobody in the room typed it."""
    line = Text("      ")
    line.append(stamp_time(hit.when), style="bright_black")
    line.append(f"  {hit.speaker:9} ", style="cyan" if hit.speaker == "user" else "green")
    if hit.sidechain:
        line.append("(sub-agent) ", style="bright_black")
    # Built with Text rather than markup so a '[' in what was said cannot be
    # parsed as a style tag (the same reason quickie's listing does).
    line.append(quote(hit))
    return line
