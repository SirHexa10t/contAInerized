"""Domain-neutral helpers shared across launch/ modules — the project's home
for cross-cutting regex / sorting / parsing / formatting work that doesn't
belong to any one domain. Anything that touches the filesystem lives in
file_access.py instead per the "all file I/O lives in one place" rule.

Leaf module: imports nothing from sibling launch/ modules — kept pull-able
from anywhere without circular-import risk.
"""

from datetime import datetime


# === Formatting ===

def plural(n: int) -> str:
    """English plural marker: '' for n==1, 's' otherwise. So '{n} day{plural(n)}'
    yields '1 day' / '2 days' / '0 days'."""
    return "" if n == 1 else "s"


def relative_time(mtime: float) -> str:
    """Human-readable relative time from an epoch mtime (e.g. '3 days ago',
    '5 minutes ago'). Display-only — used by the picker's Cont preview for
    the 'Last used' line."""
    delta = datetime.now() - datetime.fromtimestamp(mtime)
    if delta.days >= 1:
        return f"{delta.days} day{plural(delta.days)} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{plural(hours)} ago"
    minutes = delta.seconds // 60
    return f"{minutes} minute{plural(minutes)} ago" if minutes else "just now"


# === Sorting ===

def ordering_index_or_end(value, ordering) -> int:
    """Position of `value` in `ordering`, or `len(ordering)` if absent —
    pushes unknowns past the end when used as a sort-key element. Backs the
    picker's tag-set and mode-set sort keys in agents_crud."""
    return ordering.index(value) if value in ordering else len(ordering)


# === Parsing ===

def split_host_port(entry: str) -> tuple[str, str]:
    """Parse a host:port (or cidr:port) string. Returns (host, port) when a
    trailing `:port` is present, else (entry, ''). rpartition-based so an
    IPv4 with port (`1.2.3.4:80`), a CIDR with port (`10.0.0.0/8:443`), and
    a bare host (`foo.com`) all dispatch correctly."""
    host, sep, port = entry.rpartition(":")
    return (host, port) if sep else (entry, "")


def splice_block(content: str, block_text: str, keep: bool = True) -> str:
    """Reconcile a marker-bounded region of `content` against `block_text`.
    `block_text`'s first and last lines (after stripping leading/trailing
    whitespace) serve as the wrapper markers used to locate the region.

      - `keep=True` (default): ensure the block is present — replace the
        existing region if both markers are already found in order, or
        append the block at the end (with a blank-line separator) if not.
      - `keep=False`: ensure the block is absent — remove the region if it's
        there, or no-op if it isn't.

    Leading whitespace before the splice point is absorbed so the result has
    at most one blank-line separator; the leading `"\\n\\n"` is omitted at
    position 0 so a fresh document doesn't get a stray top blank line.

    Backs agents_crud.sync_memory_templates' per-template reconcile of MEMORY.md."""
    block = block_text.strip()                       # also lets us treat lines[0]/lines[-1] as real wrappers
    lines = block.splitlines()
    if len(lines) < 2:
        return content                               # need at least a start- and end-wrapper line
    start, end = lines[0], lines[-1]
    s_idx = content.find(start)
    e_idx = content.find(end)
    in_content = s_idx != -1 and s_idx < e_idx
    if not keep and not in_content:
        return content                               # nothing to remove and nothing to add
    if in_content:
        end_pos = e_idx + len(end)
    else:
        # Treat append as "splice into the empty range at end-of-content".
        s_idx = end_pos = len(content)
    # Walk past trailing newlines before the splice point — keeps the leading
    # "\n\n" separator from stacking onto existing newlines.
    while s_idx > 0 and content[s_idx - 1] == "\n":
        s_idx -= 1
    if not keep:
        return content[:s_idx] + content[end_pos:]
    prefix = "\n\n" if s_idx > 0 else ""
    return content[:s_idx] + prefix + block + content[end_pos:]
