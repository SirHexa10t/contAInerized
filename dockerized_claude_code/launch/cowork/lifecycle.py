"""The hub's singleton guard: one hub per host, and only one.

Not tidiness — correctness. Two hubs draining the same outboxes would each consume
about half the captures, so replies would vanish at random and every symptom would
point somewhere else. The pidfile is what makes "only one" enforceable rather than
assumed.

The claim is atomic (`file_access.create_exclusive`, i.e. `O_EXCL`), because two
`run.py` invocations can genuinely race: a check-then-write would let both believe
they won, however narrow the window looks.

A pidfile alone is not evidence, though — a crash or a reboot leaves one behind
with nobody serving. So every read is liveness-checked, and a stale file is cleared
rather than treated as an owner. That asymmetry is deliberate: refusing to start
because of a file left by a process that died last week is the worse failure.

Not covered here: WHEN a hub should be started or stopped. Starting belongs to
`run.py` at launch time and stopping to "no managers remain" — both of which need
the `{manager}` tag to identify a manager at all, so they land with it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ..file_access import create_exclusive, is_file, read_text, remove_path
from ..paths import hub_pid_path


@dataclass(frozen=True)
class Owner:
    """Who holds the hub singleton, as far as the pidfile can tell.

    `pid` is what a human needs to inspect or kill it; `ours` distinguishes "this
    process is the hub" from "another process is", which is the difference between
    carrying on and backing off."""
    pid: int
    ours: bool


def claim() -> Owner | None:
    """Claim the hub singleton for this process. None when another live hub holds it.

    Clears a stale pidfile before retrying, so a crashed hub never blocks the next
    one. The retry is single, not a loop: if a genuine competitor claims the file in
    the moment between our clear and our create, it now owns it and we should back
    off — that is the right answer, not something to keep fighting over."""
    if create_exclusive(hub_pid_path(), f"{os.getpid()}\n"):
        return Owner(pid=os.getpid(), ours=True)
    existing = owner()
    if existing is not None:
        return None
    remove_path(hub_pid_path())      # stale: recorded pid is not running
    if create_exclusive(hub_pid_path(), f"{os.getpid()}\n"):
        return Owner(pid=os.getpid(), ours=True)
    return None


def owner() -> Owner | None:
    """The live hub recorded in the pidfile, or None if there is none.

    None covers every "nobody is serving" case as one answer, because they are one
    answer to a caller: no file, an unreadable or malformed file, or a recorded pid
    that is not running.

    Caveat worth knowing: a pid can be REUSED, so a long-dead hub whose number was
    recycled by an unrelated process reads as alive. Nothing cheap and portable
    fixes that, so `status` prints the pid — enough for a human to check when the
    hub appears wedged."""
    path = hub_pid_path()
    if not is_file(path):
        return None
    try:
        pid = int(read_text(path).strip())
    except (ValueError, OSError):
        return None                  # a truncated or hand-edited file is not an owner
    if pid <= 0 or not _running(pid):
        return None
    return Owner(pid=pid, ours=pid == os.getpid())


def release(claimed: Owner) -> None:
    """Give up the singleton. Only the holder may, so a hub exiting after losing a
    race cannot delete the winner's pidfile on its way out."""
    if not claimed.ours:
        return
    current = owner()
    if current is not None and current.pid != claimed.pid:
        return
    remove_path(hub_pid_path())


def pid_file() -> Path:
    """Where the pidfile lives — for `status` to name, and for a human to remove
    by hand if they ever need to."""
    return hub_pid_path()


def _running(pid: int) -> bool:
    """Whether `pid` is a live process.

    `os.kill(pid, 0)` sends no signal; it only asks the kernel to check. A
    PermissionError means the process exists but belongs to another user — alive,
    which is the answer we need."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True
