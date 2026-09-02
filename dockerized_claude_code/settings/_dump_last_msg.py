"""Backs `dump_last_msg` in settings/bashrc.sh — writes the session's last
assistant reply to a markdown file. No AI involved: it reads the transcript
Claude Code already wrote and copies the text out.

The bashrc-helper pattern its sibling `_summary.py` set: python does the
parsing, bash owns the one-line entry point. Mounted read-only into every
container beside the bashrc that calls it, so `python3 _dump_last_msg.py` is
the whole contract (stdlib only, no launcher imports — it runs in-container,
where `launch/` does not exist).

WHICH transcript: the most recently modified `*.jsonl` directly inside a
`~/.claude/projects/<project>/` dir. One level only, deliberately —
subagent transcripts live in a `<session-uuid>/subagents/` subdir, and their
last message is not the session's reply. WHICH message: the last
`type: "assistant"` entry that is not a sidechain (a sidechain IS a
subagent's turn, recorded in the parent's file) and carries at least one
text block; its text blocks are joined, so a reply split across blocks
arrives whole. Tool calls and thinking blocks are skipped: they are not the
reply a human means by "the last message".
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
INSTANCE_ENV = "CLAUDE_AGENT_INSTANCE"     # staged by the launcher per instance
FALLBACK_INSTANCE = "session"              # outside a launcher container


def newest_transcript(projects_dir: Path = PROJECTS_DIR) -> Path | None:
    """The session transcript last written to, or None when there is none."""
    candidates = [path for project in projects_dir.glob("*")
                  if project.is_dir()
                  for path in project.glob("*.jsonl")
                  if path.stat().st_size > 0]
    return max(candidates, key=lambda p: p.stat().st_mtime, default=None)


def last_assistant_text(transcript: Path) -> str | None:
    """The last assistant reply's text, or None when the transcript holds no
    assistant message with text yet. Read back-to-front: the answer is
    almost always in the final lines, and these files reach tens of MB."""
    for line in reversed(transcript.read_text(encoding="utf-8",
                                              errors="replace").splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue          # a torn final line — keep walking backwards
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        content = entry.get("message", {}).get("content")
        if not isinstance(content, list):
            continue
        blocks = [block.get("text", "") for block in content
                  if isinstance(block, dict) and block.get("type") == "text"]
        text = "\n\n".join(part for part in blocks if part.strip())
        if text.strip():
            return text.strip()
    return None


def output_name(when: datetime | None = None) -> str:
    """`<instance>-message_<timestamp>.md` — the instance so a file dropped
    in a shared workspace says which agent wrote it, the timestamp so
    repeated dumps never overwrite one another."""
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    instance = os.environ.get(INSTANCE_ENV) or FALLBACK_INSTANCE
    return f"{instance}-message_{stamp}.md"


def main(argv: list[str]) -> int:
    target_dir = Path(argv[0]) if argv else Path.cwd()
    transcript = newest_transcript()
    if transcript is None:
        print(f"dump_last_msg: no session transcript under {PROJECTS_DIR}",
              file=sys.stderr)
        return 1
    text = last_assistant_text(transcript)
    if text is None:
        print(f"dump_last_msg: {transcript.name} holds no assistant reply yet",
              file=sys.stderr)
        return 1
    if not target_dir.is_dir():
        print(f"dump_last_msg: not a directory: {target_dir}", file=sys.stderr)
        return 1
    out = target_dir / output_name()
    try:
        out.write_text(text + "\n", encoding="utf-8")
    except OSError as error:      # a read-only workspace ({ro}) lands here
        print(f"dump_last_msg: cannot write {out}: {error}", file=sys.stderr)
        return 1
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
