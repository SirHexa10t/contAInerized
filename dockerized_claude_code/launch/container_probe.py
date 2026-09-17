"""One named, live container: how it is addressed, and how to interrogate it
— probe its state, wait for it, read its terminal size, exec inside it as
root.

**This module exists to break a cycle**, and that is the whole reason it is
not part of `docker_config`. The `{firewall}` subsystem needs exactly these
four calls (its updater must not insert rules until the container is up AND
init-firewall.sh has finished), while `docker_config` imports `firewall` at
module level to start that updater. The two therefore could not both hold
them: the previous arrangement kept them in `docker_config` and had
`firewall/resolver.py` do LAZY `from ..docker_config import …` inside two
functions — an import-time cycle papered over at call time, and the only one
left in the tree (2026-09-03).

With this module both sides import DOWNWARD — `container_probe` imports
nothing but `paths`, `utils` and the stdlib — so the lazy imports are gone
and the dependency graph is acyclic again.

The line it draws, for whatever lands here next: this module is about ONE
NAMED, LIVE container the launcher is coordinating with. Fleet-wide queries
("which instances are up") stay in `docker_config` with the orchestration
that asks them — different consumers (the picker, cache pruning), no cycle
to break, and moving them would buy nothing.

`CONTAINER_NAME_PREFIX` lives here for the same reason: it is the ADDRESS
every call in this module takes, and every other addresser needs it too —
`container_inject` to attach, `docker_config` to create and to list. The
creator importing the format from the addressing module keeps one definition;
the reverse would put it back behind `docker_config` where the firewall
cannot reach it.
"""

import re
import subprocess
import time

from .paths import FIREWALL_DONE_IN_CONTAINER
from .utils import shell_capture

# Prefix for every per-launch container name: `run_container` builds names
# from it, `docker_running_instances_subprocess` filters `docker ps` by it and
# strips it back off to recover instance ids, and the injection path composes
# it to attach. Keeping those consistent is a one-line change here.
CONTAINER_NAME_PREFIX = "claude-code_"
# The characters docker accepts in a container name — `[a-zA-Z0-9][a-zA-Z0-9_.-]+`
# (the daemon's RestrictedNamePattern). Every launcher container is the prefix
# above plus a label, so the prefix carries the leading-alnum requirement and a
# label needs only the body class. `tags.identity.label_error` refuses anything
# outside it up front: `docker run --name` refusing it later is a raw daemon
# error at the end of a build (2026-09-09).
CONTAINER_NAME_CHARS = re.compile(r"[a-zA-Z0-9_.-]+")

def docker_check_running_subprocess(container_name: str) -> bool:
    """True if the named container is currently in the Running state per
    `docker inspect`. False otherwise — returncode non-zero (container not
    found / daemon unreachable), or `State.Running` is anything other than
    the literal string `"true"` (docker's text output for that field).
    One-shot probe; wait_for_container_running polls this in a loop for the
    "just-created, not yet up" window."""
    r = shell_capture("docker", "inspect", "--format={{.State.Running}}", container_name)
    return r.returncode == 0 and r.stdout.strip() == "true"


def wait_for_container_running(container_name: str, timeout_seconds: float = 10) -> bool:
    """Poll `docker_check_running_subprocess` until it returns True, or
    `timeout_seconds` passes. Returns True if the container came up in time,
    False on timeout. `docker run` creates the container almost immediately
    but `docker inspect` returns 'not found' for a small window after —
    hence the poll. Used by the {firewall} updater (in firewall.resolver._updater_worker)
    before it starts issuing `docker exec` calls.

    The walrus in the while-condition reads as "while within deadline and not
    yet running, sleep". `running = False` is initialized to keep the name
    bound for the return even when the walrus never fires (deadline already
    passed on entry — `timeout_seconds <= 0`)."""
    deadline = time.monotonic() + timeout_seconds
    running = False
    while time.monotonic() < deadline and not (running := docker_check_running_subprocess(container_name)):
        time.sleep(0.1)
    return running


def wait_for_firewall_applied(container_name: str, timeout_seconds: float = 90) -> bool:
    """Gate for the phase-2 firewall updater: True when it's sensible to
    start inserting rules, False when there's nothing left to update.

    Polls for init-firewall.sh's completion marker
    (paths.FIREWALL_DONE_IN_CONTAINER). Marker present → True. Container
    stopped without it → False (init-firewall failed its self-test and took
    the container down). Deadline passed with the container still up → True
    anyway, best-effort: the script's runtime is curl-bounded to seconds, so
    a live container without a marker after this long means the marker
    mechanism itself broke — and late rules beat no rules.

    The gate exists because "container is running" is NOT "firewall is
    ready": the entrypoint runs init-firewall.sh as its first act, so an
    updater that starts inserting rules on mere running-ness races the
    script — inserts landing before its `iptables -F` were silently wiped,
    and inserts landing mid-self-test could open provider blocks that made
    the enforcement probe's target reachable, killing perfectly healthy
    launches. Used by firewall.resolver._updater_worker between
    wait_for_container_running and the first rule flush."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if docker_exec_root_subprocess(container_name, "test", "-e", str(FIREWALL_DONE_IN_CONTAINER)).returncode == 0:
            return True
        if not docker_check_running_subprocess(container_name):
            return False
        time.sleep(0.3)
    return docker_check_running_subprocess(container_name)


def container_tty_size(container: str) -> tuple[int, int] | None:
    """(rows, cols) of `container`'s OWN terminal, or None if unknowable.

    pid 1 in the container is the agent CLI (the harness layer's ENTRYPOINT), so its fd 0 is
    the pty a client attaches to, and `stty size` reports that pty's winsize.
    Run without `-t` so the exec doesn't allocate a pty of its own — and
    without `--user root`, since reading a size needs no privilege.

    Used by `container_inject` to size its pty before attaching; a stale or
    absent size is what blanks a running agent's TUI (see
    `_match_container_winsize` there)."""
    try:
        r = shell_capture("docker", "exec", container, "sh", "-c",
                          "stty size < /proc/1/fd/0", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = r.stdout.split()
    if r.returncode != 0 or len(parts) != 2:
        return None
    try:
        rows, cols = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return (rows, cols) if rows > 0 and cols > 0 else None


def docker_exec_root_subprocess(container_name: str, *cmd: str) -> subprocess.CompletedProcess:
    """Run `docker exec --user root <container_name> <cmd...>` and return the
    CompletedProcess (capture_output=True, text=True so callers can inspect
    returncode + stdout/stderr).

    The `--user root` flag is the privileged-operation pattern: it grants
    root inside the container *from outside the container's namespace*,
    bypassing whatever sudoers restrictions are in place for the in-container
    user. Used by the {firewall} updater (in firewall.resolver._flush_rules)
    to inject iptables ACCEPT rules into the running container as Phase 2 DNS
    resolutions complete. Centralised here so privileged docker-exec calls
    have a single audit point."""
    return shell_capture("docker", "exec", "--user", "root", container_name, *cmd)
