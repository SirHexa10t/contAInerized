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

The WHEN also lives here, now that the `{manager}` tag exists to define it:
`ensure_hub_running` is what `run.py` calls on a manager launch (start only if
nobody serves), and `ManagerWatch` is what the serving hub consults to stop
(exit once no manager has been running for a grace period). The hub is a global
singleton rather than any manager's child on purpose — `run.py` blocks for its
container's whole lifetime, so a child hub would die with whichever manager
happened to start it and take a second manager's routing down with it.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..file_access import (
    create_exclusive, ensure_dir, is_file, read_text, remove_path,
)
from ..paths import COWORK_SCRIPT, hub_log_path, hub_pid_path
from ..tags import Registry
from .roster import running_managers

# How long the hub keeps serving with no `{manager}` container running before it
# exits. Two gaps have to fit inside it: the seconds between run.py starting the
# hub and `docker run` bringing the manager up (ensure_hub_running is called
# post-build, so this is container start, not image build), and a human closing
# a manager to relaunch it moments later — exiting inside that window would cost
# a hub restart for no reason. Exiting late, by contrast, costs only an idle
# poll loop, so the grace leans generous.
MANAGERLESS_GRACE_SECONDS = 60.0


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


def ensure_hub_running() -> str:
    """Make sure A hub is serving; return one banner line saying what happened.

    Ensure, not start: a hub already serving (any manager's launch may have
    started it) is left alone. When none is, `cowork.py serve` is spawned
    DETACHED — `nohup`, so closing the terminal that launched this manager
    cannot SIGHUP the hub out from under a second manager — with its output
    appended to `hub.log`, because a daemon inheriting this terminal would
    scribble its event stream over the Claude session about to start.
    (`tail -f` on that log is the watch-the-team view.)

    The spawn goes through a short-lived `sh` that backgrounds the hub, echoes
    its pid, and exits — so the hub is REPARENTED TO INIT rather than staying
    this launcher's child. That is not ceremony: a child this process never
    waits on becomes a ZOMBIE when it dies, and `os.kill(pid, 0)` — the
    liveness probe — succeeds on zombies. A direct Popen therefore had a dark
    corner where a crashed hub read as "already serving" for as long as the
    manager's own run.py lived, with no new hub startable and `status` lying.
    Reparented, the hub's exit is reaped by init immediately and the probe
    stays honest. (`nohup` stands in for `start_new_session`: the hub must
    survive the launching terminal closing, and unlike `setsid` it exists on
    macOS.) This assumes pid 1 reaps orphans — true of every real host init
    (systemd, launchd), and the launcher runs host-side by design; a minimal
    container whose pid 1 is an ordinary program would not, but such an
    environment cannot host the hub in the first place.

    No claim is taken here and none is awaited: the spawned hub claims the
    pidfile itself, and if two launches race to spawn, the second hub loses the
    O_EXCL claim and exits into its log without complaint — the documented
    resolution. The one-line return leaves the caller nothing to compose."""
    existing = owner()
    if existing is not None:
        return f"already serving (pid {existing.pid})"
    log = hub_log_path()
    ensure_dir(log.parent)
    # -u: stdout is a FILE, so without it Python block-buffers and the hub's
    # event lines sit invisible for kilobytes — `tail -f` (the watch-the-team
    # view) would show nothing, and a crash would eat them.
    hub = (f"nohup {shlex.quote(sys.executable)} -u "
           f"{shlex.quote(str(COWORK_SCRIPT))} serve "
           f">> {shlex.quote(str(log))} 2>&1 < /dev/null & echo $!")
    spawned = subprocess.run(["sh", "-c", hub], capture_output=True, text=True,
                             cwd=COWORK_SCRIPT.parent)
    if spawned.returncode != 0:
        detail = spawned.stderr.strip() or "(sh reported nothing)"
        return f"FAILED to start: {detail} — run `cowork serve` by hand"
    return f"starting (pid {spawned.stdout.strip()}; log: {log})"


class ManagerWatch:
    """The serving hub's exit condition: no `{manager}` container has been
    running for MANAGERLESS_GRACE_SECONDS.

    Time-based rather than pass-counted, so the answer does not change meaning
    when someone tunes the poll interval. Two states deliberately do NOT count
    toward the grace: managers present (obviously), and docker unreachable —
    a probe hiccup must not be read as "every manager is gone", so it resets
    the clock and the hub keeps serving. `clock` is injectable for tests;
    monotonic so a wall-clock jump cannot fire it."""

    def __init__(self, registry: Registry, *,
                 grace_seconds: float = MANAGERLESS_GRACE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._registry = registry
        self._grace = grace_seconds
        self._clock = clock
        self._managerless_since: float | None = None

    def reason_to_stop(self) -> str | None:
        """A human-readable reason to exit, or None to keep serving — the shape
        `relay.serve(stop=...)` consumes, so the loop can print WHY it ended."""
        managers = running_managers(self._registry)
        if managers is None or managers:
            self._managerless_since = None
            return None
        now = self._clock()
        if self._managerless_since is None:
            self._managerless_since = now
        waited = now - self._managerless_since
        if waited < self._grace:
            return None
        return (f"no {{manager}} instance has been running for {waited:.0f}s "
                f"— nothing left to route; run.py restarts the hub on the "
                f"next manager launch")


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
