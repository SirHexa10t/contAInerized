"""Helpers for the /write-summary slash command.

Two operations, dispatched via the first CLI arg:
  diff   compare current /workspace contents against the manifest in .claude_summary;
         print one line per NEW / CHANGED / DELETED file.
  save   replace the manifest block in .claude_summary with the current listing.
         Refuses to run unless a '### File Manifest' heading is followed by
         begin/end markers, each on its own line.

Invoked by the bash wrappers `summary_diff` and `summary_save_manifest` defined in
settings/bashrc.sh. `.claude_summary` is intentionally excluded from the listing —
since `save` writes to it, tracking it would always show a CHANGED loop.
"""

import re
import sys
from pathlib import Path

ROOT = Path("/workspace")
SUMMARY = ROOT / ".claude_summary"

EXCLUDED_NAMES = {".claude_summary"}
EXCLUDED_DIR_NAMES = {".git", "__pycache__"}

BEGIN_TAG = "<!-- manifest:begin -->"
END_TAG   = "<!-- manifest:end -->"


def list_files():
    """Map every regular file under ROOT to its int mtime, with exclusions applied."""
    out = {}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.name in EXCLUDED_NAMES:
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIR_NAMES for part in rel.parts):
            continue
        out[str(rel)] = int(path.stat().st_mtime)
    return out


def find_manifest_block():
    """Read SUMMARY and locate the manifest block. Returns (text, begin, end) — the
    full file text plus the indices spanning the content between the markers.
    Returns None if SUMMARY doesn't exist, the heading is missing, or either marker
    is missing.

    The `### File Manifest` heading must appear on its own line, and both markers
    must appear on their own lines *after* that heading. This anchoring keeps the
    parser from latching onto incidental mentions of the marker strings elsewhere
    in the document (e.g., in prose explaining the manifest format)."""
    if not SUMMARY.exists():
        return None
    text = SUMMARY.read_text()
    header = re.search(r"^### File Manifest$", text, re.MULTILINE)
    if header is None:
        return None
    begin = re.search(r"^" + re.escape(BEGIN_TAG) + r"$", text[header.end():], re.MULTILINE)
    if begin is None:
        return None
    begin_pos = header.end() + begin.end()
    end = re.search(r"^" + re.escape(END_TAG) + r"$", text[begin_pos:], re.MULTILINE)
    if end is None:
        return None
    end_pos = begin_pos + end.start()
    return text, begin_pos, end_pos


def parse_manifest():
    """Read SUMMARY's manifest block into {path: epoch_int}; {} if missing or no markers."""
    found = find_manifest_block()
    if found is None:
        return {}
    text, begin, end = found
    out = {}
    for line in text[begin:end].splitlines():
        m = re.match(r"^\s*(\d+)\s+(.+?)\s*$", line)
        if m:
            out[m.group(2)] = int(m.group(1))
    return out


def _classify():
    """Yield (kind, path) for each file differing between SUMMARY's manifest and the
    current /workspace listing. kind ∈ {'NEW', 'CHANGED', 'DELETED'}."""
    prev = parse_manifest()
    curr = list_files()
    for path in sorted(set(prev) | set(curr)):
        p, c = prev.get(path), curr.get(path)
        if   p is None: yield "NEW", path
        elif c is None: yield "DELETED", path
        elif p != c:    yield "CHANGED", path


def cmd_diff():
    for kind, path in _classify():
        print(f"{kind:<10}{path}")


def cmd_files_to_check():
    """Print one path per line for files needing the AI's attention (NEW + CHANGED)."""
    for kind, path in _classify():
        if kind != "DELETED":
            print(path)


def cmd_save():
    found = find_manifest_block()
    if found is None:
        if not SUMMARY.exists():
            raise SystemExit(f"{SUMMARY} does not exist; create it first.")
        raise SystemExit(
            f"Refusing to save: missing manifest block in {SUMMARY}. "
            f"Expected a '### File Manifest' heading followed by '{BEGIN_TAG}' "
            f"and '{END_TAG}' — each on its own line."
        )
    text, begin, end = found
    items = sorted(list_files().items(), key=lambda kv: (-kv[1], kv[0]))
    new_block = "\n" + "\n".join(f"{e} {p}" for p, e in items) + "\n"
    SUMMARY.write_text(text[:begin] + new_block + text[end:])
    print(f"Wrote {len(items)} entries between '{BEGIN_TAG}' and '{END_TAG}' in {SUMMARY}.")


COMMANDS = {"diff": cmd_diff, "files": cmd_files_to_check, "save": cmd_save}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    cmd = COMMANDS.get(mode)
    if cmd is None:
        raise SystemExit(f"Unknown mode: {mode!r}; expected one of {list(COMMANDS)}.")
    cmd()
