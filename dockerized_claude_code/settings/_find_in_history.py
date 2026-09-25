#!/usr/bin/env python3
"""`find_in_history` — search this container's past conversations, from the
shell or from the muxer's alt+f popup.

The in-container half of the launcher's `--find`. Both parse a transcript
line the same way, because both call the SAME parser: `_transcript_format.py`
is mounted beside this file and is the launcher's own
`launch/transcript_format.py`. A second, look-alike parse living here would
drift on the quiet shapes — content as blocks versus a plain string, a
tool-result echo, a missing field — and a test could only notice after the
two had already disagreed.

What this file owns is the part the host cannot do: finding the transcripts
from INSIDE, where `~/.ai-agents` does not exist. The launcher stages
`AGENT_TRANSCRIPTS_DIR` per instance, which for a cluster member is that
member's own dir — never `/home/claude/.claude`, the hardcoding that made
the neighbouring `_dump_last_msg.py` read the wrong dir for every member.

In a cluster the search covers every SIBLING member, not just this one,
because "was this handled by someone else on the team" is the question
alt+f exists for, and the cluster dir is mounted whole and readable anyway.
The scope is printed, never guessed at, and the walk stops at the members
directory: it has no business anywhere else in the filesystem.

Stdlib only, like every mounted helper — there is no `launch` package here.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))   # the mounted parser sits beside this file
from _transcript_format import matched_turn                # noqa: E402

TRANSCRIPTS_ENV = "AGENT_TRANSCRIPTS_DIR"   # staged by the launcher per instance
MEMBERS_DIR_NAME = "members"                # a cluster's per-member config dirs live under this
FALLBACK_TRANSCRIPTS = Path.home() / ".claude" / "projects"

QUOTE_CHARS = 150      # of a matched turn, per row — see launch/history_find.py
QUOTE_LEAD = 50        # run-up before the match, so it reads in context
HITS_SHOWN = 3         # per conversation; the rest are counted


def transcripts_root() -> Path:
    """This container's own `projects/` dir."""
    return Path(os.environ.get(TRANSCRIPTS_ENV) or FALLBACK_TRANSCRIPTS)


def search_roots(root: Path) -> list[tuple[str, Path]]:
    """`(label, projects dir)` for everything this container may search.

    One entry outside a cluster. Inside one — recognised structurally, by
    this config dir sitting under a `members/` dir, rather than by matching
    an absolute path — one entry per sibling member, so a question like
    "did anyone look at the firewall" gets answered by the team rather than
    by one seat at it. The walk never climbs above that `members/` dir."""
    members_dir = root.parent.parent
    if members_dir.name != MEMBERS_DIR_NAME or not members_dir.is_dir():
        return [("this session", root)]
    return sorted((member.name, member / root.name)
                  for member in members_dir.iterdir() if member.is_dir())


def transcripts(projects: Path) -> list[Path]:
    """Every transcript under one `projects/` dir — the session files and
    the sub-agent files one level below them. Sorted so a run is repeatable."""
    if not projects.is_dir():
        return []
    return sorted([*projects.glob("*/*.jsonl"), *projects.glob("*/*/subagents/*.jsonl")])


def find(term: str, roots: list[tuple[str, Path]]) -> list[tuple[str, list]]:
    """`(label, hits)` per conversation that said `term`, newest match first."""
    needle = term.lower()
    found = []
    for label, projects in roots:
        hits = []
        for path in transcripts(projects):
            try:
                lines = path.read_text(errors="replace").splitlines()
            except OSError:
                continue          # a file being rotated under us is not an error
            hits += [hit for line in lines if needle in line.lower()
                     if (hit := matched_turn(line, needle, path)) is not None]
        if hits:
            found.append((label, sorted(hits, key=lambda hit: hit.when)))
    return sorted(found, key=lambda pair: pair[1][-1].when, reverse=True)


def quote(hit) -> str:
    """The matched turn as one line, with run-up, cut to a row."""
    start = max(0, hit.where - QUOTE_LEAD)
    flat = " ".join(hit.text[start:].split())
    body = flat if len(flat) <= QUOTE_CHARS else flat[:QUOTE_CHARS] + "…"
    return f"…{body}" if start else body


def report(term: str, roots: list[tuple[str, Path]], found: list) -> None:
    scope = roots[0][1] if len(roots) == 1 else f"{len(roots)} cluster members"
    print(f'Searching {scope} for "{term}"\n')
    if not found:
        print(f'  Nothing said "{term}".')
        return
    turns = sum(len(hits) for _, hits in found)
    print(f'  {turns} turn{"" if turns == 1 else "s"} '
          f'in {len(found)} conversation{"" if len(found) == 1 else "s"}:')
    for label, hits in found:
        print(f"\n  {label}   ({len(hits)})")
        for hit in hits[:HITS_SHOWN]:
            when = datetime.fromtimestamp(hit.when).strftime("%Y-%m-%d %H:%M")
            side = "(sub-agent) " if hit.sidechain else ""
            print(f"      {when}  {hit.speaker:9} {side}{quote(hit)}")
        if len(hits) > HITS_SHOWN:
            print(f"        + {len(hits) - HITS_SHOWN} more")


def main(argv: list[str]) -> int:
    term = " ".join(argv).strip()
    if not term:
        try:
            term = input("Find in past conversations: ").strip()
        except (EOFError, KeyboardInterrupt):
            return 1
    if not term:
        return 1
    roots = search_roots(transcripts_root())
    report(term, roots, find(term, roots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
