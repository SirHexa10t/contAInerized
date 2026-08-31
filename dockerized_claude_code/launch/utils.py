"""Domain-neutral helpers shared across launch/ modules — the project's home
for cross-cutting regex / sorting / parsing / formatting work that doesn't
belong to any one domain. Anything that touches the filesystem lives in
file_access.py instead per the "all file I/O lives in one place" rule.

Leaf module: imports nothing from sibling launch/ modules — kept pull-able
from anywhere without circular-import risk.
"""

import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, TypeVar

T = TypeVar("T")


# === Formatting ===

def plural(n: int) -> str:
    """English plural marker: '' for n==1, 's' otherwise. So '{n} day{plural(n)}'
    yields '1 day' / '2 days' / '0 days'."""
    return "" if n == 1 else "s"


def relative_time(mtime: float) -> str:
    """Human-readable relative time from an epoch mtime (e.g. '3 days ago',
    '5 minutes ago'). Display-only — used by the picker's Cont preview for
    the 'Last used' line. A future mtime (clock skew, NTP jump, copied file)
    clamps to 'just now' — a negative timedelta would otherwise normalize to
    days=-1 + positive seconds and render nonsense like '23 hours ago'."""
    delta = datetime.now() - datetime.fromtimestamp(mtime)
    if delta.total_seconds() < 0:
        return "just now"
    if delta.days >= 1:
        return f"{delta.days} day{plural(delta.days)} ago"
    hours = delta.seconds // 3600
    if hours >= 1:
        return f"{hours} hour{plural(hours)} ago"
    minutes = delta.seconds // 60
    return f"{minutes} minute{plural(minutes)} ago" if minutes else "just now"


# === Sorting ===

def ordering_index_or_end(value: object, ordering: Sequence[object]) -> int:
    """Position of `value` in `ordering`, or `len(ordering)` if absent —
    pushes unknowns past the end when used as a sort-key element. Backs the
    picker's tag-ordering sort keys in agents_crud. `object`-typed
    (not a TypeVar) because membership/index only need equality, and one
    caller legitimately probes with None (engine_sort_key's unknown-family
    sentinel)."""
    return ordering.index(value) if value in ordering else len(ordering)


# === Parsing ===

def split_host_port(entry: str) -> tuple[str, str]:
    """Parse a host:port (or cidr:port) string. Returns (host, port) when a
    trailing `:port` is present, else (entry, ''). rpartition-based so an
    IPv4 with port (`1.2.3.4:80`), a CIDR with port (`10.0.0.0/8:443`), and
    a bare host (`foo.com`) all dispatch correctly."""
    host, sep, port = entry.rpartition(":")
    return (host, port) if sep else (entry, "")


# === Subprocess ===

def shell_capture(*cmd: str, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """`subprocess.run(cmd, capture_output=True, text=True)` — the common
    one-shot pattern for invoking a CLI tool and inspecting its stdout/stderr.
    `check=False` (the default) so callers handle the returncode themselves.
    `timeout=N` (default None — no limit) raises subprocess.TimeoutExpired on
    expiry; callers wrap the call in try/except where applicable."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def shell_returncode(*cmd: str, env: dict[str, str] | None = None) -> int:
    """Sister to shell_capture — runs `cmd` with stdout/stderr inherited so
    output streams to the user's terminal. Returns the subprocess return code
    so callers decide how to react (sys.exit with it, retry, ignore). `env`
    optionally overlays the subprocess environment (default: inherit caller's
    full env); pass an overlay when the child needs vars beyond the caller's."""
    return subprocess.run(cmd, env=env).returncode


# === Exits ===

def call_or_exit(func: Callable[..., T], *args: Any, exceptions: type[BaseException] | tuple[type[BaseException], ...] = Exception, prefix: str = "  ", **kwargs: Any) -> T:
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


def exit_if_missing(value: Any, exit_message: str = "") -> None:
    """Exit with `exit_message` if `value` is falsy (None / empty collection /
    empty string / 0). Sister to `call_or_exit` — guards a precondition at a
    boundary rather than wrapping a call that may raise."""
    if not value:
        sys.exit(exit_message)


# === User prompts ===

def _print_header_and_body(header: str, body: list[str]) -> None:
    """Render a multi-line prompt's preamble: leading blank, the header
    indented two spaces, each body line indented two spaces (empty body
    lines render as blank lines for visual separation), trailing blank.
    Used by prompt_keypress; kept separate so future gates share the
    same visual cadence."""
    print()
    print(f"  {header}")
    for line in body:
        print(f"  {line}" if line else "")
    print()


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


# === Terminal hygiene ===

# The escape-sequence MODES a dead full-screen TUI leaves set on the terminal
# EMULATOR — beyond termios's reach, which is why `stty sane` (and docker's
# own tty restore) can't fix them. Each is the "off/normal" spelling and an
# idempotent no-op on a healthy terminal:
#   ?1000/?1002/?1003 l  mouse tracking off (click / drag / any-motion grades)
#   ?1006 l              SGR mouse encoding off (the `35;77;15M` report format)
#   ?2004 l              bracketed paste off
#   ?1049 l              leave the alternate screen
#   ?25 h                cursor visible again
#   [<u                  pop the kitty keyboard-protocol flags (Claude Code
#                        pushes them; unknown CSIs are ignored elsewhere)
#   [0m                  attribute reset
TERMINAL_MODE_RESET = (
    "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
    "\x1b[?2004l\x1b[?1049l\x1b[?25h\x1b[<u\x1b[0m"
)


def reset_terminal(drain_input: bool = False) -> None:
    """Repair the controlling terminal after a full-screen app may have died
    without cleaning up — emit TERMINAL_MODE_RESET, and optionally DRAIN
    queued input (the mouse-move reports already buffered on the tty print as
    `35;77;15M` garbage the moment a cooked-mode shell echoes them).

    No-op when stdout isn't a tty: print/piped runs (quickie) must never find
    escape bytes in their captured output."""
    if not sys.stdout.isatty():
        return
    sys.stdout.write(TERMINAL_MODE_RESET)
    sys.stdout.flush()
    if not drain_input:
        return
    try:
        import termios
        termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):   # no termios / no tty stdin — nothing queued to drop
        pass
