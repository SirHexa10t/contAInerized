"""Domain-neutral helpers shared across launch/ modules — the project's home
for cross-cutting regex / sorting / parsing / formatting work that doesn't
belong to any one domain. Anything that touches the filesystem lives in
file_access.py instead per the "all file I/O lives in one place" rule.

Leaf module: imports nothing from sibling launch/ modules — kept pull-able
from anywhere without circular-import risk.
"""

import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from typing import TypeVar

T = TypeVar("T")


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


def parse_stem(stem: str) -> tuple[str, list[str], str | None]:
    """Parse an agent-filename stem into (name, tags, parent).

    Grammar: <name>(<bracketed-tag>|<parenthesized-parent>)*
      - `[tag]` accumulates into tags (list, in the order they appear).
      - `(parent)` is single-valued; if repeated, last wins.
      - Order between brackets and parens is free: 'name[code](thinker)' and
        'name(thinker)[code]' both parse the same way.

    Examples:
        'name'                → ('name', [], None)
        'name(thinker)'       → ('name', [], 'thinker')
        'name[code]'          → ('name', ['code'], None)
        'name[code](thinker)' → ('name', ['code'], 'thinker')
        'name[a][b]'          → ('name', ['a', 'b'], None)
    """
    m = re.match(r"^([^()\[\]]+)", stem)
    if not m:
        return (stem, [], None)
    name = m.group(1)
    tags = []
    parent = None
    for paren, bracket in re.findall(r"\(([^()]+)\)|\[([^\[\]]+)\]", stem[len(name):]):
        if paren:
            parent = paren
        else:
            tags.append(bracket)
    return (name, tags, parent)


def parse_agent_name(stem: str) -> str:
    """Just the `name` half of `parse_stem` — drops [tag] / (parent) suffixes
    from a filename stem. Used to index AGENT_MD_BY_NAME in agents_crud."""
    return parse_stem(stem)[0]


# === Subprocess ===

def shell_capture(*cmd: str) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(cmd, capture_output=True, text=True)` — the common
    one-shot pattern for invoking a CLI tool and inspecting its stdout/stderr.
    `check=False` (the default) so callers handle the returncode themselves."""
    return subprocess.run(cmd, capture_output=True, text=True)


# === Exception-to-exit ===

def call_or_exit(func: Callable[..., T], *args, exceptions=Exception, prefix: str = "  ", **kwargs) -> T:
    """Call `func(*args, **kwargs)`; if it raises one of `exceptions`, print
    the exception message (prefixed with `prefix`) and `sys.exit`. Returns
    `func`'s return value on success. `exceptions` defaults to `Exception`
    (catch-all); pass a specific class or tuple for narrower handling.
    Keyword-only `exceptions` / `prefix` so they don't collide with kwargs
    forwarded to `func`."""
    try:
        return func(*args, **kwargs)
    except exceptions as e:
        sys.exit(f"{prefix}{e}")


# === User prompts ===

def _print_header_and_body(header: str, body: list[str]) -> None:
    """Render a multi-line prompt's preamble: leading blank, the header
    indented two spaces, each body line indented two spaces (empty body
    lines render as blank lines for visual separation), trailing blank.
    Shared by prompt_yn / prompt_keypress so their visual cadence stays
    identical regardless of the actual input gate that follows."""
    print()
    print(f"  {header}")
    for line in body:
        print(f"  {line}" if line else "")
    print()


def prompt_yn(header: str, body: list[str], prompt_label: str, default: bool = False) -> bool:
    """Generic multi-line Y/N prompt. `header` is the question line, `body` is a
    list of explanation/caveat lines (empty strings render as blank lines for
    visual separation), and `prompt_label` is what shows in the actual y/N input
    (e.g. '{auto}'). Returns bool; Enter alone uses `default`."""
    _print_header_and_body(header, body)
    default_marker = "Y/n" if default else "y/N"
    answer = input(f"  Enable {prompt_label}? [{default_marker}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def prompt_keypress(header: str, body: list[str]) -> None:
    """Generic multi-line notice + press-any-key gate. Same `header` / `body`
    shape as `prompt_yn` but waits on any single keypress instead of asking
    y/N — for surfacing notices the user must acknowledge before continuing.
    Pure text rendering; if the caller wants ANSI styling, bake it into the
    strings (the function leaves the cursor on a default-styled line for the
    press-any-key prompt, so the caller's body should self-reset). Falls
    back to requiring Enter when no tty is available (no termios)."""
    _print_header_and_body(header, body)
    print("  [press any key to continue] ", end="", flush=True)
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)        # cbreak keeps Ctrl+C working — raw would swallow it
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except (ImportError, OSError):   # non-Linux/macOS or no tty → fallback requires Enter
        input()
    print()
